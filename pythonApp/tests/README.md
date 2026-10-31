# Tests de non-régression

Deux suites **séparées volontairement** : sans matériel, on peut quand même
valider l'essentiel.

## 1. Sans machines — exécutables partout

```
venv\Scripts\python.exe tests\test_sans_machines.py
```

Logique pure, aucun réseau, aucune pointeuse, pythonApp arrêté :

- filtre de branche des actions d'empreinte (`gymBranchId`)
- parsing `templatev10` du PullSDK (CSV avec/sans en-tête, clé=valeur)
- encodage base64 des gabarits
- découpage du tampon temps réel (`evenements_du_tampon`) — plusieurs trames
  collées dans un même `GetRTLog`, régression du 19/08/2026
- réutilisation de session C3 (`ensure_c3_session`) — emprunter la session en
  cours, ne renouveler qu'après un échec
- horloge des panneaux (`synchroniser_horloge`) — encodage ZKTeco et cadence de
  contrôle ; la partie SDK a été vérifiée sur un C3 réel (+140 s → +0,8 s)

## 1 bis. File de tâches — temporisation du rejeu

```
venv\Scripts\python.exe tests\test_file_backoff.py
```

Suite séparée : elle charge les fonctions de file de `main.py` par l'AST, faute
de pouvoir l'importer (Kafka, identité, écritures dans APPDATA au niveau
module). Elle protège surtout la **migration de schéma** — les colonnes
`attempts` et `next_attempt_at` sont ajoutées à des bases SQLite déjà en
service chez les clients, c'est le risque de déploiement le plus sérieux du
lot.

## 1 ter. Commandes à distance (`remote_commands.py`)

```
venv\Scripts\python.exe tests\test_commandes_distance.py
```

Sans machine, sans réseau, sans Kafka : le module est piloté avec des doublures.

- réglage `REMOTE_COMMANDS_ENABLED` éteint par défaut, et `start()` qui ne crée rien quand il l'est ;
- démarrage dans `main.py` en toute fin, juste avant la boucle principale, protégé par un `try/except`
  (lecture de l'AST) ;
- commande d'une autre filiale ignorée, commande reçue après l'échéance refusée sans toucher la pointeuse ;
- pointeuse inconnue ou porte invalide refusées **avant** la vérification, pour ne pas consommer la
  commande au serveur ;
- vérification auprès du serveur obligatoire avant toute ouverture, avec la cible exacte (pointeuse,
  porte, adhérent) : le serveur ne confirme qu'une fois et que ce qu'il a enregistré ;
- même geste que `/door/open` (C3 : porte 1..4 ; autonome : durée seule), durée lue et bornée (1..10 s)
  **avant** de prendre le verrou, verrou de la machine attendu au plus jusqu'à l'échéance puis toujours
  rendu, et rendu avant de publier le résultat ;
- C3 sans session (pointeuse hors ligne ou session en cours de renouvellement) : `open_door` n'est
  jamais appelé — il ouvrirait une seconde session —, on attend la session par pas de 0,2 s jusqu'à
  l'échéance ;
- autonome : sonde TCP bornée puis `zk_sdk_lock` attendu au plus jusqu'à l'échéance ;
- une ouverture terminée après l'échéance reste `OPENED`, avec son retard en détail ;
- COM initialisé dans le fil des commandes ;
- passage publié seulement après une ouverture pour un adhérent, sens tiré de la porte (C3) ou de la
  fiche (autonome, comme `MonitorZkem`) ; signal de présence.

## 2. Avec machines — conditions requises

```
venv\Scripts\python.exe tests\test_avec_machines.py
```

Conditions vérifiées **avant** tout test :

| Condition | Attendu |
|---|---|
| pythonApp | répond sur `http://localhost:9998` |
| standalone | `192.168.2.12` (comKey 123456), connectée |
| C3 | `192.168.1.205`, connectée |

Si une condition manque, le script s'arrête en **nommant précisément** ce qui
manque et sort en **code 2** — sans exécuter le moindre test. Un test rouge
faute de matériel serait indiscernable d'une vraie régression.

Contenu : contrats des routes `/api/devices`, `/api/machines/status`,
`/getFingerprints`, `/getFace`, `/fingerprint/push`, `/health`.

**Non destructifs** : aucune empreinte supprimée, aucune porte ouverte.

## Codes de sortie

| Code | Signification |
|---|---|
| 0 | tout passe |
| 1 | au moins un test en échec |
| 2 | conditions non réunies (suite « avec machines » seulement) |

## Côté backend Java

```
mvnw.cmd -o test -Dtest=ToleranceEtBlocageTest
```

Blocage administratif et tolérance de paiement — 24 tests, aucune machine ni
base de données requise.
