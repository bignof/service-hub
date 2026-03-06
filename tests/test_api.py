from __future__ import annotations

import asyncio
from datetime import timedelta
import os
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.db import Database
from app.db_models import AgentModel
from app.main import app
from app.store import HubState, utc_now


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    database = Database("sqlite:///" + str(tmp_path / "test.db"))
    test_state = HubState(heartbeat_timeout=90, command_history_limit=200, database=database)
    database.init_schema()

    import app.main as main_module

    old_database = main_module.database
    old_hub_state = main_module.hub_state
    old_admin_token = main_module.settings.admin_token
    main_module.database = database
    main_module.hub_state = test_state
    object.__setattr__(main_module.settings, "admin_token", "test-admin-token")
    app.dependency_overrides = {}

    with TestClient(app) as test_client:
        yield test_client

    database.engine.dispose()
    main_module.database = old_database
    main_module.hub_state = old_hub_state
    object.__setattr__(main_module.settings, "admin_token", old_admin_token)


class FakeSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)


def attach_agent(state: HubState, agent_id: str, remote: str = "127.0.0.1:12345") -> FakeSocket:
    state._register_agent_sync(agent_id, remote)
    socket = FakeSocket()
    state._connections[agent_id] = socket  # type: ignore[assignment]
    return socket


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_agents_and_get_agent_return_expected_shape(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    response = client.get("/api/agents")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["agentId"] == "agent-a"
    assert body[0]["connected"] is True
    assert body[0]["online"] is True
    assert body[0]["credentialConfigured"] is False

    agent_response = client.get("/api/agents/agent-a")

    assert agent_response.status_code == 200
    assert agent_response.json()["agentId"] == "agent-a"


def test_rotate_agent_credentials_requires_admin_token_and_persists_state(client: TestClient) -> None:
    forbidden = client.post("/api/agents/agent-a/credentials/rotate")

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Invalid admin token"}

    response = client.post(
        "/api/agents/agent-a/credentials/rotate",
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agentId"] == "agent-a"
    assert body["agentKey"]
    assert body["issuedAt"]
    assert body["created"] is True

    agent_response = client.get("/api/agents/agent-a")

    assert agent_response.status_code == 200
    assert agent_response.json()["credentialConfigured"] is True
    assert agent_response.json()["keyIssuedAt"] is not None


def test_provision_agent_creates_offline_agent_and_returns_initial_key(client: TestClient) -> None:
    forbidden = client.post("/api/agents", json={"agentId": "agent-new"})

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Invalid admin token"}

    response = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-new"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["agentKey"]
    assert body["issuedAt"]
    assert body["agent"]["agentId"] == "agent-new"
    assert body["agent"]["connected"] is False
    assert body["agent"]["online"] is False
    assert body["agent"]["credentialConfigured"] is True

    conflict = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-new"},
    )

    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "Agent already exists"}


def test_provisioned_key_remains_valid_even_if_issued_at_is_old(client: TestClient) -> None:
    import app.main as main_module

    response = client.post(
        "/api/agents",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"agentId": "agent-e2e"},
    )

    assert response.status_code == 201
    agent_key = response.json()["agentKey"]

    with main_module.database.session_factory() as session:
        record = session.scalar(select(AgentModel).where(AgentModel.agent_id == "agent-e2e"))
        assert record is not None
        record.key_issued_at = utc_now() - timedelta(days=3650)
        session.commit()

    with client.websocket_connect(f"/ws/agent/agent-e2e?key={agent_key}") as websocket:
        websocket.send_json({"type": "heartbeat"})

    agent_response = client.get("/api/agents/agent-e2e")
    assert agent_response.status_code == 200
    assert agent_response.json()["credentialConfigured"] is True


def test_get_unknown_agent_returns_404(client: TestClient) -> None:
    response = client.get("/api/agents/missing-agent")

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


def test_dispatch_command_creates_record_and_returns_expected_payload(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    socket = attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        headers={
            "X-Requested-By": "platform-api",
            "X-Requested-Source": "ops-console",
        },
        json={
            "requestId": "req-dispatch-1",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["command"]["requestId"] == "req-dispatch-1"
    assert body["command"]["requestedBy"] == "platform-api"
    assert body["command"]["requestSource"] == "ops-console"
    assert socket.messages[0]["requestId"] == "req-dispatch-1"

    get_response = client.get("/api/commands/req-dispatch-1")
    assert get_response.status_code == 200
    assert get_response.json()["requestId"] == "req-dispatch-1"


def test_dispatch_update_without_image_returns_422(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")

    response = client.post(
        "/api/agents/agent-a/commands",
        json={
            "requestId": "req-invalid-update",
            "action": "update",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 422


def test_dispatch_command_to_offline_agent_returns_409(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    state._register_agent_sync("agent-a", "127.0.0.1:12345")

    response = client.post(
        "/api/agents/agent-a/commands",
        json={
            "requestId": "req-offline-1",
            "action": "restart",
            "dir": "/srv/a",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Agent is offline"}


def test_get_command_events_returns_created_ack_and_result(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-events-1", "action": "restart", "dir": "/srv/a"},
            requested_by="platform-api",
            request_source="ops-console",
        )
    )
    asyncio.run(state.mark_ack("req-events-1"))
    asyncio.run(state.mark_result("req-events-1", "success", message="done"))

    response = client.get("/api/commands/req-events-1/events")

    assert response.status_code == 200
    body = response.json()
    assert [item["eventType"] for item in body] == ["created", "ack", "result"]


def test_get_unknown_command_and_events_return_404(client: TestClient) -> None:
    command_response = client.get("/api/commands/missing")
    events_response = client.get("/api/commands/missing/events")

    assert command_response.status_code == 404
    assert events_response.status_code == 404
    assert command_response.json() == {"detail": "Command not found"}
    assert events_response.json() == {"detail": "Command not found"}


def test_list_commands_supports_sort_and_pagination(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    payload1 = {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}
    payload2 = {"type": "command", "requestId": "req-2", "action": "update", "dir": "/srv/a", "image": "nginx:latest"}

    asyncio.run(state.store_command("agent-a", payload1, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))
    asyncio.run(state.store_command("agent-a", payload2, requested_by="scheduler", request_source="batch-job"))
    asyncio.run(state.mark_result("req-2", "success", message="done"))

    response = client.get(
        "/api/commands",
        params={
            "sortBy": "updatedAt",
            "order": "asc",
            "limit": 1,
            "offset": 0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["hasMore"] is True
    assert body["sortBy"] == "updatedAt"
    assert body["order"] == "asc"
    assert len(body["items"]) == 1


def test_list_commands_tolerates_empty_query_values(client: TestClient) -> None:
    response = client.get(
        "/api/commands",
        params={
            "agentId": "",
            "status": "",
            "action": "",
            "requestedBy": "",
            "requestSource": "",
            "createdAfter": "",
            "createdBefore": "",
            "sortBy": "",
            "order": "",
            "limit": "",
            "offset": "",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["sortBy"] == "createdAt"
    assert body["order"] == "desc"
    assert body["limit"] == 100
    assert body["offset"] == 0


def test_list_agent_commands_tolerates_empty_query_values(client: TestClient) -> None:
    response = client.get(
        "/api/agents/agent-a/commands",
        params={
            "status": "",
            "action": "",
            "requestedBy": "",
            "requestSource": "",
            "createdAfter": "",
            "createdBefore": "",
            "sortBy": "",
            "order": "",
            "limit": "",
            "offset": "",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["sortBy"] == "createdAt"
    assert body["order"] == "desc"
    assert body["limit"] == 100
    assert body["offset"] == 0


def test_agent_command_history_is_paginated(client: TestClient) -> None:
    import app.main as main_module
    state = main_module.hub_state
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))
    asyncio.run(state.store_command("agent-b", {"type": "command", "requestId": "req-2", "action": "restart", "dir": "/srv/b"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-2", "success", message="done"))

    response = client.get("/api/agents/agent-a/commands", params={"status": "failed"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["requestId"] == "req-1"


def test_retry_failed_command_creates_new_command_and_audit_event(client: TestClient) -> None:
    import app.main as main_module
    state = main_module.hub_state
    fake_socket = attach_agent(state, "agent-a")
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"}, requested_by="platform-api", request_source="ops-console"))
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))

    response = client.post(
        "/api/commands/req-1/retry",
        headers={
            "X-Requested-By": "platform-api",
            "X-Requested-Source": "ops-console",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["command"]["originalRequestId"] == "req-1"
    assert body["command"]["retryCount"] == 1
    assert fake_socket.messages[0]["requestId"] == body["command"]["requestId"]

    events_response = client.get("/api/commands/req-1/events")
    assert events_response.status_code == 200
    event_types = [item["eventType"] for item in events_response.json()]
    assert "retry" in event_types


def test_retry_non_failed_command_returns_409(client: TestClient) -> None:
    import app.main as main_module

    state = main_module.hub_state
    attach_agent(state, "agent-a")
    asyncio.run(state.store_command("agent-a", {"type": "command", "requestId": "req-ok", "action": "restart", "dir": "/srv/a"}))
    asyncio.run(state.mark_result("req-ok", "success", message="done"))

    response = client.post("/api/commands/req-ok/retry")

    assert response.status_code == 409
    assert response.json() == {"detail": "Only failed commands can be retried"}


def test_retry_missing_command_returns_404(client: TestClient) -> None:
    response = client.post("/api/commands/missing/retry")

    assert response.status_code == 404
    assert response.json() == {"detail": "Command not found"}