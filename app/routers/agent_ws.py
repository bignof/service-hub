from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status


router = APIRouter()


@router.websocket("/ws/agent/{agent_id}")
async def agent_ws(websocket: WebSocket, agent_id: str) -> None:
    import app.main as main_module

    presented_key = websocket.query_params.get("key", "")
    if not await main_module.hub_state.authenticate_agent(agent_id, presented_key):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        main_module.logger.warning("Rejected agent %s due to invalid credentials", agent_id)
        return

    await websocket.accept()
    await main_module.hub_state.register_agent(agent_id, websocket, main_module._remote_address(websocket))
    main_module.logger.info("Agent %s connected", agent_id)

    try:
        while True:
            message = await websocket.receive()
            if "text" in message and message["text"] is not None:
                payload = json.loads(message["text"])
                if isinstance(payload, dict):
                    await main_module._handle_agent_message(agent_id, payload)
                else:
                    main_module.logger.warning("Agent %s sent non-object payload", agent_id)
            elif message.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        main_module.logger.info("Agent %s disconnected", agent_id)
    except json.JSONDecodeError:
        main_module.logger.warning("Agent %s sent invalid JSON", agent_id)
    except Exception:
        main_module.logger.exception("Agent %s websocket loop failed", agent_id)
    finally:
        await main_module.hub_state.disconnect_agent(agent_id, websocket)