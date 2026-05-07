"""Data collectors for Cline and Claude Code session files."""

import functools
import json
import platform
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .formatters import (
    FIELD_CACHE_READS, FIELD_CACHE_WRITES, FIELD_CACHE_SAVINGS,
    FIELD_COST, FIELD_TOKENS_IN, FIELD_TOKENS_OUT,
)
from .ingest_state import file_needs_processing, get_stored_user_text, update_file_state
from .pricing import calculate_cache_savings, calculate_cost


# ---------------------------------------------------------------------------
# Cline tool-name normalization
# ---------------------------------------------------------------------------
#
# Cline records tool usage as `say: "tool"` messages whose `text` is a JSON
# blob with a `tool` field. The vocabulary differs from Claude Code's, so we
# map each Cline tool name to the CC equivalent. That way `TOOL_ABBREV` in
# ops_data lights up the same glyphs regardless of source.

_CLINE_TOOL_MAP = {
    "readFile": "Read",
    "editedExistingFile": "Edit",
    "newFileCreated": "Write",
    "searchFiles": "Grep",
    "listFilesTopLevel": "LS",
    "listFilesRecursive": "LS",
    "listCodeDefinitionNames": "Glob",
    "webFetch": "WebFetch",
    # Browser / MCP names are already close; leave as-is.
}



# ---------------------------------------------------------------------------
# Cline
# ---------------------------------------------------------------------------

def get_cline_data_dir() -> Optional[Path]:
    home = Path.home()
    system = platform.system()
    cline_ext = "saoudrizwan.claude-dev"
    if system == "Darwin":
        p = (home / "Library" / "Application Support" / "Code" / "User"
             / "globalStorage" / cline_ext)
    elif system == "Windows":
        p = (home / "AppData" / "Roaming" / "Code" / "User"
             / "globalStorage" / cline_ext)
    elif system == "Linux":
        p = home / ".config" / "Code" / "User" / "globalStorage" / cline_ext
    else:
        return None
    return p if p.exists() else None


def find_cline_task_directories(base_path: Path) -> List[Path]:
    tasks_dir = base_path / "tasks"
    if not tasks_dir.exists():
        return []
    return [d for d in tasks_dir.iterdir() if d.is_dir()]


def parse_ui_messages(task_dir: Path, verbose: bool = False) -> Tuple[List[Dict], bool]:
    ui_messages_file = task_dir / "ui_messages.json"
    if not ui_messages_file.exists():
        return [], True

    try:
        with open(ui_messages_file, 'r') as f:
            return json.load(f), True
    except (json.JSONDecodeError, IOError) as e:
        error_str = str(e)
        if "Expecting value: line 1 column 1" in error_str or "Unterminated string" in error_str:
            return [], True
        if verbose:
            print(f"Warning: Could not parse {ui_messages_file}: {e}")
        return [], False


# Bedrock/Vertex model IDs include a regional prefix and a provider prefix
# before the family name. Strip both so `short_model` / `family_for_model`
# see the raw family token.
#
# Examples:
#   us.anthropic.claude-opus-4-7           → claude-opus-4-7
#   global.anthropic.claude-sonnet-4-5-1   → claude-sonnet-4-5-1
#   us.openai.gpt-5-5                      → gpt-5-5
#   us.amazon.nova-pro-v1                  → nova-pro-v1
#   us.meta.llama-4-70b                    → llama-4-70b
_CLINE_REGION_RE = re.compile(r'^(us|global|eu|apac|apne|apse)\.')
_CLINE_PROVIDER_RE = re.compile(
    r'^(anthropic|openai|amazon|meta|mistral|cohere|ai21|google|deepseek)\.'
)


def _normalize_cline_model(model_id: Optional[str]) -> Optional[str]:
    """Strip Bedrock/Vertex regional + provider prefixes."""
    if not model_id:
        return model_id
    stripped = _CLINE_REGION_RE.sub('', model_id)
    stripped = _CLINE_PROVIDER_RE.sub('', stripped)
    return stripped or model_id


# Matches the first <task>…</task> block emitted by Cline's system prompt
# template. Multiple paragraphs and newlines allowed inside.
_CLINE_TASK_RE = re.compile(r'<task>\n?(.*?)</task>', re.DOTALL)
# Cline wraps resumed-task prompts in <user_message>…</user_message>
_CLINE_USER_MSG_RE = re.compile(r'<user_message>\n?(.*?)</user_message>', re.DOTALL)
# Matches the working-directory line emitted in <environment_details>
_CLINE_CWD_RE = re.compile(r'Current Working Directory \(([^)]+)\)')


def _extract_cline_prompt_preview(request: str) -> str:
    """Pull a human-readable preview out of a Cline api_req `request` field.

    Priority:
      1. <user_message>…</user_message> (resumed tasks, follow-ups)
      2. <task>…</task> (first turn of a new task)
      3. First non-tag, non-empty line of the request

    The result is truncated to 80 chars and has whitespace collapsed so
    it renders cleanly in the OPS log.
    """
    if not request:
        return ""

    for pattern in (_CLINE_USER_MSG_RE, _CLINE_TASK_RE):
        match = pattern.search(request)
        if match:
            text = match.group(1).strip()
            if text:
                return " ".join(text.split())[:80]

    # Fallback: first line that doesn't look like a tool or tag
    for line in request.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('<') or stripped.startswith('['):
            continue
        return " ".join(stripped.split())[:80]
    return ""


def _project_from_cwd(cwd: str) -> str:
    """Format an absolute working directory as a short path (`~/foo/bar`)."""
    if not cwd:
        return ""
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~" + cwd[len(home):]
    return cwd


def _cline_stop_reason(post_msgs: List[Dict]) -> Optional[str]:
    """Infer a stop-reason label from messages following an api_req_started.

    Returns a short string from {"end_turn", "tool_use", "max_tokens",
    "pause", None}. Scans until the next api_req_started.
    """
    for m in post_msgs:
        if m.get('say') == 'api_req_started':
            break
        if m.get('say') in ('tool', 'command', 'browser_action_launch',
                            'use_mcp_server'):
            return 'tool_use'
        if m.get('ask') in ('tool', 'command', 'browser_action_launch',
                            'use_mcp_server'):
            return 'tool_use'
        if m.get('say') == 'completion_result' or m.get('ask') == 'completion_result':
            return 'end_turn'
        if m.get('ask') == 'plan_mode_respond' or m.get('say') == 'plan_mode_respond':
            return 'end_turn'
    return None


def _cline_tool_chain(post_msgs: List[Dict]) -> List[str]:
    """Collect tool names used between one api_req_started and the next.

    Maps Cline's vocabulary to CC-equivalent names so downstream display
    code (TOOL_ABBREV) renders the same glyphs regardless of source.
    """
    tools: List[str] = []
    for m in post_msgs:
        if m.get('say') == 'api_req_started':
            break
        if m.get('say') == 'command' or m.get('ask') == 'command':
            tools.append('Bash')
            continue
        if m.get('say') == 'browser_action_launch':
            tools.append('WebFetch')
            continue
        if m.get('say') == 'use_mcp_server' or m.get('ask') == 'use_mcp_server':
            tools.append('Tool')  # generic
            continue
        if m.get('say') != 'tool' and m.get('ask') != 'tool':
            continue
        try:
            payload = json.loads(m.get('text') or '{}')
        except (json.JSONDecodeError, TypeError):
            continue
        raw = payload.get('tool') or ''
        if not raw:
            continue
        tools.append(_CLINE_TOOL_MAP.get(raw, raw))
    return tools


def extract_cline_entries(messages: List[Dict], task_dir_name: str,
                          project: str = "") -> Dict[str, Dict]:
    """Extract keyed cost entries from Cline UI messages.

    Populates `model`, `project`, `session`, `promptPreview`, `tools`, and
    `stopReason` alongside the cost/token fields so the OPS tab can surface
    Cline activity the same way it does Claude Code.
    """
    entries: Dict[str, Dict] = {}
    api_indexes = [i for i, m in enumerate(messages)
                   if m.get('say') == 'api_req_started']

    for pos, i in enumerate(api_indexes):
        msg = messages[i]
        try:
            ts = msg.get('ts')
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts / 1000.0)
            text_data = json.loads(msg.get('text') or '{}')
            cache_reads = text_data.get(FIELD_CACHE_READS, 0)

            # --- model ---
            model_info = msg.get('modelInfo') or {}
            model = _normalize_cline_model(model_info.get('modelId'))

            # --- prompt preview (from the `request` blob on this api_req) ---
            preview = _extract_cline_prompt_preview(text_data.get('request') or '')

            # --- tools + stop reason: scan messages up to the NEXT api_req ---
            end = api_indexes[pos + 1] if pos + 1 < len(api_indexes) else len(messages)
            post = messages[i + 1:end]
            tools = _cline_tool_chain(post)
            stop_reason = _cline_stop_reason(post)
            # If this is the last api_req in the task and nothing follows,
            # treat as a completed turn so it lands in the "end_turn" bucket.
            if stop_reason is None and pos == len(api_indexes) - 1:
                stop_reason = 'end_turn'

            entry_id = f"cline:{task_dir_name}:{int(ts)}"
            entry: Dict = {
                'source': 'cline',
                'ts': dt.isoformat(),
                'session': task_dir_name,
                'isSubagent': False,
                FIELD_TOKENS_IN: text_data.get(FIELD_TOKENS_IN, 0),
                FIELD_TOKENS_OUT: text_data.get(FIELD_TOKENS_OUT, 0),
                FIELD_CACHE_WRITES: text_data.get(FIELD_CACHE_WRITES, 0),
                FIELD_CACHE_READS: cache_reads,
                FIELD_COST: text_data.get(FIELD_COST, 0.0),
                FIELD_CACHE_SAVINGS: calculate_cache_savings(cache_reads, model),
            }
            if model:
                entry['model'] = model
            if project:
                entry['project'] = project
            if preview:
                entry['promptPreview'] = preview
            if tools:
                entry['tools'] = tools
            if stop_reason:
                entry['stopReason'] = stop_reason

            entries[entry_id] = entry
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    return entries



def _project_for_cline_task(messages: List[Dict]) -> str:
    """Find the task's working directory by peeking at the first api_req.

    Cline's system prompt template embeds a `Current Working Directory (…)`
    line inside `<environment_details>`; we pull it out of the first
    `api_req_started.request` blob and normalize it to `~/…` form.
    """
    for m in messages:
        if m.get('say') != 'api_req_started':
            continue
        try:
            text_data = json.loads(m.get('text') or '{}')
        except (json.JSONDecodeError, TypeError):
            continue
        req = text_data.get('request') or ''
        cwd_match = _CLINE_CWD_RE.search(req)
        if cwd_match:
            return _project_from_cwd(cwd_match.group(1).strip())
        return ""
    return ""


def collect_cline_data(verbose: bool,
                       ingest_state: Optional[Dict] = None) -> Dict[str, Dict]:
    """Collect cost entries from Cline task directories."""
    cline_data_dir = get_cline_data_dir()
    if not cline_data_dir:
        if verbose:
            print("Cline: data directory not found, skipping")
        return {}

    task_dirs = find_cline_task_directories(cline_data_dir)
    if not task_dirs:
        if verbose:
            print("Cline: no task directories found")
        return {}

    all_entries = {}
    ok = 0
    fail = 0
    skipped = 0
    for task_dir in task_dirs:
        ui_file = task_dir / "ui_messages.json"
        if ingest_state is not None and ui_file.exists():
            needs, _ = file_needs_processing(ui_file, ingest_state)
            if not needs:
                skipped += 1
                continue

        messages, success = parse_ui_messages(task_dir, verbose=verbose)
        if success:
            ok += 1
        else:
            fail += 1
        project = _project_for_cline_task(messages)
        all_entries.update(
            extract_cline_entries(messages, task_dir.name, project=project)
        )

        if ingest_state is not None and ui_file.exists():
            try:
                stat = ui_file.stat()
                update_file_state(ingest_state, ui_file, stat.st_size)
            except OSError:
                pass


    if verbose:
        print(f"Cline: parsed {ok}/{len(task_dirs)} tasks ({skipped} skipped), "
              f"{len(all_entries)} API calls")
        if fail > 0:
            print(f"  Failed: {fail} tasks")

    return all_entries


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def get_claude_code_dir() -> Path:
    return Path.home() / ".claude"


def find_claude_code_session_files(base_path: Path) -> List[Path]:
    """Find all session JSONL files across all projects."""
    projects_dir = base_path / "projects"
    if not projects_dir.exists():
        return []
    return (
        list(projects_dir.glob("*/*.jsonl"))
        + list(projects_dir.glob("*/*/subagents/*.jsonl"))
    )


@functools.lru_cache(maxsize=512)
def _resolve_project_slug(slug: str) -> str:
    """Resolve a CC project slug like `-Users-bfidler-src-foo` to a readable path.

    Dashes serve double duty — path separator *and* legitimate character in
    directory names. We greedily match the longest real directory at each
    level, probing the filesystem with `is_dir()`. Cached by slug since every
    session file in the same project resolves identically.
    """
    parts = slug.lstrip("-").split("-")
    resolved = Path("/")
    i = 0
    while i < len(parts):
        best = None
        for j in range(len(parts), i, -1):
            candidate = "-".join(parts[i:j])
            if (resolved / candidate).is_dir():
                best = candidate
                i = j
                break
        if best is None:
            best = parts[i]
            i += 1
        resolved = resolved / best
    result = str(resolved)
    home = str(Path.home())
    if result.startswith(home):
        result = "~" + result[len(home):]
    return result


def _project_from_session_path(session_file: Path) -> str:
    """Derive a readable project path from a CC session file path.

    ~/.claude/projects/-Users-bfidler-src-techdocs-tools/abc.jsonl → ~/src/techdocs-tools
    """
    projects_dir = get_claude_code_dir() / "projects"
    try:
        rel = session_file.relative_to(projects_dir)
        slug = rel.parts[0]
    except (ValueError, IndexError):
        return ""
    return _resolve_project_slug(slug)


def _parse_ts(ts_str: str) -> datetime:
    """Parse a CC session timestamp into a UTC-naive datetime.

    CC emits ISO-8601 with a trailing `Z`. Python 3.11+ `fromisoformat`
    handles `Z` natively; keep the explicit replace for 3.9/3.10 compat.
    """
    return datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)


def _extract_user_text(obj: Dict) -> str:
    """Extract plain text from a CC 'user' JSONL record, skipping tool results.

    User messages can be plain strings, lists of content blocks, or tool_result
    wrappers. We only want genuine typed-by-human text.
    """
    msg = obj.get('message', {})
    content = msg.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                parts.append(block.get('text', ''))
        return ' '.join(p for p in parts if p)
    return ''


def _scan_tool_uses(content, spawns: Dict, obj: Dict, msg_id: str,
                    parent_session_id: str, last_prompt_id: str) -> list:
    """Walk a message's content blocks once.

    Returns the list of tool names invoked. Captures any `Agent` invocations
    into `spawns` as synthetic ledger entries for subagent analytics.
    """
    tools: list = []
    if not isinstance(content, list):
        return tools
    for block in content:
        if not isinstance(block, dict) or block.get('type') != 'tool_use':
            continue
        name = block.get('name')
        if name:
            tools.append(name)
        if name == 'Agent':
            inp = block.get('input') or {}
            spawn_id = block.get('id') or ""
            if spawn_id:
                spawns[f"spawn:{spawn_id}"] = {
                    'spawn_id': spawn_id,
                    'timestamp': obj.get('timestamp'),
                    'parent_session': parent_session_id,
                    'prompt_id': last_prompt_id,
                    'subagent_type': inp.get('subagent_type') or '(none)',
                    'description': (inp.get('description') or '')[:80],
                    'invoking_msg_id': msg_id,
                }
    return tools


def _walk_session_jsonl(session_file: Path, seek_offset: int,
                        initial_user_text: str,
                        verbose: bool) -> Tuple[Dict, Dict, int, str]:
    """Scan a JSONL file once.

    Returns ``(final_messages, spawns, last_good_offset, last_user_text)``.
    Final messages are keyed by msg_id (only assistant records with a
    ``stop_reason`` qualify — those are the billable turns).
    """
    final_messages: Dict = {}
    spawns: Dict[str, Dict] = {}
    last_good_offset = seek_offset
    last_user_text = initial_user_text
    last_prompt_id = ""
    # Subagent (sidechain) files carry `sessionId` pointing to the parent
    # session. We capture the first occurrence and reuse it for spawn
    # attribution.
    parent_session_id = ""

    try:
        with open(session_file, 'r') as f:
            if seek_offset > 0:
                f.seek(seek_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                line_end = f.tell()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                last_good_offset = line_end
                obj_type = obj.get('type')
                if not parent_session_id:
                    parent_session_id = obj.get('sessionId') or ""
                pid = obj.get('promptId')
                if pid:
                    last_prompt_id = pid

                if obj_type == 'user':
                    text = _extract_user_text(obj)
                    if text and not text.startswith('<'):
                        last_user_text = text
                    continue

                if obj_type != 'assistant':
                    continue

                msg = obj.get('message', {})
                usage = msg.get('usage')
                msg_id = msg.get('id')
                if not usage or not msg_id:
                    continue

                tools = _scan_tool_uses(
                    msg.get('content'), spawns, obj, msg_id,
                    parent_session_id, last_prompt_id,
                )

                stop_reason = msg.get('stop_reason')
                if stop_reason is not None:
                    final_messages[msg_id] = {
                        'model': msg.get('model'),
                        'usage': usage,
                        'timestamp': obj.get('timestamp'),
                        'stop_reason': stop_reason,
                        'prompt_preview': last_user_text[:80],
                        'tools': tools,
                        'prompt_id': last_prompt_id,
                        'parent_session': parent_session_id,
                    }
    except (IOError, OSError) as e:
        if verbose:
            print(f"Warning: Could not read {session_file}: {e}")
        return {}, {}, seek_offset, last_user_text

    return final_messages, spawns, last_good_offset, last_user_text


def parse_claude_code_session(session_file: Path, verbose: bool = False,
                              seek_offset: int = 0,
                              initial_user_text: str = "") -> Tuple[Dict[str, Dict], int, str]:
    """Parse a Claude Code session JSONL file into ledger entries.

    Extracts final usage per billable API call and emits synthetic entries
    with ``source="agent_spawn"`` for each Agent-tool invocation. Spawn
    entries carry ``subagentType``, ``description``, ``promptId``, and
    ``parentSession`` so the OPS dashboard can surface subagent analytics
    without re-parsing JSONL at render time.

    Args:
      session_file: Path to the JSONL file.
      verbose: Print read errors to stdout.
      seek_offset: Byte offset to resume parsing from. Used for incremental
        ingest of growing session files.
      initial_user_text: Seed for ``last_user_text``. Lets a resumed parse
        attach a prompt preview to assistant messages whose originating
        user line was consumed in an earlier pass.

    Returns:
      ``(entries, final_byte_offset, final_user_text)``.
    """
    final_messages, spawns, last_good_offset, last_user_text = _walk_session_jsonl(
        session_file, seek_offset, initial_user_text, verbose,
    )

    entries = {}
    for msg_id, data in final_messages.items():
        try:
            ts_str = data['timestamp']
            dt = _parse_ts(ts_str)
            usage = data['usage']
            model = data.get('model')

            tokens_in = usage.get('input_tokens', 0)
            tokens_out = usage.get('output_tokens', 0)
            cache_writes = usage.get('cache_creation_input_tokens', 0)
            cache_reads = usage.get('cache_read_input_tokens', 0)

            cost = calculate_cost(tokens_in, tokens_out, cache_writes, cache_reads, model)

            entry_id = f"cc:{msg_id}"
            is_subagent = 'subagents' in session_file.parts
            entries[entry_id] = {
                'source': 'cc',
                'ts': dt.isoformat(),
                'model': model,
                'project': _project_from_session_path(session_file),
                'session': session_file.stem,
                'parentSession': data.get('parent_session') or '',
                'promptId': data.get('prompt_id') or '',
                'isSubagent': is_subagent,
                'stopReason': data.get('stop_reason'),
                'promptPreview': data.get('prompt_preview', ''),
                'tools': data.get('tools', []),
                FIELD_TOKENS_IN: tokens_in,
                FIELD_TOKENS_OUT: tokens_out,
                FIELD_CACHE_WRITES: cache_writes,
                FIELD_CACHE_READS: cache_reads,
                FIELD_COST: cost,
                FIELD_CACHE_SAVINGS: calculate_cache_savings(cache_reads, model),
            }
        except (ValueError, KeyError, TypeError):
            continue

    # Emit synthetic agent-spawn entries
    project = _project_from_session_path(session_file)
    for eid, data in spawns.items():
        try:
            ts_str = data['timestamp']
            dt = _parse_ts(ts_str)
        except (ValueError, KeyError, TypeError):
            continue
        entries[eid] = {
            'source': 'agent_spawn',
            'ts': dt.isoformat(),
            'project': project,
            'session': session_file.stem,
            'parentSession': data.get('parent_session') or '',
            'promptId': data.get('prompt_id') or '',
            'subagentType': data.get('subagent_type') or '(none)',
            'description': data.get('description') or '',
            'invokingMsgId': data.get('invoking_msg_id') or '',
            FIELD_TOKENS_IN: 0, FIELD_TOKENS_OUT: 0,
            FIELD_CACHE_WRITES: 0, FIELD_CACHE_READS: 0,
            FIELD_COST: 0.0, FIELD_CACHE_SAVINGS: 0.0,
        }

    return entries, last_good_offset, last_user_text


def collect_claude_code_data(verbose: bool,
                             ingest_state: Optional[Dict] = None,
                             max_workers: int = 8) -> Dict[str, Dict]:
    """Collect cost entries from all Claude Code sessions.

    Files that need processing are parsed in parallel (I/O bound: reading JSONL
    and json.loads per line). Ingest state is updated serially afterwards to
    avoid races on the shared dict.
    """
    cc_dir = get_claude_code_dir()
    if not cc_dir.exists():
        if verbose:
            print("Claude Code: data directory not found, skipping")
        return {}

    session_files = find_claude_code_session_files(cc_dir)
    if not session_files:
        if verbose:
            print("Claude Code: no session files found")
        return {}

    work = []
    skipped = 0
    for sf in session_files:
        if ingest_state is not None:
            needs, offset = file_needs_processing(sf, ingest_state)
            if not needs:
                skipped += 1
                continue
        else:
            offset = 0
        work.append((sf, offset))

    all_entries: Dict[str, Dict] = {}
    dup_count = 0

    def _parse(item):
        sf, offset, seed = item
        return sf, parse_claude_code_session(
            sf, verbose=verbose, seek_offset=offset, initial_user_text=seed)

    work_with_seed = [
        (sf, offset,
         get_stored_user_text(ingest_state, sf) if ingest_state is not None and offset > 0 else "")
        for sf, offset in work
    ]

    if work_with_seed:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for sf, (entries, final_offset, final_user_text) in pool.map(_parse, work_with_seed):
                for eid, edata in entries.items():
                    if eid in all_entries:
                        dup_count += 1
                    all_entries[eid] = edata
                if ingest_state is not None:
                    update_file_state(ingest_state, sf, final_offset,
                                      last_user_text=final_user_text)

    if verbose:
        print(f"Claude Code: parsed {len(work)} sessions ({skipped} skipped), "
              f"{len(all_entries)} API calls")
        if dup_count > 0:
            print(f"  Note: {dup_count} msg_id collisions across sessions "
                  f"(expected for resumed sessions)")

    return all_entries


