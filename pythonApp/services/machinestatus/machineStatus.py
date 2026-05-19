import logging
import socket
import threading
import time
from typing import Any, Dict, Optional

from domain import AccessMachine
from services.websocket import send_machine_status


_registry: Dict[int, "MachineStatus"] = {}
_registry_lock = threading.Lock()


def register_status(status: "MachineStatus") -> None:
    with _registry_lock:
        _registry[status.machine.id] = status


def unregister_status(machine_id: int) -> None:
    with _registry_lock:
        _registry.pop(machine_id, None)


def get_status(machine_id: int) -> Optional["MachineStatus"]:
    with _registry_lock:
        return _registry.get(machine_id)


def get_all_statuses() -> Dict[int, "MachineStatus"]:
    with _registry_lock:
        return dict(_registry)



class MachineStatus:
    def __init__(self, machine: AccessMachine, ip: str, port: int, gym_branch_id: str):
        self.machine = machine
        self.ip = ip
        self.port = int(port)
        self.gym_branch_id = str(gym_branch_id)

        self.connected = False
        self.connecting = False
        self.reconnect_requested = False

        self.last_error = ""
        self.last_seen = 0.0
        self.online_since = 0.0
        self.offline_since = time.time()
        self.reconnect_count = 0
        self.event_count = 0
        self.heartbeat_failures = 0
        self._last_emitted_connected: Optional[bool] = None
        self._lock = threading.RLock()

        register_status(self)

    def _build_payload(self, reason: str) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "machine_status_changed",
                "machineId": self.machine.id,
                "alias": getattr(self.machine, "alias", None),
                "ip": self.ip,
                "port": self.port,
                "machineType": getattr(self.machine, "type", "STANDALONE_NEW_FIRMWARE"),
                "connected": self.connected,
                "connecting": self.connecting,
                "lastError": self.last_error,
                "lastSeen": self.last_seen,
                "onlineSince": self.online_since,
                "offlineSince": self.offline_since,
                "reconnectCount": self.reconnect_count,
                "eventCount": self.event_count,
                "heartbeatFailures": self.heartbeat_failures,
                "reconnectRequested": self.reconnect_requested,
                "reason": reason,
                "timestamp": time.time(),
            }

    def _broadcast_status(self, reason: str) -> None:
        payload = self._build_payload(reason)
        try:
            send_machine_status(payload, self.gym_branch_id)
        except Exception as ex:
            logging.warning("WS status send failed for %s: %s", self.ip, ex)

    def _emit_if_changed(self, reason: str) -> None:
        with self._lock:
            if self._last_emitted_connected == self.connected:
                return
            self._last_emitted_connected = self.connected
        self._broadcast_status(reason)

    def mark_connected(self, reason: str = "connected") -> None:
        now = time.time()
        with self._lock:
            self.connected = True
            self.connecting = False
            self.reconnect_requested = False
            self.last_error = ""
            self.last_seen = now
            self.online_since = now
            self.offline_since = 0.0
            self.reconnect_count += 1
            self.heartbeat_failures = 0
        self._emit_if_changed(reason)

    def mark_disconnected(self, error: str = "", reason: str = "disconnected") -> None:
        now = time.time()
        with self._lock:
            self.connected = False
            self.connecting = False
            if error:
                self.last_error = str(error)
            if self.offline_since == 0.0:
                self.offline_since = now
        self._emit_if_changed(reason)

    def set_connecting(self, value: bool = True) -> None:
        with self._lock:
            self.connecting = value

    def touch(self) -> None:
        with self._lock:
            self.last_seen = time.time()

    def increment_event(self) -> None:
        with self._lock:
            self.event_count += 1
            self.last_seen = time.time()

    def increment_heartbeat_failure(self) -> None:
        with self._lock:
            self.heartbeat_failures += 1

    def reset_heartbeat_failures(self) -> None:
        with self._lock:
            self.heartbeat_failures = 0

    def heartbeat_ok(self, timeout: float = 1.5) -> bool:
        try:
            with socket.create_connection((self.ip, self.port), timeout=timeout):
                self.touch()
                self.reset_heartbeat_failures()
                return True
        except OSError:
            self.increment_heartbeat_failure()
            return False

    def request_reconnect(self) -> bool:
        with self._lock:
            if self.connected:
                return False
            self.reconnect_requested = True
        self._broadcast_status("manual_reconnect_requested")
        return True

    def consume_reconnect_request(self) -> bool:
        with self._lock:
            value = self.reconnect_requested
            self.reconnect_requested = False
            return value


    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "machineId": self.machine.id,
                "alias": getattr(self.machine, "alias", None),
                "ip": self.ip,
                "port": self.port,
                "machineType": getattr(self.machine, "type", None),
                "connected": self.connected,
                "connecting": self.connecting,
                "reconnectRequested": self.reconnect_requested,
                "lastError": self.last_error,
                "lastSeen": self.last_seen,
                "onlineSince": self.online_since,
                "offlineSince": self.offline_since,
                "reconnectCount": self.reconnect_count,
                "eventCount": self.event_count,
                "heartbeatFailures": self.heartbeat_failures,
                "gymBranchId": self.gym_branch_id,
            }

    def __repr__(self) -> str:
        s = self.snapshot()
        return (
            f"MachineStatus(machineId={s['machineId']}, ip='{s['ip']}', "
            f"port={s['port']}, connected={s['connected']}, "
            f"reconnectCount={s['reconnectCount']})"
        )