import os
import ctypes
from ctypes import *
import time
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

PLCOMPRO_URL = os.getenv("PLCOMPRO_URL")

plcommpro = ctypes.CDLL(PLCOMPRO_URL)

plcommpro.Connect.argtypes = [c_char_p]
plcommpro.Connect.restype = c_void_p
plcommpro.Disconnect.argtypes = [c_void_p]
plcommpro.SetDeviceData.argtypes = [c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
plcommpro.SetDeviceData.restype = c_int
plcommpro.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
plcommpro.PullLastError.restype = c_int

# Délai d'attente des requêtes SDK, en millisecondes.
# La doc PullSDK donne 4000 en exemple, mais précise qu'un code -2 signifie
# « délai dépassé » et qu'il faut alors monter à 20000.
CONNECT_TIMEOUT_MS = 20000


def _pull_last_error():
    """
    Vrai code d'erreur du Pull SDK.

    Le code appelait ctypes.get_last_error(), qui n'a de sens que si la DLL a
    été chargée avec use_last_error=True — ce n'est pas le cas ici. Il affichait
    donc invariablement « Error code: 0 », y compris quand le SDK refusait
    l'authentification avec -14, ce qui a rendu la panne C3 indéchiffrable.
    """
    try:
        return plcommpro.PullLastError()
    except Exception:
        return "?"


def connect_to_device(ip_address, port, max_attempts=3):
    # Aucun mot de passe de communication n'est transmis aux C3.
    #
    # Le SDK refuse la connexion (PullLastError = -14) dès qu'on lui en envoie
    # un, y compris « 0 ». Les fiches machines ayant été renseignées avec un
    # comKey, tous les C3 sont devenus injoignables du jour au lendemain. La
    # notion est donc retirée du chemin C3 — elle reste en place pour les
    # terminaux ZKEM, qui l'utilisent réellement.
    #
    # ⚠️ La clé passwd doit être PRÉSENTE et VIDE : l'omettre échoue tout autant
    # que la renseigner (-14 dans les deux cas). Vérifié sur un C3 réel, et
    # conforme à la doc : « If the parameter value is null, it indicates that no
    # password is used ».
    #
    # timeout=20000 et non 4000 : le -2 renvoyé par GetDeviceData/SetDeviceData
    # est un DÉPASSEMENT DE DÉLAI, pas un refus. PullSDK User Guide : « When the
    # query result contains the error code of -2, you should set timeout to a
    # larger value, for example, timeout=20000 ». Lire la table des utilisateurs
    # pendant que le thread temps réel interroge le panneau dépasse largement
    # 4 secondes.
    params = (f"protocol=TCP,ipaddress={ip_address},port={port},"
              f"timeout={CONNECT_TIMEOUT_MS},passwd=").encode('utf-8')
    # Journalisation volontairement muette sur le chemin nominal.
    #
    # Une session C3 vit ~3,5 s : le thread temps réel la renouvelle donc une
    # dizaine de fois par minute et par panneau. En INFO, cette fonction produisait
    # deux lignes à chaque renouvellement — plusieurs centaines par heure, qui
    # noyaient les vraies anomalies. Le succès passe en DEBUG (la connexion
    # initiale reste tracée en INFO par monitor_machine, « 🔌 C3 … connecté ») et
    # seul l'échec définitif est journalisé, avec son PullLastError.
    attempts = 0
    handle = None
    last_error = None

    while attempts < max_attempts and not handle:
        logger.debug("Tentative de connexion %s/%s sur %s",
                     attempts + 1, max_attempts, ip_address)
        handle = plcommpro.Connect(params)
        attempts += 1

        if handle:
            # Surtout pas d'attente ici. Ce sleep de 2 s, prévu comme backoff de
            # reprise, s'appliquait aussi au succès : la session était donc
            # utilisée deux secondes après son ouverture. Mesuré sur un C3 réel,
            # une commande ne passe qu'IMMÉDIATEMENT après le Connect — à 0,5 s
            # elle renvoie déjà -2.
            logger.debug("Connexion établie sur %s", ip_address)
            return handle

        last_error = _pull_last_error()
        logger.debug("Échec de connexion sur %s (PullLastError=%s)",
                     ip_address, last_error)
        if attempts < max_attempts:
            time.sleep(2)

    logger.error("Connexion impossible sur %s après %s tentatives (PullLastError=%s)",
                 ip_address, max_attempts, last_error)
    return handle


def disconnect_device(handle):
    if handle:
        plcommpro.Disconnect(handle)
        logger.info("Disconnected from device.")
    else:
        logger.warning("No valid handle to disconnect.")


def add_user(handle, user_pin, card_no, start_time, end_time):
    logger.info("add_user: %s %s %s %s", start_time, end_time, user_pin, card_no)
    delete_user(user_pin, None)
    data = f"Pin={user_pin}\tCardNo={card_no}\tStartTime={start_time}\tEndTime={end_time}\tPassword=".encode('utf-8')
    result = plcommpro.SetDeviceData(handle, b"user", data, b"")
    logger.info("card_no=%s, user_pin=%s", card_no, user_pin)
    if result == 0:
        logger.info("User added successfully.")
        authorize_user(handle, user_pin)
        return True
    else:
        logger.error("Failed to add user. Error code: %s", result)
        return False


def delete_user(handle, user_pin, fingerId):
    if fingerId is None:
        data = f"Pin={user_pin}".encode('utf-8')
        result3 = plcommpro.DeleteDeviceData(handle, b"userauthorize", data, None)
    else:
        data = f"Pin={user_pin}\tFingerID={fingerId}".encode('utf-8')
        result3 = plcommpro.DeleteDeviceData(handle, b"templatev10", data, None)

    if result3 == 0:
        logger.info("User deleted successfully.")
        return True
    else:
        logger.error("Failed to delete user. Error code: %s", result3)
        return False


def authorize_user(handle, user_pin):
    data = f"Pin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1\r\nPin={user_pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"
    result = plcommpro.SetDeviceData(handle, b"userauthorize", data, b"")
    if result == 0:
        logger.info("User authorized successfully.")
        return True
    else:
        logger.error("Failed to authorize user. Error code: %s", result)
        return False


def unauthorize_user(handle, user_pin):
    table_name = "userauthorize".encode('utf-8')
    delete_condition = f"Pin={user_pin}".encode('utf-8')
    options = "".encode('utf-8')

    logger.debug("Unauthorizing user PIN %s", user_pin)
    result = plcommpro.DeleteDeviceData(handle, table_name, delete_condition, options)
    if result == 0:
        logger.info("Authorizations for user PIN %s have been successfully deleted.", user_pin)
    else:
        logger.error("Failed to delete authorizations for user PIN %s. Error code: %s", user_pin, result)
