"""Temporisation du rejeu des tâches — aucune machine, aucun réseau.

    venv\\Scripts\\python.exe tests\\test_file_backoff.py

Ce que ces tests protègent :

  - la MIGRATION DE SCHÉMA. Les colonnes attempts/next_attempt_at sont ajoutées
    à des bases SQLite déjà en service sur les postes clients. CREATE TABLE IF
    NOT EXISTS ne fait rien sur une table existante : sans migration, un poste
    déjà installé planterait au premier report de tâche. C'est le risque de
    déploiement le plus sérieux du lot.
  - le barème des paliers et le gel du rejeu tant que le délai court.
  - la libération sur succès, qui ne doit PAS effacer l'historique d'échecs.

Pourquoi charger main.py par l'AST plutôt que l'importer : son import exécute
la résolution d'identité, la création des services Kafka et des écritures dans
APPDATA. Une suite « sans machines » ne peut pas dépendre de tout ça. On extrait
donc les définitions concernées et on les exécute dans un espace de noms isolé
— c'est bien le CODE RÉEL de main.py qui est testé, pas une copie.

Le jour où ces fonctions vivront dans leur propre module, ce chargement
disparaîtra au profit d'un simple import.
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
    "BACKOFF_PALIERS", "_delai_backoff", "_migrer_task_queue",
    "initialize_task_queue_db", "add_task_to_queue", "get_next_task_from_queue",
    "mark_task_as_completed", "delete_completedTasks", "defer_task",
    "requeue_tasks_for_machine", "reset_backoff_for_machine",
    "purge_orphan_deferred_tasks",
    "TACHE_DUREE_MAX_H", "abandonner_taches_expirees",
}


def _charger():
    """Extrait de main.py les seules définitions liées à la file de tâches."""
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


def _colonnes(db):
    c = sqlite3.connect(db)
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(task_queue)")}
    finally:
        c.close()


def _ligne(db, tid):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT attempts, next_attempt_at FROM task_queue "
                         "WHERE id = ?", (tid,)).fetchone()
    finally:
        c.close()


def _echoir(db):
    """Ramène l'échéance dans le passé au lieu d'attendre réellement."""
    c = sqlite3.connect(db)
    c.execute("UPDATE task_queue SET next_attempt_at = datetime('now', '-1 seconds')")
    c.commit()
    c.close()


suite = Suite("FILE DE TACHES — temporisation du rejeu (aucune machine requise)")


# ─── Migration de schéma ─────────────────────────────────────────────────

@suite.test("base a l'ANCIEN schema -> colonnes ajoutees sans perte de donnees")
def _():
    espace = _charger()
    db = os.path.join(tempfile.mkdtemp(), "task_queue.db")
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE task_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_data TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("INSERT INTO task_queue (task_data, status) VALUES (?, 'WAITING_MACHINE')",
              (json.dumps({"machineId": 42}),))
    c.commit()
    c.close()
    verifier("attempts" not in _colonnes(db), "l'ancien schema ne doit pas les avoir")

    espace["DB_FILE"] = db
    espace["initialize_task_queue_db"]()
    cols = _colonnes(db)
    verifier("attempts" in cols, "colonne 'attempts' manquante apres migration")
    verifier("next_attempt_at" in cols, "colonne 'next_attempt_at' manquante apres migration")

    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM task_queue").fetchone()[0]
    c.close()
    egal(n, 1, "la tache existante ne doit pas etre perdue")


@suite.test("migration rejouee plusieurs fois -> idempotente")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    for _i in range(3):
        espace["initialize_task_queue_db"]()
    verifier("attempts" in _colonnes(db), "le schema doit rester intact")


@suite.test("tache anterieure a la migration (echeance NULL) -> rejouee tout de suite")
def _():
    # Compatibilite ascendante : le backlog d'un poste mis a jour ne doit pas
    # se retrouver gele en attendant une echeance qu'il n'a jamais eue.
    espace = _charger()
    db = _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    c = sqlite3.connect(db)
    c.execute("UPDATE task_queue SET status='WAITING_MACHINE', next_attempt_at=NULL "
              "WHERE id = ?", (tid,))
    c.commit()
    c.close()
    egal(espace["requeue_tasks_for_machine"](7), 1, "doit etre rejouee sans attendre")


# ─── Barème ──────────────────────────────────────────────────────────────

@suite.test("bareme des paliers, plafond compris")
def _():
    d = _charger()["_delai_backoff"]
    for tentatives, sec in {1: 60, 2: 120, 3: 300, 4: 600, 5: 900, 44: 900}.items():
        egal(d(tentatives), sec, f"essai {tentatives}")


@suite.test("valeur aberrante (0) -> premier palier, pas d'IndexError")
def _():
    egal(_charger()["_delai_backoff"](0), 60, "un compteur a 0 ne doit pas casser")


# ─── Comportement de bout en bout ────────────────────────────────────────

@suite.test("report -> compteur incremente et echeance posee")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]

    egal(espace["defer_task"](tid), (1, 60), "1er report")
    egal(espace["defer_task"](tid), (2, 120), "2e report")
    attempts, echeance = _ligne(db, tid)
    egal(attempts, 2, "compteur d'echecs")
    verifier(echeance is not None, "une echeance doit etre posee")


@suite.test("tache temporisee -> PAS rejouee tant que le delai court")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    egal(espace["requeue_tasks_for_machine"](7), 0,
         "c'est tout l'objet du correctif : ne plus rejouer immediatement")


@suite.test("delai ecoule -> rejouee, et seulement par SA machine")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    _echoir(db)
    egal(espace["requeue_tasks_for_machine"](99), 0, "une autre machine ne la reprend pas")
    egal(espace["requeue_tasks_for_machine"](7), 1, "sa machine la reprend")


# ─── Libération sur succès ───────────────────────────────────────────────

@suite.test("succes sur la machine -> attente levee immediatement")
def _():
    espace = _charger()
    db = _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    egal(espace["requeue_tasks_for_machine"](7), 0, "temporisee au depart")

    egal(espace["reset_backoff_for_machine"](7), 1, "1 tache doit etre liberee")
    egal(espace["requeue_tasks_for_machine"](7), 1, "puis rejouee sans attendre")
    verifier(_ligne(db, tid)[1] is None, "l'echeance doit etre effacee")


@suite.test("liberation -> l'historique d'echecs est CONSERVE")
def _():
    # Sur un panneau degrade qui honore une commande sur cinq, remettre le
    # compteur a zero a chaque succes empecherait la temporisation de depasser
    # son premier palier — le backoff serait sans effet la ou il sert.
    espace = _charger()
    db = _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    for _i in range(4):
        espace["defer_task"](tid)
    espace["reset_backoff_for_machine"](7)

    egal(_ligne(db, tid)[0], 4, "le compteur d'echecs ne doit PAS etre remis a zero")
    egal(espace["defer_task"](tid), (5, 900),
         "un echec apres liberation doit repartir au palier suivant")


# ─── Non-régression de la purge ──────────────────────────────────────────

@suite.test("purge : machine connue et tache recente -> rien n'est supprime")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    egal(espace["purge_orphan_deferred_tasks"]({7}, max_age_hours=24), 0,
         "la temporisation ne doit pas exposer une tache a la purge")


# ─── Plafond de la branche « injoignable » ───────────────────────────────
#
# Sans plafond, une tache qui ne peut pas aboutir est rejouee indefiniment.
# Chez vikingsgym : 12 taches a 33 essais, encore actives apres 24 h, et 0
# tache abandonnee en quatre jours. Le delai d'une journee a ete arrete avec
# l'utilisateur le 2026-08-21 ; l'accueil rejoue a la main si besoin.

def _vieillir(espace, tid, heures):
    """Recule created_at au lieu d'attendre reellement."""
    c = sqlite3.connect(espace["DB_FILE"])
    c.execute("UPDATE task_queue SET created_at = datetime('now', ?) WHERE id = ?",
              ("-%d hours" % heures, tid))
    c.commit()
    c.close()


def _statut(espace, tid):
    c = sqlite3.connect(espace["DB_FILE"])
    try:
        return c.execute("SELECT status FROM task_queue WHERE id = ?",
                         (tid,)).fetchone()[0]
    finally:
        c.close()


@suite.test("tache en attente depuis plus de 24 h -> abandonnee")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7, "user_pin": "6350",
                                 "operation": "ADD_USER"})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    _vieillir(espace, tid, 30)

    egal(espace["abandonner_taches_expirees"](), 1, "la tache doit etre abandonnee")
    egal(_statut(espace, tid), "COMPLETED",
         "une tache abandonnee sort de la file, elle ne doit plus etre rejouee")


@suite.test("tache en attente depuis moins de 24 h -> conservee")
def _():
    espace = _charger()
    _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    espace["defer_task"](tid)
    _vieillir(espace, tid, 23)

    egal(espace["abandonner_taches_expirees"](), 0,
         "23 h ne doit pas suffire : une salle fermee un week-end doit survivre")
    egal(_statut(espace, tid), "WAITING_MACHINE", "la tache reste en attente")


@suite.test("seules les taches EN ATTENTE sont concernees")
def _():
    # Une tache PENDING ancienne n'a pas encore ete tentee : la vieillir ne doit
    # pas la detruire, sinon une file en retard se viderait toute seule.
    espace = _charger()
    _base_neuve(espace)
    espace["add_task_to_queue"]({"machineId": 7})
    tid = espace["get_next_task_from_queue"]()[0]
    _vieillir(espace, tid, 48)

    egal(espace["abandonner_taches_expirees"](), 0,
         "une tache PENDING ancienne ne doit pas etre abandonnee")


@suite.test("le delai par defaut est bien d'une journee")
def _():
    espace = _charger()
    egal(espace["TACHE_DUREE_MAX_H"], 24,
         "valeur arretee avec l'utilisateur : abandon au bout d'une journee")


if __name__ == "__main__":
    sys.exit(0 if suite.executer() else 1)
