"""Harnais de test minimal — stdlib uniquement.

Volontairement sans pytest : il n'est pas installé dans le venv du pont, et
ajouter une dépendance à un poste de production pour lancer des tests serait
un mauvais compromis. Le besoin ici est simple (assertions + compte-rendu),
une centaine de lignes suffisent.
"""
import sys
import time
import traceback

# Couleurs ANSI, désactivées si la sortie est redirigée
_TTY = sys.stdout.isatty()
VERT = "\033[92m" if _TTY else ""
ROUGE = "\033[91m" if _TTY else ""
JAUNE = "\033[93m" if _TTY else ""
GRAS = "\033[1m" if _TTY else ""
RAZ = "\033[0m" if _TTY else ""


class EchecTest(AssertionError):
    pass


def verifier(condition, message):
    if not condition:
        raise EchecTest(message)


def egal(obtenu, attendu, message=""):
    if obtenu != attendu:
        raise EchecTest(f"{message} — attendu {attendu!r}, obtenu {obtenu!r}")


def parmi(obtenu, attendus, message=""):
    if obtenu not in attendus:
        raise EchecTest(f"{message} — attendu l'un de {attendus!r}, obtenu {obtenu!r}")


class Suite:
    def __init__(self, titre):
        self.titre = titre
        self.tests = []
        self.reussis = 0
        self.echecs = []
        self.ignores = 0

    def test(self, nom):
        """Décorateur d'enregistrement d'un test."""
        def deco(fn):
            self.tests.append((nom, fn))
            return fn
        return deco

    def executer(self):
        print(f"\n{GRAS}{self.titre}{RAZ}")
        print("-" * 76)
        debut = time.time()

        for nom, fn in self.tests:
            try:
                resultat = fn()
                if resultat == "ignore":
                    self.ignores += 1
                    print(f"  {JAUNE}skip{RAZ}  {nom}")
                else:
                    self.reussis += 1
                    print(f"  {VERT}ok{RAZ}    {nom}")
            except EchecTest as e:
                self.echecs.append((nom, str(e)))
                print(f"  {ROUGE}ECHEC{RAZ} {nom}")
                print(f"        {e}")
            except Exception as e:
                trace = traceback.format_exc(limit=3).strip().splitlines()[-1]
                self.echecs.append((nom, f"{type(e).__name__}: {e}"))
                print(f"  {ROUGE}ERREUR{RAZ} {nom}")
                print(f"        {type(e).__name__}: {e}")
                print(f"        {trace}")

        duree = time.time() - debut
        print("-" * 76)
        total = self.reussis + len(self.echecs) + self.ignores
        etat = f"{ROUGE}{len(self.echecs)} echec(s){RAZ}" if self.echecs else f"{VERT}tout passe{RAZ}"
        print(f"{self.reussis}/{total} reussis, {self.ignores} ignore(s) — {etat}  ({duree:.2f}s)")
        return len(self.echecs) == 0
