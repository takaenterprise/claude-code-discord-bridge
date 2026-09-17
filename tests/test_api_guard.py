"""Tests for ApiServer's local request guard (browser Origin / DNS-rebinding Host).

Regression tests for the 2026-09-17 security audit: with no api_secret the loopback
API accepted a cross-site ``text/plain`` POST (no CORS preflight) and requests with
a rebinding Host header, so a web page could spawn sessions, send DMs or delete
channels through the bot.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_discord.database.notification_repo import NotificationRepository
from claude_discord.ext.api_server import ApiServer, _hostname


@pytest.fixture
async def repo() -> NotificationRepository:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo = NotificationRepository(path)
    await repo.init_db()
    yield repo
    os.unlink(path)


@pytest.fixture
def bot() -> MagicMock:
    b = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    b.get_channel.return_value = channel
    return b


async def _client(api: ApiServer) -> TestClient:
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


@pytest.fixture
async def client(repo: NotificationRepository, bot: MagicMock) -> TestClient:
    c = await _client(ApiServer(repo=repo, bot=bot, default_channel_id=12345, port=0))
    yield c
    await c.close()


class TestHostname:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("127.0.0.1:8080", "127.0.0.1"),
            ("localhost", "localhost"),
            ("LOCALHOST:8080", "localhost"),
            ("[::1]:8080", "::1"),
            ("rebind.attacker.example:8080", "rebind.attacker.example"),
        ],
    )
    def test_hostname(self, header: str, expected: str) -> None:
        assert _hostname(header) == expected


class TestLocalRequestGuard:
    @pytest.mark.asyncio
    async def test_script_style_request_allowed(self, client: TestClient, bot: MagicMock) -> None:
        resp = await client.post("/api/notify", json={"message": "hello"})
        assert resp.status == 200
        bot.get_channel.return_value.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_cross_site_simple_post_rejected(
        self, client: TestClient, bot: MagicMock
    ) -> None:
        resp = await client.post(
            "/api/notify",
            data='{"message": "from a web page"}',
            headers={"Content-Type": "text/plain", "Origin": "https://attacker.example"},
        )
        assert resp.status == 403
        bot.get_channel.return_value.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_null_origin_rejected(self, client: TestClient) -> None:
        resp = await client.post("/api/notify", json={"message": "x"}, headers={"Origin": "null"})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rebinding_host_rejected(self, client: TestClient) -> None:
        resp = await client.get("/api/health", headers={"Host": "rebind.attacker.example:8080"})
        assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["localhost:8080", "[::1]:8080", "127.0.0.1"])
    async def test_loopback_host_names_allowed(self, client: TestClient, host: str) -> None:
        resp = await client.get("/api/health", headers={"Host": host})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_trusted_origin_allowed(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        api = ApiServer(
            repo=repo,
            bot=bot,
            default_channel_id=12345,
            port=0,
            trusted_origins=["http://127.0.0.1:3000"],
        )
        c = await _client(api)
        try:
            resp = await c.post(
                "/api/notify", json={"message": "x"}, headers={"Origin": "http://127.0.0.1:3000"}
            )
            assert resp.status == 200
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_non_loopback_bind_skips_host_check_but_not_origin(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        """Consumers that bind 0.0.0.0 rely on api_secret; the Origin check still applies."""
        api = ApiServer(repo=repo, bot=bot, default_channel_id=12345, host="0.0.0.0", port=0)
        c = await _client(api)
        try:
            ok = await c.get("/api/health", headers={"Host": "bot.internal:8080"})
            assert ok.status == 200
            ng = await c.post(
                "/api/notify", json={"message": "x"}, headers={"Origin": "https://evil.example"}
            )
            assert ng.status == 403
        finally:
            await c.close()

    @pytest.mark.asyncio
    async def test_guard_runs_before_auth(
        self, repo: NotificationRepository, bot: MagicMock
    ) -> None:
        api = ApiServer(repo=repo, bot=bot, default_channel_id=12345, port=0, api_secret="s")
        c = await _client(api)
        try:
            resp = await c.post(
                "/api/notify",
                json={"message": "x"},
                headers={"Authorization": "Bearer s", "Origin": "https://evil.example"},
            )
            assert resp.status == 403
            good = await c.post(
                "/api/notify", json={"message": "x"}, headers={"Authorization": "Bearer s"}
            )
            assert good.status == 200
        finally:
            await c.close()


class TestLoungeInviteSendsToken:
    def test_invite_curl_includes_optional_bearer_header(self) -> None:
        from claude_discord import lounge

        assert '${CCDB_API_SECRET:+-H "Authorization: Bearer $CCDB_API_SECRET"}' in (
            lounge._LOUNGE_INVITE
        )
