"""cost_approval Cog のガードテスト。

★原価承認は金銭に直結する（承認＝SS-01商品マスタの原価が変わる）。
このテストは「押せてはいけない人・場所で押せないこと」を固定するためにある。
設定漏れ（環境変数が空）で"素通り"することが最悪の失敗なので、fail-closedを重点的に見る。
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from claude_discord.cogs import cost_approval as m

OWNER = 1308238611732500552
CHANNEL = 1400000000000000001


class _User:
    def __init__(self, uid):
        self.id = uid


class _Interaction:
    def __init__(self, uid, channel_id):
        self.user = _User(uid)
        self.channel_id = channel_id


def _env(owner=str(OWNER), channel=str(CHANNEL)):
    e = {}
    if owner is not None:
        e["DISCORD_OWNER_ID"] = owner
    if channel is not None:
        e["GENKA_APPROVE_CHANNEL_ID"] = channel
    return mock.patch.dict("os.environ", e, clear=True)


class TestCheckActor:
    def test_owner_in_right_channel_passes(self):
        with _env():
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is None

    def test_other_user_rejected(self):
        with _env():
            assert m.check_actor(_Interaction(999, CHANNEL)) is not None

    def test_wrong_channel_rejected(self):
        """社長本人でも、他人が見られる場所では承認させない。"""
        with _env():
            assert m.check_actor(_Interaction(OWNER, 42)) is not None

    def test_owner_id_unset_rejects_everyone(self):
        """★設定漏れは fail-closed。空を「誰でもOK」に倒すと全員が承認できてしまう。"""
        with _env(owner=None):
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is not None

    def test_owner_id_empty_string_rejects(self):
        with _env(owner="   "):
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is not None

    def test_owner_id_non_numeric_rejects(self):
        """貼り間違い（プレースホルダ等）で誰でも通る事故を防ぐ。"""
        with _env(owner="【ここにIDを貼る】"):
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is not None

    def test_channel_unset_rejects(self):
        with _env(channel=None):
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is not None

    def test_channel_non_numeric_rejects(self):
        with _env(channel="general"):
            assert m.check_actor(_Interaction(OWNER, CHANNEL)) is not None


class TestOwnerIdParsing:
    def test_owner_id_returns_int(self):
        with _env():
            assert m.owner_id() == OWNER

    def test_owner_id_none_when_unset(self):
        with _env(owner=None):
            assert m.owner_id() is None

    def test_channel_id_none_when_unset(self):
        with _env(channel=None):
            assert m.approve_channel_id() is None


class TestRunApply:
    """スクリプトの出力の読み方。.envシャドーイング警告が前置されても壊れないこと。"""

    async def _run(self, stdout: bytes, stderr: bytes = b"", rc: int = 0):
        proc = mock.AsyncMock()
        proc.communicate.return_value = (stdout, stderr)
        with mock.patch("asyncio.create_subprocess_exec", return_value=proc):
            return await m.run_apply("list")

    @pytest.mark.asyncio
    async def test_reads_json_on_last_line(self):
        res = await self._run(b'{"ok": true, "count": 0, "items": []}\n')
        assert res["ok"] is True

    @pytest.mark.asyncio
    async def test_ignores_warning_lines_before_json(self):
        out = ("⚠️ .envシャドーイング: キー X が異なる値\n"
               '{"ok": true, "count": 1, "items": []}\n').encode()
        res = await self._run(out)
        assert res["ok"] is True
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_non_json_output_is_not_treated_as_success(self):
        """読めなかった時に ok=True を返すと、承認できていないのに成功表示になる。"""
        res = await self._run(b"Traceback (most recent call last):\n  boom\n")
        assert res["ok"] is False
        assert res["message"]


class TestItemLine:
    def test_unregistered_old_cost_is_labeled(self):
        s = m.item_line({"name": "テスト", "jan": "49", "costco_id": "", "old_cost": "",
                         "new_cost": 100})
        assert "未登録" in s
        assert "¥100" in s

    def test_missing_keys_render_dash(self):
        s = m.item_line({"name": "テスト", "jan": "", "costco_id": "", "old_cost": 1,
                         "new_cost": 2})
        assert "—" in s

    def test_serializable_item_from_script_renders(self):
        """approval_button_apply.py list が返す形をそのまま食えること。"""
        item = json.loads('{"row":2,"approval_id":"1","jan":"4901234567890",'
                          '"costco_id":"45791","name":"商品","old_cost":100,"new_cost":200}')
        assert "4901234567890" in m.item_line(item)
