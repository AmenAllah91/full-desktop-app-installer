import json

CHAMPS_MASQUES = {
    "comkey",
    "com_key",
    "password",
    "motdepasse",
    "client_secret",
    "clientsecret",
    "fingerprint_template",
}

MASQUE = "***"


def _masquer(valeur):
    if isinstance(valeur, dict):
        return {cle: (MASQUE if str(cle).lower() in CHAMPS_MASQUES else _masquer(v)) for cle, v in valeur.items()}
    if isinstance(valeur, list):
        return [_masquer(v) for v in valeur]
    return valeur


def masquer(message):
    if isinstance(message, (dict, list)):
        return json.dumps(_masquer(message), ensure_ascii=False)
    try:
        contenu = json.loads(message)
    except (TypeError, ValueError):
        return "<message non JSON, %d car.>" % len(str(message))
    return json.dumps(_masquer(contenu), ensure_ascii=False)


def commande_masquee(cmd, index_secret):
    copie = [str(x) for x in cmd]
    if 0 <= index_secret < len(copie) and copie[index_secret] not in ("", "0"):
        copie[index_secret] = MASQUE
    return copie
