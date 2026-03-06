from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status

from app.api_support import _build_command_list_response, _command_list_query_dependency, _serialize_command, get_command_events_response
from app.models import CommandDispatchRequest, CommandDispatchResponse, CommandEventSnapshot, CommandListResponse, CommandSnapshot


router = APIRouter(tags=["命令管理"])


@router.get("/api/commands", response_model=CommandListResponse, summary="查询全局命令列表", description="分页查询所有 Agent 的命令历史，支持多条件筛选与排序。")
async def list_commands(query: dict[str, Any] = Depends(_command_list_query_dependency)) -> CommandListResponse:
    return await _build_command_list_response(**query)


@router.get("/api/commands/{request_id}", response_model=CommandSnapshot, summary="查询单条命令", description="根据请求 ID 查询单条命令的最新状态。")
async def get_command(request_id: str = Path(title="请求 ID", description="要查询的命令请求 ID。")) -> CommandSnapshot:
    return await _serialize_command(request_id)


@router.get("/api/commands/{request_id}/events", response_model=list[CommandEventSnapshot], summary="查询命令事件", description="查询命令的完整审计事件流。")
async def get_command_events(request_id: str = Path(title="请求 ID", description="要查询事件流的命令请求 ID。")) -> list[CommandEventSnapshot]:
    return await get_command_events_response(request_id)


@router.post("/api/agents/{agent_id}/commands", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="下发命令", description="向指定 Agent 下发 update 或 restart 命令。")
async def dispatch_command(
    request: CommandDispatchRequest,
    agent_id: str = Path(title="Agent 标识", description="要接收命令的 Agent 唯一标识。"),
    requested_by: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方", description="调用该接口的系统或用户标识。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    import app.main as main_module

    agent = await main_module.hub_state.get_agent(agent_id)
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

    await main_module.hub_state.store_command(
        agent_id,
        payload,
        requested_by=requested_by,
        request_source=request_source,
    )

    websocket = await main_module.hub_state.get_connection(agent_id)
    if websocket is None:
        await main_module.hub_state.mark_result(request.request_id, "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(payload)
        main_module.logger.info("Dispatched command %s to agent %s", request.request_id, agent_id)
    except Exception as exc:
        main_module.logger.exception("Failed to dispatch command %s to agent %s", request.request_id, agent_id)
        await main_module.hub_state.mark_result(request.request_id, "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    command = await _serialize_command(request.request_id)
    return CommandDispatchResponse(accepted=True, command=command)


@router.post("/api/commands/{request_id}/retry", response_model=CommandDispatchResponse, status_code=status.HTTP_202_ACCEPTED, summary="重试失败命令", description="重新下发一条失败命令，并生成新的请求 ID。")
async def retry_command(
    request_id: str = Path(title="请求 ID", description="要重试的失败命令请求 ID。"),
    requested_by: str | None = Header(default=None, alias="X-Requested-By", title="请求发起方", description="调用该接口的系统或用户标识。"),
    request_source: str | None = Header(default=None, alias="X-Requested-Source", title="请求来源", description="调用来源，例如控制台、调度器。"),
) -> CommandDispatchResponse:
    import app.main as main_module

    original = await main_module.hub_state.get_command(request_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    if original["status"] != "failed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed commands can be retried")

    agent = await main_module.hub_state.get_agent(original["agent_id"])
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is offline")

    retried = await main_module.hub_state.retry_command(
        request_id,
        requested_by=requested_by,
        request_source=request_source,
    )
    if retried is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")

    _, retry_record = retried
    websocket = await main_module.hub_state.get_connection(retry_record["agent_id"])
    if websocket is None:
        await main_module.hub_state.mark_result(retry_record["request_id"], "failed", error="Agent connection is unavailable")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent connection is unavailable")

    try:
        await websocket.send_json(retry_record["payload"])
        main_module.logger.info("Retried command %s as %s for agent %s", request_id, retry_record["request_id"], retry_record["agent_id"])
    except Exception as exc:
        main_module.logger.exception("Failed to retry command %s for agent %s", request_id, retry_record["agent_id"])
        await main_module.hub_state.mark_result(retry_record["request_id"], "failed", error=f"Failed to dispatch command: {exc}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to dispatch command") from exc

    return CommandDispatchResponse(accepted=True, command=await _serialize_command(retry_record["request_id"]))