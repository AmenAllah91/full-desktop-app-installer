"""
services/MonitorADMS.py
========================
Module de monitoring pour les appareils PUSH/ADMS.

Contrairement aux monitors C3 (polling GetRTLog) et ZKEM (événements COM),
les machines PUSH envoient les pointages directement au serveur ADMS.
Ce module configure le callback et gère la publication HTTP + WebSocket.
"""

import json
import logging
import time
from datetime import datetime
from os import getenv
from typing import Optional

from domain.AccessMachine import AccessMachine
from services.MachineMonitor import make_rt_json
from services.websocket import send_pointage
from services.http_client import send_pointage as send_pointage_http


def monitor_adms(machine: AccessMachine, adapter, stop_evt,
                 tenant: str = "", gym_branch_id: str = "0"):
    """
    Thread de monitoring pour une machine PUSH.
    Ne fait pas de polling — vérifie juste que l'appareil est connecté.
    Les pointages arrivent via le serveur ADMS (callback).

    Ce thread:
    1. Configure le callback de pointage sur le serveur ADMS
    2. Vérifie périodiquement la connexion de l'appareil
    3. Publie les pointages sur HTTP (Spring Boot) + WebSocket
    """
    # Configurer le callback de pointage
    from services.adms_server import ADMSServer
    adms_server = ADMSServer()

    def on_attendance(sn: str, record: dict):
        """Callback appelé pour chaque pointage reçu."""
        try:
            pin_str = record.get("pin", "0")
            pin = int(pin_str) if pin_str.isdigit() else 0
            dt_str = record.get("datetime", "")
            status = record.get("status", "0")

            payload = make_rt_json(
                machine_id=machine.id,
                ip=machine.addresseip,
                mtype="PUSH",
                pin=pin,
                state_code=int(status) if status.isdigit() else 0,
                dt=dt_str,
                door_id=1,
                card_no=None,
                gym_branch_id=gym_branch_id,
                porte_type=machine.porte_type or "ENTREE",
            )

            try:
                payload_dict = json.loads(payload)
                send_pointage(payload_dict, gym_branch_id)
                send_pointage_http(payload_dict, machine.id)
            except Exception as ex:
                logging.error("[WebSocket/HTTP] Erreur envoi: %s", ex)

            logging.info("📡 PUSH %s → %s", machine.addresseip, payload)

        except Exception as e:
            logging.error("Erreur callback pointage ADMS: %s", e)

    # Enregistrer le callback
    adms_server.on_attendance = on_attendance

    logging.info("🟢 Monitor ADMS démarré pour %s (attente connexion...)", machine.addresseip)

    # Boucle de surveillance — vérifie juste que l'appareil reste connecté
    while not stop_evt.is_set():
        if adapter.is_connected():
            if not getattr(adapter, "_logged_connected", False):
                logging.info("✅ ADMS %s connecté (SN=%s)", machine.addresseip, adapter.sn)
                adapter._logged_connected = True
        else:
            if getattr(adapter, "_logged_connected", False):
                logging.warning("⚠️ ADMS %s déconnecté", machine.addresseip)
                adapter._logged_connected = False

        # Dormir par tranches pour pouvoir s'arrêter rapidement
        for _ in range(50):  # 50 * 0.2 = 10 secondes
            if stop_evt.is_set():
                break
            time.sleep(0.2)

    logging.warning("🔌 Monitor ADMS arrêté %s", machine.addresseip)
