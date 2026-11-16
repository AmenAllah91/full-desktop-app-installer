import time
from datetime import datetime

DELAI_PASSAGE_S = 1.5
ETAT_REFUS = -1
REFUS_VALIDITE_EXPIREE = "VALIDITE_EXPIREE"
REFUS_HORS_CRENEAU = "HORS_CRENEAU"
REFUS_POINTEUSE = "REFUS_POINTEUSE"
C3_VALIDITE_EXPIREE = frozenset({29, 33})
C3_HORS_CRENEAU = frozenset({21, 22, 35})
FORMATS_DATE = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


def pin_valide(valeur):
    try:
        pin = int(str(valeur).strip())
    except (TypeError, ValueError):
        return None
    return pin if pin > 0 else None


class SuiviVerifications:
    def __init__(self, delai=DELAI_PASSAGE_S, horloge=time.time):
        self._delai = delai
        self._horloge = horloge
        self._attente = {}
        self._dernier_passage = None

    def verifie(self, pin):
        pin = pin_valide(pin)
        if pin is None:
            return
        maintenant = self._horloge()
        if self._dernier_passage is not None and maintenant - self._dernier_passage < self._delai:
            return
        self._attente.setdefault(pin, maintenant)

    def passage(self, pin=None):
        self._attente.clear()
        self._dernier_passage = self._horloge()

    def refus_echus(self):
        if not self._attente:
            return []
        maintenant = self._horloge()
        echus = [pin for pin, instant in self._attente.items() if maintenant - instant >= self._delai]
        for pin in echus:
            del self._attente[pin]
        return echus


def date_lue(valeur):
    texte = str(valeur or "").strip()
    for forme in FORMATS_DATE:
        try:
            return datetime.strptime(texte, forme)
        except ValueError:
            continue
    return None


def motif_standalone(fin_validite, maintenant=None):
    fin = date_lue(fin_validite)
    if fin is None:
        return REFUS_POINTEUSE
    return REFUS_VALIDITE_EXPIREE if fin < (maintenant or datetime.now()) else REFUS_POINTEUSE


def motif_c3(type_evenement):
    if type_evenement in C3_VALIDITE_EXPIREE:
        return REFUS_VALIDITE_EXPIREE
    if type_evenement in C3_HORS_CRENEAU:
        return REFUS_HORS_CRENEAU
    return None


def lire_validite(zk, numero_machine, pin):
    for arguments in ((numero_machine, str(pin)), (numero_machine, str(pin), 0, 0, "", "")):
        try:
            lecture = zk.GetUserValidDate(*arguments)
        except Exception:
            continue
        if isinstance(lecture, (tuple, list)) and lecture:
            return bool(lecture[0]), (lecture[4] if len(lecture) >= 5 else None)
        return bool(lecture), None
    return None, None
