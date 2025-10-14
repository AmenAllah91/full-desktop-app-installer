import asyncio
import threading
import time

import websockets
import json

_ws_loop = None
_ws_clients = set()

async def _ws_handler(ws):
    _ws_clients.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        _ws_clients.remove(ws)

async def _ws_broadcast(payload: dict):
    if _ws_clients:
        msg = json.dumps(payload, ensure_ascii=False)
        await asyncio.gather(*(ws.send(msg) for ws in _ws_clients))

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
    t = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    t.start()
    # on attend que _ws_loop soit créé pour pouvoir diffuser ensuite
    while _ws_loop is None:
        time.sleep(0.05)

def broadcast_ws(payload: dict):
    if _ws_loop:
        asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), _ws_loop)
