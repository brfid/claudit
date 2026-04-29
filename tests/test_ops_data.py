"""Tests for pure ops_data helpers."""

from datetime import datetime, timedelta

from claudit.ops_data import (
    TOOL_ABBREV,
    aggregate_today,
    collect_entries,
    cost_bar,
    group_by_prompt,
    hourly_cost_today,
    model_color,
    percentile,
    row_activity_text,
    row_preview_text,  # noqa: F401 — alias kept for back-compat
    short_model,
    short_project,
    short_tools,
    subagent_cost_rollup,
)


# ── short_model ───────────────────────────────────────────────────────────

class TestShortModel:
    def test_opus(self):
        assert short_model("claude-opus-4-6") == "opus-4.6"

    def test_sonnet_with_date_suffix(self):
        assert short_model("claude-sonnet-4-5-20250929") == "sonnet-4.5"

    def test_haiku(self):
        assert short_model("claude-haiku-4-5-20251001") == "haiku-4.5"

    def test_none(self):
        assert short_model(None) == "—"

    def test_unknown(self):
        assert short_model("gpt-4") == "gpt"

    def test_empty(self):
        assert short_model("") == "—"


# ── model_color ───────────────────────────────────────────────────────────

class TestModelColor:
    def test_opus_amber(self):
        assert model_color("opus-4.7") == "#FF9900"

    def test_sonnet_periwinkle(self):
        assert model_color("sonnet-4.5") == "#9999CC"

    def test_haiku_mauve(self):
        assert model_color("haiku-4.5") == "#CC6699"

    def test_unknown_family_fallback(self):
        assert model_color("mystery-1") == "#CC9966"


# ── short_project ─────────────────────────────────────────────────────────

class TestShortProject:
    def test_basename(self):
        assert short_project("~/src/techdocs-tools") == "techdocs-tools"

    def test_trailing_slash(self):
        assert short_project("/foo/bar/") == "bar"

    def test_empty(self):
        assert short_project("") == "—"


# ── short_tools ───────────────────────────────────────────────────────────

class TestShortTools:
    def test_empty(self):
        assert short_tools([]) == ""

    def test_known_abbrevs(self):
        assert short_tools(["Read", "Edit", "Bash"]) == "RE$"

    def test_repeats_get_count(self):
        assert short_tools(["Read", "Read", "Read", "Edit"]) == "R×3E"

    def test_unknown_collapses_to_letter(self):
        assert short_tools(["Mystery"]) == "M"

    def test_abbrev_table_covers_common_tools(self):
        # Sanity: the abbreviation table has entries for Claude Code's core tools
        for t in ("Read", "Write", "Edit", "Bash", "Grep", "Glob"):
            assert t in TOOL_ABBREV


# ── cost_bar ──────────────────────────────────────────────────────────────

class TestCostBar:
    def test_full(self):
        assert cost_bar(10, 10, width=4) == "▓▓▓▓"

    def test_half(self):
        assert cost_bar(5, 10, width=4) == "▓▓░░"

    def test_empty(self):
        assert cost_bar(0, 10, width=4) == "░░░░"

    def test_zero_max(self):
        assert cost_bar(5, 0, width=4) == "    "


# ── percentile ────────────────────────────────────────────────────────────

class TestPercentile:
    def test_empty(self):
        assert percentile([], 0.5) == 0

    def test_median(self):
        assert percentile([1, 2, 3, 4, 5], 0.5) == 3

    def test_p95(self):
        # idx = min(4, int(5*0.95)) = 4 → last element
        assert percentile([1, 2, 3, 4, 5], 0.95) == 5

    def test_single(self):
        assert percentile([42], 0.5) == 42


# ── hourly_cost_today ─────────────────────────────────────────────────────

class TestHourlyCostToday:
    def _entry(self, dt, cost):
        return (dt, f"id-{cost}", {"cost": cost, "ts": dt.isoformat(),
                                    "source": "cc"})

    def test_empty_ledger(self):
        result = hourly_cost_today([])
        assert result == [0.0] * 24

    def test_sums_by_hour(self):
        today = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
        entries = [
            self._entry(today, 1.0),
            self._entry(today.replace(hour=14, minute=30), 2.0),
            self._entry(today.replace(hour=9), 0.5),
        ]
        result = hourly_cost_today(entries)
        assert result[14] == 3.0
        assert result[9] == 0.5
        assert result[0] == 0.0
        assert sum(result) == 3.5

    def test_ignores_other_days(self):
        today = datetime.now().replace(hour=12)
        yesterday = today - timedelta(days=1)
        entries = [
            self._entry(today, 1.0),
            self._entry(yesterday, 99.0),
        ]
        result = hourly_cost_today(entries)
        assert sum(result) == 1.0


# ── collect_entries ───────────────────────────────────────────────────────

class TestCollectEntries:
    def test_sorted_newest_first(self):
        now = datetime.now()
        ledger = {
            "a": {"source": "cc", "ts": (now - timedelta(hours=2)).isoformat(),
                  "cost": 1.0},
            "b": {"source": "cc", "ts": now.isoformat(), "cost": 2.0},
            "c": {"source": "cc", "ts": (now - timedelta(hours=1)).isoformat(),
                  "cost": 3.0},
        }
        result = collect_entries(ledger, source_filter=None)
        assert [eid for _, eid, _ in result] == ["b", "c", "a"]

    def test_source_filter(self):
        now = datetime.now()
        ledger = {
            "a": {"source": "cc", "ts": now.isoformat(), "cost": 1.0},
            "b": {"source": "cline", "ts": now.isoformat(), "cost": 2.0},
        }
        result = collect_entries(ledger, source_filter="cc")
        assert [eid for _, eid, _ in result] == ["a"]

    def test_skips_unparseable_timestamps(self):
        ledger = {
            "a": {"source": "cc", "ts": "not-a-date", "cost": 1.0},
            "b": {"source": "cc", "ts": datetime.now().isoformat(), "cost": 2.0},
        }
        result = collect_entries(ledger, source_filter=None)
        assert [eid for _, eid, _ in result] == ["b"]


# ── aggregate_today ───────────────────────────────────────────────────────

class TestAggregateToday:
    def _make_entries(self):
        now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        return [
            (now, "a", {
                "source": "cc", "ts": now.isoformat(),
                "cost": 1.0, "tokensIn": 100, "tokensOut": 50,
                "cacheSavings": 0.5, "model": "claude-opus-4-6",
                "project": "/Users/x/src/proj1", "stopReason": "end_turn",
                "isSubagent": False,
            }),
            (now - timedelta(minutes=30), "b", {
                "source": "cc", "ts": now.isoformat(),
                "cost": 2.0, "tokensIn": 200, "tokensOut": 100,
                "cacheSavings": 0.3, "model": "claude-sonnet-4-5",
                "project": "/Users/x/src/proj2", "stopReason": "tool_use",
                "isSubagent": True,
            }),
            (yesterday, "c", {
                "source": "cc", "ts": yesterday.isoformat(),
                "cost": 99.0, "tokensIn": 999, "tokensOut": 999,
                "cacheSavings": 0, "model": "claude-opus-4-6",
                "project": "", "stopReason": "end_turn",
                "isSubagent": False,
            }),
        ]

    def test_excludes_other_days(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["count"] == 2
        assert s["cost"] == 3.0

    def test_token_totals(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["tokens_in"] == 300
        assert s["tokens_out"] == 150

    def test_subagent_isolation(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["subagent_count"] == 1
        assert s["subagent_cost"] == 2.0

    def test_project_rollup(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["project_cost"]["proj1"] == 1.0
        assert s["project_cost"]["proj2"] == 2.0

    def test_model_rollup(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["model_counts"]["opus-4.6"] == 1
        assert s["model_counts"]["sonnet-4.5"] == 1

    def test_stop_rollup(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["stop_counts"]["end_turn"] == 1
        assert s["stop_counts"]["tool_use"] == 1

    def test_costs_sorted(self):
        s = aggregate_today(self._make_entries(), short_project, short_model)
        assert s["costs"] == [1.0, 2.0]


# ── row_activity_text ─────────────────────────────────────────────────────

class TestRowActivityText:
    def test_prompt_wins(self):
        r = row_activity_text({"promptPreview": "hello world"})
        assert "hello world" in r
        assert "»" in r

    def test_agent_spawn_shows_type_and_desc(self):
        r = row_activity_text({
            "source": "agent_spawn",
            "subagentType": "compound-engineering:research:ce-web-researcher",
            "description": "Web research on X",
        })
        assert "spawn" in r
        assert "ce-web-researcher" in r
        assert "Web research on X" in r

    def test_agent_spawn_without_desc(self):
        r = row_activity_text({
            "source": "agent_spawn",
            "subagentType": "Explore",
        })
        assert "spawn" in r
        assert "Explore" in r

    def test_tool_chain_when_no_prompt(self):
        r = row_activity_text({"tools": ["Read", "Edit", "Edit", "Bash"]})
        assert "Read" in r
        assert "Edit×2" in r
        assert "Bash" in r
        assert "→" in r

    def test_stop_fallback_ignores_end_turn(self):
        r = row_activity_text({"stopReason": "end_turn"})
        assert "stop=" not in r

    def test_stop_fallback_keeps_unusual(self):
        r = row_activity_text({"stopReason": "max_tokens"})
        assert "stop=max_tokens" in r

    def test_subagent_flag_in_fallback(self):
        r = row_activity_text({"isSubagent": True, "stopReason": "end_turn"})
        assert "subagent turn" in r

    def test_session_tail_last_resort(self):
        r = row_activity_text({"session": "abcdef123456"})
        assert "abcdef12" in r

    def test_fully_empty(self):
        r = row_activity_text({})
        assert r.endswith("—[/]")

    def test_collapses_whitespace(self):
        r = row_activity_text({"promptPreview": "hi\n\n\t  there"})
        assert "hi there" in r

    def test_escapes_bracket_injection(self):
        r = row_activity_text({"promptPreview": "[b]fake[/b]"})
        assert "\\[b]fake" in r

    def test_back_compat_alias(self):
        # row_preview_text is still importable and behaves like row_activity_text
        assert row_preview_text({"promptPreview": "x"}) == row_activity_text(
            {"promptPreview": "x"}
        )


# ── group_by_prompt ───────────────────────────────────────────────────────

class TestGroupByPrompt:
    def _entries(self):
        t0 = datetime.now()
        return [
            (t0, "cc:1", {
                "source": "cc", "ts": t0.isoformat(),
                "promptId": "p1", "session": "s1",
                "cost": 1.0, "tokensIn": 100, "tokensOut": 50,
                "cacheReads": 0, "cacheWrites": 0,
                "tools": ["Read"], "stopReason": "tool_use",
                "model": "claude-opus-4-6", "promptPreview": "anchor text",
                "isSubagent": False, "project": "p",
            }),
            (t0 - timedelta(seconds=5), "cc:2", {
                "source": "cc", "ts": t0.isoformat(),
                "promptId": "p1", "session": "s1",
                "cost": 2.0, "tokensIn": 200, "tokensOut": 100,
                "cacheReads": 0, "cacheWrites": 0,
                "tools": ["Edit"], "stopReason": "end_turn",
                "model": "claude-opus-4-6", "promptPreview": "",
                "isSubagent": False, "project": "p",
            }),
            (t0 - timedelta(minutes=2), "cc:3", {
                "source": "cc", "ts": t0.isoformat(),
                "promptId": "p2", "session": "s1",
                "cost": 0.5, "tokensIn": 50, "tokensOut": 25,
                "cacheReads": 0, "cacheWrites": 0,
                "tools": [], "stopReason": "end_turn",
                "model": "claude-sonnet-4-5", "promptPreview": "second prompt",
                "isSubagent": False, "project": "p",
            }),
        ]

    def test_groups_share_prompt_id(self):
        groups = group_by_prompt(self._entries())
        assert len(groups) == 2
        by_id = {g["prompt_id"]: g for g in groups}
        assert by_id["p1"]["turns"] == 2
        assert by_id["p1"]["cost"] == 3.0
        assert by_id["p2"]["turns"] == 1

    def test_newest_first(self):
        groups = group_by_prompt(self._entries())
        assert groups[0]["prompt_id"] == "p1"
        assert groups[1]["prompt_id"] == "p2"

    def test_tool_list_aggregates(self):
        groups = group_by_prompt(self._entries())
        by_id = {g["prompt_id"]: g for g in groups}
        assert set(by_id["p1"]["tools"]) == {"Read", "Edit"}

    def test_spawn_count_increments(self):
        entries = self._entries()
        entries.append((
            datetime.now(), "spawn:abc",
            {"source": "agent_spawn", "promptId": "p1",
             "subagentType": "Explore"},
        ))
        groups = group_by_prompt(entries)
        by_id = {g["prompt_id"]: g for g in groups}
        assert by_id["p1"]["spawn_count"] == 1

    def test_orphan_entries(self):
        """Entries without promptId each become their own group."""
        now = datetime.now()
        entries = [
            (now, "cc:o1", {
                "source": "cc", "cost": 1.0, "session": "s1",
                "tokensIn": 0, "tokensOut": 0,
                "cacheReads": 0, "cacheWrites": 0,
                "tools": [], "stopReason": "end_turn",
            }),
            (now - timedelta(seconds=1), "cc:o2", {
                "source": "cc", "cost": 2.0, "session": "s1",
                "tokensIn": 0, "tokensOut": 0,
                "cacheReads": 0, "cacheWrites": 0,
                "tools": [], "stopReason": "end_turn",
            }),
        ]
        groups = group_by_prompt(entries)
        assert len(groups) == 2


# ── subagent_cost_rollup ──────────────────────────────────────────────────

class TestSubagentCostRollup:
    def test_empty(self):
        assert subagent_cost_rollup([]) == {}

    def test_rolls_up_subagent_to_parent(self):
        today = datetime.now()
        entries = [
            (today, "cc:1", {
                "source": "cc", "isSubagent": True,
                "parentSession": "parent-1", "cost": 1.5,
                "ts": today.isoformat(),
            }),
            (today, "cc:2", {
                "source": "cc", "isSubagent": True,
                "parentSession": "parent-1", "cost": 0.5,
                "ts": today.isoformat(),
            }),
            (today, "cc:3", {
                "source": "cc", "isSubagent": False,
                "parentSession": "parent-2", "cost": 99.0,
                "ts": today.isoformat(),
            }),
        ]
        rollup = subagent_cost_rollup(entries)
        assert rollup == {"parent-1": 2.0}

    def test_ignores_entries_without_parent(self):
        today = datetime.now()
        entries = [
            (today, "cc:1", {
                "source": "cc", "isSubagent": True,
                "parentSession": "", "cost": 1.0,
                "ts": today.isoformat(),
            }),
        ]
        assert subagent_cost_rollup(entries) == {}


# ── aggregate_today subagent tracking ─────────────────────────────────────

class TestAggregateTodaySpawns:
    def test_spawn_entries_counted_separately(self):
        now = datetime.now()
        entries = [
            (now, "cc:1", {
                "source": "cc", "ts": now.isoformat(),
                "cost": 1.0, "tokensIn": 100, "tokensOut": 50,
                "cacheSavings": 0, "model": "claude-opus-4-6",
                "project": "/p", "stopReason": "end_turn",
                "isSubagent": False,
            }),
            (now, "spawn:abc", {
                "source": "agent_spawn", "ts": now.isoformat(),
                "subagentType": "Explore",
                "cost": 0, "tokensIn": 0, "tokensOut": 0,
                "cacheSavings": 0,
            }),
            (now, "spawn:def", {
                "source": "agent_spawn", "ts": now.isoformat(),
                "subagentType": "general-purpose",
                "cost": 0, "tokensIn": 0, "tokensOut": 0,
                "cacheSavings": 0,
            }),
            (now, "spawn:ghi", {
                "source": "agent_spawn", "ts": now.isoformat(),
                "subagentType": "Explore",
                "cost": 0, "tokensIn": 0, "tokensOut": 0,
                "cacheSavings": 0,
            }),
        ]
        s = aggregate_today(entries, short_project, short_model)
        # Billable count excludes spawns
        assert s["count"] == 1
        assert s["spawn_count"] == 3
        assert s["subagent_type_counts"]["Explore"] == 2
        assert s["subagent_type_counts"]["general-purpose"] == 1
