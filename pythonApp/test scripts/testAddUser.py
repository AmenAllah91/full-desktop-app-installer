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
    IP = "192.168.2.12"
    PORT = 4370              # port par défaut pour ZKTeco TCP/IP
    COM_KEY = 123456           # comKey de la machine (None/0/"" si elle n'en a pas)
    MACHINE_NUMBER = 1       # numéro machine souvent = 1 pour standalone
    PIN = "1"                # PIN de l'utilisateur à créer / mettre à jour
    NAME = "Amenallah Kraiem"     # Nom affiché
    PASSWORD = 0           # mot de passe (vide si non nécessaire)
    PRIVILEGE = 3            # 2 = SuperAdmin (selon devices / firmware)
    ENABLED = True           # True pour activer l'utilisateur

    try:
        zk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
    except Exception as e:
        logger.error("Impossible de créer l'objet COM zkemkeeper. Vérifiez que zkemkeeper.dll est enregistré.")
        logger.error("Exception: %s", e)
        sys.exit(1)

    # ComKey : uniquement pris en compte s'il est renseigné (non vide / non nul)
    if COM_KEY:
        try:
            zk.SetCommPassword(int(COM_KEY))
        except Exception as e:
            print("SetCommPassword a levé une exception:", e)

    # Tentative de connexion via Connect_Net
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
        print("SetUserInfo a retourné False. Vérifiez les droits, le firmware, ou utilisez SSR_SetUserInfo si disponible.")

        # essai avec SSR_SetUserInfo (signature identique sur plusieurs firmwares)
        try:
            print("Tentative avec SSR_SetUserInfo...")
            res2 = zk.SSR_SetUserInfo(
                MACHINE_NUMBER,
                PIN,
                NAME,
                PASSWORD,
                PRIVILEGE,
                int(ENABLED)
            )
            if res2:
                print("SSR_SetUserInfo a réussi — utilisateur mis à jour.")
            else:
                print("SSR_SetUserInfo a aussi échoué.")
        except Exception as e:
            print("SSR_SetUserInfo indisponible ou a levé une exception:", e)

    # Petit délai puis deconnexion
    time.sleep(0.5)
    try:
        zk.Disconnect()
    except Exception:
        pass

if __name__ == "__main__":
    main()
