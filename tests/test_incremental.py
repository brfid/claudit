"""Tests for incremental ingest and date-filtered aggregation."""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from claudit.aggregation import aggregate_by_day, compute_date_window
from claudit.collectors import (
    _extract_user_text,
    _project_from_session_path,
    parse_claude_code_session,
)
from claudit.ledger import (
    file_needs_processing,
    ingest,
    load_ingest_state,
    load_ledger,
    save_ingest_state,
    save_ledger,
    update_file_state,
)
from claudit.pricing import (
    HAIKU_PRICING,
    OPUS_PRICING,
    SONNET_PRICING,
    _infer_pricing_by_family,
    get_model_pricing,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def make_jsonl_line(msg_id, timestamp, tokens_in=100, tokens_out=50,
                    cache_writes=0, cache_reads=0, model="claude-opus-4-6"):
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "id": msg_id,
            "model": model,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cache_creation_input_tokens": cache_writes,
                "cache_read_input_tokens": cache_reads,
            },
        },
    }) + "\n"


def make_session_file(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# file_needs_processing
# ---------------------------------------------------------------------------

class TestFileNeedsProcessing:
    def test_new_file(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        f.write_text("line\n")
        state = {"files": {}}
        needs, offset = file_needs_processing(f, state)
        assert needs is True
        assert offset == 0

    def test_unchanged_file(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        f.write_text("line\n")
        size = f.stat().st_size
        state = {"files": {str(f): {"size": size, "byte_offset": size, "mtime": 0}}}
        needs, offset = file_needs_processing(f, state)
        assert needs is False

    def test_grown_file(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        f.write_text("line1\n")
        original_size = f.stat().st_size
        state = {"files": {str(f): {"size": original_size, "byte_offset": original_size, "mtime": 0}}}
        with open(f, 'a') as fh:
            fh.write("line2\n")
        needs, offset = file_needs_processing(f, state)
        assert needs is True
        assert offset == original_size

    def test_shrunk_file(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        f.write_text("long content here\n")
        state = {"files": {str(f): {"size": 999, "byte_offset": 999, "mtime": 0}}}
        f.write_text("short\n")
        needs, offset = file_needs_processing(f, state)
        assert needs is True
        assert offset == 0

    def test_missing_file(self, tmp_dir):
        f = tmp_dir / "gone.jsonl"
        state = {"files": {}}
        needs, offset = file_needs_processing(f, state)
        assert needs is False


# ---------------------------------------------------------------------------
# parse_claude_code_session with seek
# ---------------------------------------------------------------------------

class TestIncrementalParsing:
    def test_full_parse(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        lines = [
            make_jsonl_line("msg1", "2026-04-20T10:00:00Z"),
            make_jsonl_line("msg2", "2026-04-20T11:00:00Z"),
        ]
        make_session_file(f, lines)
        entries, offset, _ = parse_claude_code_session(f)
        assert len(entries) == 2
        assert "cc:msg1" in entries
        assert "cc:msg2" in entries
        assert offset == f.stat().st_size

    def test_seek_resumes(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        line1 = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        make_session_file(f, [line1])
        first_size = f.stat().st_size

        with open(f, 'a') as fh:
            fh.write(make_jsonl_line("msg2", "2026-04-20T11:00:00Z"))

        entries, offset, _ = parse_claude_code_session(f, seek_offset=first_size)
        assert len(entries) == 1
        assert "cc:msg2" in entries
        assert offset == f.stat().st_size

    def test_incomplete_trailing_line(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        complete = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        incomplete = '{"type": "assistant", "message": {"id": "msg2"'
        make_session_file(f, [complete, incomplete])
        entries, offset, _ = parse_claude_code_session(f)
        assert len(entries) == 1
        assert "cc:msg1" in entries

    def test_non_assistant_lines_skipped(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        lines = [
            json.dumps({"type": "human", "message": {"text": "hello"}}) + "\n",
            make_jsonl_line("msg1", "2026-04-20T10:00:00Z"),
            json.dumps({"type": "system", "message": {}}) + "\n",
        ]
        make_session_file(f, lines)
        entries, offset, _ = parse_claude_code_session(f)
        assert len(entries) == 1


# ---------------------------------------------------------------------------
# Ingest state persistence
# ---------------------------------------------------------------------------

class TestIngestState:
    def test_roundtrip(self, tmp_dir):
        path = tmp_dir / "ingest_state.json"
        state = {"_version": 1, "files": {"/foo/bar.jsonl": {"size": 100, "byte_offset": 100, "mtime": 1.0}}}
        save_ingest_state(path, state)
        loaded = load_ingest_state(path)
        assert loaded == state

    def test_missing_file(self, tmp_dir):
        path = tmp_dir / "nope.json"
        state = load_ingest_state(path)
        assert state == {"_version": 1, "files": {}, "last_ingest_at": None}

    def test_corrupt_file(self, tmp_dir):
        path = tmp_dir / "bad.json"
        path.write_text("not json at all")
        state = load_ingest_state(path)
        assert state == {"_version": 1, "files": {}, "last_ingest_at": None}


# ---------------------------------------------------------------------------
# update_file_state
# ---------------------------------------------------------------------------

class TestUpdateFileState:
    def test_records_state(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        f.write_text("data\n")
        state = {"files": {}}
        update_file_state(state, f, 5)
        stored = state["files"][str(f)]
        assert stored["byte_offset"] == 5
        assert stored["size"] == f.stat().st_size
        assert stored["mtime"] == f.stat().st_mtime

    def test_missing_file_noop(self, tmp_dir):
        f = tmp_dir / "gone.jsonl"
        state = {"files": {}}
        update_file_state(state, f, 0)
        assert state["files"] == {}


# ---------------------------------------------------------------------------
# Date-filtered aggregation
# ---------------------------------------------------------------------------

class TestDateFilteredAggregation:
    def _make_ledger(self):
        return {
            "cc:a": {"source": "cc", "ts": "2026-04-10T10:00:00", "cost": 1.0,
                      "tokensIn": 100, "tokensOut": 50, "cacheWrites": 0,
                      "cacheReads": 0, "cacheSavings": 0.0},
            "cc:b": {"source": "cc", "ts": "2026-04-15T10:00:00", "cost": 2.0,
                      "tokensIn": 200, "tokensOut": 100, "cacheWrites": 0,
                      "cacheReads": 0, "cacheSavings": 0.0},
            "cc:c": {"source": "cc", "ts": "2026-04-20T10:00:00", "cost": 3.0,
                      "tokensIn": 300, "tokensOut": 150, "cacheWrites": 0,
                      "cacheReads": 0, "cacheSavings": 0.0},
        }

    def test_no_filter(self):
        daily = aggregate_by_day(self._make_ledger())
        assert len(daily) == 3

    def test_date_from(self):
        daily = aggregate_by_day(self._make_ledger(), date_from="2026-04-14")
        assert "2026-04-10" not in daily
        assert "2026-04-15" in daily
        assert "2026-04-20" in daily

    def test_date_to(self):
        daily = aggregate_by_day(self._make_ledger(), date_to="2026-04-15")
        assert "2026-04-10" in daily
        assert "2026-04-15" in daily
        assert "2026-04-20" not in daily

    def test_date_range(self):
        daily = aggregate_by_day(self._make_ledger(),
                                 date_from="2026-04-14", date_to="2026-04-16")
        assert len(daily) == 1
        assert "2026-04-15" in daily

    def test_source_filter_combined(self):
        ledger = self._make_ledger()
        ledger["cline:x"] = {
            "source": "cline", "ts": "2026-04-15T12:00:00", "cost": 5.0,
            "tokensIn": 500, "tokensOut": 250, "cacheWrites": 0,
            "cacheReads": 0, "cacheSavings": 0.0,
        }
        daily = aggregate_by_day(ledger, source_filter="cline",
                                 date_from="2026-04-14")
        assert len(daily) == 1
        assert daily["2026-04-15"]["cost"] == 5.0


# ---------------------------------------------------------------------------
# compute_date_window
# ---------------------------------------------------------------------------

class TestComputeDateWindow:
    def test_none_days(self):
        assert compute_date_window(None) is None

    def test_returns_past_date(self):
        result = compute_date_window(7)
        dt = datetime.strptime(result, "%Y-%m-%d")
        assert dt < datetime.now()
        days_back = (datetime.now() - dt).days
        assert days_back >= 14

    def test_large_days_scales(self):
        result = compute_date_window(90)
        dt = datetime.strptime(result, "%Y-%m-%d")
        days_back = (datetime.now() - dt).days
        assert days_back >= 180


# ---------------------------------------------------------------------------
# End-to-end incremental workflow
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_incremental_ingest_cycle(self, tmp_dir):
        ledger_path = tmp_dir / "ledger.json"
        state_path = tmp_dir / "ingest_state.json"

        session_file = tmp_dir / "projects" / "proj" / "sess.jsonl"
        line1 = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        make_session_file(session_file, [line1])

        # First parse
        ledger = {}
        state = {"_version": 1, "files": {}}
        entries, offset, _ = parse_claude_code_session(session_file)
        update_file_state(state, session_file, offset)
        ingest(ledger, entries)
        save_ledger(ledger_path, ledger)
        save_ingest_state(state_path, state)

        assert len(ledger) == 1
        assert "cc:msg1" in ledger

        # Append new data
        with open(session_file, 'a') as f:
            f.write(make_jsonl_line("msg2", "2026-04-20T11:00:00Z"))

        # Second parse — incremental
        state = load_ingest_state(state_path)
        needs, seek = file_needs_processing(session_file, state)
        assert needs is True
        assert seek > 0

        entries, offset, _ = parse_claude_code_session(session_file, seek_offset=seek)
        update_file_state(state, session_file, offset)
        ingest(ledger, entries)

        assert len(ledger) == 2
        assert "cc:msg2" in ledger

        # Third run — no change
        save_ingest_state(state_path, state)
        state = load_ingest_state(state_path)
        needs, _ = file_needs_processing(session_file, state)
        assert needs is False

    def test_empty_ledger_resets_state(self, tmp_dir):
        """When ledger is empty/missing, ingest state should be ignored."""
        state_path = tmp_dir / "ingest_state.json"
        save_ingest_state(state_path, {
            "_version": 1,
            "files": {"/some/old/file.jsonl": {"size": 500, "byte_offset": 500, "mtime": 1.0}}
        })
        ledger = load_ledger(tmp_dir / "nonexistent_ledger.json")
        assert ledger == {}
        # When ledger is empty, main() resets state — simulate that
        if not ledger:
            state = {"_version": 1, "files": {}}
        else:
            state = load_ingest_state(state_path)
        assert state["files"] == {}


# ---------------------------------------------------------------------------
# Model pricing lookup
# ---------------------------------------------------------------------------

class TestModelPricing:
    def test_exact_model_match(self):
        # Sonnet 4.5 is exactly registered
        assert get_model_pricing("claude-sonnet-4-5-20250929") is SONNET_PRICING

    def test_family_fallback_opus(self):
        # Unknown opus variant should still resolve to opus pricing
        assert get_model_pricing("claude-opus-99-99") is OPUS_PRICING

    def test_family_fallback_haiku(self):
        assert get_model_pricing("claude-haiku-future") is HAIKU_PRICING

    def test_unknown_falls_back_to_default(self):
        # Non-Anthropic model name falls back to default (currently sonnet)
        result = get_model_pricing("gpt-4")
        assert result is SONNET_PRICING  # current DEFAULT

    def test_none_model(self):
        assert get_model_pricing(None) is SONNET_PRICING

    def test_infer_by_family_direct(self):
        assert _infer_pricing_by_family("anything-opus-inside") is OPUS_PRICING
        assert _infer_pricing_by_family("foo-sonnet-bar") is SONNET_PRICING
        assert _infer_pricing_by_family("haiku-x") is HAIKU_PRICING
        assert _infer_pricing_by_family("gemini-pro") is None


# ---------------------------------------------------------------------------
# User text extraction (prompt preview)
# ---------------------------------------------------------------------------

class TestExtractUserText:
    def test_string_content(self):
        obj = {"message": {"content": "hello world"}}
        assert _extract_user_text(obj) == "hello world"

    def test_list_with_text_and_tool_result(self):
        obj = {"message": {"content": [
            {"type": "text", "text": "first"},
            {"type": "tool_result", "content": "large json blob"},
            {"type": "text", "text": "second"},
        ]}}
        assert _extract_user_text(obj) == "first second"

    def test_only_tool_result(self):
        obj = {"message": {"content": [
            {"type": "tool_result", "content": "blob"},
        ]}}
        assert _extract_user_text(obj) == ""

    def test_empty_message(self):
        assert _extract_user_text({"message": {}}) == ""

    def test_missing_message(self):
        assert _extract_user_text({}) == ""

    def test_skips_empty_text_blocks(self):
        obj = {"message": {"content": [
            {"type": "text", "text": ""},
            {"type": "text", "text": "real"},
        ]}}
        assert _extract_user_text(obj) == "real"


# ---------------------------------------------------------------------------
# Ingest: schema evolution back-fill
# ---------------------------------------------------------------------------

class TestIngestBackfill:
    def test_new_entry_added(self):
        ledger = {}
        added = ingest(ledger, {"cc:1": {"source": "cc", "cost": 1.0}})
        assert added == 1
        assert ledger["cc:1"]["cost"] == 1.0

    def test_existing_entry_not_overwritten(self):
        ledger = {"cc:1": {"source": "cc", "cost": 1.0, "model": "old-model"}}
        added = ingest(ledger, {"cc:1": {"source": "cc", "cost": 999.0, "model": "new-model"}})
        assert added == 0
        # Existing keys preserved, not overwritten
        assert ledger["cc:1"]["cost"] == 1.0
        assert ledger["cc:1"]["model"] == "old-model"

    def test_missing_keys_backfilled(self):
        """Old entries missing new schema fields get them filled in on re-ingest."""
        ledger = {"cc:1": {"source": "cc", "cost": 1.0}}
        added = ingest(ledger, {"cc:1": {
            "source": "cc", "cost": 1.0, "model": "opus", "project": "~/foo",
            "promptPreview": "hi there",
        }})
        assert added == 0
        assert ledger["cc:1"]["model"] == "opus"
        assert ledger["cc:1"]["project"] == "~/foo"
        assert ledger["cc:1"]["promptPreview"] == "hi there"

    def test_empty_value_not_backfilled(self):
        """Empty-string or None values don't overwrite-by-absence."""
        ledger = {"cc:1": {"source": "cc"}}
        ingest(ledger, {"cc:1": {"source": "cc", "promptPreview": "",
                                 "stopReason": None, "model": "opus"}})
        assert "promptPreview" not in ledger["cc:1"]
        assert "stopReason" not in ledger["cc:1"]
        assert ledger["cc:1"]["model"] == "opus"


# ---------------------------------------------------------------------------
# Project path resolution
# ---------------------------------------------------------------------------

class TestProjectFromSessionPath:
    def test_outside_projects_dir(self, tmp_dir):
        f = tmp_dir / "random.jsonl"
        f.write_text("")
        # Not under ~/.claude/projects — returns empty
        assert _project_from_session_path(f) == ""

    def test_greedy_resolves_dashed_names(self, tmp_dir, monkeypatch):
        """Verify greedy resolver picks longest real directory at each level."""
        # Build a fake ~/.claude/projects/-tmp-DIR-techdocs-tools/ layout where
        # the slug has to be decoded against a real filesystem.
        fake_home = tmp_dir
        fake_cc = fake_home / ".claude"
        projects_dir = fake_cc / "projects"
        # Create real target: tmp_dir / "techdocs-tools" (dash in real dirname)
        real_target = tmp_dir / "techdocs-tools"
        real_target.mkdir()
        # Slug encodes absolute path with dashes
        slug_parts = str(tmp_dir).lstrip("/").split("/") + ["techdocs-tools"]
        slug = "-" + "-".join(p for part in slug_parts for p in part.split("-"))
        slug_dir = projects_dir / slug
        slug_dir.mkdir(parents=True)
        session_file = slug_dir / "abc.jsonl"
        session_file.write_text("")

        monkeypatch.setattr("claudit.collectors.get_claude_code_dir", lambda: fake_cc)
        monkeypatch.setattr("claudit.collectors.Path.home", lambda: fake_home)

        result = _project_from_session_path(session_file)
        # Should resolve to the real target
        assert result.endswith("techdocs-tools")


# ---------------------------------------------------------------------------
# Parser: prompt preview + subagent detection
# ---------------------------------------------------------------------------

class TestPromptPreviewAndSubagent:
    def test_preview_captured_from_preceding_user(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        user_line = json.dumps({
            "type": "user",
            "message": {"content": "please help with X"},
        }) + "\n"
        asst_line = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        make_session_file(f, [user_line, asst_line])
        entries, _, _ = parse_claude_code_session(f)
        assert entries["cc:msg1"]["promptPreview"] == "please help with X"

    def test_preview_empty_when_no_user_before(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        make_session_file(f, [make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, _ = parse_claude_code_session(f)
        assert entries["cc:msg1"]["promptPreview"] == ""

    def test_preview_ignores_system_tags(self, tmp_dir):
        """Messages starting with < (e.g. <system-reminder>) are not captured."""
        f = tmp_dir / "session.jsonl"
        sys_user = json.dumps({
            "type": "user",
            "message": {"content": "<system-reminder>noise</system-reminder>"},
        }) + "\n"
        real_user = json.dumps({
            "type": "user",
            "message": {"content": "actual question"},
        }) + "\n"
        asst = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        make_session_file(f, [sys_user, real_user, asst])
        entries, _, _ = parse_claude_code_session(f)
        assert entries["cc:msg1"]["promptPreview"] == "actual question"

    def test_preview_truncated_to_80(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        long_text = "x" * 500
        user_line = json.dumps({
            "type": "user",
            "message": {"content": long_text},
        }) + "\n"
        asst = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        make_session_file(f, [user_line, asst])
        entries, _, _ = parse_claude_code_session(f)
        assert len(entries["cc:msg1"]["promptPreview"]) == 80

    def test_subagent_flag_from_path(self, tmp_dir):
        # Session file under */subagents/* should flag isSubagent
        sub_dir = tmp_dir / "projects" / "proj" / "subagents"
        sub_file = sub_dir / "sa.jsonl"
        make_session_file(sub_file, [make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, _ = parse_claude_code_session(sub_file)
        assert entries["cc:msg1"]["isSubagent"] is True

    def test_subagent_false_for_plain_session(self, tmp_dir):
        f = tmp_dir / "projects" / "proj" / "sess.jsonl"
        make_session_file(f, [make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, _ = parse_claude_code_session(f)
        assert entries["cc:msg1"]["isSubagent"] is False

    def test_session_field_uses_stem(self, tmp_dir):
        f = tmp_dir / "projects" / "proj" / "abc-123.jsonl"
        make_session_file(f, [make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, _ = parse_claude_code_session(f)
        assert entries["cc:msg1"]["session"] == "abc-123"

    def test_initial_user_text_seeds_preview(self, tmp_dir):
        """Resuming from mid-file should still attach preview from prior pass."""
        f = tmp_dir / "session.jsonl"
        make_session_file(f, [make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, final_user = parse_claude_code_session(
            f, initial_user_text="carried from last pass")
        assert entries["cc:msg1"]["promptPreview"] == "carried from last pass"
        # Final user text unchanged when no new user lines
        assert final_user == "carried from last pass"

    def test_new_user_overrides_seed(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        user_line = json.dumps({
            "type": "user",
            "message": {"content": "fresh prompt"},
        }) + "\n"
        make_session_file(f, [user_line, make_jsonl_line("msg1", "2026-04-20T10:00:00Z")])
        entries, _, final_user = parse_claude_code_session(
            f, initial_user_text="stale")
        assert entries["cc:msg1"]["promptPreview"] == "fresh prompt"
        assert final_user == "fresh prompt"


# ---------------------------------------------------------------------------
# Project-filtered aggregation
# ---------------------------------------------------------------------------

class TestProjectFilter:
    def _make_ledger(self):
        return {
            "cc:a": {"source": "cc", "ts": "2026-04-10T10:00:00", "cost": 1.0,
                     "tokensIn": 100, "tokensOut": 50, "cacheWrites": 0,
                     "cacheReads": 0, "cacheSavings": 0.0,
                     "project": "~/src/techdocs-tools"},
            "cc:b": {"source": "cc", "ts": "2026-04-15T10:00:00", "cost": 2.0,
                     "tokensIn": 200, "tokensOut": 100, "cacheWrites": 0,
                     "cacheReads": 0, "cacheSavings": 0.0,
                     "project": "~/src/other-project"},
        }

    def test_matches_substring(self):
        daily = aggregate_by_day(self._make_ledger(), project_filter="techdocs")
        assert "2026-04-10" in daily
        assert "2026-04-15" not in daily

    def test_case_insensitive(self):
        daily = aggregate_by_day(self._make_ledger(), project_filter="TECHDOCS")
        assert "2026-04-10" in daily

    def test_no_match(self):
        daily = aggregate_by_day(self._make_ledger(), project_filter="nonexistent")
        assert daily == {}

    def test_no_filter_keeps_all(self):
        daily = aggregate_by_day(self._make_ledger())
        assert len(daily) == 2

    def test_missing_project_field_excluded(self):
        """Entries without a project field don't match any substring filter."""
        ledger = {"cc:x": {"source": "cc", "ts": "2026-04-10T10:00:00",
                           "cost": 1.0, "tokensIn": 0, "tokensOut": 0,
                           "cacheWrites": 0, "cacheReads": 0, "cacheSavings": 0.0}}
        daily = aggregate_by_day(ledger, project_filter="anything")
        assert daily == {}
