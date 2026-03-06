from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI

from app.api_support import _command_list_query_dependency, _handle_agent_message, _remote_address, _require_admin_token, _serialize_command
from app.config import settings
from app.db import Database
from app.routers.agent_ws import agent_ws, router as agent_ws_router
from app.routers.agents import get_agent, list_agents, provision_agent, rotate_agent_credentials, router as agents_router
from app.routers.commands import dispatch_command, get_command, get_command_events, list_commands, retry_command, router as commands_router
from app.routers.system import health, router as system_router
from app.store import HubState


CHINA_TZ = timezone(timedelta(hours=8))


class ChinaTimeFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, CHINA_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
for handler in logging.getLogger().handlers:
    handler.setFormatter(ChinaTimeFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logger = logging.getLogger(__name__)

database = Database(settings.database_url)
hub_state = HubState(
    heartbeat_timeout=settings.heartbeat_timeout,
    command_history_limit=settings.command_history_limit,
    database=database,
)


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
app.include_router(system_router)
app.include_router(agents_router)
app.include_router(commands_router)
app.include_router(agent_ws_router)