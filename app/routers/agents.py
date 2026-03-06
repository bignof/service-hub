from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Path, status

from app.api_support import _require_admin_token
from app.models import AgentCredentialResponse, AgentProvisionRequest, AgentProvisionResponse, AgentSnapshot


router = APIRouter()


@router.get("/api/agents", response_model=list[AgentSnapshot], summary="查询全部 Agent", description="返回当前所有 Agent 的最新连接状态与在线状态。", tags=["Agent 管理"])
async def list_agents() -> list[AgentSnapshot]:
    import app.main as main_module

    agents = await main_module.hub_state.list_agents()
    return [AgentSnapshot.model_validate(agent) for agent in agents]


@router.get("/api/agents/{agent_id}", response_model=AgentSnapshot, summary="查询单个 Agent", description="根据 Agent 标识查询其最新状态。", tags=["Agent 管理"])
async def get_agent(agent_id: str = Path(title="Agent 标识", description="要查询的 Agent 唯一标识。")) -> AgentSnapshot:
    import app.main as main_module

    agent = await main_module.hub_state.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentSnapshot.model_validate(agent)


@router.post("/api/agents", response_model=AgentProvisionResponse, status_code=status.HTTP_201_CREATED, summary="创建 Agent 并签发初始密钥", description="创建新的 Agent 记录，并返回该 Agent 的首次连接密钥。密钥默认长期有效，直到显式轮换。", tags=["Agent 管理"])
async def provision_agent(
    request: AgentProvisionRequest,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="Hub 管理令牌，值来自环境变量 ADMIN_TOKEN。"),
) -> AgentProvisionResponse:
    import app.main as main_module

    _require_admin_token(admin_token)
    provisioned = await main_module.hub_state.provision_agent(request.agent_id)
    if provisioned is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent already exists")

    return AgentProvisionResponse(
        agent=AgentSnapshot.model_validate(provisioned["agent"]),
        agent_key=provisioned["agent_key"],
        issued_at=provisioned["issued_at"],
    )


@router.post("/api/agents/{agent_id}/credentials/rotate", response_model=AgentCredentialResponse, summary="轮换 Agent 密钥", description="为指定 Agent 重新签发连接密钥。新密钥默认长期有效，旧密钥在轮换后立即失效。", tags=["Agent 管理"])
async def rotate_agent_credentials(
    agent_id: str = Path(title="Agent 标识", description="要轮换密钥的 Agent 唯一标识。"),
    admin_token: str | None = Header(default=None, alias="X-Admin-Token", title="管理令牌", description="Hub 管理令牌，值来自环境变量 ADMIN_TOKEN。"),
) -> AgentCredentialResponse:
    import app.main as main_module

    _require_admin_token(admin_token)
    credential = await main_module.hub_state.rotate_agent_key(agent_id)
    return AgentCredentialResponse.model_validate(credential)

