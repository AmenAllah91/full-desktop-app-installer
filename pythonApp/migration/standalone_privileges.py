"""
Recense les privileges des utilisateurs d'une pointeuse autonome, et retire
le privilege administrateur aux adherents (PIN < 100000).

    venv\\Scripts\\python.exe migration\\standalone_privileges.py --ip 192.168.1.201
    venv\\Scripts\\python.exe migration\\standalone_privileges.py --ip 192.168.1.201 --corriger

Sans --corriger, rien n'est ecrit. La cle de communication se lit dans la
variable d'environnement ZK_COMKEY et n'est jamais affichee. Le pont doit
etre arrete pendant l'operation.
"""
import argparse
import json
import os
import socket
import sys
from datetime import datetime

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(ICI))

from services.privilegeMachine import PRIVILEGE_UTILISATEUR, est_pin_employe  # noqa: E402

MN = 1


def joignable(ip, port):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def connecter(ip, port, comkey):
    import win32com.client
    z = win32com.client.gencache.EnsureDispatch("zkemkeeper.ZKEM")
    if comkey:
        z.SetCommPassword(int(comkey))
    if not z.Connect_Net(ip, port):
        return None
    return z


def lire_utilisateurs(z):
    utilisateurs = []
    z.ReadAllUserID(MN)
    while True:
        ok, pin, nom, mot_de_passe, privilege, actif = z.SSR_GetAllUserInfo(MN)
        if not ok:
            break
        utilisateurs.append({"pin": str(pin), "nom": nom, "privilege": int(privilege),
                             "actif": bool(actif)})
    return utilisateurs


def classer(utilisateurs):
    adherents_admins = [u for u in utilisateurs
                        if u["privilege"] != PRIVILEGE_UTILISATEUR and not est_pin_employe(u["pin"])]
    employes_admins = [u for u in utilisateurs
                       if u["privilege"] != PRIVILEGE_UTILISATEUR and est_pin_employe(u["pin"])]
    return adherents_admins, employes_admins


def retrograder(z, pin):
    ok, nom, mot_de_passe, privilege, actif = z.SSR_GetUserInfo(MN, str(pin))
    if not ok:
        return False
    ok, carte = z.GetStrCardNumber()
    z.SetStrCardNumber(carte if ok and carte else "0")
    return bool(z.SSR_SetUserInfo(MN, str(pin), nom, mot_de_passe, PRIVILEGE_UTILISATEUR, actif))


def corriger(z, adherents_admins):
    echecs = []
    z.EnableDevice(MN, False)
    try:
        for u in adherents_admins:
            if not retrograder(z, u["pin"]):
                echecs.append(u["pin"])
    finally:
        try:
            z.RefreshData(MN)
        finally:
            z.EnableDevice(MN, True)
    return echecs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", required=True)
    p.add_argument("--port", type=int, default=4370)
    p.add_argument("--corriger", action="store_true")
    p.add_argument("--sans-admin", action="store_true",
                   help="corriger meme si aucun employe administrateur ne reste sur la pointeuse")
    p.add_argument("--sortie", default=None)
    args = p.parse_args()

    if not joignable(args.ip, args.port):
        print("%s:%s injoignable : pointeuse eteinte." % (args.ip, args.port), file=sys.stderr)
        return 2
    z = connecter(args.ip, args.port, os.environ.get("ZK_COMKEY"))
    if z is None:
        print("Connect_Net refuse sur %s (cle de communication ?)." % args.ip, file=sys.stderr)
        return 2
    try:
        utilisateurs = lire_utilisateurs(z)
        adherents_admins, employes_admins = classer(utilisateurs)
        print("%s : %d utilisateurs, %d adherents administrateurs, %d employes administrateurs"
              % (args.ip, len(utilisateurs), len(adherents_admins), len(employes_admins)))
        for u in adherents_admins:
            print("  pin %-8s privilege %s" % (u["pin"], u["privilege"]))
        sortie = args.sortie or os.path.join(
            ICI, "privileges_%s_%s.json" % (args.ip.replace(".", "-"), datetime.now().strftime("%Y%m%d-%H%M%S")))
        with open(sortie, "w", encoding="utf-8") as f:
            json.dump({"ip": args.ip, "utilisateurs": len(utilisateurs),
                       "adherentsAdmins": adherents_admins, "employesAdmins": employes_admins},
                      f, indent=2, ensure_ascii=False)
        print("releve ecrit dans %s" % sortie)
        if not args.corriger:
            print("lecture seule : relancer avec --corriger pour retirer le privilege aux adherents.")
            return 0
        if not adherents_admins:
            return 0
        if not employes_admins and not args.sans_admin:
            print("aucun employe administrateur sur cette pointeuse : sans administrateur, le menu "
                  "s'ouvre sans controle. Programmer d'abord un employe, ou relancer avec --sans-admin.",
                  file=sys.stderr)
            return 1
        echecs = corriger(z, adherents_admins)
        print("%d adherents remis en utilisateur normal, %d echecs"
              % (len(adherents_admins) - len(echecs), len(echecs)))
        for pin in echecs:
            print("  echec pin %s" % pin)
        return 1 if echecs else 0
    finally:
        try:
            z.Disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
