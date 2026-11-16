import json
import os
import sys
import unittest
from datetime import datetime

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)
sys.path.insert(0, PONT)

from services.refusPointeuse import (  # noqa: E402
    ETAT_REFUS, REFUS_HORS_CRENEAU, REFUS_POINTEUSE, REFUS_VALIDITE_EXPIREE,
    SuiviVerifications, lire_validite, motif_c3, motif_standalone)


def lire(*chemin):
    with open(os.path.join(*chemin), encoding="utf-8") as f:
        return f.read()


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def avance(self, secondes):
        self.t += secondes


class TestSuiviVerifications(unittest.TestCase):
    def setUp(self):
        self.horloge = Horloge()
        self.suivi = SuiviVerifications(delai=1.5, horloge=self.horloge)

    def test_verification_suivie_d_un_passage_aucun_refus(self):
        self.suivi.verifie(3040)
        self.horloge.avance(0.1)
        self.suivi.passage(3040)
        self.horloge.avance(5)
        self.assertEqual(self.suivi.refus_echus(), [])

    def test_verification_sans_passage_refus_apres_le_delai(self):
        self.suivi.verifie(3040)
        self.horloge.avance(1.0)
        self.assertEqual(self.suivi.refus_echus(), [])
        self.horloge.avance(0.6)
        self.assertEqual(self.suivi.refus_echus(), [3040])
        self.assertEqual(self.suivi.refus_echus(), [])

    def test_passage_avant_la_verification_aucun_refus(self):
        self.suivi.passage(3040)
        self.horloge.avance(0.05)
        self.suivi.verifie(3040)
        self.horloge.avance(5)
        self.assertEqual(self.suivi.refus_echus(), [])

    def test_identifiant_different_du_pin_efface_par_le_passage(self):
        self.suivi.verifie(4757514)
        self.horloge.avance(0.1)
        self.suivi.passage(3056)
        self.horloge.avance(5)
        self.assertEqual(self.suivi.refus_echus(), [])

    def test_verification_echouee_ignoree(self):
        for valeur in (-1, 0, None, "", "abc"):
            self.suivi.verifie(valeur)
        self.horloge.avance(5)
        self.assertEqual(self.suivi.refus_echus(), [])

    def test_deux_essais_rapproches_un_seul_refus(self):
        self.suivi.verifie(3040)
        self.horloge.avance(0.5)
        self.suivi.verifie("3040")
        self.horloge.avance(1.1)
        self.assertEqual(self.suivi.refus_echus(), [3040])

    def test_refus_puis_nouveau_passage_valide(self):
        self.suivi.verifie(3040)
        self.horloge.avance(2)
        self.assertEqual(self.suivi.refus_echus(), [3040])
        self.suivi.verifie(3041)
        self.suivi.passage(3041)
        self.horloge.avance(2)
        self.assertEqual(self.suivi.refus_echus(), [])


class TestMotifs(unittest.TestCase):
    def test_validite_depassee(self):
        maintenant = datetime(2026, 11, 17, 8, 0, 0)
        self.assertEqual(motif_standalone("2026-11-10 23:59:59", maintenant), REFUS_VALIDITE_EXPIREE)
        self.assertEqual(motif_standalone("2026/11/10 23:59:59", maintenant), REFUS_VALIDITE_EXPIREE)
        self.assertEqual(motif_standalone("2026-11-16", maintenant), REFUS_VALIDITE_EXPIREE)

    def test_validite_en_cours_ou_illisible(self):
        maintenant = datetime(2026, 11, 17, 8, 0, 0)
        self.assertEqual(motif_standalone("2026-11-30 23:59:59", maintenant), REFUS_POINTEUSE)
        for valeur in (None, "", "0", "pas une date"):
            self.assertEqual(motif_standalone(valeur, maintenant), REFUS_POINTEUSE)

    def test_evenements_c3(self):
        self.assertEqual(motif_c3(29), REFUS_VALIDITE_EXPIREE)
        self.assertEqual(motif_c3(33), REFUS_VALIDITE_EXPIREE)
        self.assertEqual(motif_c3(22), REFUS_HORS_CRENEAU)
        for code in (0, 14, 27, 200, 255):
            self.assertIsNone(motif_c3(code))


class FauxZk:
    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def GetUserValidDate(self, *arguments):
        self.appels.append(arguments)
        reponse = self.reponses.pop(0)
        if isinstance(reponse, Exception):
            raise reponse
        return reponse


class TestLectureValidite(unittest.TestCase):
    def test_utilisateur_connu(self):
        zk = FauxZk([(True, 1, 1, "2026-11-01 00:00:00", "2026-11-10 23:59:59")])
        self.assertEqual(lire_validite(zk, 1, 3040), (True, "2026-11-10 23:59:59"))
        self.assertEqual(zk.appels, [(1, "3040")])

    def test_utilisateur_inconnu(self):
        self.assertEqual(lire_validite(FauxZk([(False, 0, 0, "", "")]), 1, 4757514), (False, ""))

    def test_seconde_forme_d_appel(self):
        zk = FauxZk([TypeError("arguments"), (True, 1, 1, "2026-11-01 00:00:00", "2026-11-30 23:59:59")])
        self.assertEqual(lire_validite(zk, 1, 3040), (True, "2026-11-30 23:59:59"))
        self.assertEqual(len(zk.appels), 2)

    def test_sdk_muet(self):
        self.assertEqual(lire_validite(FauxZk([RuntimeError("COM"), RuntimeError("COM")]), 1, 3040), (None, None))


class TestMessage(unittest.TestCase):
    def test_motif_absent_par_defaut_present_sur_un_refus(self):
        try:
            from services.MachineMonitor import make_rt_json
        except Exception as ex:
            self.skipTest("MachineMonitor non importable ici : %s" % ex)
        commun = dict(machine_id=7, ip="10.0.0.1", mtype="STANDALONE_NEW_FIRMWARE", pin=3040,
                      dt="2026-11-17 08:00:00", door_id=1, card_no=None, gym_branch_id="1003",
                      porte_type="ENTREE", tenant="empire")
        passage = json.loads(make_rt_json(state_code=0, **commun))
        self.assertTrue(passage["is_access_valid"])
        self.assertNotIn("deny_reason", passage)
        refus = json.loads(make_rt_json(state_code=ETAT_REFUS, deny_reason=REFUS_VALIDITE_EXPIREE, **commun))
        self.assertFalse(refus["is_access_valid"])
        self.assertEqual(refus["deny_reason"], "VALIDITE_EXPIREE")
        self.assertEqual(set(refus) - set(passage), {"deny_reason"})


class TestCablage(unittest.TestCase):
    def test_la_pointeuse_autonome_ecoute_la_verification(self):
        source = lire(PONT, "services", "MonitorZkem.py")
        self.assertIn("def OnVerify(self, user_id):", source)
        self.assertIn("self.suivi.verifie(user_id)", source)
        self.assertIn("self.suivi.passage(enroll)", source)
        self.assertIn("signaler_refus(base, machine_number, events_instance, ip)", source)
        self.assertIn("deny_reason=motif,", source)
        self.assertIn("if not connu:", source)

    def test_le_c3_porte_le_motif(self):
        source = lire(PONT, "services", "MachineMonitor.py")
        self.assertIn("deny_reason=motif_c3(state),", source)


if __name__ == "__main__":
    unittest.main()
