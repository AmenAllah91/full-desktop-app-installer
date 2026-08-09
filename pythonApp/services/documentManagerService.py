# documentManagerService.py
import logging
import os
from pathlib import Path

import requests


class DocumentManagerService:
    """
    Passerelle vers document-manager (gateway MinIO).

    Sert à récupérer une biophoto qui n'existe pas encore sur ce PC : sur un poste
    fraîchement installé, le dossier local des photos est vide alors que la photo
    a bien été saisie et stockée dans MinIO.
    """

    def __init__(self, base_url=None, timeout=15):
        self.base_url = (base_url or os.getenv(
            "DOCUMENT_MANAGER_URL",
            "https://app.yogym.co/document-management"
        )).rstrip("/")
        self.session = requests.Session()
        self.timeout = timeout

    def download_faceid_photo(self, pin: str, tenant: str, dest_path) -> bool:
        """
        Télécharge la photo FaceID de l'adhérent et l'enregistre sur ce PC.

        :param pin:       code utilisateur
        :param tenant:    tenant courant (porte le bucket MinIO côté serveur)
        :param dest_path: chemin complet du fichier à écrire
        :return:          True si la photo a été récupérée, False si elle n'existe
                          pas dans MinIO (jamais saisie) ou en cas d'erreur
        """
        if not tenant:
            logging.error("❌ TENANT absent du .env : impossible de récupérer la biophoto du PIN %s", pin)
            return False

        url = f"{self.base_url}/public/faceid-photo/{tenant}/{pin}"

        try:
            resp = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as ex:
            logging.error("❌ document-manager injoignable (%s) : %s", url, ex)
            return False

        if resp.status_code == 404:
            logging.warning("⚠️ Aucune biophoto dans MinIO pour le PIN %s (tenant=%s)", pin, tenant)
            return False

        if resp.status_code != 200:
            logging.error("❌ document-manager a répondu %s pour le PIN %s : %s",
                          resp.status_code, pin, resp.text[:200])
            return False

        if not resp.content:
            logging.error("❌ Biophoto vide renvoyée pour le PIN %s", pin)
            return False

        try:
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # écriture en deux temps : on ne veut pas laisser un JPG tronqué
            # derrière nous, il serait réutilisé tel quel au prochain envoi
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
        except OSError as ex:
            logging.error("❌ Impossible d'enregistrer la biophoto ➜ %s : %s", dest_path, ex)
            return False

        logging.info("☁️ Biophoto récupérée depuis MinIO ➜ %s (%s octets)", dest, len(resp.content))
        return True
