import asyncio
import threading
import time
import logging

import websockets
import json
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

_ws_loop = None
_ws_clients = set()


async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        async for message in ws:
            try:
                data = json.loads(message)
                logger.info("[WebSocket] Received: %s", data)
                if data.get('action') == 'subscribe':
                    logger.info(
                        "[WebSocket] Client subscribed to %s for gymBranchId: %s",
                        data.get('channel'), data.get('gymBranchId'))
            except json.JSONDecodeError:
                logger.warning("[WebSocket] Invalid JSON received: %s", message)
    finally:
        _ws_clients.remove(ws)


async def _ws_broadcast(payload: dict):
    if _ws_clients:
        msg = json.dumps(payload, ensure_ascii=False)
        await asyncio.gather(*(ws.send(msg) for ws in _ws_clients), return_exceptions=True)


def _ws_thread():
    """Thread dédié : crée la loop, la démarre, puis lance le serveur."""
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
    """Start the WebSocket server in a separate thread."""
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
