from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Path, Query, WebSocket, status
from pydantic import Field, TypeAdapter, ValidationError

from app.models import CommandEventSnapshot, CommandListResponse, CommandSnapshot


logger = logging.getLogger(__name__)

SORT_BY_PATTERN = "^(createdAt|updatedAt)$"
ORDER_PATTERN = "^(asc|desc)$"
DATETIME_QUERY_ADAPTER = TypeAdapter(datetime)
SORT_BY_QUERY_ADAPTER = TypeAdapter(Annotated[str, Field(pattern=SORT_BY_PATTERN)])
ORDER_QUERY_ADAPTER = TypeAdapter(Annotated[str, Field(pattern=ORDER_PATTERN)])
LIMIT_QUERY_ADAPTER = TypeAdapter(Annotated[int, Field(ge=1, le=500)])
OFFSET_QUERY_ADAPTER = TypeAdapter(Annotated[int, Field(ge=0)])


def _normalize_query_value(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value


def _query_error_detail(field_name: str, value: str, errors: list[Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for error in errors:
        detail = dict(error)
        detail["loc"] = ["query", field_name]
        detail["input"] = value
        details.append(detail)
    return details


def _parse_query_with_adapter(
    *,
    field_name: str,
    value: str | None,
    adapter: TypeAdapter[Any],
    default: Any,
    errors: list[dict[str, Any]],
) -> Any:
    normalized = _normalize_query_value(value)
    if normalized is None:
        return default

    try:
        return adapter.validate_python(normalized)
    except ValidationError as exc:
        errors.extend(_query_error_detail(field_name, normalized, exc.errors()))
        return default


def _parse_command_list_query(
    *,
    agent_id: str | None,
    status_filter: str | None,
    action: str | None,
    requested_by: str | None,
    request_source: str | None,
    created_after: str | None,
    created_before: str | None,
    sort_by: str | None,
    order: str | None,
    limit: str | None,
    offset: str | None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    parsed = {
        "agent_id": _normalize_query_value(agent_id),
        "status_filter": _normalize_query_value(status_filter),
        "action": _normalize_query_value(action),
        "requested_by": _normalize_query_value(requested_by),
        "request_source": _normalize_query_value(request_source),
        "created_after": _parse_query_with_adapter(
            field_name="createdAfter",
            value=created_after,
            adapter=DATETIME_QUERY_ADAPTER,
            default=None,
            errors=errors,
        ),
        "created_before": _parse_query_with_adapter(
            field_name="createdBefore",
            value=created_before,
            adapter=DATETIME_QUERY_ADAPTER,
            default=None,
            errors=errors,
        ),
        "sort_by": _parse_query_with_adapter(
            field_name="sortBy",
            value=sort_by,
            adapter=SORT_BY_QUERY_ADAPTER,
            default="createdAt",
            errors=errors,
        ),
        "order": _parse_query_with_adapter(
            field_name="order",
            value=order,
            adapter=ORDER_QUERY_ADAPTER,
            default="desc",
            errors=errors,
        ),
        "limit": _parse_query_with_adapter(
            field_name="limit",
            value=limit,
            adapter=LIMIT_QUERY_ADAPTER,
            default=100,
            errors=errors,
        ),
        "offset": _parse_query_with_adapter(
            field_name="offset",
            value=offset,
            adapter=OFFSET_QUERY_ADAPTER,
            default=0,
            errors=errors,
        ),
    }

    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    return parsed


def _command_list_query_dependency(
    agent_id_filter: str | None = Query(default=None, alias="agentId", title="Agent 标识", description="按 Agent 标识筛选。"),
    status_filter: str | None = Query(default=None, alias="status", title="状态", description="按命令状态筛选。"),
    action: str | None = Query(default=None, title="动作", description="按命令动作筛选。"),
    requested_by: str | None = Query(default=None, alias="requestedBy", title="请求发起方", description="按请求发起方筛选。"),
    request_source: str | None = Query(default=None, alias="requestSource", title="请求来源", description="按请求来源筛选。"),
    created_after: str | None = Query(default=None, alias="createdAfter", title="起始时间", description="只返回创建时间大于等于该时间的命令。"),
    created_before: str | None = Query(default=None, alias="createdBefore", title="结束时间", description="只返回创建时间小于等于该时间的命令。"),
    sort_by: str | None = Query(default="createdAt", alias="sortBy", title="排序字段", description="支持 createdAt 或 updatedAt。"),
    order: str | None = Query(default="desc", title="排序方向", description="支持 asc 或 desc。"),
    limit: str | None = Query(default="100", title="分页大小", description="单次返回的最大记录数。"),
    offset: str | None = Query(default="0", title="偏移量", description="分页偏移量。"),
) -> dict[str, Any]:
    return _parse_command_list_query(
        agent_id=agent_id_filter,
        status_filter=status_filter,
        action=action,
        requested_by=requested_by,
        request_source=request_source,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        order=order,
        limit=limit,
        offset=offset,
    )


def _remote_address(websocket: WebSocket) -> str | None:
    if websocket.client is None:
        return None
    return f"{websocket.client.host}:{websocket.client.port}"


def _require_admin_token(admin_token: str | None) -> None:
    import app.main as main_module

    if not main_module.settings.admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Admin token is not configured")
    if admin_token != main_module.settings.admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin token")


async def _build_command_list_response(
    *,
    agent_id: str | None,
    status_filter: str | None,
    action: str | None,
    requested_by: str | None,
    request_source: str | None,
    created_after: datetime | None,
    created_before: datetime | None,
    sort_by: str,
    order: str,
    limit: int,
    offset: int,
) -> CommandListResponse:
    import app.main as main_module

    result = await main_module.hub_state.list_commands(
        agent_id=agent_id,
        status=status_filter,
        action=action,
        requested_by=requested_by,
        request_source=request_source,
        created_after=created_after,
        created_before=created_before,
        sort_by=sort_by,
        order=order,
        limit=limit,
        offset=offset,
    )
    return CommandListResponse(
        items=[CommandSnapshot.model_validate(item) for item in result["items"]],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
        has_more=result["has_more"],
        sort_by=result["sort_by"],
        order=result["order"],
    )


async def _serialize_command(request_id: str) -> CommandSnapshot:
    import app.main as main_module

    record = await main_module.hub_state.get_command(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    return CommandSnapshot.model_validate(record)


async def _handle_agent_message(agent_id: str, payload: dict[str, Any]) -> None:
    import app.main as main_module

    msg_type = payload.get("type")
    if not msg_type:
        logger.warning("Agent %s sent message without type: %s", agent_id, payload)
        return

    await main_module.hub_state.touch_agent(agent_id, msg_type)

    if msg_type == "heartbeat":
        return

    if msg_type == "ack":
        request_id = payload.get("requestId")
        if request_id:
            await main_module.hub_state.mark_ack(request_id)
        return

    if msg_type == "result":
        request_id = payload.get("requestId")
        if request_id:
            await main_module.hub_state.mark_result(
                request_id,
                payload.get("status", "failed"),
                output=payload.get("output"),
                message=payload.get("message"),
                error=payload.get("error"),
            )
        return

    if msg_type == "pong":
        return

    logger.info("Unhandled message type from %s: %s", agent_id, msg_type)


async def get_command_events_response(
    request_id: str = Path(title="请求 ID", description="要查询事件流的命令请求 ID。"),
) -> list[CommandEventSnapshot]:
    import app.main as main_module

    command = await main_module.hub_state.get_command(request_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    events = await main_module.hub_state.list_command_events(request_id)
    return [CommandEventSnapshot.model_validate(item) for item in events]