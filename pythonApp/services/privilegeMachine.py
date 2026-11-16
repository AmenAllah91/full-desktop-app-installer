PIN_PLANCHER_EMPLOYE = 100000
PRIVILEGE_UTILISATEUR = 0
PRIVILEGE_ADMIN = 3
DEMANDE_UTILISATEUR = "USER"
DEMANDE_ADMIN = "ADMIN"


def est_pin_employe(pin):
    try:
        return int(str(pin).strip()) >= PIN_PLANCHER_EMPLOYE
    except (TypeError, ValueError):
        return False


def privilege_machine(pin, demande=None):
    if not est_pin_employe(pin):
        return PRIVILEGE_UTILISATEUR
    if str(demande or "").strip().upper() == DEMANDE_UTILISATEUR:
        return PRIVILEGE_UTILISATEUR
    return PRIVILEGE_ADMIN
