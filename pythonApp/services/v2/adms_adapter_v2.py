"""
services/v2/adms_adapter_v2.py
===============================
Adapter PUSH/ADMS v2 — reécrit proprement.

Chaque machine (quel que soit son type d'origine) est pilotée
via le protocole ADMS : les commandes sont mises en file,
l'appareil les récupère au heartbeat, et renvoie le résultat.
"""

import base64
import hashlib
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional, Dict

logger = logging.getLogger("ADMS-v2")


def _normalize_date(raw: str, is_end: bool = False) -> str:
    """
    Convertit une date au format attendu par le protocole ADMS.
    Entrée possible : '20250415' ou '2025-04-15' ou '2025-04-15 00:00:00'
    Sortie : 'YYYY-MM-DD HH:MM:SS'
    """
    if not raw:
        return ""
    raw = raw.strip()

    # Déjà au bon format
    if len(raw) == 19 and "-" in raw and ":" in raw:
        return raw

    suffix = " 23:59:59" if is_end else " 00:00:00"

    # Format yyyyMMdd
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d") + suffix
        except ValueError:
            pass

    # Format yyyy-MM-dd
    if len(raw) == 10 and "-" in raw:
        return raw + suffix

    return raw


class ADMSAdapterV2:
    """
    Adapter pour une machine ZKTeco en mode PUSH.

    Cycle de vie :
      1. L'adapter est créé avec les infos de la machine
      2. L'appareil se connecte au serveur ADMS (handshake)
      3. Les commandes sont mises en file
      4. L'appareil poll /iclock/getrequest et récupère les commandes
      5. L'appareil exécute et renvoie le résultat via /iclock/devicecmd
    """

    def __init__(self, machine):
        self.machine = machine
        self.sn: Optional[str] = None
        self.connected = False

        # Info appareil (rempli au handshake)
        self.push_version: str = ""
        self.device_type: str = ""
        self.device_ip: str = ""
        self.last_seen: Optional[datetime] = None

        # Compteur de commandes
        self._cmd_counter = 1000
        self._lock = threading.Lock()

        # File de commandes ADMS
        self._command_queue: list[str] = []

        # Résultats : cmd_id -> résultat brut
        self._results: Dict[int, Optional[str]] = {}
        self._result_events: Dict[int, threading.Event] = {}

    # ----------------------------------------------------------------
    # Connexion — en PUSH c'est l'appareil qui initie
    # ----------------------------------------------------------------

    def is_connected(self) -> bool:
        """True si l'appareil a fait un heartbeat dans les 60 dernières secondes."""
        if not self.last_seen:
            return False
        elapsed = (datetime.now() - self.last_seen).total_seconds()
        return elapsed < 60

    # ----------------------------------------------------------------
    # Callbacks — appelés par le serveur ADMS
    # ----------------------------------------------------------------

    def on_handshake(self, sn: str, push_ver: str, device_type: str, ip: str):
        """Appelé au premier contact de l'appareil."""
        self.sn = sn
        self.push_version = push_ver
        self.device_type = device_type
        self.device_ip = ip
        self.connected = True
        self.last_seen = datetime.now()
        logger.info("✅ Handshake: SN=%s, IP=%s, Type=%s, PushVer=%s",
                     sn, ip, device_type, push_ver)

    def on_heartbeat(self):
        """Appelé à chaque heartbeat."""
        self.last_seen = datetime.now()

    def get_next_command(self) -> Optional[str]:
        """Retourne la prochaine commande en file (FIFO)."""
        with self._lock:
            if self._command_queue:
                return self._command_queue.pop(0)
        return None

    def on_command_result(self, body: str):
        """Appelé quand l'appareil renvoie le résultat d'une commande."""
        try:
            parts = {}
            for seg in body.strip().split("&"):
                if "=" in seg:
                    k, v = seg.split("=", 1)
                    parts[k.strip()] = v.strip()

            cmd_id = int(parts.get("ID", 0))
            ret = parts.get("Return", "?")

            if ret == "0":
                logger.info("✅ CMD #%s OK", cmd_id)
            else:
                logger.warning("❌ CMD #%s FAIL (Return=%s)", cmd_id, ret)

            with self._lock:
                self._results[cmd_id] = body
                evt = self._result_events.get(cmd_id)
                if evt:
                    evt.set()

        except Exception as e:
            logger.error("Parse result error: %s — body=%s", e, body[:200])

    # ----------------------------------------------------------------
    # Helpers internes
    # ----------------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._cmd_counter += 1
            return self._cmd_counter

    def _enqueue(self, cmd: str, timeout: float = 30.0) -> bool:
        """
        Met une commande en file et attend la confirmation.
        Retourne True si Return=0.
        """
        try:
            cmd_id = int(cmd.split(":")[1])
        except (IndexError, ValueError):
            cmd_id = 0

        evt = threading.Event()
        with self._lock:
            self._command_queue.append(cmd)
            self._result_events[cmd_id] = evt

        identifier = self.sn or self.machine.alias or self.machine.addresseip
        logger.info("📥 [%s] CMD en file: %s", identifier, cmd[:120])

        if evt.wait(timeout=timeout):
            result = self._results.get(cmd_id, "")
            with self._lock:
                self._results.pop(cmd_id, None)
                self._result_events.pop(cmd_id, None)
            return "Return=0" in result
        else:
            logger.warning("⏰ [%s] Timeout CMD #%s après %ss", identifier, cmd_id, timeout)
            with self._lock:
                self._result_events.pop(cmd_id, None)
            return False

    def _fire_and_forget(self, cmd: str):
        """Met en file sans attendre le résultat."""
        with self._lock:
            self._command_queue.append(cmd)

    # ----------------------------------------------------------------
    # Opérations métier
    # ----------------------------------------------------------------

    def add_user(self, pin: str, name: str, card_no: str,
                 start_time: str, end_time: str) -> bool:
        cid = self._next_id()
        start_fmt = _normalize_date(start_time, is_end=False)
        end_fmt = _normalize_date(end_time, is_end=True)

        # Format documenté PUSH SDK :
        # C:<id>:DATA UPDATE USERINFO PIN=x\tName=x\tPri=0\tPasswd=\tCard=\tGrp=1
        #   \tTZ=0000000100000000\tVerify=-1\tViceCard=\tStartDatetime=0\tEndDatetime=0
        parts = [f"C:{cid}:DATA UPDATE USERINFO PIN={pin}",
                 f"\tName={name}",
                 "\tPri=0",
                 "\tPasswd=",
                 f"\tCard={card_no}" if card_no else "\tCard=",
                 "\tGrp=1",
                 "\tTZ=0000000100000000",
                 "\tVerify=-1",
                 "\tViceCard="]
        if start_fmt:
            parts.append(f"\tStartDatetime={start_fmt}")
        else:
            parts.append("\tStartDatetime=0")
        if end_fmt:
            parts.append(f"\tEndDatetime={end_fmt}")
        else:
            parts.append("\tEndDatetime=0")

        logger.info("📝 ADD_USER: PIN=%s, Name=%s, Card=%s, %s → %s",
                     pin, name, card_no, start_fmt, end_fmt)
        return self._enqueue("".join(parts))

    def delete_user(self, pin: str, finger_id=None) -> bool:
        cid = self._next_id()
        if finger_id is not None:
            cmd = f"C:{cid}:DATA DELETE BIODATA PIN={pin}\tNo={finger_id}\tType=1"
        else:
            cmd = f"C:{cid}:DATA DELETE USERINFO PIN={pin}"
        return self._enqueue(cmd)

    def authorize_user(self, pin: str) -> bool:
        cid = self._next_id()
        cmd = (f"C:{cid}:DATA UPDATE USERAUTHORIZE PIN={pin}"
               f"\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1")
        return self._enqueue(cmd)

    def unauthorize_user(self, pin: str) -> bool:
        cid = self._next_id()
        cmd = (f"C:{cid}:DATA UPDATE USERINFO PIN={pin}"
               f"\tStartDatetime=2020-01-01 00:00:00"
               f"\tEndDatetime=2020-01-02 23:59:59")
        logger.info("🚫 UNAUTHORIZE: PIN=%s", pin)
        return self._enqueue(cmd)

    def add_fingerprint(self, user_id: str, fingerprint_template: bytes,
                        finger_id: int) -> bool:
        if isinstance(fingerprint_template, (bytes, bytearray)):
            tpl_b64 = base64.b64encode(fingerprint_template).decode("utf-8")
        else:
            tpl_b64 = str(fingerprint_template)

        cid = self._next_id()
        cmd = (f"C:{cid}:DATA UPDATE BIODATA PIN={user_id}"
               f"\tNo={finger_id}"
               f"\tIndex=0\tValid=1\tDuress=0"
               f"\tType=1\tMajorVer=5\tMinorVer=50\tFormat=0"
               f"\tTmp={tpl_b64}")
        return self._enqueue(cmd, timeout=60)

    def delete_fingerprint(self, pin: str, finger_id: int) -> bool:
        return self.delete_user(pin, finger_id=finger_id)

    def open_door(self, door_no: int = 1, duration: int = 5) -> bool:
        cid = self._next_id()
        duration = max(1, min(int(duration), 60))
        cmd = f"C:{cid}:CONTROL DEVICE 01:{door_no}:{duration}"
        return self._enqueue(cmd, timeout=15)

    def upload_user_photo(self, pin: str, photo_path: str) -> bool:
        if not os.path.isfile(photo_path):
            logger.error("❌ Photo introuvable: %s", photo_path)
            return False

        with open(photo_path, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode("utf-8")

        cid = self._next_id()
        cmd = (f"C:{cid}:DATA UPDATE BIOPHOTO PIN={pin}"
               f"\tType=9\tFormat=0"
               f"\tContent={photo_b64}")
        return self._enqueue(cmd, timeout=60)

    def sync_time(self) -> bool:
        cid = self._next_id()
        cmd = f"C:{cid}:SET OPTIONS DateTime={int(time.time())}"
        return self._enqueue(cmd)

    def reboot(self) -> bool:
        cid = self._next_id()
        cmd = f"C:{cid}:CONTROL DEVICE 02"
        self._fire_and_forget(cmd)
        return True

    def get_info(self) -> bool:
        cid = self._next_id()
        fields = "~SerialNumber,FirmVer,~DeviceName,IPAddress,MACAddress,~Platform"
        cmd = f"C:{cid}:GET OPTIONS {fields}"
        self._fire_and_forget(cmd)
        return True
