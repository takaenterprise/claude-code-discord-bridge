"""Tests for the bot-memo 書込前契約検査 (memo_contract.py) and its wiring
into _run_helper._write_bot_memo().

Covers: analyze_markdown's structural counting (including the fence-hides-
markdown-structure rule), check_memo_contract's violation vocabulary,
repair_entry's deterministic fixes, log_contract_event's JSONL trail
(including the PASS-is-recorded-too and fail-open-on-exception behaviour),
and the end-to-end wiring inside run_claude_with_config().
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_discord.claude.types import MessageType, StreamEvent
from claude_discord.cogs._run_helper import run_claude_with_config
from claude_discord.cogs.run_config import RunConfig
from claude_discord.ext import memo_contract
from claude_discord.ext.bot_memo import build_file_header
from claude_discord.ext.memo_contract import (
    ContractStats,
    ContractVerdict,
    analyze_markdown,
    check_memo_contract,
    contract_enabled,
    log_contract_event,
    repair_entry,
)

_VALID_ENTRY = (
    "### 09:05 — ccdb (thread 123456, session s1)\n\n"
    "**Q:** 明日の在庫はどうなっていますか、確認をお願いします\n\n"
    "**A:** 在庫は問題なく確保できています、明日の出荷にも影響ありません\n\n"
    "---\n\n"
)


@pytest.fixture(autouse=True)
def _reset_reject_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a clean consecutive-reject streak (module-global)."""
    monkeypatch.setattr(memo_contract, "_consecutive_rejects", 0)


class TestAnalyzeMarkdown:
    def test_counts_prose_table_and_code_blocks(self) -> None:
        text = (
            "---\ntype: bot-conversation\nwhen: 2026-08-29\ntopic: [ccdb, Discord]\n---\n\n"
            "# thread\n\n"
            "some prose here\n\n"
            "| a | b |\n"
            "| - | - |\n\n"
            "```\ncode line\n```\n"
        )
        stats = analyze_markdown(text)
        assert stats.fm_keys == ("type", "when", "topic")
        assert stats.table_rows == 2
        assert stats.code_blocks == 1
        assert stats.fence_balanced is True
        assert stats.prose_chars > 0

    def test_fence_hides_markdown_structure_inside_it(self) -> None:
        """## and | inside a fenced block must not be counted as headings/tables."""
        text = "prose outside\n\n```\n## not a heading\n| not | a table |\n```\n"
        stats = analyze_markdown(text)
        assert stats.table_rows == 0
        assert stats.code_blocks == 1
        # Only "prose outside" (13 chars) counts — the fenced lines do not.
        assert stats.prose_chars == len("prose outside")

    def test_unbalanced_fence_detected(self) -> None:
        stats = analyze_markdown("prose\n\n```\nunclosed code\n")
        assert stats.fence_balanced is False
        assert stats.code_blocks == 0

    def test_no_frontmatter_gives_empty_fm_keys(self) -> None:
        stats = analyze_markdown("just some prose, no frontmatter block at all\n")
        assert stats.fm_keys == ()

    def test_empty_text_is_zero_stats_and_balanced(self) -> None:
        stats = analyze_markdown("")
        assert stats.prose_chars == 0
        assert stats.table_rows == 0
        assert stats.code_blocks == 0
        assert stats.fence_balanced is True
        assert stats.fm_keys == ()


class TestCheckMemoContract:
    def test_a_normal_entry_with_header_ok(self) -> None:
        header = build_file_header("ccdb", "雑談スレ", datetime(2026, 8, 29, 9, 0))
        verdict = check_memo_contract(header + _VALID_ENTRY)
        assert verdict.ok is True
        assert verdict.violations == ()

    def test_b_missing_frontmatter_flagged(self) -> None:
        verdict = check_memo_contract(_VALID_ENTRY)  # no header prepended
        assert verdict.ok is False
        assert "fm_missing" in verdict.violations

    def test_missing_one_required_key_flagged_individually(self) -> None:
        text = (
            "---\ntype: bot-conversation\nwhen: 2026-08-29\n---\n\nsome real prose content here\n"
        )
        verdict = check_memo_contract(text)
        assert "fm_key_missing:topic" in verdict.violations
        assert "fm_missing" not in verdict.violations
        assert "fm_key_missing:type" not in verdict.violations

    def test_required_fm_keys_empty_skips_frontmatter_checks(self) -> None:
        """Continuing-file entries (no header) pass required_fm_keys=() and are not
        penalized for lacking frontmatter of their own."""
        verdict = check_memo_contract(_VALID_ENTRY, required_fm_keys=())
        assert "fm_missing" not in verdict.violations
        assert verdict.ok is True

    def test_c_prose_zero_flags_prose_below_floor(self) -> None:
        header = build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0))
        entry = "```\ncode only, no prose\n```\n"
        verdict = check_memo_contract(header + entry)
        assert "prose_below_floor" in verdict.violations

    def test_d_unclosed_fence_flagged(self) -> None:
        header = build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0))
        entry = "some prose\n\n```\nunclosed\n"
        verdict = check_memo_contract(header + entry)
        assert "unbalanced_fence" in verdict.violations

    def test_table_rows_and_code_blocks_thresholds(self) -> None:
        header = build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0))
        entry = "prose only, no table, no code\n"
        verdict = check_memo_contract(header + entry, min_table_rows=1, min_code_blocks=1)
        assert "table_rows_below" in verdict.violations
        assert "code_blocks_below" in verdict.violations

    def test_stats_are_attached_to_verdict(self) -> None:
        verdict = check_memo_contract("prose\n")
        assert isinstance(verdict.stats, ContractStats)


class TestRepairEntry:
    def test_d_closes_unclosed_fence(self) -> None:
        entry = "some prose\n\n```\nunclosed code\n"
        verdict = ContractVerdict(
            ok=False,
            violations=("unbalanced_fence",),
            stats=ContractStats(5, 0, 0, False, ()),
        )
        repaired = repair_entry(entry, verdict)
        assert analyze_markdown(repaired).fence_balanced is True
        assert "unclosed code" in repaired  # original content preserved

    def test_prose_zero_appends_placeholder_naming_violations(self) -> None:
        entry = "```\ncode only\n```\n"
        verdict = check_memo_contract(entry, required_fm_keys=())
        assert "prose_below_floor" in verdict.violations
        repaired = repair_entry(entry, verdict)
        assert "本文なし" in repaired
        # After repair, re-checking must now find real prose.
        reverdict = check_memo_contract(repaired, required_fm_keys=())
        assert "prose_below_floor" not in reverdict.violations

    def test_fm_key_missing_is_not_repaired_in_place(self) -> None:
        """repair_entry only ever appends — it cannot invent frontmatter it
        doesn't own (that lives in the header, not the entry)."""
        entry = "some prose\n"
        verdict = ContractVerdict(
            ok=False,
            violations=("fm_key_missing:topic",),
            stats=ContractStats(11, 0, 0, True, ("type", "when")),
        )
        repaired = repair_entry(entry, verdict)
        assert repaired == entry

    def test_ok_verdict_leaves_entry_untouched(self) -> None:
        verdict = check_memo_contract(
            build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0)) + _VALID_ENTRY
        )
        assert repair_entry(_VALID_ENTRY, verdict) == _VALID_ENTRY


class TestLogContractEvent:
    def test_g_pass_is_recorded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path))
        verdict = check_memo_contract(
            build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0)) + _VALID_ENTRY
        )
        log_contract_event("ccdb", 42, 1, verdict, "pass", datetime(2026, 8, 29, 9, 0))

        log_path = tmp_path / "memo_contract.jsonl"
        assert log_path.exists()
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert record["outcome"] == "pass"
        assert record["bot"] == "ccdb"
        assert record["thread_id"] == 42
        assert record["violations"] == []
        assert record["stats"]["prose_chars"] > 0

    def test_reject_is_recorded_with_violations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path))
        verdict = check_memo_contract("prose only\n")  # missing frontmatter
        log_contract_event("ccdb", 1, 2, verdict, "reject", datetime(2026, 8, 29, 9, 0))

        record = json.loads(
            (tmp_path / "memo_contract.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["outcome"] == "reject"
        assert "fm_missing" in record["violations"]

    def test_check_error_records_null_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path))
        log_contract_event("ccdb", 1, 0, None, "check_error", datetime(2026, 8, 29, 9, 0))

        record = json.loads(
            (tmp_path / "memo_contract.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert record["outcome"] == "check_error"
        assert record["violations"] == []
        assert record["stats"] is None

    def test_never_raises_when_data_dir_unwritable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os
        import stat

        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("permission bits are not enforceable as root or on Windows")

        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(stat.S_IREAD | stat.S_IEXEC)
        monkeypatch.setenv("CCDB_DATA_DIR", str(readonly / "nested"))

        try:
            verdict = check_memo_contract("prose\n", required_fm_keys=())
            # Must not raise.
            log_contract_event("ccdb", 1, 1, verdict, "pass", datetime(2026, 8, 29, 9, 0))
        finally:
            readonly.chmod(stat.S_IRWXU)

    def test_consecutive_reject_streak_logs_error_at_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path))
        verdict = check_memo_contract("prose only\n")  # always fm_missing -> reject-shaped
        now = datetime(2026, 8, 29, 9, 0)

        with caplog.at_level("ERROR", logger="claude_discord.ext.memo_contract"):
            for _ in range(19):
                log_contract_event("ccdb", 1, 1, verdict, "reject", now)
            assert not any("consecutive rejects" in r.message for r in caplog.records)
            log_contract_event("ccdb", 1, 1, verdict, "reject", now)  # 20th
            assert any("consecutive rejects" in r.message for r in caplog.records)

    def test_pass_resets_the_streak(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path))
        verdict_bad = check_memo_contract("prose only\n")
        verdict_ok = check_memo_contract(
            build_file_header("ccdb", "t", datetime(2026, 8, 29, 9, 0)) + _VALID_ENTRY
        )
        now = datetime(2026, 8, 29, 9, 0)
        for _ in range(10):
            log_contract_event("ccdb", 1, 1, verdict_bad, "reject", now)
        log_contract_event("ccdb", 1, 1, verdict_ok, "pass", now)
        assert memo_contract._consecutive_rejects == 0


class TestContractEnabled:
    def test_default_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOT_MEMO_CONTRACT", raising=False)
        assert contract_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off"])
    def test_falsey_values_turn_it_off(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOT_MEMO_CONTRACT", value)
        assert contract_enabled() is False


class TestWiringIntoRunHelper:
    """End-to-end: run_claude_with_config() -> _write_bot_memo() -> gate."""

    @pytest.fixture
    def thread(self) -> MagicMock:
        t = MagicMock(spec=discord.Thread)
        t.id = 900001
        t.name = "契約検査スレ"
        t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        return t

    @pytest.fixture
    def runner(self) -> MagicMock:
        r = MagicMock()
        r.working_dir = None
        return r

    def _make_async_gen(self, events: list[StreamEvent]):
        async def gen(*args, **kwargs):
            for e in events:
                yield e

        return gen

    def _simple_events(self, text: str) -> list[StreamEvent]:
        return [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-c1"),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text=text,
                session_id="sess-c1",
                cost_usd=0.01,
                duration_ms=500,
            ),
        ]

    @pytest.mark.asyncio
    async def test_a_normal_turn_passes_and_is_written(
        self,
        thread: MagicMock,
        runner: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path / "memo"))
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path / "data"))
        runner.run = self._make_async_gen(
            self._simple_events("在庫は十分にあり、明日の出荷対応にも問題ありません")
        )

        config = RunConfig(thread=thread, runner=runner, prompt="在庫の状況を教えてください")
        result = await run_claude_with_config(config)

        assert result == "sess-c1"
        memo_files = list((tmp_path / "memo").iterdir())
        assert len(memo_files) == 1
        content = memo_files[0].read_text(encoding="utf-8")
        assert "type: bot-conversation" in content
        assert "在庫は十分にあり" in content

        log_path = tmp_path / "data" / "memo_contract.jsonl"
        assert log_path.exists()
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert record["outcome"] == "pass"

    @pytest.mark.asyncio
    async def test_f_inner_check_raises_but_write_still_happens(
        self,
        thread: MagicMock,
        runner: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path / "memo"))
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path / "data"))

        from claude_discord.ext import memo_contract as mc

        def _boom(*args, **kwargs):
            raise RuntimeError("contract checker exploded")

        monkeypatch.setattr(mc, "check_memo_contract", _boom)

        runner.run = self._make_async_gen(self._simple_events("チェックが壊れても記帳は残る"))
        config = RunConfig(thread=thread, runner=runner, prompt="質問2")
        result = await run_claude_with_config(config)

        assert result == "sess-c1"
        memo_files = list((tmp_path / "memo").iterdir())
        assert len(memo_files) == 1
        assert "チェックが壊れても記帳は残る" in memo_files[0].read_text(encoding="utf-8")

        log_path = tmp_path / "data" / "memo_contract.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert record["outcome"] == "check_error"

    @pytest.mark.asyncio
    async def test_confirmed_violation_blocks_the_write(
        self,
        thread: MagicMock,
        runner: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A violation that survives repair must skip the file write entirely."""
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path / "memo"))
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path / "data"))

        from claude_discord.ext import memo_contract as mc

        # Force min_table_rows-style unrepairable violation regardless of content.
        def _always_reject(*args, **kwargs):
            stats = ContractStats(0, 0, 0, True, ())
            return ContractVerdict(ok=False, violations=("table_rows_below",), stats=stats)

        monkeypatch.setattr(mc, "check_memo_contract", _always_reject)

        runner.run = self._make_async_gen(self._simple_events("これは書き込まれないはず"))
        config = RunConfig(thread=thread, runner=runner, prompt="質問3")
        result = await run_claude_with_config(config)

        assert result == "sess-c1"
        assert not (tmp_path / "memo").exists() or list((tmp_path / "memo").iterdir()) == []

        log_path = tmp_path / "data" / "memo_contract.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        assert record["outcome"] == "reject"

    @pytest.mark.asyncio
    async def test_h_contract_off_restores_v1_behaviour(
        self,
        thread: MagicMock,
        runner: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BOT_MEMO_CONTRACT=off: entry always written, no memo_contract.jsonl at all —
        even for content that would otherwise be rejected (empty prose)."""
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path / "memo"))
        monkeypatch.setenv("CCDB_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("BOT_MEMO_CONTRACT", "off")

        runner.run = self._make_async_gen(self._simple_events("```\nコードだけ、本文なし\n```"))
        config = RunConfig(thread=thread, runner=runner, prompt="質問4")
        result = await run_claude_with_config(config)

        assert result == "sess-c1"
        memo_files = list((tmp_path / "memo").iterdir())
        assert len(memo_files) == 1  # written despite what would be prose_below_floor
        assert not (tmp_path / "data" / "memo_contract.jsonl").exists()
