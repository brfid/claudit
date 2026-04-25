"""Tests for incremental ingest and date-filtered aggregation."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_cost_tracker import (
    aggregate_by_day,
    compute_date_window,
    file_needs_processing,
    ingest,
    load_ingest_state,
    load_ledger,
    parse_claude_code_session,
    save_ingest_state,
    save_ledger,
    update_file_state,
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
        entries, offset = parse_claude_code_session(f)
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

        entries, offset = parse_claude_code_session(f, seek_offset=first_size)
        assert len(entries) == 1
        assert "cc:msg2" in entries
        assert offset == f.stat().st_size

    def test_incomplete_trailing_line(self, tmp_dir):
        f = tmp_dir / "session.jsonl"
        complete = make_jsonl_line("msg1", "2026-04-20T10:00:00Z")
        incomplete = '{"type": "assistant", "message": {"id": "msg2"'
        make_session_file(f, [complete, incomplete])
        entries, offset = parse_claude_code_session(f)
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
        entries, offset = parse_claude_code_session(f)
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
        assert state == {"_version": 1, "files": {}}

    def test_corrupt_file(self, tmp_dir):
        path = tmp_dir / "bad.json"
        path.write_text("not json at all")
        state = load_ingest_state(path)
        assert state == {"_version": 1, "files": {}}


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
        entries, offset = parse_claude_code_session(session_file)
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

        entries, offset = parse_claude_code_session(session_file, seek_offset=seek)
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
