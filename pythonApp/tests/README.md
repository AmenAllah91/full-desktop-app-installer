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
