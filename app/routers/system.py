from __future__ import annotations

from fastapi import APIRouter


router = APIRouter(tags=["系统"])


@router.get("/health", summary="健康检查", description="检查服务与数据库连通性。")
async def health() -> dict[str, str]:
    import app.main as main_module

    await main_module.hub_state.check_database()
    return {"status": "ok"}