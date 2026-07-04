"""
SetUserInfo on a ZKTeco device using zkemkeeper COM.
Connects to 192.168.1.204:4370 and sets user PIN "1" as SuperAdmin (privilege=2).

Usage:
  python set_superadmin_zk.py
"""

import sys
import time

try:
    import win32com.client
except Exception as e:
    print("Erreur: pywin32 non installé. Faites: pip install pywin32")
    raise


def has_comkey(value):
    if value is None:
        return False
    if isinstance(value, str):
        value = value.strip()
        if value == "" or value == "0":
            return False
        return True
    # cas int/float
    return value != 0


def main():
    IP = "192.168.2.12"
    PORT = 4370              # port par défaut pour ZKTeco TCP/IP
    COM_KEY = 0         # comKey de la machine (None/0/"" si elle n'en a pas)
    MACHINE_NUMBER = 1        # numéro machine souvent = 1 pour standalone
    PIN = "5"                 # PIN de l'utilisateur à créer / mettre à jour
    NAME = "Aymen Chaabani" # Nom affiché
    PASSWORD = 0              # mot de passe (vide si non nécessaire)
    PRIVILEGE = 3             # 2 = SuperAdmin (selon devices / firmware)
    ENABLED = True            # True pour activer l'utilisateur

    # Créer instance COM
    try:
        zk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
    except Exception as e:
        print("Impossible de créer l'objet COM zkemkeeper. Vérifiez que zkemkeeper.dll est enregistré.")
        print("Exception:", e)
        sys.exit(1)

    # --- Gestion du comKey ---
    if has_comkey(COM_KEY):
        try:
            zk.SetCommPassword(int(COM_KEY))
            logger.info("ComKey détecté et appliqué: %s", COM_KEY)
        except Exception as e:
            logger.error("SetCommPassword a levé une exception: %s", e)
            # on continue quand même, ça ne doit pas bloquer la connexion
    else:
        logger.info("Aucun comKey défini, on ne l'applique pas.")

    # Tentative de connexion via Connect_Net
    try:
        connected = zk.Connect_Net(IP, PORT)
    except Exception as e:
        connected = False
        print("Connect_Net a levé une exception:", e)

    if not connected:
        # essai fallback : Connect (parfois disponible selon modèle)
        try:
            print("Connect_Net a échoué, tentative avec Connect...")
            connected = zk.Connect(IP, PORT)
        except Exception as e:
            connected = False
            print("Connect a levé une exception:", e)

    if not connected:
        print(f"Échec de la connexion à {IP}:{PORT}. Vérifier l'IP, le réseau et que la machine est allumée.")
        sys.exit(2)

    print(f"Connecté à {IP}:{PORT} ✅")

    # Optionnel : enregistrer les événements (si besoin)
    try:
        # RegEvent <= enregistre des événements sur la machine (0 signifie toutes)
        zk.RegEvent(MACHINE_NUMBER, 65535)
    except Exception:
        # si RegEvent non supporté, ce n'est pas bloquant
        pass

    # SetUserInfo retourne généralement True/False (1/0)
    try:
        result = zk.SSR_SetUserInfo(
            MACHINE_NUMBER,
            PIN,         # enrollNumber / PIN
            NAME,        # nom
            PASSWORD,    # mot de passe (string)
            PRIVILEGE,   # privilege (0 user,1 admin,2 superadmin ...)
            int(ENABLED) # enabled: 1 or 0
        )
    except Exception as e:
        print("Erreur lors de l'appel SetUserInfo:", e)
        # Déconnexion propre
        try:
            zk.Disconnect()
        except Exception:
            pass
        sys.exit(3)

    if result:
        print(f"Utilisateur PIN={PIN} créé / mis à jour avec succès (privilege={PRIVILEGE}).")
    else:
        print("SetUserInfo a retourné False. Vérifiez les droits, le firmware, ou utilisez SSR_SetUserInfo si disponible.")
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

    time.sleep(0.5)
    try:
        zk.Disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()