# services/c3_timezone.py
"""
Traduction d'un horaire hebdomadaire en calendrier ZKTeco, cote C3 (Pull SDK).

Meme modele que la standalone : une timezone porte un NUMERO DE CRENEAU, et ce
numero est ecrit tel quel comme `TimezoneId`. Le creneau 1 est la timezone par
defaut -- et le C3, comme la standalone, porte deja d'usine un calendrier 1
ouvert 00:00-23:59 les sept jours, auquel toutes les fiches `userauthorize`
renvoient. slot=1 veut donc dire "ne rien ecrire", et le comportement actuel
est preserve.

Le C3 est plus direct que la standalone : pas de groupe, pas de combinaison de
deverrouillage. L'affectation se fait fiche par fiche dans `userauthorize`,
`(Pin, AuthorizeTimezoneId, AuthorizeDoorId)` -- donc par utilisateur ET par
porte.

Mesures du 2026-08-25 sur le C3 192.168.1.205 (ACP-260) :

  - la table s'appelle `timezone`. **`timeseg` n'existe pas et fait planter le
    process** : exit 127, aucune exception Python, la DLL emporte tout. Sur un
    pont en production ca tue le service sans une ligne de journal. Ne jamais
    construire un nom de table dynamiquement ;
  - colonnes : `TimezoneId`, puis `<Jour>Time1..3` pour Sun..Sat, puis
    `Hol1..3Time1..3` -- ces dernieres laissees telles quelles, YoGym ne gere
    pas les jours feries ;
  - chaque champ est un ENTIER `debutHHMM * 10000 + finHHMM`, `0` = ferme.
    Verifie en ecriture/relecture : `MonTime1=6001200` se relit `6001200` et
    vaut 06:00-12:00 ;
  - TROIS intervalles par jour nativement (`TueTime1/2/3`), la ou la standalone
    n'en offre qu'un par calendrier ;
  - `SuperAuthorize = 0` sur les 167 fiches du panneau : le sujet du privilege
    admin ne se pose pas de ce cote.
"""

import logging

from services.zkem_timezone import (
    HoraireInvalide, _encoder_heure, slot_valide, SLOT_DEFAUT,
)

# Cle de l'entite Timezone cote back -> prefixe de colonne dans la table C3.
JOURS_C3 = [
    ("sunday", "Sun"), ("monday", "Mon"), ("tuesday", "Tue"),
    ("wednesday", "Wed"), ("thursday", "Thu"), ("friday", "Fri"),
    ("saturday", "Sat"),
]

FERME = 0
TABLE = b"timezone"    # surtout pas b"timeseg" -- voir l'en-tete


def encoder_intervalle(plage):
    """
    "06:00-12:00" -> 6001200. Vide, None ou absent -> 0 (jour ferme).

    L'entier est `debutHHMM * 10000 + finHHMM`. 00:00-23:59 donne donc 2359,
    ce qui explique la valeur d'usine du calendrier 1.
    """
    if not plage or not str(plage).strip():
        return FERME
    plage = str(plage).strip()
    if "-" not in plage:
        raise HoraireInvalide("plage %s invalide (attendu HH:MM-HH:MM)" % plage)
    debut, fin = plage.split("-", 1)
    return int(_encoder_heure("", debut)) * 10000 + int(_encoder_heure("", fin))


def encoder_timezone(slot, horaire):
    """
    Construit la ligne `timezone` a envoyer a SetDeviceData.

    Les VINGT-ET-UN champs de la semaine sont ecrits explicitement, y compris
    les zeros : SetDeviceData met a jour la ligne existante champ par champ,
    et un champ omis garde sa valeur precedente. Sans ca, retrecir un horaire
    laisserait l'ancien creneau ouvert en Time2/Time3.
    """
    if not isinstance(horaire, dict):
        raise HoraireInvalide("horaire attendu sous forme de dict, recu %r"
                              % type(horaire).__name__)

    champs = ["TimezoneId=%d" % slot]
    for cle, prefixe in JOURS_C3:
        # Un jour illisible est FERME, et ne fait pas tomber la semaine : voir
        # encoder_semaine cote standalone pour le detail du piege (le front y
        # ecrit le libelle traduit « Pas de timezone »).
        try:
            valeur = encoder_intervalle(horaire.get(cle))
        except HoraireInvalide as ex:
            logging.warning("Jour %s illisible (%r) : ferme pour ce creneau — %s",
                            cle, horaire.get(cle), ex)
            valeur = FERME
        champs.append("%sTime1=%d" % (prefixe, valeur))
        # Le modele metier n'expose qu'un intervalle par jour (un adherent n'a
        # qu'une timezone par machine). Les deux autres sont remis a zero pour
        # que la ligne soit entierement deterministe.
        champs.append("%sTime2=0" % prefixe)
        champs.append("%sTime3=0" % prefixe)

    # Les colonnes Hol1..3Time1..3 ne sont pas ecrites : YoGym ne gere pas les
    # jours feries, et la table `holiday` du panneau est vide. Rien a piloter.
    return "\t".join(champs)


def ecrire_timezone(adapter, slot, horaire, alias=""):
    """
    Ecrit le calendrier du creneau sur le panneau. Retourne le creneau
    reellement utilisable -- SLOT_DEFAUT si on retombe sur le defaut.

    Ne leve pas : un horaire illisible fait retomber sur le creneau par defaut
    (acces permanent, comportement d'avant la fonctionnalite) plutot que de
    refuser l'acces a un adherent qui a paye.
    """
    slot = slot_valide(slot)
    if slot is None or slot == SLOT_DEFAUT:
        # Creneau par defaut, ou cloud plus ancien que ce pont : le calendrier 1
        # est deja ouvert 24/7 sur le panneau, on ne le reecrit pas.
        return SLOT_DEFAUT

    try:
        data = encoder_timezone(slot, horaire or {})
    except HoraireInvalide as ex:
        logging.error("Horaire illisible pour le creneau %s sur %s (%s) — "
                      "repli sur le creneau par defaut", slot, alias, ex)
        return SLOT_DEFAUT

    if not adapter._set(TABLE, data):
        logging.error("Ecriture du creneau %s KO sur %s — repli sur le defaut",
                      slot, alias)
        return SLOT_DEFAUT

    logging.info("🕒 Creneau %s ecrit sur %s (C3)", slot, alias)
    return slot
