import logging
import time
import threading
from typing import List, Dict, Any

from domain.AccessMachine import AccessMachine

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_machines: Dict[int, Dict[str, Any]] = {}


def register_machine(machine: AccessMachine):
    with _lock:
        _machines[machine.id] = {
            "alias": machine.alias,
            "ip": machine.addresseip,
            "port": machine.port,
            "type": machine.type,
            "status": "disconnected",
            "message": "En attente de connexion",
            "level": "disconnected",
            "lastEvent": None,
            "lastSeen": None,
        }
    logger.info("[machine_status] Machine %s (%s) enregistrée", machine.alias, machine.addresseip)


def mark_connected(machine: AccessMachine, message: str = "connected"):
    now = time.time()
    with _lock:
        entry = _machines.get(machine.id)
        if entry:
            entry["status"] = "connected"
            entry["message"] = message
            entry["level"] = "info"
            entry["lastSeen"] = now
            state = _make_state(machine.id, entry)
        else:
            _machines[machine.id] = {
                "alias": machine.alias, "ip": machine.addresseip,
                "port": machine.port, "type": machine.type,
                "status": "connected", "message": message, "level": "info",
                "lastEvent": None, "lastSeen": now,
            }
            state = _make_state(machine.id, _machines[machine.id])
    _broadcast(state)
    logger.info("[machine_status] %s → connected (%s)", machine.alias, message)


def mark_disconnected(machine: AccessMachine, message: str = "", level: str = "disconnected"):
    with _lock:
        entry = _machines.get(machine.id)
        if entry:
            entry["status"] = "disconnected"
            entry["message"] = message
            entry["level"] = level
            state = _make_state(machine.id, entry)
        else:
            _machines[machine.id] = {
                "alias": machine.alias, "ip": machine.addresseip,
                "port": machine.port, "type": machine.type,
                "status": "disconnected", "message": message, "level": level,
                "lastEvent": None, "lastSeen": None,
            }
            state = _make_state(machine.id, _machines[machine.id])
    _broadcast(state)
    logger.info("[machine_status] %s → disconnected (%s)", machine.alias, message)


def mark_event(machine: AccessMachine):
    now = time.time()
    with _lock:
        entry = _machines.get(machine.id)
        if entry:
            entry["lastEvent"] = now
            entry["lastSeen"] = now


def snapshot() -> List[Dict[str, Any]]:
    with _lock:
        return [_make_state(mid, info) for mid, info in _machines.items()]


def _make_state(mid: int, info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "machineId": mid,
        "alias": info.get("alias", ""),
        "ip": info.get("ip", ""),
        "port": info.get("port"),
        "type": info.get("type", ""),
        "status": info["status"],
        "level": info["level"],
        "message": info["message"],
        "lastEvent": info.get("lastEvent"),
        "lastSeen": info.get("lastSeen"),
    }


def _broadcast(state: Dict[str, Any]):
    try:
        from services.websocket import broadcast_ws
        broadcast_ws({
            "type": "machine_status_changed",
            "channel": "machinestatus",
            "data": state,
            "timestamp": time.time(),
        })
    except Exception as ex:
        logger.debug("[machine_status] broadcast failed: %s", ex)
