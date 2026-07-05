import asyncio
import socket
import threading
import time
import logging
import json
from datetime import datetime
from urllib.parse import parse_qs
import websockets
from websockets.server import WebSocketServerProtocol
from typing import Dict, Any, Optional, Set

_ws_loop = None
_ws_clients: Set[WebSocketServerProtocol] = set()
_ws_server_ready = threading.Event()


def _ws_headers(headers_or_request):
    """Normaliser l'accès aux headers (Request ou Headers selon la version websockets)."""
    if hasattr(headers_or_request, "headers"):
        return headers_or_request.headers
    return headers_or_request


async def _ws_process_request(path, request_headers):
    """Handle CORS preflight for Chrome Private Network Access (CORS-RFC1918)."""
    h = _ws_headers(request_headers)
    upgrade = h.get("Upgrade", "")
    if upgrade.lower() == "websocket":
        return None

    origin = h.get("Origin", "")
    cors_origin = origin if origin else "*"

    response_headers = [
        ("Access-Control-Allow-Origin", cors_origin),
        ("Access-Control-Allow-Private-Network", "true"),
        ("Access-Control-Allow-Methods", "GET, OPTIONS"),
        ("Access-Control-Allow-Headers",
         "Content-Type, Authorization, Sec-WebSocket-Protocol, Sec-WebSocket-Extensions"),
        ("Access-Control-Max-Age", "86400"),
    ]
    return 200, response_headers, b"OK"


async def _ws_process_response(path, request_headers, response_headers):
    """Add CORS + Private Network Access headers to WebSocket upgrade response."""
    h = _ws_headers(request_headers)
    origin = h.get("Origin", "")
    if origin:
        rh = getattr(response_headers, "headers", response_headers)
        rh["Access-Control-Allow-Origin"] = origin
        rh["Access-Control-Allow-Private-Network"] = "true"
        rh["Access-Control-Allow-Credentials"] = "true"
    return response_headers


async def _ws_handler(ws: WebSocketServerProtocol):
    _ws_clients.add(ws)
    authenticated = False
    gym_branch_id = None

    ws_path = getattr(ws, "path", getattr(ws.request, "path", ""))
    query = parse_qs(ws_path.split("?", 1)[1]) if "?" in ws_path else {}
    token_from_url = query.get("access_token", [None])[0]
    if token_from_url:
        print("[WebSocket] Token extrait de l'URL (length=%s)", len(token_from_url))
        authenticated = True

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print("[WebSocket] JSON invalide: %s", raw[:200])
                continue

            action = data.get("action")

            if action == "authenticate":
                token = data.get("access_token", "")
                print("[WebSocket] Authentification reçue (token length=%s)", len(token))
                authenticated = True
                gym_branch_id = data.get("gymBranchId")
                await ws.send(json.dumps({"action": "authenticated", "status": "ok"}))

            elif action == "subscribe":
                ch = data.get("channel")
                gb = data.get("gymBranchId")
                print("[WebSocket] Subscribe channel=%s gymBranchId=%s", ch, gb)
                if gb:
                    gym_branch_id = gb

                if ch == "mahcinestatus":
                    try:
                        import main as _main
                        for ctx in _main.get_all_device_contexts():
                            send_machine_status(ctx.machine, ctx.adapter, _main.app_version)
                    except Exception as e:
                        print("[WebSocket] Could not send initial machine statuses: %s", e)

            else:
                print("[WebSocket] Message non géré: %s", data)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        print("[WebSocket] Erreur handler: %s", e)
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
        server = await websockets.serve(
            _ws_handler,
            "localhost",
            8765,
            process_request=_ws_process_request,
            process_response=_ws_process_response,
            ping_interval=20,
            ping_timeout=20,
        )
        print("[WebSocket] Démarré sur ws://localhost:8765")
        _ws_server_ready.set()
        await server.wait_closed()

    _ws_loop.create_task(_start())
    _ws_loop.run_forever()


def start_ws_server():
    t = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    t.start()

    while _ws_loop is None:
        time.sleep(0.05)

    if not _ws_server_ready.wait(timeout=5):
        print("[WebSocket] Le serveur ne s'est pas signalé prêt dans les 5s")


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
    payload = {
        "type": "fingerprint",
        "channel": "fingerprint",
        "gymBranchId": gym_branch_id,
        "data": fingerprint_data,
        "timestamp": time.time()
    }

    print(f"[WebSocket] Broadcasting fingerprint for gym {gym_branch_id}: {fingerprint_data}")
    broadcast_ws(payload)


def _ts_value(val) -> Optional[int]:
    """Convert a datetime/string/float to Unix timestamp (int) or None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return int(val.timestamp())
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(datetime.strptime(val, "%Y-%m-%d %H:%M:%S").timestamp())
        except (ValueError, TypeError):
            return None
    return None


def _check_tcp(ip: str, port: int, timeout: float = 2.0) -> bool:
    """Vérifie si un port TCP est joignable (connexion réelle, pas de cache)."""
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        sock.close()
        return True
    except (OSError, socket.timeout):
        return False


def send_machine_status(machine, adapter, app_version: str = "v1"):
    """Broadcast machine status change to WebSocket clients in real-time."""
    if app_version == "v2":
        connected = adapter.is_connected()
    else:
        connected = _check_tcp(machine.addresseip, int(machine.port))

    payload = {
        "type": "machine_status_changed",
        "data": {
            "machineId": machine.id,
            "alias": machine.alias,
            "ip": machine.addresseip,
            "port": machine.port,
            "machineType": machine.type,
            "connected": connected,
            "lastError": getattr(adapter, "last_error", ""),
            "lastSeen": _ts_value(getattr(adapter, "last_seen", None)),
            "onlineSince": _ts_value(getattr(adapter, "online_since", None)),
            "offlineSince": _ts_value(getattr(adapter, "offline_since", None)),
            "reconnectCount": getattr(adapter, "reconnect_count", 0),
            "eventCount": getattr(adapter, "event_count", 0),
            "reason": "connected" if connected else "disconnected",
            "timestamp": int(time.time())
        }
    }
    broadcast_ws(payload)


def send_machine_status_from_ctx(ctx, app_version: str = "v1"):
    """Convenience: extract machine+adapter from a DeviceContext object."""
    send_machine_status(ctx.machine, ctx.adapter, app_version)


def start_machine_status_broadcast(get_devices_fn, interval: int = 5):
    """
    Lance un thread qui broadcast l'état de toutes les machines toutes les `interval` secondes.
    `get_devices_fn` doit retourner une liste de tuples (machine, adapter, app_version).
    """
    def _loop():
        while True:
            try:
                devices = get_devices_fn()
                for machine, adapter, app_version in devices:
                    send_machine_status(machine, adapter, app_version)
            except Exception as e:
                print("[Broadcast] Erreur lors du broadcast périodique: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="MachineStatusBroadcast")
    t.start()
    print("[Broadcast] Thread de statut machines démarré (interval=%ss)", interval)


def get_connected_clients_count() -> int:
    return len(_ws_clients)


def is_server_running() -> bool:
    return _ws_loop is not None and not _ws_loop.is_closed()