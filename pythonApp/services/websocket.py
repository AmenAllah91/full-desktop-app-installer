import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Optional, Set
from weakref import WeakSet

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)


WS_HOST = "localhost"
WS_PORT = 8765
WS_LOOP_POLL_INTERVAL = 0.05  # seconds to wait for loop init



class WsChannel(str, Enum):
    """All supported WebSocket broadcast channels."""
    POINTAGE    = "pointage"
    SESSION     = "session"
    FINGERPRINT = "fingerprint"
    MACHINE     = "machine_status_changed"


# ── Payload Model ──────────────────────────────────────────────────────────────

@dataclass
class WsPayload:
    """Standardised WebSocket broadcast payload."""
    type:          str
    channel:       str
    gymBranchId:   str
    data:          Dict[str, Any]
    timestamp:     float = field(default_factory=time.time)

    @classmethod
    def from_channel(
        cls,
        channel: WsChannel,
        data: Dict[str, Any],
        gym_branch_id: str,
    ) -> "WsPayload":
        return cls(
            type=channel.value,
            channel=channel.value,
            gymBranchId=gym_branch_id,
            data=data,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Server State ───────────────────────────────────────────────────────────────

class WsServerState:
    """
    Encapsulates all mutable server state.
    WeakSet ensures dead connections are garbage-collected automatically.
    """

    def __init__(self) -> None:
        # WeakSet: automatically drops disconnected clients without manual cleanup
        self._clients: WeakSet[WebSocketServerProtocol] = WeakSet()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @loop.setter
    def loop(self, value: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = value

    def add_client(self, ws: WebSocketServerProtocol) -> None:
        self._clients.add(ws)
        logger.info("[WebSocket] Client connected. Total: %d", self.client_count)

    def remove_client(self, ws: WebSocketServerProtocol) -> None:
        self._clients.discard(ws)
        logger.info("[WebSocket] Client disconnected. Total: %d", self.client_count)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def active_clients(self) -> Set[WebSocketServerProtocol]:
        # Snapshot to avoid mutation during iteration
        return set(self._clients)

    @property
    def is_running(self) -> bool:
        return self._loop is not None and not self._loop.is_closed()


# Module-level singleton
_state = WsServerState()



def _handle_subscription(data: Dict[str, Any]) -> None:
    """Log subscription requests from clients."""
    channel       = data.get("channel", "unknown")
    gym_branch_id = data.get("gymBranchId", "unknown")
    logger.info(
        "[WebSocket] Client subscribed to channel '%s' for gymBranchId: %s",
        channel,
        gym_branch_id,
    )



async def _ws_handler(ws: WebSocketServerProtocol) -> None:
    _state.add_client(ws)
    try:
        async for raw in ws:
            await _handle_message(raw)
    except websockets.exceptions.ConnectionClosedError as e:
        logger.warning("[WebSocket] Connection closed unexpectedly: %s", e)
    finally:
        _state.remove_client(ws)


async def _handle_message(raw: str) -> None:
    try:
        data: Dict[str, Any] = json.loads(raw)
        logger.debug("[WebSocket] Received: %s", data)

        action = data.get("action")
        if action == "subscribe":
            _handle_subscription(data)
        else:
            logger.debug("[WebSocket] Unhandled action: %s", action)

    except json.JSONDecodeError:
        logger.warning("[WebSocket] Invalid JSON received: %s", raw)



async def _ws_broadcast(payload: Dict[str, Any]) -> None:
    """
    Coroutine: sends payload to all active clients concurrently.
    return_exceptions=True ensures one failing send doesn't abort the rest.
    """
    clients = _state.active_clients
    if not clients:
        logger.debug("[WebSocket] No clients connected, skipping broadcast.")
        return

    msg = json.dumps(payload, ensure_ascii=False)
    results = await asyncio.gather(
        *(ws.send(msg) for ws in clients),
        return_exceptions=True,
    )

    for ws, result in zip(clients, results):
        if isinstance(result, Exception):
            logger.error("[WebSocket] Failed to send to client %s: %s", ws.remote_address, result)


def broadcast_ws(payload: Dict[str, Any]) -> None:
    if not _state.is_running:
        logger.warning("[WebSocket] Server not running — broadcast dropped.")
        return

    asyncio.run_coroutine_threadsafe(_ws_broadcast(payload), _state.loop)



async def _start_server() -> None:
    """Start the WebSocket server and block until it closes."""
    async with websockets.serve(_ws_handler, WS_HOST, WS_PORT) as server:
        logger.info("[WebSocket] Listening on ws://%s:%d", WS_HOST, WS_PORT)
        await server.wait_closed()


def _ws_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _state.loop = loop

    try:
        loop.run_until_complete(_start_server())
    except Exception:
        logger.exception("[WebSocket] Server crashed.")
    finally:
        loop.close()


def start_ws_server() -> None:
    """
    Start the WebSocket server in a background daemon thread.
    Blocks until the event loop is ready to accept broadcasts.
    """
    if _state.is_running:
        logger.warning("[WebSocket] Server already running.")
        return

    thread = threading.Thread(target=_ws_thread, daemon=True, name="WebSocketThread")
    thread.start()

    # Spin-wait until the loop is initialised and ready
    while not _state.is_running:
        time.sleep(WS_LOOP_POLL_INTERVAL)

    logger.info("[WebSocket] Server is ready.")



def _broadcast_channel(
    channel: WsChannel,
    data: Dict[str, Any],
    gym_branch_id: str,
) -> None:
    """
    Validate, build, and broadcast a typed channel payload.

    Raises:
        ValueError: if gym_branch_id is empty.
        TypeError:  if data is not a dict.
    """
    if not gym_branch_id:
        raise ValueError("gym_branch_id must not be empty.")
    if not isinstance(data, dict):
        raise TypeError(f"data must be a dict, got {type(data).__name__}.")

    payload = WsPayload.from_channel(channel, data, gym_branch_id)

    logger.info(
        "[WebSocket] Broadcasting '%s' for gym %s",
        channel.value,
        gym_branch_id,
    )

    broadcast_ws(payload.to_dict())


def send_pointage(data: Dict[str, Any], gym_branch_id: str) -> None:
    _broadcast_channel(WsChannel.POINTAGE, data, gym_branch_id)


def send_session(data: Dict[str, Any], gym_branch_id: str) -> None:
    _broadcast_channel(WsChannel.SESSION, data, gym_branch_id)


def send_fingerprint(data: Dict[str, Any], gym_branch_id: str) -> None:
    _broadcast_channel(WsChannel.FINGERPRINT, data, gym_branch_id)


def send_machine_status(data: Dict[str, Any], gym_branch_id: str) -> None:
    _broadcast_channel(WsChannel.MACHINE, data, gym_branch_id)


def get_connected_clients_count() -> int:
    """Return the number of currently connected WebSocket clients."""
    return _state.client_count


def is_server_running() -> bool:
    """Return True if the WebSocket server event loop is active."""
    return _state.is_running