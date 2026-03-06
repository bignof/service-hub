import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket
from sqlalchemy import case, func, select

from app.db import Database
from app.db_models import AgentModel, CommandEventModel, CommandModel


CHINA_TZ = timezone(timedelta(hours=8))

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_china_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CHINA_TZ)


def _as_storage_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _loads_payload(value: str) -> dict[str, Any]:
    return json.loads(value) if value else {}


def _agent_to_dict(record: AgentModel) -> dict[str, Any]:
    return {
        "agent_id": record.agent_id,
        "status": record.status,
        "credential_configured": bool(record.agent_key_hash),
        "remote": record.remote_addr,
        "key_issued_at": _as_china_time(record.key_issued_at),
        "connected_at": _as_china_time(record.connected_at),
        "disconnected_at": _as_china_time(record.last_disconnect_at),
        "last_seen_at": _as_china_time(record.last_seen_at),
        "last_heartbeat_at": _as_china_time(record.last_heartbeat_at),
        "last_pong_at": _as_china_time(record.last_pong_at),
    }


def _hash_agent_key(agent_key: str) -> str:
    return hashlib.sha256(agent_key.encode("utf-8")).hexdigest()


def _generate_agent_key() -> str:
    return secrets.token_urlsafe(32)


def command_to_dict(record: CommandModel) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "agent_id": record.agent_id,
        "status": record.status,
        "action": record.action,
        "dir": record.target_dir,
        "image": record.target_image,
        "original_request_id": record.original_request_id,
        "retry_count": record.retry_count,
        "requested_by": record.requested_by,
        "request_source": record.request_source,
        "payload": _loads_payload(record.payload_json),
        "output": record.output,
        "message": record.message,
        "error": record.error,
        "created_at": _as_china_time(record.created_at),
        "updated_at": _as_china_time(record.updated_at),
        "ack_at": _as_china_time(record.ack_at),
        "result_at": _as_china_time(record.result_at),
    }


def command_event_to_dict(record: CommandEventModel) -> dict[str, Any]:
    return {
        "id": record.id,
        "request_id": record.request_id,
        "event_type": record.event_type,
        "payload": _loads_payload(record.payload_json),
        "created_at": _as_china_time(record.created_at),
    }


def _apply_command_filters(
    statement: Any,
    *,
    agent_id: str | None,
    status: str | None,
    action: str | None,
    requested_by: str | None,
    request_source: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> Any:
    created_after = _as_storage_utc(created_after)
    created_before = _as_storage_utc(created_before)

    if agent_id:
        statement = statement.where(CommandModel.agent_id == agent_id)
    if status:
        statement = statement.where(CommandModel.status == status)
    if action:
        statement = statement.where(CommandModel.action == action)
    if requested_by:
        statement = statement.where(CommandModel.requested_by == requested_by)
    if request_source:
        statement = statement.where(CommandModel.request_source == request_source)
    if created_after:
        statement = statement.where(CommandModel.created_at >= created_after)
    if created_before:
        statement = statement.where(CommandModel.created_at <= created_before)
    return statement


class HubState:
    def __init__(self, heartbeat_timeout: int, command_history_limit: int, database: Database) -> None:
        self.heartbeat_timeout = heartbeat_timeout
        self.command_history_limit = command_history_limit
        self.database = database
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self.database.init_schema)

    async def check_database(self) -> bool:
        return await asyncio.to_thread(self.database.ping)

    async def register_agent(self, agent_id: str, websocket: WebSocket, remote: str | None) -> None:
        async with self._lock:
            self._connections[agent_id] = websocket
        await asyncio.to_thread(self._register_agent_sync, agent_id, remote)

    async def disconnect_agent(self, agent_id: str, websocket: WebSocket | None = None) -> None:
        should_persist = False
        async with self._lock:
            active = self._connections.get(agent_id)
            if websocket is not None and active is not websocket:
                return

            if agent_id in self._connections:
                self._connections.pop(agent_id, None)
                should_persist = True

        if should_persist:
            await asyncio.to_thread(self._disconnect_agent_sync, agent_id)

    async def touch_agent(self, agent_id: str, event_type: str) -> None:
        await asyncio.to_thread(self._touch_agent_sync, agent_id, event_type)

    async def get_connection(self, agent_id: str) -> WebSocket | None:
        async with self._lock:
            return self._connections.get(agent_id)

    async def store_command(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        original_request_id: str | None = None,
        retry_count: int = 0,
        requested_by: str | None = None,
        request_source: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._store_command_sync,
            agent_id,
            payload,
            original_request_id,
            retry_count,
            requested_by,
            request_source,
        )

    async def retry_command(
        self,
        request_id: str,
        *,
        requested_by: str | None = None,
        request_source: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return await asyncio.to_thread(self._retry_command_sync, request_id, requested_by, request_source)

    async def mark_ack(self, request_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._mark_ack_sync, request_id)

    async def mark_result(
        self,
        request_id: str,
        status: str,
        *,
        output: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._mark_result_sync, request_id, status, output, message, error)

    async def get_command(self, request_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_command_sync, request_id)

    async def list_commands(
        self,
        agent_id: str | None = None,
        *,
        status: str | None = None,
        action: str | None = None,
        requested_by: str | None = None,
        request_source: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        sort_by: str = "createdAt",
        order: str = "desc",
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        effective_limit = limit or self.command_history_limit
        return await asyncio.to_thread(
            self._list_commands_sync,
            agent_id,
            status,
            action,
            requested_by,
            request_source,
            created_after,
            created_before,
            sort_by,
            order,
            effective_limit,
            offset,
        )

    async def list_command_events(self, request_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_command_events_sync, request_id)

    async def rotate_agent_key(self, agent_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._rotate_agent_key_sync, agent_id)

    async def authenticate_agent(self, agent_id: str, presented_key: str) -> bool:
        return await asyncio.to_thread(self._authenticate_agent_sync, agent_id, presented_key)

    async def provision_agent(self, agent_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._provision_agent_sync, agent_id)

    async def list_agents(self) -> list[dict[str, Any]]:
        connection_ids = await self._connection_ids()
        agents, summaries = await asyncio.to_thread(self._list_agents_with_summaries_sync)
        snapshots = [self._snapshot_agent(agent, connection_ids, summaries.get(agent["agent_id"], {})) for agent in agents]
        return sorted(snapshots, key=lambda item: item["agent_id"])

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        connection_ids = await self._connection_ids()
        record, summary = await asyncio.to_thread(self._get_agent_with_summary_sync, agent_id)
        if record is None:
            return None
        return self._snapshot_agent(record, connection_ids, summary)

    async def _connection_ids(self) -> set[str]:
        async with self._lock:
            return set(self._connections.keys())

    def _snapshot_agent(self, record: dict[str, Any], connection_ids: set[str], summary: dict[str, Any]) -> dict[str, Any]:
        last_seen_at = record["last_seen_at"]
        connected = record["agent_id"] in connection_ids
        online = bool(
            connected
            and last_seen_at is not None
            and utc_now() - last_seen_at <= timedelta(seconds=self.heartbeat_timeout)
        )
        return {
            "agent_id": record["agent_id"],
            "connected": connected,
            "online": online,
            "credential_configured": record["credential_configured"],
            "remote": record["remote"],
            "key_issued_at": record["key_issued_at"],
            "connected_at": record["connected_at"],
            "disconnected_at": record["disconnected_at"],
            "last_seen_at": record["last_seen_at"],
            "last_heartbeat_at": record["last_heartbeat_at"],
            "last_pong_at": record["last_pong_at"],
            "stale_after_seconds": self.heartbeat_timeout,
            "queued_commands": summary.get("queued_commands", 0),
            "processing_commands": summary.get("processing_commands", 0),
            "last_command_created_at": summary.get("last_command_created_at"),
        }

    def _command_summary_map_sync(self, agent_id: str | None = None) -> dict[str, dict[str, Any]]:
        statement = select(
            CommandModel.agent_id,
            func.sum(case((CommandModel.status == "queued", 1), else_=0)).label("queued_commands"),
            func.sum(case((CommandModel.status == "processing", 1), else_=0)).label("processing_commands"),
            func.max(CommandModel.created_at).label("last_command_created_at"),
        ).group_by(CommandModel.agent_id)
        if agent_id is not None:
            statement = statement.where(CommandModel.agent_id == agent_id)

        with self.database.session_factory() as session:
            rows = session.execute(statement).all()

        summaries: dict[str, dict[str, Any]] = {}
        for row in rows:
            summaries[row.agent_id] = {
                "queued_commands": int(row.queued_commands or 0),
                "processing_commands": int(row.processing_commands or 0),
                "last_command_created_at": _as_china_time(row.last_command_created_at),
            }
        return summaries

    def _list_agents_with_summaries_sync(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        return self._list_agents_sync(), self._command_summary_map_sync()

    def _get_agent_with_summary_sync(self, agent_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        record = self._get_agent_sync(agent_id)
        if record is None:
            return None, {}
        summaries = self._command_summary_map_sync(agent_id)
        return record, summaries.get(agent_id, {})

    def _register_agent_sync(self, agent_id: str, remote: str | None) -> None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if record is None:
                record = AgentModel(agent_id=agent_id, created_at=now, updated_at=now)
                session.add(record)

            record.status = "online"
            record.remote_addr = remote
            record.connected_at = now
            record.last_disconnect_at = None
            record.last_seen_at = now
            record.updated_at = now
            session.commit()

    def _rotate_agent_key_sync(self, agent_id: str) -> dict[str, Any]:
        now = utc_now()
        agent_key = _generate_agent_key()
        agent_key_hash = _hash_agent_key(agent_key)
        created = False
        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if record is None:
                record = AgentModel(
                    agent_id=agent_id,
                    status="offline",
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                created = True

            record.agent_key_hash = agent_key_hash
            record.key_issued_at = now
            record.updated_at = now
            session.commit()

        return {
            "agent_id": agent_id,
            "agent_key": agent_key,
            "issued_at": now,
            "created": created,
        }

    def _provision_agent_sync(self, agent_id: str) -> dict[str, Any] | None:
        now = utc_now()
        agent_key = _generate_agent_key()
        agent_key_hash = _hash_agent_key(agent_key)

        with self.database.session_factory() as session:
            existing = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if existing is not None:
                return None

            record = AgentModel(
                agent_id=agent_id,
                status="offline",
                agent_key_hash=agent_key_hash,
                key_issued_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.commit()

        return {
            "agent": {
                "agent_id": agent_id,
                "connected": False,
                "online": False,
                "credential_configured": True,
                "remote": None,
                "key_issued_at": now,
                "connected_at": None,
                "disconnected_at": None,
                "last_seen_at": None,
                "last_heartbeat_at": None,
                "last_pong_at": None,
                "stale_after_seconds": self.heartbeat_timeout,
            },
            "agent_key": agent_key,
            "issued_at": now,
        }

    def _authenticate_agent_sync(self, agent_id: str, presented_key: str) -> bool:
        if not presented_key:
            return False

        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if record is None or not record.agent_key_hash:
                return False
            # Agent key defaults to non-expiring. key_issued_at is audit metadata only.
            return hmac.compare_digest(record.agent_key_hash, _hash_agent_key(presented_key))

    def _disconnect_agent_sync(self, agent_id: str) -> None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if record is None:
                return

            record.status = "offline"
            record.last_disconnect_at = now
            record.updated_at = now
            session.commit()

    def _touch_agent_sync(self, agent_id: str, event_type: str) -> None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            if record is None:
                record = AgentModel(
                    agent_id=agent_id,
                    status="online",
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)

            record.last_seen_at = now
            record.updated_at = now
            if event_type == "heartbeat":
                record.last_heartbeat_at = now
            elif event_type == "pong":
                record.last_pong_at = now
            session.commit()

    def _store_command_sync(
        self,
        agent_id: str,
        payload: dict[str, Any],
        original_request_id: str | None,
        retry_count: int,
        requested_by: str | None,
        request_source: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.session_factory() as session:
            record = CommandModel(
                request_id=payload["requestId"],
                agent_id=agent_id,
                action=payload["action"],
                target_dir=payload["dir"],
                target_image=payload.get("image"),
                status="queued",
                original_request_id=original_request_id,
                retry_count=retry_count,
                requested_by=requested_by,
                request_source=request_source,
                payload_json=json.dumps(payload, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.add(
                CommandEventModel(
                    request_id=payload["requestId"],
                    event_type="created",
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    created_at=now,
                )
            )
            session.commit()
            return command_to_dict(record)

    def _retry_command_sync(
        self,
        request_id: str,
        requested_by: str | None,
        request_source: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(CommandModel).where(CommandModel.request_id == request_id))
            if record is None:
                return None

            payload = _loads_payload(record.payload_json)
            new_request_id = str(uuid4())
            retry_payload = {
                "type": "command",
                "requestId": new_request_id,
                "action": record.action,
                "dir": record.target_dir,
            }
            if record.target_image:
                retry_payload["image"] = record.target_image

            retry_record = CommandModel(
                request_id=new_request_id,
                agent_id=record.agent_id,
                action=record.action,
                target_dir=record.target_dir,
                target_image=record.target_image,
                status="queued",
                original_request_id=record.request_id,
                retry_count=record.retry_count + 1,
                requested_by=requested_by,
                request_source=request_source,
                payload_json=json.dumps(retry_payload, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
            session.add(retry_record)
            session.add(
                CommandEventModel(
                    request_id=new_request_id,
                    event_type="created",
                    payload_json=json.dumps(retry_payload, ensure_ascii=False),
                    created_at=now,
                )
            )
            session.add(
                CommandEventModel(
                    request_id=record.request_id,
                    event_type="retry",
                    payload_json=json.dumps(
                        {
                            "newRequestId": new_request_id,
                            "requestedBy": requested_by,
                            "requestSource": request_source,
                            "retryCount": retry_record.retry_count,
                            "payload": payload,
                        },
                        ensure_ascii=False,
                    ),
                    created_at=now,
                )
            )
            session.commit()
            return command_to_dict(record), command_to_dict(retry_record)

    def _mark_ack_sync(self, request_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(CommandModel).where(CommandModel.request_id == request_id))
            if record is None:
                return None

            record.status = "processing"
            record.ack_at = now
            record.updated_at = now
            session.add(
                CommandEventModel(
                    request_id=request_id,
                    event_type="ack",
                    payload_json=json.dumps({"status": "processing"}, ensure_ascii=False),
                    created_at=now,
                )
            )
            session.commit()
            return command_to_dict(record)

    def _mark_result_sync(
        self,
        request_id: str,
        status: str,
        output: str | None,
        message: str | None,
        error: str | None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.database.session_factory() as session:
            record = session.scalar(select(CommandModel).where(CommandModel.request_id == request_id))
            if record is None:
                return None

            record.status = status
            record.output = output
            record.message = message
            record.error = error
            record.result_at = now
            record.updated_at = now
            session.add(
                CommandEventModel(
                    request_id=request_id,
                    event_type="result",
                    payload_json=json.dumps(
                        {
                            "status": status,
                            "output": output,
                            "message": message,
                            "error": error,
                        },
                        ensure_ascii=False,
                    ),
                    created_at=now,
                )
            )
            session.commit()
            return command_to_dict(record)

    def _get_command_sync(self, request_id: str) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            record = session.scalar(select(CommandModel).where(CommandModel.request_id == request_id))
            return command_to_dict(record) if record is not None else None

    def _list_commands_sync(
        self,
        agent_id: str | None,
        status: str | None,
        action: str | None,
        requested_by: str | None,
        request_source: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        sort_by: str,
        order: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            statement = _apply_command_filters(
                select(CommandModel),
                agent_id=agent_id,
                status=status,
                action=action,
                requested_by=requested_by,
                request_source=request_source,
                created_after=created_after,
                created_before=created_before,
            )
            total_statement = _apply_command_filters(
                select(func.count()).select_from(CommandModel),
                agent_id=agent_id,
                status=status,
                action=action,
                requested_by=requested_by,
                request_source=request_source,
                created_after=created_after,
                created_before=created_before,
            )

            total = int(session.scalar(total_statement) or 0)
            sort_column = CommandModel.updated_at if sort_by == "updatedAt" else CommandModel.created_at
            sort_method = sort_column.asc if order == "asc" else sort_column.desc
            tie_breaker = CommandModel.id.asc() if order == "asc" else CommandModel.id.desc()
            statement = statement.order_by(sort_method(), tie_breaker).offset(offset).limit(limit)
            records = session.scalars(statement).all()
            items = [command_to_dict(record) for record in records]
            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(items) < total,
                "sort_by": sort_by,
                "order": order,
            }

    def _list_command_events_sync(self, request_id: str) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            statement = (
                select(CommandEventModel)
                .where(CommandEventModel.request_id == request_id)
                .order_by(CommandEventModel.created_at.asc(), CommandEventModel.id.asc())
            )
            records = session.scalars(statement).all()
            return [command_event_to_dict(record) for record in records]

    def _list_agents_sync(self) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            records = session.scalars(select(AgentModel).order_by(AgentModel.agent_id.asc())).all()
            return [_agent_to_dict(record) for record in records]

    def _get_agent_sync(self, agent_id: str) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            record = session.scalar(select(AgentModel).where(AgentModel.agent_id == agent_id))
            return _agent_to_dict(record) if record is not None else None
