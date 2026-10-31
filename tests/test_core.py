import asyncio
import base64
import hashlib
import inspect
import json
import os
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from xconn_core._client.async_ import _setup, register_async
from xconn_core.exception import ApplicationError
from xconn_core.quic import AsyncQUICConnection
from xconn_core.requests import PublishRequest, RegisterRequest
from xconn_core.types import Invocation

from main import app
from deskconn import helpers, uris
from deskconn.api import auth


def test_password_hash_preserves_existing_credentials():
    password = "test-password"
    salt = "test-salt"
    expected = base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 1000, 32)).decode()
    assert helpers.hash_password(password, salt) == expected
    assert helpers.verify_password(password, salt, expected)
    assert not helpers.verify_password("incorrect", salt, expected)


class RegistrationSession:
    def __init__(self):
        self.handlers = {}

    def register(self, uri, handler):
        return RegisterRequest(uri, handler, session=self)

    async def _execute_register(self, request):
        self.handlers[request.procedure] = request.handler


@pytest.mark.asyncio
async def test_all_account_procedures_register_with_core():
    session = RegistrationSession()
    for uri, handler in app.procedures.items():
        await register_async(session, uri, handler)

    assert session.handlers.keys() == app.procedures.keys()
    assert "io.xconn.deskconn.account.login.verify" in session.handlers
    assert "io.xconn.deskconn.desktop.attach" in session.handlers


@pytest.mark.asyncio
async def test_login_invocation_resolves_dependencies_and_preserves_errors(monkeypatch):
    database = object()

    @asynccontextmanager
    async def dependency():
        yield database

    uri = "io.xconn.deskconn.account.login"
    handler = auth.component.procedures[uri]
    default = inspect.signature(handler).parameters["db"].default
    monkeypatch.setattr(default, "dependency", dependency)
    user = SimpleNamespace(is_verified=True)
    lookup = AsyncMock(return_value=user)
    otp = AsyncMock()
    monkeypatch.setattr(auth.user_backend, "get_user_by_email", lookup)
    monkeypatch.setattr(auth.user_backend, "generate_and_save_otp", otp)
    session = RegistrationSession()
    await register_async(session, uri, handler)

    await session.handlers[uri](Invocation(["test@example.com"], {}, {}))
    lookup.assert_awaited_once_with(database, "test@example.com")
    otp.assert_awaited_once_with(database, user, helpers.OTP_PURPOSE_LOGIN)

    lookup.return_value = None
    with pytest.raises(ApplicationError) as error:
        await session.handlers[uri](Invocation(["missing@example.com"], {}, {}))
    assert error.value.uri == uris.ERROR_USER_NOT_FOUND


@pytest.mark.asyncio
async def test_core_router_registration_rpc_and_publication():
    binary = os.environ.get("XCONN_TRANSPORT_PEER")
    if not binary:
        pytest.skip("Set XCONN_TRANSPORT_PEER to the Core transport_peer binary")

    process = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        peer = json.loads(process.stdout.readline())
        async with await AsyncQUICConnection.connect(
            peer["quic"], server_name="localhost", certificate=bytes.fromhex(peer["quic_certificate"])
        ) as connection:
            session = await connection.open_session("realm")
            try:
                await _setup(app, session)
                await helpers.call_cloud_router_rpc(session, "echo", ["value", 42], "RPC failed")
                received = asyncio.Queue()
                subscription = await session.subscribe("account.test", received.put).execute()
                await (
                    session.publish("account.test")
                    .args("key", "role")
                    .options(acknowledge=True, exclude_me=False)
                    .execute()
                )
                event = await asyncio.wait_for(received.get(), 5)
                assert event.args == ["key", "role"]
                await subscription.unsubscribe()
            finally:
                await session.close()
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.mark.parametrize("router_url", ["ws://127.0.0.1:1/ws", "rs://127.0.0.1:1/rs"])
def test_service_launcher_uses_cryptosign(monkeypatch, router_url):
    import xconn_core
    from main import run

    monkeypatch.setenv("ROUTER_URL", router_url)
    monkeypatch.setenv("DESKCONN_ACCOUNT_AUTHID", "test-account-service")
    monkeypatch.setenv("DESKCONN_ACCOUNT_PRIVATE_KEY", "01" * 32)
    captured = []
    monkeypatch.setattr(xconn_core, "connect", lambda app, config: captured.append((app, config)))
    run()

    target, config = captured[0]
    assert target == "main:app"
    assert config.url == router_url
    assert config.realm == "io.xconn.deskconn"
    assert config.authmethod == "cryptosign"
    assert config.authid == "test-account-service"
    assert config.private_key == "01" * 32


@pytest.mark.asyncio
async def test_account_get_preserves_caller_identity_and_response(monkeypatch):
    from uuid import UUID
    from deskconn.api import user

    uri = "io.xconn.deskconn.account.get"
    handler = user.component.procedures[uri]
    database = object()

    @asynccontextmanager
    async def dependency():
        yield database

    default = inspect.signature(handler).parameters["db"].default
    monkeypatch.setattr(default, "dependency", dependency)
    user_id = "b7f1a8b5-59d2-42a2-b55b-77a26505ad13"
    record = SimpleNamespace(id=UUID(user_id), email="test@example.com", name="Test User")
    lookup = AsyncMock(return_value=record)
    monkeypatch.setattr(user.user_backend, "get_user_by_email", lookup)
    session = RegistrationSession()
    await register_async(session, uri, handler)

    result = await session.handlers[uri](Invocation([], {}, {"caller_authid": "test@example.com"}))
    lookup.assert_awaited_once_with(database, "test@example.com")
    assert result.args == [{"email": "test@example.com", "name": "Test User", "id": user_id}]


@pytest.mark.asyncio
async def test_new_principal_publishes_acknowledged_key_notification(monkeypatch):
    from deskconn.api import principal

    database = object()
    user = SimpleNamespace(id="user-id", email="test@example.com")
    record = SimpleNamespace(public_key="test-public-key")
    monkeypatch.setattr(principal.principal_backend, "principal_exists_by_public_key", AsyncMock(return_value=False))
    monkeypatch.setattr(principal.principal_backend, "create_principal", AsyncMock(return_value=record))
    monkeypatch.setattr(
        principal.desktop_backend,
        "get_user_desktops_authid_with_authrole",
        AsyncMock(return_value=[("machine-id", "user:owner")]),
    )

    class NotificationSession:
        def publish(self, topic):
            return PublishRequest(topic, session=self)

        async def _execute_publish(self, request):
            self.request = request

    session = NotificationSession()
    monkeypatch.setattr(principal.component, "_session", session)
    result = await principal.create_and_notify_principal(database, record, user)

    assert result is record
    assert session.request.topic == helpers.TOPIC_KEY_ADD.format(machine_id="machine-id")
    assert session.request.arguments == [user.email, record.public_key, "user:owner"]
    assert session.request.request_options == {"acknowledge": True}
