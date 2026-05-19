"""
services/adms_adapter.py
========================
Adapter pour les appareils ZKTeco en mode PUSH (protocole ADMS).
Implémente la même interface DeviceAdapter que PlcommAdapter et ZkemAdapter.

Différence fondamentale:
  - Pull/Standalone: le bridge initie la connexion et envoie les commandes directement
  - PUSH: l'appareil se connecte au bridge, les commandes sont mises en queue
    et récupérées par l'appareil via le heartbeat (/iclock/getrequest)
"""

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional, List, Dict, Any

from services.adapters import DeviceAdapter


class ADMSAdapter(DeviceAdapter):
    """
    Adapter pour machines ZKTeco PUSH/ADMS (SpeedFace-V3L, ProFace, etc.)

    Au lieu d'appeler directement la machine, cet adapter:
    1. Met la commande ADMS en queue
    2. L'appareil vient chercher la commande via le heartbeat
    3. L'appareil exécute et renvoie le résultat

    Le serveur ADMS HTTP est partagé (ADMSServer singleton) entre
    tous les ADMSAdapter d'une même instance.
    """

    def __init__(self, machine):
        super().__init__(machine)
        self.sn: Optional[str] = None  # Numéro de série, rempli au handshake
        self.connected = False
        self._cmd_counter = 1000
        self._lock = threading.Lock()

        # Queue de commandes ADMS pour cet appareil
        self._command_queue: list[str] = []

        # Résultats des commandes (cmd_id -> result string)
        self._results: Dict[int, Optional[str]] = {}
        self._result_events: Dict[int, threading.Event] = {}

        # Info device
        self.push_version: str = ""
        self.device_type: str = ""
        self.last_seen: Optional[str] = None

    # ----------------------------------------------------------------
    # Connexion — pas de connexion active, juste un état
    # ----------------------------------------------------------------

    def connect(self, max_attempts: int = 3) -> bool:
        """
        En mode PUSH, c'est l'appareil qui se connecte au serveur.
        Cette méthode ne fait que vérifier si l'appareil est connecté.
        """
        if self.connected and self.sn:
            return True
        logging.info("⏳ ADMS [%s]: en attente de connexion de l'appareil...", self.machine.addresseip)
        return self.connected

    def disconnect(self):
        """Marque l'appareil comme déconnecté."""
        self.connected = False
        logging.info("🔌 ADMS [%s]: déconnecté (SN=%s)", self.machine.addresseip, self.sn)

    def is_connected(self) -> bool:
        """Vérifie si l'appareil est toujours connecté via le heartbeat."""
        if not self.last_seen:
            return False
        try:
            last = datetime.fromisoformat(self.last_seen)
            elapsed = (datetime.now() - last).total_seconds()
            return elapsed < 60  # Considéré déconnecté après 60s sans heartbeat
        except Exception:
            return False

    # ----------------------------------------------------------------
    # ADMS internal — appelé par le serveur ADMS
    # ----------------------------------------------------------------

    def on_handshake(self, sn: str, push_ver: str, device_type: str, ip: str):
        """Appelé par le serveur ADMS quand l'appareil fait le handshake."""
        self.sn = sn
        self.push_version = push_ver
        self.device_type = device_type
        self.connected = True
        self.last_seen = datetime.now().isoformat()
        logging.info("✅ ADMS handshake: SN=%s, PushVer=%s, IP=%s", sn, push_ver, ip)

    def on_heartbeat(self):
        """Appelé à chaque heartbeat (/iclock/getrequest)."""
        self.last_seen = datetime.now().isoformat()

    def get_next_command(self) -> Optional[str]:
        """Récupère la prochaine commande à envoyer à l'appareil."""
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
                logging.info("✅ ADMS CMD OK: ID=%s", cmd_id)
            else:
                logging.warning("❌ ADMS CMD FAIL: ID=%s, Return=%s", cmd_id, ret)

            # Stocker le résultat et notifier les waiters
            with self._lock:
                self._results[cmd_id] = body
                evt = self._result_events.get(cmd_id)
                if evt:
                    evt.set()

        except Exception as e:
            logging.error("ADMS parse result error: %s", e)

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            self._cmd_counter += 1
            return self._cmd_counter

    def _enqueue(self, cmd: str, timeout: float = 30.0) -> bool:
        """
        Met une commande en queue et attend le résultat (optionnel).
        Retourne True si la commande a été confirmée avec Return=0.
        """
        # Extraire cmd_id
        try:
            cmd_id = int(cmd.split(":")[1])
        except (IndexError, ValueError):
            cmd_id = 0

        # Créer un event pour attendre le résultat
        evt = threading.Event()
        with self._lock:
            self._command_queue.append(cmd)
            self._result_events[cmd_id] = evt

        logging.info("📥 ADMS queue [%s]: %s", self.sn or self.machine.addresseip, cmd)

        # Attendre le résultat
        if evt.wait(timeout=timeout):
            result = self._results.get(cmd_id, "")
            # Nettoyer
            with self._lock:
                self._results.pop(cmd_id, None)
                self._result_events.pop(cmd_id, None)
            return "Return=0" in result
        else:
            logging.warning("⏰ ADMS timeout cmd %s après %ss", cmd_id, timeout)
            with self._lock:
                self._result_events.pop(cmd_id, None)
            return False

    def _enqueue_fire_and_forget(self, cmd: str):
        """Met en queue sans attendre le résultat."""
        with self._lock:
            self._command_queue.append(cmd)
        logging.info("📥 ADMS queue (async) [%s]: %s", self.sn or "?", cmd)

    # ----------------------------------------------------------------
    # DeviceAdapter interface — opérations métier
    # ----------------------------------------------------------------

    def add_user(self, pin, name, card, start, end) -> bool:
        cid = self._next_id()
        parts = [f"C:{cid}:DATA UPDATE user"]
        parts.append(f"\tPin={pin}")
        parts.append(f"\tName={name}")
        if card:
            parts.append(f"\tCardNo={card}")
        parts.append(f"\tPri=0")
        parts.append(f"\tGrp=1")
        if start:
            parts.append(f"\tStartTime={start}")
        if end:
            parts.append(f"\tEndTime={end}")
        return self._enqueue("".join(parts))

    def delete_user(self, pin, finger_id=None) -> bool:
        cid = self._next_id()
        if finger_id is not None:
            cmd = f"C:{cid}:DATA DELETE biodata\tPin={pin}\tNo={finger_id}\tType=1"
        else:
            cmd = f"C:{cid}:DATA DELETE user\tPin={pin}"
        return self._enqueue(cmd)

    def authorize_user(self, pin) -> bool:
        cid = self._next_id()
        cmd = (
            f"C:{cid}:DATA UPDATE userauthorize"
            f"\tPin={pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1"
        )
        return self._enqueue(cmd)

    def unauthorize_user(self, pin) -> bool:
        cid = self._next_id()
        cmd = (
            f"C:{cid}:DATA UPDATE user"
            f"\tPin={pin}"
            f"\tStartTime=2020-01-01 00:00:00"
            f"\tEndTime=2020-01-02 23:59:59"
        )
        return self._enqueue(cmd)

    def add_fingerprint(self, user_id, fingerprint_template: bytes,
                        finger_id: int, save_to_kafka: bool = True) -> bool:
        import base64
        cid = self._next_id()

        if isinstance(fingerprint_template, (bytes, bytearray)):
            tpl_b64 = base64.b64encode(fingerprint_template).decode("utf-8")
        else:
            tpl_b64 = str(fingerprint_template)

        cmd = (
            f"C:{cid}:DATA UPDATE biodata"
            f"\tPin={user_id}\tNo={finger_id}"
            f"\tIndex=0\tValid=1\tDuress=0"
            f"\tType=1\tMajorVer=5\tMinorVer=50\tFormat=0"
            f"\tTmp={tpl_b64}"
        )
        return self._enqueue(cmd, timeout=60)

    def delete_fingerprint(self, pin, finger_id) -> bool:
        return self.delete_user(pin, finger_id=finger_id)

    def get_fingerprints(self, pin: str) -> list:
        """
        Query les empreintes via ADMS.
        Note: le résultat arrive de façon asynchrone sur /iclock/querydata.
        """
        cid = self._next_id()
        cmd = f"C:{cid}:DATA QUERY tablename=biodata,fielddesc=*,filter=Pin={pin}"
        self._enqueue_fire_and_forget(cmd)
        # Les résultats arrivent sur querydata, géré par le serveur ADMS
        return []

    def open_door(self, door_no: int = 1, duration: int = 5, **kwargs) -> bool:
        cid = self._next_id()
        duration = max(1, min(int(duration), 60))
        cmd = f"C:{cid}:CONTROL DEVICE 01:{door_no}:{duration}"
        return self._enqueue(cmd, timeout=15)

    def upload_user_photo(self, pin: str, photo_path: str) -> bool:
        """Upload photo via ADMS — lit le fichier et envoie en base64."""
        import base64
        import os
        if not os.path.isfile(photo_path):
            logging.error("❌ Photo introuvable: %s", photo_path)
            return False

        with open(photo_path, "rb") as f:
            photo_b64 = base64.b64encode(f.read()).decode("utf-8")

        cid = self._next_id()
        cmd = (
            f"C:{cid}:DATA UPDATE biophoto"
            f"\tPin={pin}\tType=9\tFormat=0"
            f"\tContent={photo_b64}"
        )
        return self._enqueue(cmd, timeout=60)

    def download_user_photo(self, pin: str, path: str) -> bool:
        """
        En PUSH, on ne peut pas télécharger directement.
        On envoie une QUERY et le résultat arrive en async.
        """
        cid = self._next_id()
        cmd = f"C:{cid}:DATA QUERY tablename=biophoto,fielddesc=*,filter=Pin={pin}"
        self._enqueue_fire_and_forget(cmd)
        return False

    def sync_time(self) -> bool:
        cid = self._next_id()
        cmd = f"C:{cid}:SET OPTIONS DateTime={int(time.time())}"
        return self._enqueue(cmd)

    def reboot(self) -> bool:
        cid = self._next_id()
        cmd = f"C:{cid}:CONTROL DEVICE 02"
        self._enqueue_fire_and_forget(cmd)
        return True

    def get_info(self) -> bool:
        cid = self._next_id()
        cmd = f"C:{cid}:GET OPTIONS ~SerialNumber,FirmVer,~DeviceName,IPAddress,MACAddress,~Platform"
        self._enqueue_fire_and_forget(cmd)
        return True
