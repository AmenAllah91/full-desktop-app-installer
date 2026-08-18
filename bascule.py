"""
Bascule le pont Python entre INTEGRATION et PRODUCTION.

    python bascule.py          -> affiche l'environnement courant
    python bascule.py int      -> bascule sur l'integration
    python bascule.py prod     -> bascule sur la PRODUCTION

Ne modifie que les deux lignes YOGYM_BASE_URL et KAFKA_BROKER. Tout le reste du
fichier — commentaires, lignes vides, ordre, encodage — est preserve a l'octet
pres.

Pourquoi un script et pas des lignes commentees a basculer a la main : le
basculement doit etre ATOMIQUE. Un poste client s'est retrouve avec l'URL de
production et le broker d'integration, et a perdu deux jours de pointages sans
que rien ne le signale. Ici les deux lignes changent ensemble, ou pas du tout.
"""
import sys
from pathlib import Path

ENV = Path(__file__).parent / "pythonApp" / ".env"

CIBLES = {
    "int": {
        "nom": "INTEGRATION",
        "YOGYM_BASE_URL": "https://integration.yogym.co",
        "KAFKA_BROKER": "54.38.35.221:9094",
    },
    "prod": {
        "nom": "PRODUCTION",
        "YOGYM_BASE_URL": "https://app.yogym.co",
        "KAFKA_BROKER": "51.178.55.238:9094",
    },
}

CLES = ("YOGYM_BASE_URL", "KAFKA_BROKER")


def lire():
    if not ENV.exists():
        print(f"[ERREUR] Fichier introuvable : {ENV}")
        sys.exit(1)
    # newline="" : pas de traduction universelle des fins de ligne, sinon le
    # fichier CRLF serait reecrit en LF.
    with open(ENV, "r", encoding="utf-8", newline="") as f:
        return f.read()


def valeurs(texte):
    trouve = {}
    for ligne in texte.splitlines():
        nue = ligne.strip()
        if nue.startswith("#") or "=" not in nue:
            continue
        cle, _, val = nue.partition("=")
        if cle.strip() in CLES:
            trouve[cle.strip()] = val.strip()
    return trouve


def environnement(vals):
    broker = vals.get("KAFKA_BROKER", "")
    url = vals.get("YOGYM_BASE_URL", "")
    est_prod = "51.178.55.238" in broker, "app.yogym.co" in url
    if all(est_prod):
        return "PRODUCTION"
    if not any(est_prod):
        return "INTEGRATION" if ("54.38.35.221" in broker or "integration" in url) else "INCONNU"
    return "INCOHERENT"


def afficher(texte):
    vals = valeurs(texte)
    env = environnement(vals)
    print("\nConfiguration de pythonApp/.env :\n")
    for cle in CLES:
        print(f"   {cle:16} = {vals.get(cle, '<absent>')}")
    print()
    if env == "PRODUCTION":
        print("   >> environnement : *** PRODUCTION ***")
    elif env == "INCOHERENT":
        print("   >> environnement : *** INCOHERENT ***")
        print("      Une cle pointe sur la production et l'autre sur l'integration.")
        print("      C'est exactement la panne qui a fait perdre les pointages")
        print("      d'un client. Relancer : python bascule.py int|prod")
    else:
        print(f"   >> environnement : {env.lower()}")
    print()
    return env


def basculer(texte, cible):
    conf = CIBLES[cible]
    lignes = texte.splitlines(keepends=True)
    vues = set()

    for i, ligne in enumerate(lignes):
        nue = ligne.lstrip()
        if nue.startswith("#"):
            continue
        for cle in CLES:
            if nue.startswith(cle + "="):
                fin = ligne[len(ligne.rstrip("\r\n")):]     # \r\n ou \n d'origine
                lignes[i] = f"{cle}={conf[cle]}{fin}"
                vues.add(cle)

    manquantes = set(CLES) - vues
    if manquantes:
        print(f"[ERREUR] Cle(s) absente(s) du .env : {', '.join(sorted(manquantes))}")
        print("         Le fichier n'a pas ete modifie.")
        sys.exit(1)

    contenu = "".join(lignes)
    with open(ENV, "w", encoding="utf-8", newline="") as f:
        f.write(contenu)
    return contenu


if __name__ == "__main__":
    texte = lire()

    if len(sys.argv) < 2:
        afficher(texte)
        sys.exit(0)

    cible = sys.argv[1].lower()
    if cible not in CIBLES:
        print(f"[ERREUR] Cible inconnue : {sys.argv[1]!r}")
        print("         Valeurs acceptees : int | prod")
        sys.exit(1)

    nouveau = basculer(texte, cible)

    if cible == "prod":
        print()
        print("  ##########################################################")
        print("  #                                                        #")
        print("  #   ATTENTION : le pont pointe maintenant sur la PROD    #")
        print("  #                                                        #")
        print("  ##########################################################")
    else:
        print("\n  ---- Le pont pointe sur l'INTEGRATION ----")

    afficher(nouveau)
