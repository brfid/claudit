"""Tests for pipeline gap detection, backup rotation, and orphan cleanup."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from llmcars.ledger import (
    BACKUP_RETAIN,
    backup_dir,
    hours_since_last_ingest,
    load_ingest_state,
    prune_orphan_file_state,
    rotate_backup,
    save_ingest_state,
    save_ledger,
    stamp_ingest,
)
from llmcars.pipeline import DEFAULT_MAX_GAP_HOURS, run_ingest


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestIngestTimestamp:
    def test_stamp_and_read(self, tmp_dir):
        state = {"_version": 1, "files": {}, "last_ingest_at": None}
        stamp_ingest(state)
        gap = hours_since_last_ingest(state)
        assert gap is not None
        assert 0 <= gap < 0.01  # under 36 seconds

    def test_unknown_timestamp(self):
        assert hours_since_last_ingest({"last_ingest_at": None}) is None
        assert hours_since_last_ingest({}) is None
        assert hours_since_last_ingest({"last_ingest_at": "not-a-date"}) is None

    def test_stale_timestamp(self):
        two_days_ago = (datetime.now() - timedelta(hours=48)).isoformat()
        gap = hours_since_last_ingest({"last_ingest_at": two_days_ago})
        assert 47.9 < gap < 48.1


class TestBackupRotation:
    def test_creates_backup(self, tmp_dir):
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"entry1": {"cost": 1.0}})
        result = rotate_backup(ledger_path)
        today = datetime.now().strftime("%Y-%m-%d")
        assert result == backup_dir(ledger_path) / f"ledger-{today}.json"
        assert result.exists()
        assert json.loads(result.read_text()) == {"entry1": {"cost": 1.0}}

    def test_idempotent_per_day(self, tmp_dir):
        """Running backup twice same day is a no-op."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        first = rotate_backup(ledger_path)
        second = rotate_backup(ledger_path)
        assert first is not None
        assert second is None  # idempotent

    def test_missing_source_skipped(self, tmp_dir):
        """No ledger file → no backup."""
        ledger_path = tmp_dir / "missing.json"
        assert rotate_backup(ledger_path) is None

    def test_retention_prunes_old(self, tmp_dir):
        """Only keep N most recent backups."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {})
        d = backup_dir(ledger_path)
        d.mkdir(parents=True, exist_ok=True)
        # Create 10 fake old backups
        for i in range(10):
            (d / f"ledger-2020-01-{i:02d}.json").write_text("{}")
        rotate_backup(ledger_path, retain=3)
        remaining = sorted(d.glob("ledger-*.json"))
        # Should be 3 total: today's + 2 oldest-keepers (sorted lex)
        assert len(remaining) == 3
        # The newest is today's
        today = datetime.now().strftime("%Y-%m-%d")
        assert remaining[-1].name == f"ledger-{today}.json"


class TestOrphanCleanup:
    def test_drops_missing_files(self, tmp_dir):
        real = tmp_dir / "real.jsonl"
        real.write_text("")
        state = {
            "_version": 1,
            "files": {
                str(real): {"size": 0, "byte_offset": 0},
                "/does/not/exist.jsonl": {"size": 100, "byte_offset": 50},
                "/also/gone.jsonl": {"size": 0, "byte_offset": 0},
            },
        }
        pruned = prune_orphan_file_state(state)
        assert pruned == 2
        assert str(real) in state["files"]
        assert "/does/not/exist.jsonl" not in state["files"]


class TestGapDetection:
    def test_first_run_no_gap_trigger(self, tmp_dir, capsys):
        """First run: no previous timestamp, so no gap-triggered deep mode."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {})
        ledger = {}

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False)

        captured = capsys.readouterr()
        assert "Deep rescan" not in captured.out

    def test_fresh_ingest_no_trigger(self, tmp_dir, capsys):
        """Previous run <24h ago: stays incremental."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        # Seed an ingest-state that says we ran 1 hour ago
        state_path = ledger_path.parent / "ingest_state.json"
        recent = (datetime.now() - timedelta(hours=1)).isoformat()
        save_ingest_state(state_path, {
            "_version": 1, "files": {}, "last_ingest_at": recent,
        })

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False)

        captured = capsys.readouterr()
        assert "Deep rescan" not in captured.out

    def test_stale_ingest_triggers_deep(self, tmp_dir, capsys):
        """Previous run >24h ago: auto-promote to deep mode."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        stale = (datetime.now() - timedelta(hours=48)).isoformat()
        save_ingest_state(state_path, {
            "_version": 1,
            "files": {"/some/old/file.jsonl": {"size": 100, "byte_offset": 50}},
            "last_ingest_at": stale,
        })

        with patch("llmcars.pipeline.collect_cline_data", return_value={}) as cline, \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}) as cc:
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       max_gap_hours=24.0)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out
        # Collectors called with fresh ingest_state (no old file entry)
        cline_state = cline.call_args.kwargs["ingest_state"]
        cc_state = cc.call_args.kwargs["ingest_state"]
        assert "/some/old/file.jsonl" not in cline_state.get("files", {})
        assert "/some/old/file.jsonl" not in cc_state.get("files", {})

    def test_custom_gap_threshold(self, tmp_dir, capsys):
        """User can tune the threshold."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        save_ingest_state(state_path, {
            "_version": 1, "files": {},
            "last_ingest_at": (datetime.now() - timedelta(hours=2)).isoformat(),
        })

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            # 1-hour threshold: 2h gap should trigger
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       max_gap_hours=1.0)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out

    def test_explicit_deep_flag(self, tmp_dir, capsys):
        """--deep forces rescan even with fresh ingest state."""
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"e1": {}})
        ledger = {"e1": {}}
        state_path = ledger_path.parent / "ingest_state.json"
        save_ingest_state(state_path, {
            "_version": 1, "files": {},
            "last_ingest_at": datetime.now().isoformat(),
        })

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=False,
                       deep=True)

        captured = capsys.readouterr()
        assert "Deep rescan" in captured.out


class TestBackupIntegration:
    def test_run_ingest_creates_backup(self, tmp_dir):
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {"pre": {"cost": 1.0}})
        ledger = {"pre": {"cost": 1.0}}

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=True)

        today = datetime.now().strftime("%Y-%m-%d")
        assert (backup_dir(ledger_path) / f"ledger-{today}.json").exists()


class TestDefaults:
    def test_default_gap_hours(self):
        assert DEFAULT_MAX_GAP_HOURS == 24.0

    def test_default_retention(self):
        assert BACKUP_RETAIN == 7


class TestTimestampPersisted:
    def test_ingest_stamps_state(self, tmp_dir):
        ledger_path = tmp_dir / "ledger.json"
        save_ledger(ledger_path, {})
        ledger = {}

        with patch("llmcars.pipeline.collect_cline_data", return_value={}), \
             patch("llmcars.pipeline.collect_claude_code_data", return_value={}):
            run_ingest(ledger_path, ledger, source="all", quiet=True)

        state = load_ingest_state(ledger_path.parent / "ingest_state.json")
        assert state["last_ingest_at"] is not None
