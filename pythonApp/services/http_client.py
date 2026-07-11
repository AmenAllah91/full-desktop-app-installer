import json
import logging
import os
import requests
from typing import Optional

logger = logging.getLogger(__name__)

SPRING_BOOT_BASE = os.getenv("SPRING_BOOT_URL", "http://localhost:8081")
DESKTOP_AGENT_BASE = os.getenv("DESKTOP_AGENT_URL", f"http://localhost:{os.getenv('FLASK_PORT', '9998')}")

_session = requests.Session()
_session.timeout = 10


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = os.getenv("SPRING_BOOT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def send_pointage(pointage_data: dict, machine_id) -> bool:
    try:
        payload = {
            "date": pointage_data.get("access_date_time"),
            "mode": pointage_data.get("machine_type", ""),
            "type": "acces",
            "statut": pointage_data.get("is_access_valid", True),
            "idAdherent": pointage_data.get("pin", 0),
            "etat": "valid" if pointage_data.get("is_access_valid", True) else "refused",
            "remarques": "",
            "porte": pointage_data.get("porte_type", "ENTREE"),
        }
        resp = _session.post(
            f"{SPRING_BOOT_BASE}/api/pointages?machineId={machine_id}",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        logger.debug("Pointage envoyé à Spring Boot: %s", pointage_data.get("access_date_time"))
        return True
    except Exception as e:
        logger.error("Erreur envoi pointage à Spring Boot: %s", e)
        return False


def send_photo(pin: str, photo_b64: str, machine_id: int, gym_branch_id: str, addresseip: str, port: int, machine_type: str):
    try:
        payload = {
            "gymBranchId": int(gym_branch_id),
            "userPin": pin,
            "machineId": machine_id,
            "addresseip": addresseip,
            "port": port,
            "type": machine_type,
        }
        resp = _session.post(
            f"{SPRING_BOOT_BASE}/api/biometric/publish-photo",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        logger.info("Photo publiée vers Spring Boot pour PIN=%s", pin)
        return True
    except Exception as e:
        logger.error("Erreur publication photo vers Spring Boot: %s", e)
        return False


def send_access_request_to_agent(access_request: dict) -> bool:
    try:
        resp = _session.post(
            f"{DESKTOP_AGENT_BASE}/tasks/access",
            json=access_request,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        logger.debug("Requête d'accès envoyée à l'agent desktop")
        return True
    except Exception as e:
        logger.error("Erreur envoi requête d'accès à l'agent desktop: %s", e)
        return False


def send_fingerprint_action_to_agent(action: dict) -> bool:
    try:
        resp = _session.post(
            f"{DESKTOP_AGENT_BASE}/fingerprint/actions",
            json=action,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        logger.debug("Action empreinte envoyée à l'agent desktop")
        return True
    except Exception as e:
        logger.error("Erreur envoi action empreinte à l'agent desktop: %s", e)
        return False
