from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


MODEL_CONFIG = ConfigDict(
    alias_generator=to_camel,
    populate_by_name=True,
    serialize_by_alias=True,
)


def titled_model_config(title: str) -> ConfigDict:
    return ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        title=title,
    )


class CommandDispatchRequest(BaseModel):
    model_config = ConfigDict(title="下发命令请求")

    requestId: str = Field(default_factory=lambda: str(uuid4()), title="请求 ID")
    action: Literal["update", "restart"] = Field(title="动作")
    dir: str = Field(title="目标目录")
    image: str | None = Field(default=None, title="目标镜像")

    @property
    def request_id(self) -> str:
        return self.requestId

    @model_validator(mode="after")
    def validate_image(self) -> "CommandDispatchRequest":
        if self.action == "update" and not self.image:
            raise ValueError("Action 'update' requires the 'image' field")
        return self


class AgentSnapshot(BaseModel):
    model_config = titled_model_config("Agent 状态快照")

    agent_id: str = Field(title="Agent 标识")
    connected: bool = Field(title="是否已连接")
    online: bool = Field(title="是否在线")
    credential_configured: bool = Field(title="是否已配置凭据")
    remote: str | None = Field(default=None, title="远端地址")
    key_issued_at: datetime | None = Field(default=None, title="密钥签发时间", description="最近一次签发时间，仅用于审计，不代表过期时间。")
    connected_at: datetime | None = Field(default=None, title="连接时间")
    disconnected_at: datetime | None = Field(default=None, title="断开时间")
    last_seen_at: datetime | None = Field(default=None, title="最后活跃时间")
    last_heartbeat_at: datetime | None = Field(default=None, title="最后心跳时间")
    last_pong_at: datetime | None = Field(default=None, title="最后 Pong 时间")
    stale_after_seconds: int = Field(title="离线判定秒数")
    queued_commands: int = Field(default=0, title="排队中的命令数")
    processing_commands: int = Field(default=0, title="执行中的命令数")
    last_command_created_at: datetime | None = Field(default=None, title="最近命令创建时间")


class AgentCredentialResponse(BaseModel):
    model_config = titled_model_config("Agent 凭据响应")

    agent_id: str = Field(title="Agent 标识")
    agent_key: str = Field(title="Agent 密钥")
    issued_at: datetime = Field(title="签发时间", description="签发时间，仅用于审计；该密钥不会按时间自动过期，直到被轮换。")
    created: bool = Field(title="是否为首次创建")


class AgentProvisionRequest(BaseModel):
    model_config = ConfigDict(title="创建 Agent 请求")

    agentId: str = Field(title="Agent 标识")

    @property
    def agent_id(self) -> str:
        return self.agentId


class AgentProvisionResponse(BaseModel):
    model_config = titled_model_config("创建 Agent 响应")

    agent: AgentSnapshot = Field(title="Agent 信息")
    agent_key: str = Field(title="Agent 密钥")
    issued_at: datetime = Field(title="签发时间", description="签发时间，仅用于审计；该密钥不会按时间自动过期，直到被轮换。")


class CommandSnapshot(BaseModel):
    model_config = titled_model_config("命令快照")

    request_id: str = Field(title="请求 ID")
    agent_id: str = Field(title="Agent 标识")
    status: str = Field(title="状态")
    action: str = Field(title="动作")
    dir: str = Field(title="目标目录")
    image: str | None = Field(default=None, title="目标镜像")
    original_request_id: str | None = Field(default=None, title="原始请求 ID")
    retry_count: int = Field(default=0, title="重试次数")
    requested_by: str | None = Field(default=None, title="请求发起方")
    request_source: str | None = Field(default=None, title="请求来源")
    payload: dict[str, Any] = Field(title="原始负载")
    output: str | None = Field(default=None, title="执行输出")
    message: str | None = Field(default=None, title="结果消息")
    error: str | None = Field(default=None, title="错误信息")
    created_at: datetime = Field(title="创建时间")
    updated_at: datetime = Field(title="更新时间")
    ack_at: datetime | None = Field(default=None, title="确认时间")
    result_at: datetime | None = Field(default=None, title="结果时间")


class CommandEventSnapshot(BaseModel):
    model_config = titled_model_config("命令事件")

    id: int = Field(title="事件 ID")
    request_id: str = Field(title="请求 ID")
    event_type: str = Field(title="事件类型")
    payload: dict[str, Any] = Field(title="事件负载")
    created_at: datetime = Field(title="创建时间")


class CommandListResponse(BaseModel):
    model_config = titled_model_config("命令列表响应")

    items: list[CommandSnapshot] = Field(title="命令列表")
    total: int = Field(title="总数")
    limit: int = Field(title="分页大小")
    offset: int = Field(title="偏移量")
    has_more: bool = Field(title="是否还有更多")
    sort_by: Literal["createdAt", "updatedAt"] = Field(title="排序字段")
    order: Literal["asc", "desc"] = Field(title="排序方向")


class CommandDispatchResponse(BaseModel):
    model_config = titled_model_config("命令下发响应")

    accepted: bool = Field(title="是否已受理")
    command: CommandSnapshot = Field(title="命令详情")
