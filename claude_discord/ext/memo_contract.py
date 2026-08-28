"""記憶橋 v1 書込前契約検査 — TakaBrainへ書く前に「黙って消えた記帳」を止める.

他社実測事故（LLM書き直しで13,654字→1,933字・出典7件消失・実在しないリンク
生成）と同じ罠の射程が、当社の記憶橋にもある。本モジュールは「これから
書かれるmarkdownが frontmatter有り・散文が床以上・フェンス閉じ、という
最低限の形を満たしているか」を、実際の書込直前に検査する。

配線は ``claude_discord/cogs/_run_helper.py`` の ``_write_bot_memo`` が持つ
（このモジュール自身は ``append_memo`` を呼ばない・TakaBrainへは書かない）。
検査対象のmarkdownは、``bot_memo.memo_path`` / ``bot_memo.build_file_header``
が計算する「実際に書かれるファイルの中身」と同じ定義から組み立てる—品質の
定義（frontmatterの形）を複製しないため。

設計方針（記憶橋v1の "記帳失敗で本流を止めない" 流儀を検査層にも適用）:
    - 検査そのものが例外を投げても **fail-open**（書込を通し、
      ``outcome="check_error"`` を記録するだけ）。検査バグによる無言の記憶
      喪失は本末転倒 — 阻止するのは契約違反が確定した時だけ。
    - ``log_contract_event`` は never-raise。PASSも含め全件記録する
      （沈黙故障＝ログ自体が止まっている、を検知するため）。

Public API:
    analyze_markdown(text) -> ContractStats
    check_memo_contract(file_text, **thresholds) -> ContractVerdict
    repair_entry(entry, verdict) -> str
    log_contract_event(bot_name, thread_id, attempt, verdict, outcome, now) -> None
    contract_enabled() -> bool
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 散文の最低字数（frontmatter・表・フェンス内を除いた残り）。
DEFAULT_MIN_PROSE_CHARS = 20

# 既定で必須とするfrontmatterキー。定数はこのファイルだけに置く
# （bot_memo.build_file_header() が実際に書く3キーと一致させること）。
REQUIRED_FM_KEYS: tuple[str, ...] = ("type", "when", "topic")

# 直近何件の連続rejectでERRORログを出すか（沈黙故障＝検査が厳しすぎて
# 全滅している、の検知用）。
_CONSECUTIVE_REJECT_ALERT_EVERY = 20

_FENCE_LINE_RE = re.compile(r"^\s*```")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:")


@dataclass(frozen=True)
class ContractStats:
    """Structural counts extracted from one markdown text."""

    prose_chars: int
    table_rows: int
    code_blocks: int
    fence_balanced: bool
    fm_keys: tuple[str, ...]


@dataclass(frozen=True)
class ContractVerdict:
    """Result of checking one markdown text against the contract."""

    ok: bool
    violations: tuple[str, ...]
    stats: ContractStats


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a leading ``---``/``---`` frontmatter block off from the body.

    Returns ``(frontmatter_text, body_text)``. If the text does not start
    with a frontmatter marker, or the opening marker is never closed, no
    frontmatter block is recognized and the whole text is returned as body
    (``frontmatter_text`` is ``""``).
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return "", text


def _extract_fm_keys(fm_text: str) -> tuple[str, ...]:
    """Extract top-level ``key:`` names from a frontmatter block's text."""
    keys = []
    for line in fm_text.split("\n"):
        m = _FM_KEY_RE.match(line)
        if m:
            keys.append(m.group(1))
    return tuple(keys)


def analyze_markdown(text: str) -> ContractStats:
    """Extract structural stats from ``text`` (pure, never raises).

    The leading frontmatter block (if any) is split off and scanned only
    for key names. In the remaining body, lines are classified in fence
    order: a ``` fence toggles in/out of a code block; content inside a
    fence never counts as prose or a table row (so a fenced code sample's
    ``##`` headings or ``|`` pipes are never mistaken for real markdown
    structure); outside a fence, a line matching ``|...|`` counts as a
    table row, a blank line counts as nothing, and anything else counts
    toward prose_chars (its stripped length).
    """
    fm_text, body = _split_frontmatter(text)
    fm_keys = _extract_fm_keys(fm_text)

    in_fence = False
    fence_marker_count = 0
    code_blocks = 0
    table_rows = 0
    prose_chars = 0

    for line in body.split("\n"):
        if _FENCE_LINE_RE.match(line):
            fence_marker_count += 1
            if in_fence:
                code_blocks += 1
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if _TABLE_ROW_RE.match(line):
            table_rows += 1
            continue
        prose_chars += len(stripped)

    return ContractStats(
        prose_chars=prose_chars,
        table_rows=table_rows,
        code_blocks=code_blocks,
        fence_balanced=(fence_marker_count % 2 == 0),
        fm_keys=fm_keys,
    )


def check_memo_contract(
    file_text: str,
    *,
    min_prose_chars: int = DEFAULT_MIN_PROSE_CHARS,
    min_table_rows: int = 0,
    min_code_blocks: int = 0,
    required_fm_keys: tuple[str, ...] = REQUIRED_FM_KEYS,
) -> ContractVerdict:
    """Check ``file_text`` against the memo contract (pure, never raises).

    ``required_fm_keys`` gates the frontmatter checks entirely — pass ``()``
    when checking a fragment that is not expected to carry frontmatter
    itself (e.g. one turn's entry being appended to an already-created
    file). When non-empty and no frontmatter block is present at all, the
    single ``fm_missing`` violation is reported (not one per required key);
    when a frontmatter block is present but missing specific keys, one
    ``fm_key_missing:<key>`` violation is reported per missing key.
    """
    stats = analyze_markdown(file_text)
    violations: list[str] = []

    if required_fm_keys:
        if not stats.fm_keys:
            violations.append("fm_missing")
        else:
            for key in required_fm_keys:
                if key not in stats.fm_keys:
                    violations.append(f"fm_key_missing:{key}")

    if stats.prose_chars < min_prose_chars:
        violations.append("prose_below_floor")
    if not stats.fence_balanced:
        violations.append("unbalanced_fence")
    if stats.table_rows < min_table_rows:
        violations.append("table_rows_below")
    if stats.code_blocks < min_code_blocks:
        violations.append("code_blocks_below")

    return ContractVerdict(ok=not violations, violations=tuple(violations), stats=stats)


def repair_entry(entry: str, verdict: ContractVerdict) -> str:
    """Deterministically repair the violations found in ``verdict``.

    This is the actual substance of v1's "1回再生成" — the memo builder
    (``build_memo_entry``) is a non-LLM pure function, so a true LLM
    re-generation is structurally impossible here; what can be done
    deterministically is: close an unclosed fence, and — if there is no
    prose at all — append a visible placeholder note naming what was wrong,
    so a human skimming the file later sees "this entry was rejected/empty"
    rather than a silent gap. A later LLM-summarization stage can replace
    just this function without touching the check/log contract around it.

    Violations this cannot repair (missing frontmatter keys, too few table
    rows/code blocks) are left as-is — repair_entry only ever appends text,
    never removes or reinterprets what the caller already wrote.
    """
    repaired = entry
    if "unbalanced_fence" in verdict.violations:
        repaired = repaired.rstrip("\n") + "\n```\n"
    if "prose_below_floor" in verdict.violations:
        summary = ", ".join(verdict.violations)
        repaired = repaired.rstrip("\n") + f"\n(本文なし: {summary})\n"
    return repaired


# Process-wide streak of consecutive "reject" outcomes, used only to decide
# when to fire the "検査が厳しすぎて全滅している" ERROR alert below. Reset
# on any pass/pass_after_repair. Deliberately not per-bot/per-thread — the
# alert is about the check itself misbehaving, not one thread's content.
_consecutive_rejects = 0


def log_contract_event(
    bot_name: str,
    thread_id: int,
    attempt: int,
    verdict: ContractVerdict | None,
    outcome: str,
    now: datetime,
) -> None:
    """Append one JSONL record of a contract check outcome.

    outcome one of: pass | reject | pass_after_repair | skip | check_error.
    ``verdict`` may be ``None`` for ``check_error`` (the check itself raised
    before producing one) — recorded with null violations/stats.

    Writes to ``<CCDB_DATA_DIR or 'data'>/memo_contract.jsonl``, one line
    per event, PASS included (so a total outage of this log itself is
    visible as silence rather than as an all-green void). Never raises —
    any failure here (unwritable data dir, etc.) is logged at WARNING and
    swallowed, matching the rest of the bot-memo bridge's "記帳失敗で本流を
    止めない" contract.
    """
    global _consecutive_rejects
    try:
        if outcome == "reject":
            _consecutive_rejects += 1
        elif outcome in ("pass", "pass_after_repair"):
            _consecutive_rejects = 0

        record = {
            "ts": now.isoformat(),
            "bot": bot_name,
            "thread_id": thread_id,
            "attempt": attempt,
            "outcome": outcome,
            "violations": list(verdict.violations) if verdict is not None else [],
            "stats": asdict(verdict.stats) if verdict is not None else None,
        }
        data_dir = Path(os.getenv("CCDB_DATA_DIR") or "data")
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "memo_contract.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        if (
            outcome == "reject"
            and _consecutive_rejects > 0
            and _consecutive_rejects % _CONSECUTIVE_REJECT_ALERT_EVERY == 0
        ):
            logger.error(
                "Bot-memo contract: %d consecutive rejects — the contract may be too "
                "strict and is silently discarding memos (thread %s, bot %s)",
                _consecutive_rejects,
                thread_id,
                bot_name,
            )
    except Exception:
        logger.warning(
            "Failed to log memo-contract event for thread %s — skipping",
            thread_id,
            exc_info=True,
        )


def contract_enabled() -> bool:
    """Whether the write-before-write contract-check gate (書込前契約検査) is active.

    Default ON. A deployment can opt OUT by setting ``BOT_MEMO_CONTRACT`` to
    a falsey value (``false`` / ``0`` / ``no`` / ``off``) to fall back to
    exact v1 behaviour (entries always written unchecked, no
    memo_contract.jsonl) — same gate style as ``LOUNGE_ENABLED`` etc. in
    ``_run_helper.py``.
    """
    return os.getenv("BOT_MEMO_CONTRACT", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
