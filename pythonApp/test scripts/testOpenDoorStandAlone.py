import win32com.client
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

IP = "192.168.1.229"
PORT = 4370
MACHINE_NUMBER = 1

logger.info("Connexion à %s:%s (Standalone)...", IP, PORT)

try:
    zk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
except Exception as e:
    logger.error("Erreur création objet COM: %s", e)
    raise

if zk.Connect_Net(IP, PORT):
    logger.info("Connecté avec succès!")
    logger.info("Test ouverture porte...")
    if zk.ACUnlock(MACHINE_NUMBER, 3):
        logger.info("Porte ouverte (ACUnlock 3s)!")
    else:
        logger.warning("ACUnlock a échoué")
    time.sleep(1)
    zk.Disconnect()
    logger.info("Déconnecté.")
else:
    logger.error("Échec de connexion.")
