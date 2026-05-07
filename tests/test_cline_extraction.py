"""Tests for the enriched Cline entry extractor.

Covers model normalization, prompt-preview extraction (<task>, <user_message>,
fallback), tool-chain capture with Cline→CC name mapping, stop-reason
inference from post-api_req messages, and project resolution from the
system prompt's working-directory hint.
"""

import json
from unittest.mock import patch

from llmcars.collectors import (
    _cline_stop_reason,
    _cline_tool_chain,
    _extract_cline_prompt_preview,
    _normalize_cline_model,
    _project_for_cline_task,
    _project_from_cwd,
    extract_cline_entries,
)


# ---------------------------------------------------------------------------
# _normalize_cline_model
# ---------------------------------------------------------------------------

class TestNormalizeClineModel:
    def test_bedrock_region_prefix(self):
        assert _normalize_cline_model("us.anthropic.claude-opus-4-7") == \
            "claude-opus-4-7"

    def test_global_prefix(self):
        assert _normalize_cline_model("global.anthropic.claude-sonnet-4-5") == \
            "claude-sonnet-4-5"

    def test_bare_provider_prefix(self):
        assert _normalize_cline_model("anthropic.claude-opus-4") == \
            "claude-opus-4"

    def test_already_normalized(self):
        assert _normalize_cline_model("claude-opus-4-6") == "claude-opus-4-6"

    def test_none(self):
        assert _normalize_cline_model(None) is None

    def test_empty(self):
        assert _normalize_cline_model("") == ""


# ---------------------------------------------------------------------------
# _extract_cline_prompt_preview
# ---------------------------------------------------------------------------

class TestExtractClinePromptPreview:
    def test_task_block(self):
        req = "<task>\ntest things\n</task>\n\nmore context"
        assert _extract_cline_prompt_preview(req) == "test things"

    def test_user_message_wins_over_task(self):
        """Resumed tasks carry the new message under <user_message>."""
        req = (
            "<task>\nold task\n</task>\n"
            "<user_message>\nnew follow-up\n</user_message>\n"
        )
        assert _extract_cline_prompt_preview(req) == "new follow-up"

    def test_collapses_whitespace(self):
        req = "<task>\nhello   world\n\n\nfoo\n</task>"
        assert _extract_cline_prompt_preview(req) == "hello world foo"

    def test_truncates_to_80(self):
        long = "x" * 200
        assert len(_extract_cline_prompt_preview(f"<task>{long}</task>")) == 80

    def test_fallback_first_line(self):
        """When no <task>/<user_message>, the first non-tag line wins."""
        req = "<environment>\n</environment>\nhello from user\nmore stuff"
        assert _extract_cline_prompt_preview(req) == "hello from user"


    def test_empty_request(self):
        assert _extract_cline_prompt_preview("") == ""

    def test_no_match(self):
        req = "<only><tags></tags></only>"
        # All lines start with '<' so fallback gives empty
        assert _extract_cline_prompt_preview(req) == ""


# ---------------------------------------------------------------------------
# _project_from_cwd
# ---------------------------------------------------------------------------

class TestProjectFromCwd:
    def test_under_home(self, tmp_path):
        with patch("llmcars.collectors.Path.home", lambda: tmp_path):
            assert _project_from_cwd(str(tmp_path / "src" / "proj")) == \
                "~/src/proj"

    def test_exact_home(self, tmp_path):
        with patch("llmcars.collectors.Path.home", lambda: tmp_path):
            assert _project_from_cwd(str(tmp_path)) == "~"

    def test_outside_home(self, tmp_path):
        with patch("llmcars.collectors.Path.home", lambda: tmp_path):
            assert _project_from_cwd("/var/log") == "/var/log"

    def test_empty(self):
        assert _project_from_cwd("") == ""


# ---------------------------------------------------------------------------
# _cline_tool_chain
# ---------------------------------------------------------------------------

class TestClineToolChain:
    def test_maps_cline_names_to_cc(self):
        msgs = [
            {"say": "tool", "text": json.dumps({"tool": "readFile"})},
            {"say": "tool", "text": json.dumps({"tool": "editedExistingFile"})},
            {"say": "tool", "text": json.dumps({"tool": "newFileCreated"})},
        ]
        assert _cline_tool_chain(msgs) == ["Read", "Edit", "Write"]

    def test_commands_become_bash(self):
        msgs = [
            {"say": "command", "text": "ls"},
            {"say": "tool", "text": json.dumps({"tool": "readFile"})},
        ]
        assert _cline_tool_chain(msgs) == ["Bash", "Read"]

    def test_stops_at_next_api_req(self):
        msgs = [
            {"say": "tool", "text": json.dumps({"tool": "readFile"})},
            {"say": "api_req_started"},
            {"say": "tool", "text": json.dumps({"tool": "searchFiles"})},
        ]
        # Only the first Read before the next api_req should count
        assert _cline_tool_chain(msgs) == ["Read"]

    def test_unknown_tool_passes_through(self):
        msgs = [{"say": "tool", "text": json.dumps({"tool": "mysteriousTool"})}]
        assert _cline_tool_chain(msgs) == ["mysteriousTool"]

    def test_malformed_tool_skipped(self):
        msgs = [
            {"say": "tool", "text": "not json"},
            {"say": "tool", "text": json.dumps({"tool": "readFile"})},
        ]
        assert _cline_tool_chain(msgs) == ["Read"]

    def test_empty(self):
        assert _cline_tool_chain([]) == []

    def test_ask_tool_included(self):
        msgs = [{"ask": "tool", "text": json.dumps({"tool": "readFile"})}]
        assert _cline_tool_chain(msgs) == ["Read"]


# ---------------------------------------------------------------------------
# _cline_stop_reason
# ---------------------------------------------------------------------------

class TestClineStopReason:
    def test_tool_use(self):
        msgs = [{"say": "tool", "text": "{}"}]
        assert _cline_stop_reason(msgs) == "tool_use"

    def test_command_is_tool_use(self):
        msgs = [{"say": "command", "text": "ls"}]
        assert _cline_stop_reason(msgs) == "tool_use"

    def test_completion_is_end_turn(self):
        msgs = [{"say": "completion_result", "text": "done"}]
        assert _cline_stop_reason(msgs) == "end_turn"

    def test_plan_mode_respond_is_end_turn(self):
        msgs = [{"ask": "plan_mode_respond", "text": ""}]
        assert _cline_stop_reason(msgs) == "end_turn"

    def test_stops_at_next_api_req(self):
        msgs = [
            {"say": "api_req_started"},
            {"say": "tool", "text": "{}"},
        ]
        assert _cline_stop_reason(msgs) is None

    def test_nothing_interesting(self):
        msgs = [{"say": "task_progress", "text": "- [x] done"}]
        assert _cline_stop_reason(msgs) is None


# ---------------------------------------------------------------------------
# extract_cline_entries — the full pipeline
# ---------------------------------------------------------------------------

def _api_req(ts, request="", cost=0.01, tokens_in=100, tokens_out=50,
             cache_writes=0, cache_reads=0,
             model_id="us.anthropic.claude-opus-4-7", mode="act"):
    text = json.dumps({
        "request": request,
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "cacheWrites": cache_writes,
        "cacheReads": cache_reads,
        "cost": cost,
    })
    return {
        "ts": ts,
        "type": "say",
        "say": "api_req_started",
        "text": text,
        "modelInfo": {
            "providerId": "bedrock",
            "modelId": model_id,
            "mode": mode,
        },
    }


class TestExtractClineEntries:
    def test_basic_fields_populated(self):

        """A single api_req followed by tools → tool_use stop reason."""
        msgs = [
            _api_req(1778000000000, request="<task>\nfix bug\n</task>"),
            {"say": "tool", "text": json.dumps({"tool": "readFile"})},
        ]
        entries = extract_cline_entries(msgs, "task123", project="~/src/foo")
        assert len(entries) == 1
        entry = next(iter(entries.values()))
        assert entry["source"] == "cline"
        assert entry["model"] == "claude-opus-4-7"
        assert entry["project"] == "~/src/foo"
        assert entry["session"] == "task123"
        assert entry["promptPreview"] == "fix bug"
        assert entry["tools"] == ["Read"]
        assert entry["stopReason"] == "tool_use"
        assert entry["isSubagent"] is False

    def test_completion_result_end_turn(self):
        """Final turn whose span only contains a completion → end_turn."""
        msgs = [
            _api_req(1778000000000, request="<task>\nfinish\n</task>"),
            {"say": "text", "text": "here's the result"},
            {"say": "completion_result", "text": "done"},
        ]
        entries = extract_cline_entries(msgs, "t1")
        assert next(iter(entries.values()))["stopReason"] == "end_turn"


    def test_tool_use_stop_reason(self):
        """An api_req followed by a tool then another api_req → tool_use."""
        msgs = [
            _api_req(1778000000000, request="<task>\nX\n</task>"),
            {"say": "tool", "text": json.dumps({"tool": "searchFiles"})},
            _api_req(1778000001000),
            {"say": "completion_result", "text": "done"},
        ]
        entries = extract_cline_entries(msgs, "t1")
        ordered = sorted(entries.values(), key=lambda e: e["ts"])
        assert ordered[0]["stopReason"] == "tool_use"
        assert ordered[0]["tools"] == ["Grep"]
        assert ordered[1]["stopReason"] == "end_turn"

    def test_last_api_req_gets_end_turn_default(self):
        """If nothing follows the final api_req, we still stamp end_turn."""
        msgs = [_api_req(1778000000000, request="<task>\nx\n</task>")]
        entries = extract_cline_entries(msgs, "t1")
        assert next(iter(entries.values()))["stopReason"] == "end_turn"

    def test_no_preview_no_field(self):
        """Empty preview shouldn't clobber the key so downstream can tell."""
        msgs = [_api_req(1778000000000, request="")]
        entries = extract_cline_entries(msgs, "t1")
        entry = next(iter(entries.values()))
        assert "promptPreview" not in entry

    def test_user_message_preview(self):
        req = (
            "<task>\ninitial task\n</task>\n"
            "...\n"
            "<user_message>\ntest again\n</user_message>\n"
        )
        msgs = [_api_req(1778000000000, request=req)]
        entries = extract_cline_entries(msgs, "t1")
        assert next(iter(entries.values()))["promptPreview"] == "test again"

    def test_token_fields_preserved(self):
        msgs = [_api_req(1778000000000, tokens_in=42, tokens_out=7,
                         cache_writes=100, cache_reads=200, cost=0.05)]
        entries = extract_cline_entries(msgs, "t1")
        entry = next(iter(entries.values()))
        assert entry["tokensIn"] == 42
        assert entry["tokensOut"] == 7
        assert entry["cacheWrites"] == 100
        assert entry["cacheReads"] == 200
        assert entry["cost"] == 0.05
        # cacheSavings should be computed, not zero
        assert entry["cacheSavings"] > 0

    def test_entry_id_format(self):
        msgs = [_api_req(1778000000000)]
        entries = extract_cline_entries(msgs, "task123")
        eid = next(iter(entries.keys()))
        assert eid == "cline:task123:1778000000000"

    def test_malformed_text_skipped(self):
        msgs = [
            {"ts": 1, "say": "api_req_started", "text": "not json",
             "modelInfo": {}},
            _api_req(1778000000000, request="<task>\nok\n</task>"),
        ]
        entries = extract_cline_entries(msgs, "t1")
        assert len(entries) == 1  # only the valid one

    def test_missing_ts_skipped(self):
        msgs = [_api_req(0)]  # ts=0 is falsy → skipped
        entries = extract_cline_entries(msgs, "t1")
        assert entries == {}


# ---------------------------------------------------------------------------
# _project_for_cline_task
# ---------------------------------------------------------------------------

class TestProjectForClineTask:
    def test_extracts_cwd_from_env_details(self, tmp_path):
        req = (
            "<task>\ntest\n</task>\n"
            "<environment_details>\n"
            f"# Current Working Directory ({tmp_path}) Files\n"
            "file1.py\n"
            "</environment_details>"
        )
        msgs = [_api_req(1778000000000, request=req)]
        with patch("llmcars.collectors.Path.home", lambda: tmp_path):
            # Same dir as home → "~"
            assert _project_for_cline_task(msgs) == "~"

    def test_no_api_req(self):
        assert _project_for_cline_task([]) == ""
        assert _project_for_cline_task([{"say": "task"}]) == ""

    def test_no_cwd_line(self):
        msgs = [_api_req(1778000000000, request="<task>\nx\n</task>")]
        assert _project_for_cline_task(msgs) == ""
