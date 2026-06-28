import asyncio
import threading
import time
import logging
import json
from typing import Dict, Any, Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

_ws_loop = None
_ws_clients: Set[WebSocketServerProtocol] = set()


async def _ws_handler(ws: WebSocketServerProtocol):
    _ws_clients.add(ws)
    authenticated = False
    gym_branch_id = None

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[WebSocket] JSON invalide: %s", raw[:200])
                continue

            action = data.get("action")

            if action == "authenticate":
                token = data.get("access_token", "")
                logger.info("[WebSocket] Authentification reçue (token length=%s)", len(token))
                authenticated = True
                gym_branch_id = data.get("gymBranchId")
                await ws.send(json.dumps({"action": "authenticated", "status": "ok"}))

            elif action == "subscribe":
                ch = data.get("channel")
                gb = data.get("gymBranchId")
                logger.info("[WebSocket] Subscribe channel=%s gymBranchId=%s", ch, gb)
                if gb:
                    gym_branch_id = gb

            else:
                logger.info("[WebSocket] Message non géré: %s", data)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error("[WebSocket] Erreur handler: %s", e)
    finally:
        _ws_clients.discard(ws)
        logger.info("[WebSocket] Client déconnecté (total: %s)", len(_ws_clients))


async def _ws_broadcast(payload: dict):
    if _ws_clients:
        msg = json.dumps(payload, ensure_ascii=False)
        await asyncio.gather(*(ws.send(msg) for ws in _ws_clients.copy()), return_exceptions=True)


def _ws_thread():
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)

    async def _start():
        server = await websockets.serve(_ws_handler, "localhost", 8765)
        logger.info("[WebSocket] Démarré sur ws://localhost:8765")
        await server.wait_closed()

    _ws_loop.create_task(_start())
    _ws_loop.run_forever()


def start_ws_server():
    t = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    t.start()

    while _ws_loop is None:
        time.sleep(0.05)


def broadcast_ws(payload: dict):
    if _ws_loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), _ws_loop)


def send_pointage(pointage_data: Dict[str, Any], gym_branch_id: str):
    payload = {
        "type": "pointage",
        "channel": "pointage",
        "gymBranchId": gym_branch_id,
        "data": pointage_data,
        "timestamp": time.time()
    }
    logger.info("[WebSocket] Broadcasting pointage for gym %s", gym_branch_id)
    broadcast_ws(payload)


def send_session(session_data: Dict[str, Any], gym_branch_id: str):
    payload = {
        "type": "session",
        "channel": "session",
        "gymBranchId": gym_branch_id,
        "data": session_data,
        "timestamp": time.time()
    }
    logger.info("[WebSocket] Broadcasting session for gym %s", gym_branch_id)
    broadcast_ws(payload)


def send_fingerprint(fingerprint_data: Dict[str, Any], gym_branch_id: str):
    payload = {
        "type": "fingerprint",
        "channel": "fingerprint",
        "gymBranchId": gym_branch_id,
        "data": fingerprint_data,
        "timestamp": time.time()
    }
    logger.info("[WebSocket] Broadcasting fingerprint for gym %s", gym_branch_id)
    broadcast_ws(payload)


def get_connected_clients_count() -> int:
    return len(_ws_clients)


def is_server_running() -> bool:
    return _ws_loop is not None and not _ws_loop.is_closed()
