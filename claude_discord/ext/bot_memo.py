"""記憶橋 v1 — bot会話をTakaBrainへ自動記帳する.

botとの会話が終わるたびに、その要点（誰と・何を話し・何が返ったか）を
Obsidian vault（TakaBrain）へmarkdownで自動記帳する。VMのbotで外出先に話した
内容が、Syncthing同期経由でローカルのAI秘書からも思い出せるようになる。

既定は完全OFF。呼び出し側（``_run_helper.py``）が ``BOT_MEMO_DIR`` 環境変数の
有無で分岐する — このモジュール自身はフラグを見ない、純粋なビルダー＋I/O層。

設計方針（erabe_mask_ledgerやnotify系と同型の「記帳失敗で本流を止めない」流儀）:
    - ``build_memo_entry`` は純関数。ファイルI/Oも例外送出もしない。
    - ``append_memo`` は失敗しても例外を上げず ``None`` を返し、WARNログを出す。
      呼び出し元（Discordへの返信フロー）を止めないことを最優先する。

Public API:
    build_memo_entry(...) -> str       # 1ターン分のmarkdown断片を組み立てる
    append_memo(...) -> Path | None    # ファイルへ追記する（失敗時はNone）
    memo_path(...) -> Path             # 書込先パスの計算（純関数）
    build_file_header(...) -> str      # frontmatter+見出しの組立（純関数）

``memo_path`` と ``build_file_header`` は append_memo() 内部の計算を抽出した
ものであり、``memo_contract`` 検査層（_run_helper.py の書込前ゲート）が
「これから書かれるファイルの中身」を実際の書込と同じ定義で組み立てて検査
できるようにするために公開している。品質の定義（frontmatterの形・パスの
命名規則）をここ1箇所だけに保ち、検査側での複製ドリフトを防ぐ。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 切り詰め上限（仕様: prompt先頭300字・result先頭1500字）。
_PROMPT_MAX_CHARS = 300
_RESULT_MAX_CHARS = 1500

# ファイル名・見出しに使うthread_nameの安全化での最大長。
_THREAD_NAME_MAX_CHARS = 50

# bot名をファイル名へ埋め込む際の最大長（過度に長いBOT_NAME環境変数対策）。
_BOT_NAME_MAX_CHARS = 40


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending a visible truncation marker.

    The marker makes it obvious to a human reading the memo later that the
    text was cut, rather than silently ending mid-sentence.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…（{max_chars}字超のため省略）"


# Lines in user/model text that could pass for memo structure: headings (``#``),
# the record separator / frontmatter fence (``---``) and other thematic breaks or
# setext underlines, and the ``**Q:**`` / ``**A:**`` field labels. Leading
# indentation is kept so the match covers what Markdown itself would treat as
# structure.
_FORGEABLE_LINE_RE = re.compile(
    r"^([ \t]*)(\*\*[QA]:\*\*|#|-{3,}|\*{3,}|_{3,}|={3,})",
    re.MULTILINE,
)


def _neutralize_structure(text: str) -> str:
    """Backslash-escape line-leading memo structure inside *text*.

    Without this, a prompt or reply containing a line such as
    ``### 12:01 — bot1 ...`` followed by ``**Q:**`` / ``**A:**`` / ``---``
    would read as a separate, forged turn in the memo (security audit run-3, 2026-09-18). A leading
    backslash is a standard Markdown escape: rendered text still reads the
    same, but no line of the snippet starts with ``#`` / ``---`` / ``**Q:**``.
    """

    def _escape(m: re.Match[str]) -> str:
        indent, token = m.group(1), m.group(2)
        if token.startswith("**"):
            return indent + "\\*\\*" + token[2:]
        return indent + "\\" + token

    # A bare CR is a Markdown line ending too — normalise before matching.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _FORGEABLE_LINE_RE.sub(_escape, text)


def _sanitize_for_fs(name: str, max_chars: int) -> str:
    """Make a string safe to use in a filename or markdown heading.

    Strips path separators and newlines (which would otherwise break the
    filename or corrupt the markdown structure), collapses whitespace, and
    truncates to max_chars. Falls back to a stable placeholder for empty
    input so callers always get a non-empty, filesystem-safe string.
    """
    cleaned = name.replace("/", "_").replace("\\", "_")
    cleaned = re.sub(r"[\r\n]+", " ", cleaned).strip()
    return cleaned[:max_chars] if cleaned else "thread"


def build_memo_entry(
    bot_name: str,
    thread_id: int,
    thread_name: str,
    prompt: str,
    result_text: str,
    session_id: str | None,
    now: datetime,
    discord_user_id: str | None = None,
) -> str:
    """Build a markdown fragment for one conversation turn.

    Pure function — no file I/O, never raises for well-formed string inputs.
    Returns a self-contained block (heading + Q/A + separator) ready to be
    appended to a memo file via append_memo(). `prompt` is truncated to 300
    chars and `result_text` to 1500 chars, each with a visible "truncated"
    marker when cut (see _truncate()).

    The heading records the speaker (``discord_user_id``; ``(unknown)`` when the
    turn has no Discord author, e.g. a scheduled or restart-resume run), and
    line-leading ``#`` / ``---`` / ``**Q:**`` / ``**A:**`` in the prompt and
    result are escaped so they cannot fake another turn (see
    _neutralize_structure()).

    `thread_name` is accepted for interface symmetry with append_memo() but
    is not otherwise used here — the per-turn heading identifies the turn by
    time/bot/thread id/session, while the thread name itself is written once
    as the file-level heading by append_memo() on first creation.
    """
    del thread_name  # unused here — see docstring
    time_label = now.strftime("%H:%M")
    prompt_snippet = _neutralize_structure(_truncate(prompt.strip(), _PROMPT_MAX_CHARS))
    result_snippet = _neutralize_structure(_truncate(result_text.strip(), _RESULT_MAX_CHARS))
    session_label = session_id or "(none)"
    user_label = re.sub(r"\s+", "", discord_user_id or "") or "(unknown)"

    lines = [
        f"### {time_label} — {bot_name} "
        f"(thread {thread_id}, session {session_label}, user {user_label})",
        "",
        f"**Q:** {prompt_snippet}",
        "",
        f"**A:** {result_snippet}",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def memo_path(memo_dir: str, bot_name: str, thread_id: int, now: datetime) -> Path:
    """Compute the target memo file path for this bot/thread/day.

    Pure — no file I/O, never raises for well-formed inputs. Naming scheme:
    ``<memo_dir>/YYYY-MM-DD_<bot_name>_<thread_idの下6桁>.md`` (one file per
    thread per day).

    Extracted from append_memo() so callers that need to know *before*
    writing whether a given call would create a new file (e.g. the
    memo-contract check gate, which decides whether frontmatter is required
    in what it validates) can compute the same path without re-deriving the
    naming rule.
    """
    date_label = now.strftime("%Y-%m-%d")
    safe_bot = _sanitize_for_fs(bot_name, _BOT_NAME_MAX_CHARS)
    thread_suffix = str(thread_id)[-6:]
    filename = f"{date_label}_{safe_bot}_{thread_suffix}.md"
    return Path(memo_dir) / filename


def build_file_header(bot_name: str, thread_name: str, now: datetime) -> str:
    """Build the frontmatter + heading block written once, at file creation.

    Pure — no file I/O, never raises for well-formed inputs. Obsidian
    frontmatter (``type: bot-conversation`` / ``when: YYYY-MM-DD`` /
    ``topic: [<bot_name>, Discord]``) plus a heading with the thread name.

    Extracted from append_memo() so the memo-contract check gate can
    validate the exact header text that would be written for a new file,
    rather than maintaining a second definition of "what a valid header
    looks like" that could drift out of sync with this one.
    """
    date_label = now.strftime("%Y-%m-%d")
    safe_bot = _sanitize_for_fs(bot_name, _BOT_NAME_MAX_CHARS)
    safe_thread_name = _sanitize_for_fs(thread_name, _THREAD_NAME_MAX_CHARS)
    return (
        "---\n"
        "type: bot-conversation\n"
        f"when: {date_label}\n"
        f"topic: [{safe_bot}, Discord]\n"
        "---\n\n"
        f"# {safe_thread_name}\n\n"
    )


def append_memo(
    memo_dir: str,
    bot_name: str,
    thread_id: int,
    thread_name: str,
    entry: str,
    now: datetime,
) -> Path | None:
    """Append one turn's memo entry to the day's file for this thread.

    File path and first-write header text come from memo_path() /
    build_file_header() (see those for the naming scheme and header
    contents).

    Never raises — any failure (missing/unwritable dir, permission error,
    etc.) is logged at WARNING and None is returned, so a memo failure never
    interrupts the bot's normal reply flow (same "記帳失敗で本流を止めない"
    contract as erabe_mask_ledger / the notify modules).
    """
    try:
        target_path = memo_path(memo_dir, bot_name, thread_id, now)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        is_new_file = not target_path.exists()
        with target_path.open("a", encoding="utf-8") as f:
            if is_new_file:
                f.write(build_file_header(bot_name, thread_name, now))
            f.write(entry)

        return target_path
    except Exception:
        logger.warning(
            "Failed to append bot memo for thread %s — skipping", thread_id, exc_info=True
        )
        return None
