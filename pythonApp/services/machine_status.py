# services/machine_status.py
#
# Suivi de l'état de connectivité des machines d'accès + diffusion websocket.
# Le front (pointage-sidebar) attend des messages de la forme :
#   {"type": "machine_status_changed", "data": {<MachineStatusEvent>}}
# où <MachineStatusEvent> = machineId, alias, ip, port, machineType, connected,
# lastError, lastSeen, onlineSince, offlineSince, reconnectCount, eventCount,
# reason, timestamp.
import logging
import threading
import time

from services.websocket import broadcast_ws

logger = logging.getLogger(__name__)

_states: dict = {}   # machine_id -> état courant (dict au format MachineStatusEvent)
_lock = threading.Lock()


def _new_state(machine) -> dict:
    return {
        "machineId": machine.id,
        "alias": getattr(machine, "alias", "") or "",
        "ip": getattr(machine, "addresseip", "") or "",
        "port": getattr(machine, "port", 0) or 0,
        "machineType": getattr(machine, "type", "") or "",
        "connected": False,
        "lastError": "",
        "lastSeen": 0,
        "onlineSince": 0,
        "offlineSince": 0,
        "reconnectCount": 0,
        "eventCount": 0,
        "reason": "disconnected",
        "timestamp": time.time(),
    }


def _broadcast(state: dict):
    try:
        broadcast_ws({
            "type": "machine_status_changed",
            "channel": "machinestatus",
            "data": state,
            "timestamp": time.time(),
        })
    except Exception as ex:
        logger.error("[MachineStatus] Erreur broadcast: %s", ex)


def register_machine(machine):
    """Crée l'entrée (état 'déconnecté') sans diffuser — appelé au démarrage."""
    with _lock:
        if machine.id not in _states:
            _states[machine.id] = _new_state(machine)


def mark_connected(machine, reason: str = "connected"):
    """À appeler quand une machine (re)devient joignable. Ne diffuse que si l'état change."""
    now = time.time()
    with _lock:
        state = _states.setdefault(machine.id, _new_state(machine))
        was_connected = state["connected"]
        state["connected"] = True
        state["lastSeen"] = now
        state["lastError"] = ""
        state["reason"] = reason if not was_connected else state["reason"]
        state["timestamp"] = now
        if not was_connected:
            state["onlineSince"] = now
            if reason == "reconnected":
                state["reconnectCount"] += 1
        changed = not was_connected
        snapshot = dict(state)
    if changed:
        logger.info("[MachineStatus] 🟢 Machine %s (%s) connectée (%s)",
                    machine.id, snapshot["ip"], reason)
        _broadcast(snapshot)


def mark_disconnected(machine, error: str = "", reason: str = "disconnected"):
    """À appeler quand une machine devient injoignable. Ne diffuse que si l'état change."""
    now = time.time()
    with _lock:
        state = _states.setdefault(machine.id, _new_state(machine))
        was_connected = state["connected"]
        state["connected"] = False
        state["lastError"] = error or state["lastError"]
        state["reason"] = reason
        state["timestamp"] = now
        if was_connected:
            state["offlineSince"] = now
        changed = was_connected
        snapshot = dict(state)
    if changed:
        logger.warning("[MachineStatus] 🔴 Machine %s (%s) déconnectée (%s) : %s",
                       machine.id, snapshot["ip"], reason, error)
        _broadcast(snapshot)


def mark_event(machine):
    """À appeler à chaque événement temps réel reçu — rafraîchit lastSeen sans diffuser."""
    now = time.time()
    with _lock:
        state = _states.get(machine.id)
        if state is None:
            state = _states.setdefault(machine.id, _new_state(machine))
        state["eventCount"] += 1
        state["lastSeen"] = now
        # Recevoir un événement prouve que la machine est en ligne.
        if not state["connected"]:
            state["connected"] = True
            state["onlineSince"] = now
            state["reason"] = "connected"
            state["timestamp"] = now
            snapshot = dict(state)
        else:
            snapshot = None
    if snapshot:
        _broadcast(snapshot)


def snapshot() -> list:
    """État courant de toutes les machines (pour l'envoi initial aux clients ws)."""
    with _lock:
        return [dict(s) for s in _states.values()]
