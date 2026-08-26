# services/zkem_timezone.py
"""
Traduction d'un horaire hebdomadaire en calendrier ZKTeco, pour les pointeuses
standalone (zkemkeeper).

Le modele tient en une phrase : une timezone porte un NUMERO DE CRENEAU de 1 a
10, et ce numero sert a la fois de calendrier, de groupe d'acces et de
combinaison de deverrouillage sur l'appareil. Rien a allouer, rien a memoriser,
aucun etat local -- la pointeuse est sa propre table de correspondance.

    SetTZInfo(slot, horaire)          le calendrier slot porte les plages
    SSR_SetGroupTZ(slot, slot)        le groupe slot pointe sur ce calendrier
    SSR_SetUnLockGroup(slot, slot)    le groupe slot ouvre seul
    SetUserGroup(pin, slot)           l'adherent entre dans ce groupe

Deux consequences voulues :

  - le CRENEAU 1 est la timezone par defaut (00:00-23:59, sept jours). C'est
    deja l'etat d'usine de toutes les pointeuses en service : groupe 1 ->
    calendrier 1 -> 24/7, et tous les utilisateurs existants y sont. slot=1
    veut donc dire "ne rien ecrire", et le comportement actuel est preserve
    au bit pres ;

  - modifier les horaires d'une timezone REECRIT LE MEME CRENEAU. Tous les
    adherents du groupe changent d'horaire d'un coup, sans qu'il faille
    repousser un seul acces.

Mesures du 2026-08-25 sur SenseFace 3A (firmware Ver 6.60), voir
experience_timezone_standalone.py :

  - un calendrier est une chaine de 56 caracteres, 7 jours x "HHMM"+"HHMM",
    DIMANCHE EN PREMIER (confirme en relisant une configuration posee a la
    main sur l'ecran, pas deduit) ;
  - 50 calendriers, 10 combinaisons de deverrouillage. C'est la limite de 10
    qui plafonne le nombre de creneaux : un groupe n'ouvre que s'il figure
    dans une combinaison ;
  - un jour ferme s'ecrit "00000000" ;
  - NE JAMAIS passer par SetUserTZStr / SetUserTZs : sur ce firmware la
    fonction ment (renvoie True sans ecrire), corrompt le 4e emplacement, et
    sa remise a zero laisse un residu pointant le calendrier 1 -- donc un
    acces 24/7 accorde par accident. Les calendriers passent par le GROUPE.
"""

import logging

# Ordre des sept blocs dans la chaine de 56 caracteres. Ce sont aussi les noms
# des colonnes de l'entite Timezone cote back, dans le meme ordre : les deux se
# correspondent terme a terme, il n'y a pas de table de conversion.
JOURS = ["sunday", "monday", "tuesday", "wednesday",
         "thursday", "friday", "saturday"]

FERME = "00000000"
OUVERT = "00002359"
HORAIRE_24_7 = OUVERT * 7

# Creneau reserve : la timezone par defaut. Etat d'usine de toute pointeuse.
SLOT_DEFAUT = 1
SLOT_MAX = 10


class HoraireInvalide(ValueError):
    """L'horaire recu du cloud n'est pas exploitable."""


def encoder_semaine(horaire):
    """
    horaire : dict {"monday": "06:00-12:00", "sunday": None, ...}
    Retourne la chaine de 56 caracteres attendue par SetTZInfo.

    UN JOUR ILLISIBLE EST FERME, ET NE FAIT PAS TOMBER LA SEMAINE.

    Le front n'ecrit pas null pour un jour ferme : il y met le LIBELLE TRADUIT
    (« Pas de timezone », « No Timezone »). Un seul jour dans ce cas faisait
    rejeter tout l'horaire, et appliquer_timezone retombait alors sur le creneau
    par defaut — c'est-a-dire 24h/24. Un gerant qui posait une restriction
    obtenait un acces permanent (constate le 2026-08-26 : creneau « 09:15-23:58 »
    demande, adherent retrouve dans le groupe 1).

    Fermer le jour est le repli sur : au pire l'adherent se presente a l'accueil,
    alors que l'inverse ouvre la porte a quelqu'un qui n'aurait pas du entrer.
    Le journal nomme la valeur fautive pour qu'elle se corrige a la source.
    """
    if not isinstance(horaire, dict):
        raise HoraireInvalide("horaire attendu sous forme de dict, recu %r"
                              % type(horaire).__name__)

    morceaux = []
    for jour in JOURS:
        plage = horaire.get(jour)
        if not plage or not str(plage).strip():
            morceaux.append(FERME)
            continue
        try:
            morceaux.append(_encoder_plage(jour, str(plage).strip()))
        except HoraireInvalide as ex:
            logging.warning("Jour %s illisible (%r) : ferme pour ce creneau — %s",
                            jour, plage, ex)
            morceaux.append(FERME)
    return "".join(morceaux)


def _encoder_plage(jour, plage):
    if "-" not in plage:
        raise HoraireInvalide("plage %s invalide pour %s (attendu HH:MM-HH:MM)"
                              % (plage, jour))
    debut, fin = plage.split("-", 1)
    return _encoder_heure(jour, debut) + _encoder_heure(jour, fin)


def _encoder_heure(jour, heure):
    heure = heure.strip()
    if ":" not in heure:
        raise HoraireInvalide("heure %s invalide pour %s (attendu HH:MM)"
                              % (heure, jour))
    h, m = heure.split(":", 1)
    try:
        h, m = int(h), int(m)
    except ValueError:
        raise HoraireInvalide("heure %s invalide pour %s" % (heure, jour))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HoraireInvalide("heure %s hors bornes pour %s" % (heure, jour))
    return "%02d%02d" % (h, m)


def decoder_semaine(chaine):
    """Inverse d'encoder_semaine, pour les journaux et les tests."""
    if not chaine or len(chaine) != 56:
        return {}
    resultat = {}
    for i, jour in enumerate(JOURS):
        bloc = chaine[i * 8:(i + 1) * 8]
        if bloc == FERME:
            resultat[jour] = None
        else:
            resultat[jour] = "%s:%s-%s:%s" % (bloc[0:2], bloc[2:4],
                                              bloc[4:6], bloc[6:8])
    return resultat


def slot_valide(slot):
    """Un creneau exploitable, ou None si la valeur ne veut rien dire."""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return None
    return slot if SLOT_DEFAUT <= slot <= SLOT_MAX else None


def appliquer_timezone(zk, mn, pin, slot, horaire, alias=""):
    """
    Place l'adherent `pin` dans le groupe correspondant au creneau `slot`,
    apres avoir (re)ecrit le calendrier et le groupe de ce creneau.

    A appeler DANS une session deja ouverte, appareil deja fige par
    EnableDevice(False) -- comme le fait add_user.

    Retourne True si l'acces horaire est en place, False sinon. Ne leve pas :
    un horaire absent ou invalide fait retomber sur le creneau par defaut, qui
    est le comportement actuel (acces permanent), et le journalise.
    """
    slot = slot_valide(slot)

    if slot is None:
        # Cloud plus ancien que ce pont, ou activite sans timezone : on ne
        # touche a rien. L'adherent reste dans le groupe ou il est, c'est-a-dire
        # le groupe 1 / 24/7 pour tout le parc existant.
        logging.debug("Aucun creneau horaire pour le pin %s sur %s : "
                      "comportement inchange", pin, alias or "cette machine")
        return True

    if slot == SLOT_DEFAUT:
        # Timezone par defaut. Le groupe 1 existe deja et pointe deja sur un
        # calendrier 24/7 sur toutes les pointeuses en service -- on ne le
        # reecrit surtout pas, ce serait ecrire pour rien sur du materiel qui
        # est deja dans l'etat voulu. On s'assure seulement que l'adherent y
        # est : il a pu etre deplace par un abonnement precedent.
        return _poser_groupe(zk, mn, pin, SLOT_DEFAUT, alias)

    try:
        chaine = encoder_semaine(horaire or {})
    except HoraireInvalide as ex:
        # Refuser l'acces serait pire : l'adherent a paye. On retombe sur le
        # comportement d'avant la fonctionnalite, en le disant clairement.
        logging.error("Horaire illisible pour le pin %s sur %s (%s) — "
                      "repli sur le creneau par defaut", pin, alias, ex)
        return _poser_groupe(zk, mn, pin, SLOT_DEFAUT, alias)

    if chaine == HORAIRE_24_7:
        # Une timezone sans restriction reelle. Inutile de consommer un creneau.
        return _poser_groupe(zk, mn, pin, SLOT_DEFAUT, alias)

    # Les trois ecritures sont idempotentes : les rejouer a chaque acces coute
    # trois appels SDK et garantit qu'une pointeuse remise a zero ou remplacee
    # se reconfigure toute seule au premier abonnement, sans intervention.
    if not zk.SetTZInfo(mn, slot, chaine):
        logging.error("SetTZInfo(%s) KO sur %s — creneau non ecrit", slot, alias)
        return False

    # VaildHoliday=0, VerifyStyle=0 : les valeurs du groupe 1 d'usine.
    # VerifyStyle=0 signifie "suivre le mode de verification de l'appareil" ;
    # imposer un mode ici casserait le parametrage du club.
    if not zk.SSR_SetGroupTZ(mn, slot, slot, 0, 0, 0, 0):
        logging.error("SSR_SetGroupTZ(%s) KO sur %s", slot, alias)
        return False

    # Un seul groupe par combinaison. En mettre deux sur la meme ligne
    # exigerait la presentation successive d'un badge de chaque groupe pour
    # ouvrir la porte.
    if not zk.SSR_SetUnLockGroup(mn, slot, slot, 0, 0, 0, 0):
        logging.error("SSR_SetUnLockGroup(%s) KO sur %s", slot, alias)
        return False

    logging.info("🕒 Creneau %s sur %s : %s", slot, alias,
                 _resumer(decoder_semaine(chaine)))
    return _poser_groupe(zk, mn, pin, slot, alias)


def ecrire_creneau(zk, mn, slot, horaire, alias=""):
    """
    Reecrit le calendrier d'un creneau, SANS toucher a aucun utilisateur.

    Les adherents deja places dans le groupe correspondant changent d'horaire
    du meme coup : c'est ce qui permet a une modification de timezone de
    s'appliquer sans repousser un seul acces.

    A appeler dans une session ouverte, appareil fige par EnableDevice(False).
    """
    slot = slot_valide(slot)
    if slot is None or slot == SLOT_DEFAUT:
        # Le creneau 1 est le 24/7 d'usine : ni modifiable cote cloud, ni
        # reecrit ici. Recevoir une demande pour lui est sans objet.
        logging.info("Creneau %s ignore sur %s : c'est celui par defaut",
                     slot, alias or "cette machine")
        return True

    try:
        chaine = encoder_semaine(horaire or {})
    except HoraireInvalide as ex:
        logging.error("Horaire illisible pour le creneau %s sur %s (%s) — "
                      "calendrier inchange", slot, alias, ex)
        return False

    if not zk.SetTZInfo(mn, slot, chaine):
        logging.error("SetTZInfo(%s) KO sur %s", slot, alias)
        return False
    if not zk.SSR_SetGroupTZ(mn, slot, slot, 0, 0, 0, 0):
        logging.error("SSR_SetGroupTZ(%s) KO sur %s", slot, alias)
        return False
    if not zk.SSR_SetUnLockGroup(mn, slot, slot, 0, 0, 0, 0):
        logging.error("SSR_SetUnLockGroup(%s) KO sur %s", slot, alias)
        return False

    logging.info("🕒 Creneau %s mis a jour sur %s : %s", slot, alias,
                 _resumer(decoder_semaine(chaine)))
    return True


def _poser_groupe(zk, mn, pin, slot, alias):
    # SetUserGroup prend le PIN en ENTIER, contrairement aux fonctions SSR_ qui
    # le prennent en chaine. Un PIN non numerique n'a pas de groupe possible.
    try:
        pin_entier = int(pin)
    except (TypeError, ValueError):
        logging.error("PIN %r non numerique : SetUserGroup impossible sur %s",
                      pin, alias)
        return False

    if not zk.SetUserGroup(mn, pin_entier, slot):
        logging.error("SetUserGroup(pin=%s, groupe=%s) KO sur %s",
                      pin, slot, alias)
        return False
    logging.info("👤 pin %s -> groupe %s sur %s", pin, slot, alias)
    return True


def _resumer(jours):
    return " ".join("%s=%s" % (j[:3], jours.get(j) or "ferme") for j in JOURS)
