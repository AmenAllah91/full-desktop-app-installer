"""Tests du pont NE NÉCESSITANT NI MACHINES NI pythonApp démarré.

Logique pure, exécutable n'importe où : ils protègent les règles de parsing
et de filtrage sur lesquelles reposent l'import d'empreintes et le ciblage
par branche.

    venv\\Scripts\\python.exe tests\\test_sans_machines.py
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harnais import Suite, verifier, egal  # noqa: E402

suite = Suite("PONT — logique pure (aucune machine requise)")


# ─── Filtre de branche des actions d'empreinte ───────────────────────────
#
# Réplique de la règle de main.py::process_fingerprint_actions. Un message
# sans gymBranchId doit rester diffusé à toutes les branches (compatibilité
# avec les postes non mis à jour) ; avec gymBranchId, seul le poste concerné
# doit le traiter.

def branche_traite(message_branche, poste_branche):
    cible = message_branche
    if cible is not None and str(cible) != str(poste_branche):
        return False
    return True


@suite.test("message sans gymBranchId -> traite par toutes les branches")
def _():
    verifier(branche_traite(None, "1003"), "un message sans branche doit etre traite")


@suite.test("message cible sur la branche du poste -> traite")
def _():
    verifier(branche_traite("1003", "1003"), "meme branche, doit traiter")


@suite.test("message cible sur une AUTRE branche -> ignore")
def _():
    verifier(not branche_traite("1004", "1003"), "autre branche, ne doit pas traiter")


@suite.test("comparaison int/str tolerante (1003 == '1003')")
def _():
    verifier(branche_traite(1003, "1003"), "le type ne doit pas changer le resultat")


# ─── Parsing templatev10 (C3 / PullSDK) ──────────────────────────────────
#
# Le SDK renvoie tantôt du CSV avec entête, tantôt du clé=valeur. Une
# régression ici casserait l'import d'empreintes depuis une pointeuse C3.

def _parser():
    from services.adapters import PlcommAdapter
    return PlcommAdapter.__dict__["_parse_templatev10_rows"]


@suite.test("parse le format CSV avec entete")
def _():
    parse = _parser()
    brut = ("Size,UID,Pin,FingerID,Valid,Template,Resverd,EndTag\r\n"
            "1198,12,3040,1,1,QUJDRA==,,\r\n"
            "1198,12,3040,2,1,RUZHSA==,,")
    lignes = parse(None, brut)
    egal(len(lignes), 2, "deux gabarits attendus")
    egal(lignes[0]["Pin"], "3040", "Pin mal extrait")
    egal(lignes[0]["FingerID"], "1", "FingerID mal extrait")
    egal(lignes[1]["Template"], "RUZHSA==", "Template mal extrait")


@suite.test("parse le format cle=valeur separe par tabulations")
def _():
    parse = _parser()
    brut = "Pin=3040\tFingerID=1\tTemplate=QUJDRA==\r\nPin=3040\tFingerID=2\tTemplate=RUZHSA=="
    lignes = parse(None, brut)
    egal(len(lignes), 2, "deux gabarits attendus")
    egal(lignes[0]["Pin"], "3040", "Pin mal extrait en KV")
    egal(lignes[1]["FingerID"], "2", "FingerID mal extrait en KV")


@suite.test("entree vide -> liste vide, pas d'exception")
def _():
    parse = _parser()
    egal(parse(None, ""), [], "chaine vide")
    egal(parse(None, None), [], "None")


@suite.test("CSV sans entete (1re ligne numerique) -> entete par defaut")
def _():
    parse = _parser()
    brut = "1198,12,3040,1,1,QUJDRA==,,"
    lignes = parse(None, brut)
    egal(len(lignes), 1, "une ligne attendue")
    egal(lignes[0]["Pin"], "3040", "repli sur l'entete connu")


# ─── Encodage des gabarits ───────────────────────────────────────────────

@suite.test("aller-retour base64 d'un gabarit binaire")
def _():
    gabarit = bytes(range(256)) * 4
    encode = base64.b64encode(gabarit).decode("utf-8")
    egal(base64.b64decode(encode), gabarit, "le gabarit doit survivre a l'aller-retour")


@suite.test("base64 invalide -> erreur detectable (route /fingerprint/push -> 400)")
def _():
    try:
        base64.b64decode("pas du base64 !!!", validate=True)
        raise AssertionError("aurait du lever")
    except AssertionError:
        raise
    except Exception:
        pass  # comportement attendu


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
