import asyncio
import logging
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, WebSocketDisconnect

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.db import Database
from app.main import ChinaTimeFormatter, _handle_agent_message, _localize_openapi, _remote_address, _serialize_command, agent_ws, dispatch_command, retry_command
from app.models import CommandDispatchRequest
from app.store import HubState


class RecordingState:
    def __init__(self) -> None:
        self.touched: list[tuple[str, str]] = []
        self.acked: list[str] = []
        self.results: list[tuple[str, str, str | None, str | None, str | None]] = []
        self.commands: dict[str, dict[str, Any]] = {}
        self.command_events: list[str] = []
        self.agent: dict[str, Any] | None = {"agent_id": "agent-a", "online": True}
        self.connection: Any = None
        self.retried: tuple[dict[str, Any], dict[str, Any]] | None = None
        self.auth_result = True
        self.auth_calls: list[tuple[str, str]] = []

    async def touch_agent(self, agent_id: str, event_type: str) -> None:
        self.touched.append((agent_id, event_type))

    async def mark_ack(self, request_id: str) -> None:
        self.acked.append(request_id)

    async def mark_result(
        self,
        request_id: str,
        status: str,
        *,
        output: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        self.results.append((request_id, status, output, message, error))

    async def get_command(self, request_id: str) -> dict[str, Any] | None:
        return self.commands.get(request_id)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self.agent

    async def store_command(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        requested_by: str | None = None,
        request_source: str | None = None,
    ) -> None:
        self.commands[payload["requestId"]] = {
            "request_id": payload["requestId"],
            "agent_id": agent_id,
            "status": "queued",
            "action": payload["action"],
            "dir": payload["dir"],
            "image": payload.get("image"),
            "original_request_id": None,
            "retry_count": 0,
            "requested_by": requested_by,
            "request_source": request_source,
            "payload": payload,
            "output": None,
            "message": None,
            "error": None,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "ack_at": None,
            "result_at": None,
        }

    async def get_connection(self, agent_id: str) -> Any:
        return self.connection

    async def retry_command(
        self,
        request_id: str,
        *,
        requested_by: str | None = None,
        request_source: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return self.retried

    async def authenticate_agent(self, agent_id: str, presented_key: str) -> bool:
        self.auth_calls.append((agent_id, presented_key))
        return self.auth_result

    async def register_agent(self, agent_id: str, websocket: Any, remote: str | None) -> None:
        self.command_events.append(f"register:{agent_id}:{remote}")

    async def disconnect_agent(self, agent_id: str, websocket: Any | None = None) -> None:
        self.command_events.append(f"disconnect:{agent_id}")


class FailingSocket:
    async def send_json(self, payload: dict[str, Any]) -> None:
        raise RuntimeError("boom")


class RecordingSocket:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


class FakeAgentWebSocket:
    def __init__(self, agent_key: str, messages: list[Any], client: Any | None = None) -> None:
        self.query_params = {"key": agent_key}
        self._messages = iter(messages)
        self.client = client
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int) -> None:
        self.closed_code = code

    async def receive(self) -> Any:
        value = next(self._messages)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HubState:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.init_schema()
    hub_state = HubState(heartbeat_timeout=90, command_history_limit=2, database=database)

    import app.main as main_module

    monkeypatch.setattr(main_module, "hub_state", hub_state)
    yield hub_state
    database.engine.dispose()


def test_remote_address_and_serialize_command(state: HubState) -> None:
    websocket = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1", port=9000))

    assert _remote_address(websocket) == "127.0.0.1:9000"
    assert _remote_address(SimpleNamespace(client=None)) is None

    with pytest.raises(HTTPException, match="Command not found"):
        asyncio.run(_serialize_command("missing"))

    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}))

    snapshot = asyncio.run(_serialize_command("req-1"))

    assert snapshot.request_id == "req-1"


def test_china_time_formatter_and_openapi_localization(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    formatter = ChinaTimeFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", (), None)
    record.created = 0

    assert formatter.formatTime(record, "%Y-%m-%d %H:%M:%S") == "1970-01-01 08:00:00"

    schema = {
        "paths": {
            "/health": {
                "get": {
                    "responses": {
                        "200": {"description": "Successful Response"},
                        "422": {"description": "Validation Error"},
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "A": {"title": "Response Health Health Get"},
                "HTTPValidationError": {"title": "x", "properties": {"detail": {"title": "detail"}}},
                "ValidationError": {
                    "title": "y",
                    "properties": {
                        "loc": {"title": "loc"},
                        "msg": {"title": "msg"},
                        "type": {"title": "type"},
                    },
                },
            }
        },
    }

    localized = _localize_openapi(schema)

    assert localized["paths"]["/health"]["get"]["responses"]["200"]["description"] == "请求成功"
    assert localized["paths"]["/health"]["get"]["responses"]["422"]["description"] == "请求校验失败"
    assert localized["components"]["schemas"]["A"]["title"] == "健康检查响应"
    assert localized["components"]["schemas"]["HTTPValidationError"]["title"] == "HTTP 请求校验错误"
    assert localized["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"]["title"] == "错误详情"
    assert localized["components"]["schemas"]["ValidationError"]["properties"]["loc"]["title"] == "错误位置"

    calls = {"count": 0}

    def fake_openapi() -> dict[str, Any]:
        calls["count"] += 1
        return {
            "paths": {},
            "components": {"schemas": {}},
        }

    monkeypatch.setattr(main_module, "original_openapi", fake_openapi)
    monkeypatch.setattr(main_module.app, "openapi_schema", None)

    first = main_module.custom_openapi()
    second = main_module.custom_openapi()

    assert first is second
    assert calls["count"] == 1


def test_lifespan_initializes_hub_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    initialized = {"value": False}

    class StubState:
        async def initialize(self) -> None:
            initialized["value"] = True

    monkeypatch.setattr(main_module, "hub_state", StubState())

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(run_lifespan())

    assert initialized["value"] is True


def test_handle_agent_message_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    monkeypatch.setattr(main_module, "hub_state", recording_state)

    asyncio.run(_handle_agent_message("agent-a", {}))
    asyncio.run(_handle_agent_message("agent-a", {"type": "heartbeat"}))
    asyncio.run(_handle_agent_message("agent-a", {"type": "ack"}))
    asyncio.run(_handle_agent_message("agent-a", {"type": "ack", "requestId": "req-1"}))
    asyncio.run(_handle_agent_message("agent-a", {"type": "result"}))
    asyncio.run(
        _handle_agent_message(
            "agent-a",
            {"type": "result", "requestId": "req-2", "status": "success", "output": "ok", "message": "done"},
        )
    )
    asyncio.run(_handle_agent_message("agent-a", {"type": "pong"}))
    asyncio.run(_handle_agent_message("agent-a", {"type": "custom"}))

    assert recording_state.touched == [
        ("agent-a", "heartbeat"),
        ("agent-a", "ack"),
        ("agent-a", "ack"),
        ("agent-a", "result"),
        ("agent-a", "result"),
        ("agent-a", "pong"),
        ("agent-a", "custom"),
    ]
    assert recording_state.acked == ["req-1"]
    assert recording_state.results == [("req-2", "success", "ok", "done", None)]


def test_dispatch_command_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    monkeypatch.setattr(main_module, "hub_state", recording_state)

    request = CommandDispatchRequest(request_id="req-1", action="restart", dir="/srv/a")

    recording_state.agent = None
    with pytest.raises(HTTPException, match="Agent not found"):
        asyncio.run(dispatch_command(request=request, agent_id="agent-a"))

    recording_state.agent = {"agent_id": "agent-a", "online": False}
    with pytest.raises(HTTPException, match="Agent is offline"):
        asyncio.run(dispatch_command(request=request, agent_id="agent-a"))

    recording_state.agent = {"agent_id": "agent-a", "online": True}
    recording_state.connection = None
    with pytest.raises(HTTPException, match="Agent connection is unavailable"):
        asyncio.run(dispatch_command(request=request, agent_id="agent-a"))
    assert recording_state.results[-1] == ("req-1", "failed", None, None, "Agent connection is unavailable")

    recording_state.connection = FailingSocket()
    with pytest.raises(HTTPException, match="Failed to dispatch command"):
        asyncio.run(dispatch_command(request=request, agent_id="agent-a"))
    assert recording_state.results[-1][0] == "req-1"
    assert recording_state.results[-1][-1] == "Failed to dispatch command: boom"


def test_dispatch_command_success_includes_image(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    recording_state.connection = RecordingSocket()
    monkeypatch.setattr(main_module, "hub_state", recording_state)

    request = CommandDispatchRequest(request_id="req-image", action="update", dir="/srv/a", image="nginx:1.27")

    response = asyncio.run(
        dispatch_command(
            request=request,
            agent_id="agent-a",
            requested_by=None,
            request_source=None,
        )
    )

    assert response.accepted is True
    assert response.command.image == "nginx:1.27"
    assert recording_state.connection.payloads == [
        {
            "type": "command",
            "requestId": "req-image",
            "action": "update",
            "dir": "/srv/a",
            "image": "nginx:1.27",
        }
    ]


def test_retry_command_error_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    monkeypatch.setattr(main_module, "hub_state", recording_state)

    with pytest.raises(HTTPException, match="Command not found"):
        asyncio.run(retry_command("missing"))

    recording_state.commands["req-1"] = {"request_id": "req-1", "agent_id": "agent-a", "status": "success"}
    with pytest.raises(HTTPException, match="Only failed commands can be retried"):
        asyncio.run(retry_command("req-1"))

    recording_state.commands["req-1"] = {"request_id": "req-1", "agent_id": "agent-a", "status": "failed"}
    recording_state.agent = None
    with pytest.raises(HTTPException, match="Agent not found"):
        asyncio.run(retry_command("req-1"))

    recording_state.agent = {"agent_id": "agent-a", "online": False}
    with pytest.raises(HTTPException, match="Agent is offline"):
        asyncio.run(retry_command("req-1"))

    recording_state.agent = {"agent_id": "agent-a", "online": True}
    recording_state.retried = None
    with pytest.raises(HTTPException, match="Command not found"):
        asyncio.run(retry_command("req-1"))

    recording_state.retried = (
        recording_state.commands["req-1"],
        {
            "request_id": "req-2",
            "agent_id": "agent-a",
            "payload": {"type": "command", "requestId": "req-2", "action": "restart", "dir": "/srv/a"},
        },
    )
    recording_state.connection = None
    with pytest.raises(HTTPException, match="Agent connection is unavailable"):
        asyncio.run(retry_command("req-1"))
    assert recording_state.results[-1] == ("req-2", "failed", None, None, "Agent connection is unavailable")

    recording_state.connection = FailingSocket()
    with pytest.raises(HTTPException, match="Failed to dispatch command"):
        asyncio.run(retry_command("req-1"))
    assert recording_state.results[-1][0] == "req-2"


def test_agent_ws_rejects_invalid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    recording_state.auth_result = False
    websocket = FakeAgentWebSocket("wrong-token", [])
    monkeypatch.setattr(main_module, "hub_state", recording_state)

    asyncio.run(agent_ws(websocket, "agent-a"))

    assert websocket.closed_code == 1008
    assert recording_state.auth_calls == [("agent-a", "wrong-token")]
    assert recording_state.command_events == []


def test_agent_ws_handles_messages_and_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    recording_state = RecordingState()
    handled_payloads: list[dict[str, Any]] = []

    async def fake_handle(agent_id: str, payload: dict[str, Any]) -> None:
        handled_payloads.append(payload)

    websocket = FakeAgentWebSocket(
        "agent-key",
        [
            {"text": json.dumps({"type": "heartbeat"})},
            {"text": json.dumps(["not-a-dict"])},
            {"type": "websocket.disconnect"},
        ],
        client=SimpleNamespace(host="10.0.0.8", port=8765),
    )
    monkeypatch.setattr(main_module, "hub_state", recording_state)
    monkeypatch.setattr(main_module, "_handle_agent_message", fake_handle)

    asyncio.run(agent_ws(websocket, "agent-a"))

    assert websocket.accepted is True
    assert recording_state.auth_calls == [("agent-a", "agent-key")]
    assert handled_payloads == [{"type": "heartbeat"}]
    assert recording_state.command_events == ["register:agent-a:10.0.0.8:8765", "disconnect:agent-a"]


def test_agent_ws_handles_decode_disconnect_and_generic_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_module

    for message in [
        [{"text": "{"}],
        [WebSocketDisconnect()],
        [RuntimeError("socket-failed")],
    ]:
        recording_state = RecordingState()
        websocket = FakeAgentWebSocket("agent-key", message)
        monkeypatch.setattr(main_module, "hub_state", recording_state)

        asyncio.run(agent_ws(websocket, "agent-a"))

        assert recording_state.auth_calls == [("agent-a", "agent-key")]
        assert recording_state.command_events[0].startswith("register:agent-a:")
        assert recording_state.command_events[-1] == "disconnect:agent-a"
