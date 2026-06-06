import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

try:
    import win32com.client
except Exception as e:
    logger.error("pywin32 non installé. Faites: pip install pywin32")
    raise

def main():
    IP = "192.168.1.229"
    PORT = 4370
    MACHINE_NUMBER = 1
    PIN = "1"
    NAME = "Amenallah Kraiem"
    PASSWORD = "123456"
    PRIVILEGE = 2
    ENABLED = True

    try:
        zk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
    except Exception as e:
        logger.error("Impossible de créer l'objet COM zkemkeeper. Vérifiez que zkemkeeper.dll est enregistré.")
        logger.error("Exception: %s", e)
        sys.exit(1)

    try:
        connected = zk.Connect_Net(IP, PORT)
    except Exception as e:
        connected = False
        logger.error("Connect_Net a levé une exception: %s", e)

    if not connected:
        try:
            logger.info("Connect_Net a échoué, tentative avec Connect...")
            connected = zk.Connect(IP, PORT)
        except Exception as e:
            connected = False
            logger.error("Connect a levé une exception: %s", e)

    if not connected:
        logger.error("Échec de la connexion à %s:%s. Vérifier l'IP, le réseau et que la machine est allumée.", IP, PORT)
        sys.exit(2)

    logger.info("Connecté à %s:%s", IP, PORT)

    try:
        zk.RegEvent(MACHINE_NUMBER, 65535)
    except Exception:
        pass

    try:
        result = zk.SSR_SetUserInfo(
            MACHINE_NUMBER,
            PIN,
            NAME,
            PASSWORD,
            PRIVILEGE,
            int(ENABLED)
        )
    except Exception as e:
        logger.error("Erreur lors de l'appel SetUserInfo: %s", e)
        try:
            zk.Disconnect()
        except Exception:
            pass
        sys.exit(3)

    if result:
        logger.info("Utilisateur PIN=%s créé / mis à jour avec succès (privilege=%s).", PIN, PRIVILEGE)
    else:
        logger.warning("SetUserInfo a retourné False. Vérifiez les droits, le firmware, ou utilisez SSR_SetUserInfo si disponible.")
        try:
            logger.info("Tentative avec SSR_SetUserInfo...")
            res2 = zk.SSR_SetUserInfo(
                MACHINE_NUMBER,
                PIN,
                NAME,
                PASSWORD,
                PRIVILEGE,
                int(ENABLED)
            )
            if res2:
                logger.info("SSR_SetUserInfo a réussi — utilisateur mis à jour.")
            else:
                logger.error("SSR_SetUserInfo a aussi échoué.")
        except Exception as e:
            logger.error("SSR_SetUserInfo indisponible ou a levé une exception: %s", e)

    time.sleep(0.5)
    try:
        zk.Disconnect()
    except Exception:
        pass

if __name__ == "__main__":
    main()
