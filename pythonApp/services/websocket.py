import asyncio
import threading
import time

import websockets
import json
from typing import Dict, Any, Optional

_ws_loop = None
_ws_clients = set()


async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        async for message in ws:
            # Handle incoming messages (subscriptions, etc.)
            try:
                data = json.loads(message)
                print(f"[WebSocket] Received: {data}")
                if data.get('action') == 'subscribe':
                    print(
                        f"[WebSocket] Client subscribed to {data.get('channel')} for gymBranchId: {data.get('gymBranchId')}")
            except json.JSONDecodeError:
                print(f"[WebSocket] Invalid JSON received: {message}")
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
        print("[WebSocket] Démarré sur ws://localhost:8765")
        # On ne ferme jamais - wait_closed bloque.
        await server.wait_closed()

    # On planifie la coroutine de démarrage, puis on lance la loop.
    _ws_loop.create_task(_start())
    _ws_loop.run_forever()


def start_ws_server():
    """Start the WebSocket server in a separate thread."""
    t = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    t.start()

    # on attend que _ws_loop soit créé pour pouvoir diffuser ensuite
    while _ws_loop is None:
        time.sleep(0.05)


def broadcast_ws(payload: dict):
    """Generic broadcast function."""
    if _ws_loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), _ws_loop)


# === SPECIFIC FUNCTIONS FOR POINTAGE, SESSION, FINGERPRINT ===

def send_pointage(pointage_data: Dict[str, Any], gym_branch_id: str):
    """
    Send pointage data to WebSocket clients.

    Args:
        pointage_data: Dictionary containing pointage information
        gym_branch_id: Gym branch identifier for filtering clients
    """
    payload = {
        "type": "pointage",
        "channel": "pointage",
        "gymBranchId": gym_branch_id,
        "data": pointage_data,
        "timestamp": time.time()
    }

    print(f"[WebSocket] Broadcasting pointage for gym {gym_branch_id}: {pointage_data}")
    broadcast_ws(payload)


def send_session(session_data: Dict[str, Any], gym_branch_id: str):
    """
    Send session data to WebSocket clients.

    Args:
        session_data: Dictionary containing session information
        gym_branch_id: Gym branch identifier for filtering clients
    """
    payload = {
        "type": "session",
        "channel": "session",
        "gymBranchId": gym_branch_id,
        "data": session_data,
        "timestamp": time.time()
    }

    print(f"[WebSocket] Broadcasting session for gym {gym_branch_id}: {session_data}")
    broadcast_ws(payload)


def send_fingerprint(fingerprint_data: Dict[str, Any], gym_branch_id: str):
    """
    Send fingerprint data to WebSocket clients.

    Args:
        fingerprint_data: Dictionary containing fingerprint information
        gym_branch_id: Gym branch identifier for filtering clients
    """
    payload = {
        "type": "fingerprint",
        "channel": "fingerprint",
        "gymBranchId": gym_branch_id,
        "data": fingerprint_data,
        "timestamp": time.time()
    }

    print(f"[WebSocket] Broadcasting fingerprint for gym {gym_branch_id}: {fingerprint_data}")
    broadcast_ws(payload)



def get_connected_clients_count() -> int:
    """Get the number of currently connected WebSocket clients."""
    return len(_ws_clients)


def is_server_running() -> bool:
    """Check if the WebSocket server is running."""
    return _ws_loop is not None and not _ws_loop.is_closed()