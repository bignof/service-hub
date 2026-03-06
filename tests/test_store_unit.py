import asyncio
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.db import Database
from app.db_models import AgentModel
from app.store import CHINA_TZ, HubState, _as_china_time, _as_storage_utc, _generate_agent_key, _hash_agent_key, _loads_payload, utc_now


def _create_state(tmp_path: Path, history_limit: int = 2) -> tuple[HubState, Database]:
    database = Database(f"sqlite:///{tmp_path / 'store.db'}")
    database.init_schema()
    return HubState(heartbeat_timeout=1, command_history_limit=history_limit, database=database), database


def test_store_helpers_normalize_payload_and_time() -> None:
    now = utc_now()
    generated_key = _generate_agent_key()

    assert _loads_payload("") == {}
    assert _loads_payload('{"value": 1}') == {"value": 1}
    assert _as_china_time(None) is None
    assert _as_china_time(now.replace(tzinfo=None)) == now.replace(tzinfo=None).replace(tzinfo=now.tzinfo).astimezone(CHINA_TZ)
    assert _as_china_time(now) == now.astimezone(CHINA_TZ)
    assert _as_storage_utc(None) is None
    assert _as_storage_utc(now.replace(tzinfo=None)) == now.replace(tzinfo=None).replace(tzinfo=now.tzinfo)
    assert isinstance(generated_key, str)
    assert len(generated_key) >= 32
    assert _hash_agent_key("secret") == _hash_agent_key("secret")
    assert _hash_agent_key("secret") != _hash_agent_key("other")


def test_hub_state_agent_lifecycle_and_database_ops(tmp_path: Path) -> None:
    state, database = _create_state(tmp_path)
    websocket = object()
    other_websocket = object()

    assert asyncio.run(state.initialize()) is None
    assert asyncio.run(state.check_database()) is True

    asyncio.run(state.register_agent("agent-a", websocket, "127.0.0.1:1234"))
    assert asyncio.run(state.get_connection("agent-a")) is websocket

    agent = asyncio.run(state.get_agent("agent-a"))
    assert agent is not None
    assert agent["connected"] is True
    assert agent["online"] is True

    asyncio.run(state.touch_agent("agent-b", "heartbeat"))
    asyncio.run(state.touch_agent("agent-b", "pong"))
    detached = asyncio.run(state.get_agent("agent-b"))
    assert detached is not None
    assert detached["connected"] is False
    assert detached["online"] is False
    assert detached["last_heartbeat_at"] is not None
    assert detached["last_pong_at"] is not None

    asyncio.run(state.disconnect_agent("agent-a", other_websocket))
    assert asyncio.run(state.get_connection("agent-a")) is websocket

    asyncio.run(state.disconnect_agent("agent-a", websocket))
    asyncio.run(state.disconnect_agent("missing"))
    disconnected = asyncio.run(state.get_agent("agent-a"))
    assert disconnected is not None
    assert disconnected["connected"] is False
    assert disconnected["online"] is False
    assert disconnected["disconnected_at"] is not None

    database.engine.dispose()


def test_hub_state_command_queries_filters_and_events(tmp_path: Path) -> None:
    state, database = _create_state(tmp_path)

    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-1", "action": "restart", "dir": "/srv/a"},
            requested_by="alice",
            request_source="console",
        )
    )
    asyncio.run(state.mark_ack("req-1"))
    asyncio.run(state.mark_result("req-1", "failed", output="boom", message="retry", error="boom"))
    first_command = asyncio.run(state.get_command("req-1"))
    assert first_command is not None

    asyncio.run(
        state.store_command(
            "agent-b",
            {"type": "command", "requestId": "req-2", "action": "update", "dir": "/srv/b", "image": "nginx:latest"},
            requested_by="bob",
            request_source="scheduler",
        )
    )
    asyncio.run(state.mark_result("req-2", "success", message="done"))

    filtered = asyncio.run(
        state.list_commands(
            "agent-a",
            status="failed",
            action="restart",
            requested_by="alice",
            request_source="console",
            created_after=first_command["created_at"] - timedelta(seconds=1),
            created_before=first_command["created_at"] + timedelta(seconds=1),
            sort_by="createdAt",
            order="desc",
            limit=None,
            offset=0,
        )
    )
    assert filtered["total"] == 1
    assert filtered["limit"] == 2
    assert filtered["items"][0]["request_id"] == "req-1"

    paged = asyncio.run(state.list_commands(sort_by="updatedAt", order="asc", limit=1, offset=1))
    assert paged["total"] == 2
    assert paged["has_more"] is False
    assert paged["items"][0]["request_id"] == "req-2"

    events = asyncio.run(state.list_command_events("req-1"))
    assert [item["event_type"] for item in events] == ["created", "ack", "result"]

    missing = asyncio.run(state.get_command("missing"))
    assert missing is None

    agents = asyncio.run(state.list_agents())
    assert [item["agent_id"] for item in agents] == []

    database.engine.dispose()


def test_hub_state_retry_and_missing_updates(tmp_path: Path) -> None:
    state, database = _create_state(tmp_path)

    assert asyncio.run(state.mark_ack("missing")) is None
    assert asyncio.run(state.mark_result("missing", "failed", error="boom")) is None
    assert asyncio.run(state.retry_command("missing")) is None

    asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-1", "action": "update", "dir": "/srv/a", "image": "busybox:1"},
            requested_by="alice",
            request_source="console",
        )
    )
    asyncio.run(state.mark_result("req-1", "failed", error="boom"))

    original, retried = asyncio.run(state.retry_command("req-1", requested_by="alice", request_source="console"))

    assert original["request_id"] == "req-1"
    assert retried["original_request_id"] == "req-1"
    assert retried["retry_count"] == 1
    assert retried["payload"]["image"] == "busybox:1"

    retry_events = asyncio.run(state.list_command_events("req-1"))
    assert retry_events[-1]["event_type"] == "retry"

    database.engine.dispose()


def test_agent_key_does_not_expire_by_issued_timestamp(tmp_path: Path) -> None:
    state, database = _create_state(tmp_path)

    assert asyncio.run(state.authenticate_agent("missing", "")) is False

    credential = asyncio.run(state.rotate_agent_key("agent-a"))

    assert asyncio.run(state.authenticate_agent("missing", credential["agent_key"])) is False

    with database.session_factory() as session:
        record = session.scalar(select(AgentModel).where(AgentModel.agent_id == "agent-a"))
        assert record is not None
        record.key_issued_at = utc_now() - timedelta(days=3650)
        session.commit()

    assert asyncio.run(state.authenticate_agent("agent-a", credential["agent_key"])) is True

    database.engine.dispose()


def test_command_timestamps_are_serialized_in_china_timezone(tmp_path: Path) -> None:
    state, database = _create_state(tmp_path)

    command = asyncio.run(
        state.store_command(
            "agent-a",
            {"type": "command", "requestId": "req-cn", "action": "restart", "dir": "/srv/a"},
        )
    )

    assert command["created_at"].utcoffset() == timedelta(hours=8)
    assert command["updated_at"].utcoffset() == timedelta(hours=8)

    database.engine.dispose()
