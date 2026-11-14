import json
import logging
import os
import threading
import time

import requests

from domain.AccessMachine import AccessMachine

VARIABLES = {
    "club": "YOCLUB_POSTE_CLUB",
    "client_id": "YOCLUB_POSTE_CLIENT_ID",
    "client_secret": "YOCLUB_POSTE_CLIENT_SECRET",
    "token_url": "YOCLUB_POSTE_TOKEN_URL",
    "numero": "YOCLUB_POSTE_NUMERO",
    "filiale": "YOCLUB_POSTE_FILIALE",
}
FICHIER_COPIE = "machines_poste.json"
ROUTE_MACHINES = "/gym-management/api/poste/machines"
ROUTE_COMMANDES = "/gym-management/api/poste/commandes/"
PART_DE_VIE = 0.8
DELAI_JETON = 10
DELAI_LECTURE = 5

logger = logging.getLogger("identite_poste")

_ENV_AU_LANCEMENT = {nom: os.environ.get(nom) for nom in VARIABLES.values()}


class ErreurJeton(Exception):
    pass


class IdentitePoste:

    def __init__(self, club, client_id, client_secret, token_url, numero, filiale):
        self.club = club
        self.client_id = client_id
        self._client_secret = client_secret
        self.token_url = token_url
        self.numero = numero
        self.filiale = filiale

    @property
    def client_secret(self):
        return self._client_secret

    def __repr__(self):
        return f"IdentitePoste(club={self.club!r}, numero={self.numero!r}, filiale={self.filiale!r})"

    __str__ = __repr__

    @classmethod
    def depuis_env(cls, env=None):
        source = _ENV_AU_LANCEMENT if env is None else env
        valeurs = {champ: str(source.get(nom) or "").strip() for champ, nom in VARIABLES.items()}
        manquantes = [VARIABLES[champ] for champ, valeur in valeurs.items() if not valeur]
        if not manquantes:
            return cls(**valeurs)
        if len(manquantes) < len(VARIABLES):
            logger.warning("Identité du poste incomplète (%s manquante(s)) : routes publiques conservées",
                           ", ".join(manquantes))
        return None


class JetonPoste:

    def __init__(self, identite, http, horloge=time.monotonic):
        self._identite = identite
        self._http = http
        self._horloge = horloge
        self._verrou = threading.Lock()
        self._valeur = None
        self._renouveler_a = 0.0

    def valeur(self):
        with self._verrou:
            if self._valeur is None or self._horloge() >= self._renouveler_a:
                self._obtenir()
            return self._valeur

    def invalider(self):
        with self._verrou:
            self._valeur = None

    def _obtenir(self):
        self._valeur = None
        try:
            reponse = self._http.post(
                self._identite.token_url,
                data={"grant_type": "client_credentials",
                      "client_id": self._identite.client_id,
                      "client_secret": self._identite.client_secret},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=DELAI_JETON)
        except requests.RequestException as exc:
            raise ErreurJeton(f"jeton du poste injoignable ({type(exc).__name__})") from None
        if reponse.status_code != 200:
            raise ErreurJeton(f"jeton du poste refusé (HTTP {reponse.status_code})")
        try:
            corps = reponse.json()
            valeur = corps["access_token"]
            duree = float(corps.get("expires_in") or 0)
        except Exception:
            raise ErreurJeton("réponse de jeton illisible") from None
        if not valeur or duree <= 0:
            raise ErreurJeton("réponse de jeton incomplète")
        self._valeur = valeur
        self._renouveler_a = self._horloge() + duree * PART_DE_VIE


class ClientPoste:

    def __init__(self, identite, base_url, version, http=None, horloge=time.monotonic):
        self.identite = identite
        self.base_url = base_url.rstrip("/")
        self.version = str(version)
        self._http = http or requests.Session()
        self._jeton = JetonPoste(identite, self._http, horloge)

    def _entetes(self):
        return {"Authorization": f"Bearer {self._jeton.valeur()}",
                "X-Yoclub-Poste-Numero": self.identite.numero,
                "X-Yoclub-Poste-Version": self.version}

    def get(self, chemin, params=None, timeout=DELAI_LECTURE):
        url = f"{self.base_url}{chemin}"
        reponse = self._http.get(url, params=params, headers=self._entetes(), timeout=timeout)
        if reponse.status_code == 401:
            self._jeton.invalider()
            reponse = self._http.get(url, params=params, headers=self._entetes(), timeout=timeout)
        return reponse

    def commande_confirmee(self, command_id, params, timeout):
        reponse = self.get(f"{ROUTE_COMMANDES}{command_id}", params=params, timeout=timeout)
        return reponse.status_code == 204


def _vers_machines(donnees):
    return [AccessMachine(
        id=item.get("id"),
        alias=item.get("alias"),
        addresseip=item.get("addresseip"),
        port=item.get("port"),
        statut=item.get("statut"),
        type=item.get("type"),
        door1=item.get("door1"),
        door2=item.get("door2"),
        door3=item.get("door3"),
        door4=item.get("door4"),
        porte_type=item.get("porte_type"),
        comKey=item.get("comKey") or 0,
    ) for item in donnees]


class ServiceMachinesPoste:

    def __init__(self, client, dossier):
        self.client = client
        self.fichier = os.path.join(dossier, FICHIER_COPIE)

    def get_access_machines(self, gym_branch_id=None, tenant=None):
        identite = self.client.identite
        reponse = self.client.get(ROUTE_MACHINES, params={"branche": identite.filiale})
        reponse.raise_for_status()
        donnees = reponse.json()
        if not isinstance(donnees, list) or not all(isinstance(item, dict) for item in donnees):
            raise ValueError("liste des pointeuses du poste illisible")
        machines = _vers_machines(donnees)
        self._ecrire_copie(donnees)
        return machines

    def _ecrire_copie(self, donnees):
        identite = self.client.identite
        contenu = {"club": identite.club, "filiale": identite.filiale, "enregistreeLe": int(time.time()),
                   "machines": donnees}
        temporaire = f"{self.fichier}.tmp"
        try:
            with open(temporaire, "w", encoding="utf-8") as f:
                json.dump(contenu, f, ensure_ascii=False)
            os.replace(temporaire, self.fichier)
        except OSError as exc:
            logger.warning("Copie locale des pointeuses non enregistrée : %s", exc)

    def lire_copie(self):
        identite = self.client.identite
        try:
            with open(self.fichier, encoding="utf-8") as f:
                contenu = json.load(f)
            if contenu.get("club") != identite.club or str(contenu.get("filiale")) != identite.filiale:
                logger.warning("Copie locale des pointeuses d'un autre club ou d'une autre filiale : ignorée")
                return None
            donnees = contenu["machines"]
            if not isinstance(donnees, list):
                return None
            return _vers_machines(donnees)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("Copie locale des pointeuses illisible : %s", type(exc).__name__)
            return None


def copie_de_secours(service):
    lire = getattr(service, "lire_copie", None)
    return lire() if callable(lire) else None


def service_machines(identite, base_url, version, dossier, public):
    if identite is None:
        return public()
    return ServiceMachinesPoste(ClientPoste(identite, base_url, version), dossier)
