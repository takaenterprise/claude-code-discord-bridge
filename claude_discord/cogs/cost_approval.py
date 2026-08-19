"""原価承認Cog — /genka-approve でSS-01「商品マスタ修正承認待ち」をボタン承認する.

===============================================================================
背景（社長裁定 2026-08-19）:
  原価修正の承認は以前「LINEで『承認』と返信 → n8n A1 が反映」だった。A1は2026-07-18〜19に
  deactivateされ a1_register cron へ移管された際、**LINEの受信口だけが作られず**承認操作が
  宙に浮いていた（以後はスプレッドシートを人手で書き換える運用）。復活先はDiscordのボタン。

役割分担（意図的にここで切っている）:
  本Cog … ボタンの描画・押下受信・**本人確認**
  ec-automation-system/scripts/approval_button_apply.py … シートの読み書き

  ★Botプロセスにシートの鍵（サービスアカウント）を持たせないための分離。
   本Cogはスクリプトをsubprocessで叩き、返ってくる1行JSONを読むだけ。
  ★押下は「シートのステータスを承認済にする」までで、商品マスタへの実反映は既存の
   approval_worker（cron */15）が行う。最大15分の遅れがある代わりに、実績のある反映処理と
   その2段CASをそのまま使える（社長裁定の(a)案）。

安全（原価承認は金銭に直結する）:
  - **DISCORD_OWNER_ID と一致するユーザーしか押せない**（/exec と同じ判定）。
    未設定なら全員拒否＝誰でも押せる状態を作らない（fail-closed）
  - **許可チャンネル以外では反応しない**（GENKA_APPROVE_CHANNEL_ID）。未設定なら全拒否
  - ボタンは**1件に1つ**。承認IDは1バッチ複数行にまたがるため、まとめ承認は別商品を
    巻き込む事故になる
  - 押下後はボタンを無効化。押した本人には5分間だけ「取り消し」ボタンを出す
    （既存の反映は15分毎なので間に合う）
  - 行番号だけでなくJANも渡して照合させる（並べ替え・行挿入での取り違え防止）
===============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

SCRIPT = "/home/ubuntu/ec-automation-system/scripts/approval_button_apply.py"
SCRIPT_CWD = "/home/ubuntu/ec-automation-system"
ENV_FILE = "/home/ubuntu/ec-automation-system/scripts/.env"
TIMEOUT = 60
UNDO_WINDOW = 300  # 誤タップ取り消しを受け付ける秒数（既存の反映cronは15分毎）

COLOR_PENDING = 0xF39C12
COLOR_OK = 0x2ECC71
COLOR_NG = 0xE74C3C


def _build_subprocess_env() -> dict[str, str]:
    """サブプロセス用の環境変数（haiban_command._build_subprocess_env と同型）."""
    env = {**os.environ}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        logger.warning("env file not found: %s", ENV_FILE)
    return env


async def run_apply(*args: str) -> dict:
    """approval_button_apply.py を叩いて1行JSONを返す。失敗も必ずdictで返す."""
    proc = await asyncio.create_subprocess_exec(
        "python3", SCRIPT, *args,
        cwd=SCRIPT_CWD, env=_build_subprocess_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "message": f"{TIMEOUT}秒以内に応答がありませんでした"}
    text = (out or b"").decode("utf-8", "replace").strip()
    # スクリプトは最後の1行にJSONを出す（.envシャドーイング警告などが前に付くことがある）
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    tail = (err or b"").decode("utf-8", "replace").strip()[-300:]
    return {"ok": False, "message": f"結果を読み取れませんでした: {tail or text[-300:]}"}


def owner_id() -> int | None:
    """承認を押してよい唯一のユーザー。未設定なら None＝全員拒否（fail-closed）."""
    raw = os.getenv("DISCORD_OWNER_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def approve_channel_id() -> int | None:
    """承認ボタンを受け付けるチャンネル。未設定なら None＝全拒否（fail-closed）."""
    raw = os.getenv("GENKA_APPROVE_CHANNEL_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def check_actor(interaction: discord.Interaction) -> str | None:
    """押した人・場所を検査する純関数寄りのガード。問題なければNone、あれば理由文字列.

    ★ここが金銭に直結する唯一の関門。設定漏れは「通す」ではなく「止める」に倒す。
    """
    oid = owner_id()
    if oid is None:
        return "DISCORD_OWNER_ID が未設定のため、安全のため誰も承認できません"
    if interaction.user.id != oid:
        return "この承認ボタンは社長のみが押せます"
    cid = approve_channel_id()
    if cid is None:
        return "GENKA_APPROVE_CHANNEL_ID が未設定のため、安全のため承認できません"
    if interaction.channel_id != cid:
        return "このチャンネルでは承認できません（承認専用チャンネルでお願いします）"
    return None


def item_line(item: dict) -> str:
    old = item.get("old_cost")
    old_s = "未登録" if old in ("", None) else f"¥{old}"
    return (f"**{item.get('name', '')[:60]}**\n"
            f"JAN {item.get('jan') or '—'} ／ 品番 {item.get('costco_id') or '—'}\n"
            f"{old_s} → **¥{item.get('new_cost')}**（税抜）")


class UndoView(discord.ui.View):
    """承認直後だけ出る取り消しボタン（誤タップ対策）."""

    def __init__(self, row: int, jan: str) -> None:
        super().__init__(timeout=UNDO_WINDOW)
        self._row, self._jan = row, jan

    @discord.ui.button(label="↩ 取り消す", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        reason = check_actor(interaction)
        if reason:
            await interaction.response.send_message(f"⛔ {reason}", ephemeral=True)
            return
        await interaction.response.defer()
        res = await run_apply("undo", "--row", str(self._row), "--jan", self._jan,
                              "--by", str(interaction.user.id), "--execute")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        emb = discord.Embed(
            description=("↩ " if res.get("ok") else "⚠ ") + str(res.get("message", "")),
            color=COLOR_OK if res.get("ok") else COLOR_NG)
        if interaction.message:
            await interaction.message.edit(view=self)
        await interaction.followup.send(embed=emb)
        logger.info("cost approval undo row=%s ok=%s by=%s",
                    self._row, res.get("ok"), interaction.user.id)


class ApproveView(discord.ui.View):
    """1件分の承認ボタン。★1行に1つ（承認IDまとめ承認は別商品を巻き込むため作らない）."""

    def __init__(self, row: int, jan: str) -> None:
        super().__init__(timeout=None)
        self._row, self._jan = row, jan

    @discord.ui.button(label="✅ 承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        reason = check_actor(interaction)
        if reason:
            await interaction.response.send_message(f"⛔ {reason}", ephemeral=True)
            logger.warning("cost approval rejected: %s (user=%s row=%s)",
                           reason, interaction.user.id, self._row)
            return
        await interaction.response.defer()
        # ★--execute は hooks/guardrail.sh L6-b7 のゲート対象。シェル直叩き（AI含む）は
        # permitが無いと止まるが、Botはフックを通らないのでここで明示的に渡す
        res = await run_apply("approve", "--row", str(self._row), "--jan", self._jan,
                              "--by", str(interaction.user.id), "--execute")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()
        if interaction.message:
            await interaction.message.edit(view=self)
        ok = bool(res.get("ok"))
        emb = discord.Embed(
            description=("✅ " if ok else "⚠ ") + str(res.get("message", "")),
            color=COLOR_OK if ok else COLOR_NG)
        await interaction.followup.send(embed=emb,
                                        view=UndoView(self._row, self._jan) if ok else None)
        logger.info("cost approval row=%s jan=%s ok=%s by=%s",
                    self._row, self._jan, ok, interaction.user.id)


class CostApprovalCog(commands.Cog):
    """/genka-approve — 承認待ちの原価修正を1件ずつボタンで承認する."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="genka-approve",
        description="承認待ちの原価修正をボタンで承認する（社長のみ・承認専用チャンネル限定）",
    )
    @app_commands.describe(limit="一度に出す件数（既定10・多いとチャンネルが流れます）")
    async def genka_approve(self, interaction: discord.Interaction, limit: int = 10) -> None:
        reason = check_actor(interaction)
        if reason:
            await interaction.response.send_message(f"⛔ {reason}", ephemeral=True)
            return
        await interaction.response.defer()
        res = await run_apply("list")
        if not res.get("ok"):
            await interaction.followup.send(
                embed=discord.Embed(description=f"⚠ {res.get('message', '')}", color=COLOR_NG))
            return
        items = res.get("items", [])
        if not items:
            await interaction.followup.send(
                embed=discord.Embed(description="承認待ちはありません", color=COLOR_OK))
            return
        shown = items[: max(1, limit)]
        await interaction.followup.send(embed=discord.Embed(
            title=f"💴 原価修正の承認待ち {len(items)}件（うち{len(shown)}件を表示）",
            description="1件ずつ押してください。押した後5分間は取り消せます。",
            color=COLOR_PENDING))
        for it in shown:
            await interaction.followup.send(
                embed=discord.Embed(description=item_line(it), color=COLOR_PENDING),
                view=ApproveView(it["row"], it["jan"]))
        if len(items) > len(shown):
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"残り{len(items) - len(shown)}件は `/genka-approve limit:{len(items)}` で出せます",
                    color=COLOR_PENDING))
