"""Identité du poste (services/identitePoste.py) — sans machines, sans réseau.

    python tests\\test_identite_poste.py
"""
import io
import json
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from harnais import Suite, verifier, egal  # noqa: E402
from services import identitePoste as ip  # noqa: E402

suite = Suite("PONT — identité du poste (aucune machine, aucun réseau)")

BASE = "https://integration.yo-club.app"
SECRET = "s3cr3t-du-poste"
ENV = {
    "YOCLUB_POSTE_CLUB": "empire",
    "YOCLUB_POSTE_CLIENT_ID": "yoclub-poste",
    "YOCLUB_POSTE_CLIENT_SECRET": SECRET,
    "YOCLUB_POSTE_TOKEN_URL": "https://login-int.yo-club.app/realms/empire/protocol/openid-connect/token",
    "YOCLUB_POSTE_NUMERO": "P-0007",
    "YOCLUB_POSTE_FILIALE": "1003",
}
MACHINE = {"id": 12, "alias": "Entrée", "addresseip": "192.168.1.205", "port": 4370, "statut": "ACTIVE",
           "type": "C3", "door1": 1, "door2": 0, "door3": 0, "door4": 0, "porte_type": "ENTREE",
           "comKey": None}


class Reponse:
    def __init__(self, status_code, corps=None):
        self.status_code = status_code
        self._corps = corps

    def json(self):
        if isinstance(self._corps, Exception):
            raise self._corps
        return self._corps

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Http:
    def __init__(self, jetons=None, gets=None):
        self.jetons = list(jetons or [])
        self.gets = list(gets or [])
        self.posts_faits = []
        self.gets_faits = []
        self.numero = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts_faits.append((url, data, headers, timeout))
        suivant = self.jetons.pop(0) if self.jetons else None
        if isinstance(suivant, Exception):
            raise suivant
        if suivant is not None:
            return suivant
        self.numero += 1
        return Reponse(200, {"access_token": f"jeton-{self.numero}", "expires_in": 300})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets_faits.append((url, params, headers, timeout))
        suivant = self.gets.pop(0) if self.gets else Reponse(200, [MACHINE])
        if isinstance(suivant, Exception):
            raise suivant
        return suivant


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Journal:
    def __enter__(self):
        self.flux = io.StringIO()
        self.gestion = logging.StreamHandler(self.flux)
        self.racine = logging.getLogger()
        self.niveau = self.racine.level
        self.racine.addHandler(self.gestion)
        self.racine.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.racine.removeHandler(self.gestion)
        self.racine.setLevel(self.niveau)

    def texte(self):
        return self.flux.getvalue()


class Banc:
    def __init__(self, jetons=None, gets=None, dossier=None, env=None):
        self.dossier = dossier or tempfile.mkdtemp(prefix="poste-")
        self.http = Http(jetons, gets)
        self.horloge = Horloge()
        self.identite = ip.IdentitePoste.depuis_env(env or ENV)
        self.client = ip.ClientPoste(self.identite, BASE, "remote-1", http=self.http, horloge=self.horloge)
        self.service = ip.ServiceMachinesPoste(self.client, self.dossier)

    def nettoyer(self):
        shutil.rmtree(self.dossier, ignore_errors=True)


@suite.test("identité complète lue dans l'environnement")
def _():
    identite = ip.IdentitePoste.depuis_env(ENV)
    verifier(identite is not None, "identité attendue")
    egal(identite.club, "empire", "club")
    egal(identite.filiale, "1003", "filiale")
    egal(identite.numero, "P-0007", "numéro")


@suite.test("variable absente ou vide -> pas d'identité (routes publiques)")
def _():
    egal(ip.IdentitePoste.depuis_env({}), None, "aucune variable")
    for nom in ENV:
        sans = {k: v for k, v in ENV.items() if k != nom}
        egal(ip.IdentitePoste.depuis_env(sans), None, f"{nom} absente")
        vide = dict(ENV, **{nom: "  "})
        egal(ip.IdentitePoste.depuis_env(vide), None, f"{nom} vide")


@suite.test("identité partielle : avertissement sans le secret")
def _():
    with Journal() as j:
        ip.IdentitePoste.depuis_env({k: v for k, v in ENV.items() if k != "YOCLUB_POSTE_NUMERO"})
    verifier("YOCLUB_POSTE_NUMERO" in j.texte(), "la variable manquante est nommée")
    verifier(SECRET not in j.texte(), "le secret ne doit pas apparaître")


@suite.test("lue au lancement du processus, jamais après un chargement de .env")
def _():
    avant = dict(os.environ)
    try:
        os.environ.update(ENV)
        egal(ip.IdentitePoste.depuis_env(), None, "variables posées après le lancement ignorées")
    finally:
        os.environ.clear()
        os.environ.update(avant)


@suite.test("repr de l'identité sans le secret")
def _():
    identite = ip.IdentitePoste.depuis_env(ENV)
    verifier(SECRET not in repr(identite) and SECRET not in str(identite), "secret dans repr")


@suite.test("jeton : client_credentials en formulaire vers YOCLUB_POSTE_TOKEN_URL")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        egal(len(b.http.posts_faits), 1, "un seul appel de jeton")
        url, data, headers, _ = b.http.posts_faits[0]
        egal(url, ENV["YOCLUB_POSTE_TOKEN_URL"], "URL du jeton")
        egal(data, {"grant_type": "client_credentials", "client_id": "yoclub-poste",
                    "client_secret": SECRET}, "formulaire")
        egal(headers.get("Content-Type"), "application/x-www-form-urlencoded", "type de contenu")
    finally:
        b.nettoyer()


@suite.test("jeton réutilisé avant 80 % de sa durée, renouvelé après")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        b.horloge.t += 239
        b.service.get_access_machines()
        egal(len(b.http.posts_faits), 1, "réutilisé à 239 s")
        b.horloge.t += 2
        b.service.get_access_machines()
        egal(len(b.http.posts_faits), 2, "renouvelé à 241 s")
        egal(b.http.gets_faits[-1][2]["Authorization"], "Bearer jeton-2", "nouveau jeton utilisé")
    finally:
        b.nettoyer()


@suite.test("pointeuses : route du poste, filiale et en-têtes du contrat")
def _():
    b = Banc()
    try:
        machines = b.service.get_access_machines("autre", "autre")
        url, params, headers, _ = b.http.gets_faits[0]
        egal(url, f"{BASE}/gym-management/api/poste/machines", "URL")
        egal(params, {"branche": "1003"}, "paramètre branche")
        egal(headers["Authorization"], "Bearer jeton-1", "jeton")
        egal(headers["X-Yoclub-Poste-Numero"], "P-0007", "numéro du poste")
        egal(headers["X-Yoclub-Poste-Version"], "remote-1", "version")
        egal(len(machines), 1, "une pointeuse")
        m = machines[0]
        egal((m.id, m.alias, m.addresseip, m.port, m.type, m.door1, m.porte_type, m.comKey),
             (12, "Entrée", "192.168.1.205", 4370, "C3", 1, "ENTREE", 0), "champs")
    finally:
        b.nettoyer()


@suite.test("401 : jeton renouvelé et appel rejoué une fois")
def _():
    b = Banc(gets=[Reponse(401), Reponse(200, [MACHINE])])
    try:
        machines = b.service.get_access_machines()
        egal(len(machines), 1, "pointeuses après rejeu")
        egal(len(b.http.posts_faits), 2, "deux jetons")
        egal(b.http.gets_faits[1][2]["Authorization"], "Bearer jeton-2", "rejoué avec le nouveau jeton")
    finally:
        b.nettoyer()


@suite.test("401 répété : erreur, pas de boucle")
def _():
    b = Banc(gets=[Reponse(401), Reponse(401), Reponse(401)])
    try:
        try:
            b.service.get_access_machines()
            verifier(False, "une erreur était attendue")
        except requests.HTTPError:
            pass
        egal(len(b.http.gets_faits), 2, "un seul rejeu")
    finally:
        b.nettoyer()


@suite.test("copie gardée sur le disque et relue après redémarrage")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        fichier = os.path.join(b.dossier, ip.FICHIER_COPIE)
        verifier(os.path.isfile(fichier), "copie absente")
        apres = Banc(dossier=b.dossier, gets=[requests.ConnectionError("réseau coupé")])
        copie = ip.copie_de_secours(apres.service)
        verifier(copie is not None, "copie illisible")
        egal([(m.id, m.addresseip, m.comKey) for m in copie], [(12, "192.168.1.205", 0)], "copie")
    finally:
        b.nettoyer()


@suite.test("sans réseau : erreur levée, la copie reste disponible")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        b.http.gets.append(requests.ConnectionError("réseau coupé"))
        b.horloge.t += 1000
        try:
            b.service.get_access_machines()
            verifier(False, "une erreur était attendue")
        except requests.ConnectionError:
            pass
        egal(len(ip.copie_de_secours(b.service)), 1, "copie")
    finally:
        b.nettoyer()


@suite.test("jeton refusé : erreur levée, copie disponible, rien d'écrasé")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        avant = open(os.path.join(b.dossier, ip.FICHIER_COPIE), encoding="utf-8").read()
        b2 = Banc(dossier=b.dossier, jetons=[Reponse(401, {"error": "invalid_client"})])
        try:
            b2.service.get_access_machines()
            verifier(False, "une erreur était attendue")
        except Exception:
            pass
        egal(len(b2.http.gets_faits), 0, "aucune lecture sans jeton")
        egal(open(os.path.join(b.dossier, ip.FICHIER_COPIE), encoding="utf-8").read(), avant, "copie intacte")
        egal(len(ip.copie_de_secours(b2.service)), 1, "copie")
    finally:
        b.nettoyer()


@suite.test("réponse invalide : erreur levée, copie non écrasée")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        b.http.gets.append(Reponse(200, {"pas": "une liste"}))
        try:
            b.service.get_access_machines()
            verifier(False, "une erreur était attendue")
        except ValueError:
            pass
        egal(len(ip.copie_de_secours(b.service)), 1, "copie intacte")
    finally:
        b.nettoyer()


@suite.test("copie d'un autre club ou d'une autre filiale ignorée")
def _():
    b = Banc()
    try:
        b.service.get_access_machines()
        autre = Banc(dossier=b.dossier, env=dict(ENV, YOCLUB_POSTE_FILIALE="2000"))
        egal(ip.copie_de_secours(autre.service), None, "autre filiale")
        autre = Banc(dossier=b.dossier, env=dict(ENV, YOCLUB_POSTE_CLUB="vikingsgym"))
        egal(ip.copie_de_secours(autre.service), None, "autre club")
    finally:
        b.nettoyer()


@suite.test("copie absente ou abîmée -> None")
def _():
    b = Banc()
    try:
        egal(ip.copie_de_secours(b.service), None, "absente")
        with open(os.path.join(b.dossier, ip.FICHIER_COPIE), "w", encoding="utf-8") as f:
            f.write("{pas du json")
        egal(ip.copie_de_secours(b.service), None, "abîmée")
    finally:
        b.nettoyer()


@suite.test("service public (sans identité) : pas de copie de secours")
def _():
    class Public:
        def get_access_machines(self, gym_branch_id, tenant):
            return []
    egal(ip.copie_de_secours(Public()), None, "aucune copie")


@suite.test("choix du service : poste avec identité, public sans")
def _():
    dossier = tempfile.mkdtemp(prefix="poste-")
    try:
        public = object()
        egal(ip.service_machines(None, BASE, "remote-1", dossier, lambda: public), public, "sans identité")
        avec = ip.service_machines(ip.IdentitePoste.depuis_env(ENV), BASE, "remote-1", dossier, lambda: public)
        verifier(isinstance(avec, ip.ServiceMachinesPoste), "service du poste attendu")
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


@suite.test("confirmation d'une commande : route du poste, 204 oui, 404 non")
def _():
    b = Banc(gets=[Reponse(204), Reponse(404)])
    try:
        params = {"gymBranchId": "1003", "machineId": 12, "door": 2}
        egal(b.client.commande_confirmee("c1", params, 2), True, "204")
        egal(b.client.commande_confirmee("c1", params, 2), False, "404")
        url, envoyes, headers, timeout = b.http.gets_faits[0]
        egal(url, f"{BASE}/gym-management/api/poste/commandes/c1", "URL")
        egal(envoyes, params, "mêmes paramètres que la route publique")
        egal(headers["Authorization"], "Bearer jeton-1", "jeton")
        egal(headers["X-Yoclub-Poste-Numero"], "P-0007", "numéro")
        egal(timeout, 2, "délai")
    finally:
        b.nettoyer()


@suite.test("ni secret ni jeton dans le journal, même en échec")
def _():
    b = Banc(gets=[Reponse(401), Reponse(401), requests.ConnectionError("coupé")])
    try:
        with Journal() as j:
            for _ in range(2):
                try:
                    b.service.get_access_machines()
                except Exception as exc:
                    logging.warning("échec : %s", exc)
            ip.copie_de_secours(b.service)
        texte = j.texte()
        verifier(SECRET not in texte, "secret journalisé")
        verifier("jeton-" not in texte, "jeton journalisé")
    finally:
        b.nettoyer()


@suite.test("main.py : identité lue avant tout .env, copie de secours au démarrage")
def _():
    import ast
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    arbre = ast.parse(open(chemin, encoding="utf-8").read())
    imports = [n for n in arbre.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    modules = [getattr(n, "module", None) or n.names[0].name for n in imports]
    premier_projet = next(i for i, m in enumerate(modules) if m.split(".")[0] in ("domain", "services", "kafka_service", "config"))
    egal(modules[premier_projet], "services.identitePoste", "identitePoste importé avant le code du projet")
    fetch = next(n for n in arbre.body if isinstance(n, ast.FunctionDef) and n.name == "fetch_machines_with_retry")
    verifier("copie_de_secours(machineService)" in ast.unparse(fetch), "repli sur la copie au démarrage")


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
