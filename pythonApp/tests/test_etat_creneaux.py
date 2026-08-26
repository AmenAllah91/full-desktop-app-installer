"""Rattrapage des calendriers — aucune machine, aucun réseau.

    venv\\Scripts\\python.exe tests\\test_etat_creneaux.py

Ce que ces tests protègent :

  - le TROU que ce mécanisme est là pour boucher. Une pointeuse éteinte plus
    longtemps que la rétention Kafka gardait un calendrier périmé en silence :
    sa tâche était purgée au bout de 24 h et plus rien ne la rejouait. L'état
    voulu, lui, doit survivre à cette purge ET au redémarrage du pont.
  - le fait que la réconciliation NE réécrive PAS à chaque tour de boucle.
    refresh_machines_loop passe toutes les 30 s sur chaque machine : sans le
    verrou de révision, tout le parc verrait ses dix calendriers réécrits en
    permanence.
  - la MIGRATION : timezone_state est créée sur des bases SQLite déjà en
    service chez les clients. Une base à l'ancien schéma doit s'ouvrir sans
    perdre ses tâches.

Le chargement par l'AST est celui de test_file_backoff.py, et pour la même
raison : importer main.py exécute la résolution d'identité, Kafka et des
écritures dans APPDATA. C'est bien le code réel qui est testé.
"""
import ast
import json
import logging
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harnais import Suite, verifier, egal  # noqa: E402

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "main.py")

VOULUS = {
    "_migrer_task_queue", "initialize_task_queue_db", "add_task_to_queue",
    "get_next_task_from_queue", "requeue_tasks_for_machine", "defer_task",
    "BACKOFF_PALIERS", "_delai_backoff",
    "enregistrer_creneau_voulu", "revision_creneaux", "creneaux_voulus",
    "reconcilier_creneaux", "_creneaux_appliques",
}


def _charger():
    src = open(MAIN, encoding="utf-8").read()
    gardes = []
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name in VOULUS:
            gardes.append(n)
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in VOULUS for t in n.targets):
            gardes.append(n)

    manquants = VOULUS - ({n.name for n in gardes if isinstance(n, ast.FunctionDef)}
                          | {t.id for n in gardes if isinstance(n, ast.Assign)
                             for t in n.targets if isinstance(t, ast.Name)})
    if manquants:
        raise AssertionError(
            f"introuvables dans main.py : {sorted(manquants)} — renommees ou deplacees ?")

    mod = ast.Module(body=gardes, type_ignores=[])
    ast.fix_missing_locations(mod)
    espace = {"sqlite3": sqlite3, "json": json, "logging": logging, "DB_FILE": None}
    exec(compile(mod, MAIN, "exec"), espace)
    return espace


def _base_neuve(espace):
    espace["DB_FILE"] = os.path.join(tempfile.mkdtemp(), "task_queue.db")
    espace["initialize_task_queue_db"]()
    return espace["DB_FILE"]


class Machine:
    """Le strict minimum que reconcilier_creneaux lit sur une machine."""

    def __init__(self, mid=7, ip="192.168.2.230", port=4370):
        self.id = mid
        self.addresseip = ip
        self.port = port


def _taches(db):
    c = sqlite3.connect(db)
    try:
        return [json.loads(r[0]) for r in
                c.execute("SELECT task_data FROM task_queue ORDER BY id")]
    finally:
        c.close()


suite = Suite("CRENEAUX — etat voulu et rattrapage (aucune machine requise)")


# ─── L'état voulu se retient ─────────────────────────────────────────────

@suite.test("un creneau enregistre se relit a l'identique")
def _():
    espace = _charger()
    _base_neuve(espace)
    horaire = {"monday": "09:00-12:00", "sunday": None}
    espace["enregistrer_creneau_voulu"](3, horaire)

    egal(espace["creneaux_voulus"](), [(3, horaire)],
         "l'horaire doit revenir tel quel, valeurs nulles comprises")


@suite.test("reenregistrer le meme creneau le REMPLACE au lieu de l'empiler")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"monday": "09:00-12:00"})
    espace["enregistrer_creneau_voulu"](3, {"monday": "14:00-18:00"})

    egal(espace["creneaux_voulus"](), [(3, {"monday": "14:00-18:00"})],
         "un calendrier est un ETAT : la derniere valeur ecrase la precedente")


@suite.test("plusieurs creneaux coexistent, tries par slot")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](5, {"friday": "18:00-22:00"})
    espace["enregistrer_creneau_voulu"](2, {"monday": "06:00-09:00"})

    egal([s for s, _ in espace["creneaux_voulus"]()], [2, 5],
         "l'ordre par slot rend le rattrapage previsible")


@suite.test("chaque enregistrement fait avancer la revision")
def _():
    espace = _charger()
    _base_neuve(espace)
    egal(espace["revision_creneaux"](), 0, "base neuve = rien n'a jamais ete recu")

    espace["enregistrer_creneau_voulu"](3, {"monday": "09:00-12:00"})
    premiere = espace["revision_creneaux"]()
    verifier(premiere > 0, "la premiere reception doit sortir de zero")

    espace["enregistrer_creneau_voulu"](3, {"monday": "14:00-18:00"})
    verifier(espace["revision_creneaux"]() > premiere,
             "modifier un creneau deja connu doit AUSSI avancer la revision, "
             "sinon les machines ne verraient jamais le changement")


@suite.test("un horaire illisible en base locale n'emporte pas les autres")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](2, {"monday": "06:00-09:00"})
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})

    c = sqlite3.connect(db)
    c.execute("UPDATE timezone_state SET weekly_schedule = '{ceci n est pas du json' "
              "WHERE slot = 2")
    c.commit()
    c.close()

    egal(espace["creneaux_voulus"](), [(3, {"friday": "18:00-22:00"})],
         "le creneau sain doit survivre au creneau corrompu")


# ─── Le rattrapage ───────────────────────────────────────────────────────

@suite.test("une machine en retard recoit TOUS les creneaux connus")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](2, {"monday": "06:00-09:00"})
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})

    egal(espace["reconcilier_creneaux"](Machine()), 2, "deux creneaux a poser")

    taches = _taches(db)
    egal(len(taches), 2, "une tache par creneau")
    egal({t["operation"] for t in taches}, {"UPDATE_TIMEZONE"},
         "le rattrapage passe par l'operation normale, pas par un chemin a part")
    egal(sorted(t["timezone_slot"] for t in taches), [2, 3])
    egal(taches[0]["machineId"], 7, "adressee a la machine qui revient")
    egal(taches[0]["port"], "4370", "le port doit etre une CHAINE, comme partout ailleurs")


@suite.test("une machine deja a jour ne recoit RIEN au tour suivant")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})
    m = Machine()

    egal(espace["reconcilier_creneaux"](m), 1, "premier passage : rattrapage")
    egal(espace["reconcilier_creneaux"](m), 0,
         "refresh_machines_loop repasse toutes les 30 s : sans ce verrou, tout "
         "le parc serait reecrit en permanence")
    egal(len(_taches(db)), 1, "aucune tache supplementaire ne doit apparaitre")


@suite.test("un creneau modifie apres coup relance le rattrapage")
def _():
    espace = _charger()
    espace["_creneaux_appliques"].clear()
    db = _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})
    m = Machine()
    espace["reconcilier_creneaux"](m)

    espace["enregistrer_creneau_voulu"](3, {"friday": "20:00-23:00"})
    egal(espace["reconcilier_creneaux"](m), 1,
         "la revision a bouge : la machine doit repasser")


@suite.test("deux machines sont suivies separement")
def _():
    espace = _charger()
    espace["_creneaux_appliques"].clear()
    _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})

    egal(espace["reconcilier_creneaux"](Machine(mid=7)), 1)
    egal(espace["reconcilier_creneaux"](Machine(mid=8)), 1,
         "une machine a jour ne doit pas dispenser sa voisine du rattrapage")
    egal(espace["reconcilier_creneaux"](Machine(mid=7)), 0)


@suite.test("un pont qui n'a jamais rien recu ne pose aucune tache")
def _():
    espace = _charger()
    espace["_creneaux_appliques"].clear()
    db = _base_neuve(espace)

    egal(espace["reconcilier_creneaux"](Machine()), 0,
         "installation neuve : rien a rattraper, et surtout rien a inventer")
    egal(_taches(db), [], "aucune ecriture ne doit partir vers la pointeuse")


@suite.test("le rattrapage survit a la PURGE des taches — le trou d'origine")
def _():
    espace = _charger()
    espace["_creneaux_appliques"].clear()
    db = _base_neuve(espace)

    # La machine etait eteinte : le message arrive, la tache est mise en file...
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})
    espace["add_task_to_queue"]({"machineId": 7, "operation": "UPDATE_TIMEZONE",
                                 "timezone_slot": 3})

    # ...puis purgee au bout de 24 h, comme le fait abandonner_taches_expirees.
    c = sqlite3.connect(db)
    c.execute("DELETE FROM task_queue")
    c.commit()
    c.close()
    egal(_taches(db), [], "la file est bien vide, l'ancien mecanisme a tout perdu")

    egal(espace["reconcilier_creneaux"](Machine()), 1,
         "l'etat voulu doit rattraper ce que la file a laisse tomber")
    egal(_taches(db)[0]["timezone_slot"], 3)


@suite.test("le rattrapage survit au REDEMARRAGE du pont")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})
    espace["reconcilier_creneaux"](Machine())

    # Redemarrage : nouvel espace de noms, memoire volatile perdue, MEME base.
    apres = _charger()
    apres["DB_FILE"] = db
    egal(apres["revision_creneaux"](), espace["revision_creneaux"](),
         "l'etat voulu est sur disque, il doit traverser le redemarrage")
    egal(apres["reconcilier_creneaux"](Machine()), 1,
         "au demarrage on reapplique une fois : c'est le filet recherche")


# ─── Migration ───────────────────────────────────────────────────────────

@suite.test("base a l'ANCIEN schema -> timezone_state creee sans perte")
def _():
    espace = _charger()
    db = os.path.join(tempfile.mkdtemp(), "task_queue.db")
    espace["DB_FILE"] = db

    c = sqlite3.connect(db)
    c.execute("CREATE TABLE task_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "task_data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', "
              "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("INSERT INTO task_queue (task_data) VALUES ('{\"machineId\": 7}')")
    c.commit()
    c.close()

    espace["initialize_task_queue_db"]()

    c = sqlite3.connect(db)
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        restantes = c.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
    finally:
        c.close()

    verifier("timezone_state" in tables, "la table doit etre creee sur une base existante")
    egal(restantes, 1, "les taches deja en file ne doivent pas disparaitre")


@suite.test("initialiser deux fois ne perd pas l'etat deja retenu")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["enregistrer_creneau_voulu"](3, {"friday": "18:00-22:00"})

    espace["initialize_task_queue_db"]()          # CREATE TABLE IF NOT EXISTS

    egal(espace["creneaux_voulus"](), [(3, {"friday": "18:00-22:00"})],
         "un redemarrage ne doit pas remettre les creneaux a zero")


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
