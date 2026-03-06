import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Header, Path, Query, WebSocket, WebSocketDisconnect, status

from app.config import settings
from app.db import Database
from app.models import AgentCredentialResponse, AgentProvisionRequest, AgentProvisionResponse, AgentSnapshot, CommandDispatchRequest, CommandDispatchResponse, CommandEventSnapshot, CommandListResponse, CommandSnapshot
from app.store import HubState


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _localize_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                if not isinstance(response, dict):
                    continue
                if response.get("description") == "Successful Response":
                    response["description"] = "请求成功"
                elif response.get("description") == "Validation Error":
                    response["description"] = "请求校验失败"

    schemas = schema.get("components", {}).get("schemas", {})
    response_title_map = {
        "Response Health Health Get": "健康检查响应",
        "Response List Agents Api Agents Get": "Agent 列表响应",
        "Response Get Command Events Api Commands  Request Id  Events Get": "命令事件列表响应",
    }
    for schema_def in schemas.values():
        if not isinstance(schema_def, dict):
            continue
        title = schema_def.get("title")
        if title in response_title_map:
            schema_def["title"] = response_title_map[title]

    if "HTTPValidationError" in schemas:
        schemas["HTTPValidationError"]["title"] = "HTTP 请求校验错误"
        detail = schemas["HTTPValidationError"].get("properties", {}).get("detail")
        if isinstance(detail, dict):
            detail["title"] = "错误详情"

    if "ValidationError" in schemas:
        schemas["ValidationError"]["title"] = "字段校验错误"
        properties = schemas["ValidationError"].get("properties", {})
        title_map = {
            "loc": "错误位置",
            "msg": "错误消息",
            "type": "错误类型",
        }
        for key, localized_title in title_map.items():
            if key in properties and isinstance(properties[key], dict):
                properties[key]["title"] = localized_title

    return schema


@asynccontextmanager
async def lifespan(_: FastAPI):
    await hub_state.initialize()
    yield

app = FastAPI(
    title="service-hub 接口文档",
    version="0.1.0",
    description="Service Hub 的 Agent 管理、命令下发与审计查询接口。",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "系统", "description": "系统健康检查接口。"},
        {"name": "Agent 管理", "description": "Agent 查询、创建与密钥管理接口。"},
        {"name": "命令管理", "description": "命令查询、下发、重试与审计接口。"},
    ],
)
original_openapi = app.openapi


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = _localize_openapi(original_openapi())
    return app.openapi_schema


app.openapi = custom_openapi
database = Database(settings.database_url)
hub_state = HubState(
    heartbeat_timeout=settings.heartbeat_timeout,
    command_history_limit=settings.command_history_limit,
    database=database,
)


def _remote_address(websocket: WebSocket) -> str | None:
    if websocket.client is None:
        return None
    return f"{websocket.client.host}:{websocket.client.port}"


def _require_admin_token(admin_token: str | None) -> None:
    if admin_token != settings.admin_token:
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
    result = await hub_state.list_commands(
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
    record = await hub_state.get_command(request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    return CommandSnapshot.model_validate(record)


async def _handle_agent_message(agent_id: str, payload: dict[str, Any]) -> None:
    msg_type = payload.get("type")
    if not msg_type:
        logger.warning("Agent %s sent message without type: %s", agent_id, payload)
        return

    await hub_state.touch_agent(agent_id, msg_type)

    if msg_type == "heartbeat":
        return

    if msg_type == "ack":
        request_id = payload.get("requestId")
        if request_id:
            await hub_state.mark_ack(request_id)
        return

    if msg_type == "result":
        request_id = payload.get("requestId")
        if request_id:
            await hub_state.mark_result(
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


@app.get("/health", summary="健康检查", description="检查服务与数据库连通性。", tags=["系统"])
async def health() -> dict[str, str]:
    await hub_state.check_database()
    return {"status": "ok"}


@app.get("/api/agents", response_model=list[AgentSnapshot], summary="查询全部 Agent", description="返回当前所有 Agent 的最新连接状态与在线状态。", tags=["Agent 管理"])
async def list_agents() -> list[AgentSnapshot]:
    agents = await hub_state.list_agents()
    return [AgentSnapshot.model_validate(agent) for agent in agents]


@app.get("/api/agents/{agent_id}", response_model=AgentSnapshot, summary="查询单个 Agent", description="根据 Agent 标识查询其最新状态。", tags=["Agent 管理"])
async def get_agent(agent_id: str = Path(title="Agent 标识", description="要查询的 Agent 唯一标识。")) -> AgentSnapshot:
    agent = await hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentSnapshot.model_validate(agent)


@app.post("/api/agents", response_model=AgentProvisionResponse, status_code=status.HTTP_201_CREATED, summary="创建 Agent 并签发初始密钥", description="创建新的 Agent 记录，并返回该 Agent 的首次连接密钥。", tags=["Agent 管理"])
async def provision_agent(
    request: AgentProvisionRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="Hub 管理令牌，值来自环境变量 ADMIN_TOKEN。"),
) -> AgentProvisionResponse:
    _require_admin_token(admin_token)
    provisioned = await hub_state.provision_agent(request.agent_id)
    if provisioned is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent already exists")

    return AgentProvisionResponse(
        agent=AgentSnapshot.model_validate(provisioned["agent"]),
        agent_key=provisioned["agent_key"],
        issued_at=provisioned["issued_at"],
    )


@app.post("/api/agents/{agent_id}/credentials/rotate", response_model=AgentCredentialResponse, summary="轮换 Agent 密钥", description="为指定 Agent 重新签发连接密钥。", tags=["Agent 管理"])
async def rotate_agent_credentials(
    agent_id: str = Path(title="Agent 标识", description="要轮换密钥的 Agent 唯一标识。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="Hub 管理令牌，值来自环境变量 ADMIN_TOKEN。"),
) -> AgentCredentialResponse:
    _require_admin_token(admin_token)
    credential = await hub_state.rotate_agent_key(agent_id)
    return AgentCredentialResponse.model_validate(credential)


@app.get("/api/agents/{agent_id}/commands", response_model=CommandListResponse, summary="查询 Agent 命令历史", description="按 Agent 维度分页查询命令历史，支持状态、动作、审计字段和时间范围筛选。", tags=["命令管理"])
async def list_agent_commands(
    agent_id: str = Path(title="Agent 标识", description="要查询命令历史的 Agent 唯一标识。"),
    status_filter: str | None = Query(default=None, alias="status", title="状态", description="按命令状态筛选。"),
    action: str | None = Query(default=None, title="动作", description="按命令动作筛选，例如 update 或 restart。"),
    requested_by: str | None = Query(default=None, alias="requestedBy", title="请求发起方", description="按请求发起方筛选。"),
    request_source: str | None = Query(default=None, alias="requestSource", title="请求来源", description="按请求来源筛选。"),
    created_after: datetime | None = Query(default=None, alias="createdAfter", title="起始时间", description="只返回创建时间大于等于该时间的命令。"),
    created_before: datetime | None = Query(default=None, alias="createdBefore", title="结束时间", description="只返回创建时间小于等于该时间的命令。"),
    sort_by: str = Query(default="createdAt", alias="sortBy", pattern="^(createdAt|updatedAt)$", title="排序字段", description="支持 createdAt 或 updatedAt。"),
    order: str = Query(default="desc", pattern="^(asc|desc)$", title="排序方向", description="支持 asc 或 desc。"),
    limit: int = Query(default=100, ge=1, le=500, title="分页大小", description="单次返回的最大记录数。"),
    offset: int = Query(default=0, ge=0, title="偏移量", description="分页偏移量。"),
) -> CommandListResponse:
    return await _build_command_list_response(
        agent_id=agent_id,
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


@app.get("/api/commands", response_model=CommandListResponse, summary="查询全局命令列表", description="分页查询所有 Agent 的命令历史，支持多条件筛选与排序。", tags=["命令管理"])
async def list_commands(
    agent_id: str | None = Query(default=None, alias="agentId", title="Agent 标识", description="按 Agent 标识筛选。"),
    status_filter: str | None = Query(default=None, alias="status", title="状态", description="按命令状态筛选。"),
    action: str | None = Query(default=None, title="动作", description="按命令动作筛选。"),
    requested_by: str | None = Query(default=None, alias="requestedBy", title="请求发起方", description="按请求发起方筛选。"),
    request_source: str | None = Query(default=None, alias="requestSource", title="请求来源", description="按请求来源筛选。"),
    created_after: datetime | None = Query(default=None, alias="createdAfter", title="起始时间", description="只返回创建时间大于等于该时间的命令。"),
    created_before: datetime | None = Query(default=None, alias="createdBefore", title="结束时间", description="只返回创建时间小于等于该时间的命令。"),
    sort_by: str = Query(default="createdAt", alias="sortBy", pattern="^(createdAt|updatedAt)$", title="排序字段", description="支持 createdAt 或 updatedAt。"),
    order: str = Query(default="desc", pattern="^(asc|desc)$", title="排序方向", description="支持 asc 或 desc。"),
    limit: int = Query(default=100, ge=1, le=500, title="分页大小", description="单次返回的最大记录数。"),
    offset: int = Query(default=0, ge=0, title="偏移量", description="分页偏移量。"),
) -> CommandListResponse:
    return await _build_command_list_response(
        agent_id=agent_id,
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


@app.get("/api/commands/{request_id}", response_model=CommandSnapshot, summary="查询单条命令", description="根据请求 ID 查询单条命令的最新状态。", tags=["命令管理"])
async def get_command(request_id: str = Path(title="请求 ID", description="要查询的命令请求 ID。")) -> CommandSnapshot:
    return await _serialize_command(request_id)


@app.get("/api/commands/{request_id}/events", response_model=list[CommandEventSnapshot], summary="查询命令事件", description="查询命令的完整审计事件流。", tags=["命令管理"])
async def get_command_events(request_id: str = Path(title="请求 ID", description="要查询事件流的命令请求 ID。")) -> list[CommandEventSnapshot]:
    command = await hub_state.get_command(request_id)
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    events = await hub_state.list_command_events(request_id)
    return [CommandEventSnapshot.model_validate(item) for item in events]


@app.post("/api/agents/{agent_id}/commands", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="下发命令", description="向指定 Agent 下发 update 或 restart 命令。", tags=["命令管理"])
async def dispatch_command(
    request: CommandDispatchRequest,
    agent_id: str = Path(title="Agent 标识", description="要接收命令的 Agent 唯一标识。"),
    requested_by: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方", description="调用该接口的系统或用户标识。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    agent = await hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    payload = {
        "type": "command",
        "requestId": request.request_id,
        "action": request.action,
        "dir": request.dir,
    }
    if request.image:
        payload["image"] = request.image

    await hub_state.store_command(
        agent_id,
        payload,
        requested_by=requested_by,
        request_source=request_source,
    )

    websocket = await hub_state.get_connection(agent_id)
    if websocket is None:
        await hub_state.mark_result(request.request_id, "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(payload)
        logger.info("Dispatched command %s to agent %s", request.request_id, agent_id)
    except Exception as exc:
        logger.exception("Failed to dispatch command %s to agent %s", request.request_id, agent_id)
        await hub_state.mark_result(request.request_id, "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    command = await _serialize_command(request.request_id)
    return CommandDispatchResponse(accepted=True, command=command)


@app.post("/api/commands/{request_id}/retry", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="重试失败命令", description="重新下发一条失败命令，并生成新的请求 ID。", tags=["命令管理"])
async def retry_command(
    request_id: str = Path(title="请求 ID", description="要重试的失败命令请求 ID。"),
    requested_by: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方", description="调用该接口的系统或用户标识。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    original = await hub_state.get_command(request_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    if original["status"] != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed commands can be retried")

    agent = await hub_state.get_agent(original["agent_id"])
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    retried = await hub_state.retry_command(
        request_id,
        requested_by=requested_by,
        request_source=request_source,
    )
    if retried is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    _, retry_record = retried
    websocket = await hub_state.get_connection(retry_record["agent_id"])
    if websocket is None:
        await hub_state.mark_result(retry_record["request_id"], "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(retry_record["payload"])
        logger.info("Retried command %s as %s for agent %s", request_id, retry_record["request_id"], retry_record["agent_id"])
    except Exception as exc:
        logger.exception("Failed to retry command %s for agent %s", request_id, retry_record["agent_id"])
        await hub_state.mark_result(retry_record["request_id"], "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    return CommandDispatchResponse(accepted=True, command=await _serialize_command(retry_record["request_id"]))


@app.websocket("/ws/agent/{agent_id}")
async def agent_ws(websocket: WebSocket, agent_id: str) -> None:
    presented_key = websocket.query_params.get("key", "")
    if not await hub_state.authenticate_agent(agent_id, presented_key):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("Rejected agent %s due to invalid credentials", agent_id)
        return

    await websocket.accept()
    await hub_state.register_agent(agent_id, websocket, _remote_address(websocket))
    logger.info("Agent %s connected", agent_id)

    try:
        while True:
            message = await websocket.receive()
            if "text" in message and message["text"] is not None:
                payload = json.loads(message["text"])
                if isinstance(payload, dict):
                    await _handle_agent_message(agent_id, payload)
                else:
                    logger.warning("Agent %s sent non-object payload", agent_id)
            elif message.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        logger.info("Agent %s disconnected", agent_id)
    except json.JSONDecodeError:
        logger.warning("Agent %s sent invalid JSON", agent_id)
    except Exception:
        logger.exception("Agent %s websocket loop failed", agent_id)
    finally:
        await hub_state.disconnect_agent(agent_id, websocket)
