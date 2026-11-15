import json
import os
import sys
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)
sys.path.insert(0, PONT)

from domain.AccessMachine import AccessMachine  # noqa: E402
from domain.DoorType import DoorType  # noqa: E402
from services.journalSur import MASQUE, commande_masquee, masquer  # noqa: E402

COMKEY = "48213"


def lire(*chemin):
    with open(os.path.join(*chemin), encoding="utf-8") as f:
        return f.read()


class TestMasquer(unittest.TestCase):
    def test_comkey_masquee_a_toute_profondeur(self):
        message = json.dumps({"pin": "12", "comKey": COMKEY,
                              "machines": [{"ip": "10.0.0.1", "com_key": COMKEY, "comkey": COMKEY}]})
        sortie = masquer(message)
        self.assertNotIn(COMKEY, sortie)
        contenu = json.loads(sortie)
        self.assertEqual(contenu["pin"], "12")
        self.assertEqual(contenu["comKey"], MASQUE)
        self.assertEqual(contenu["machines"][0]["ip"], "10.0.0.1")
        self.assertEqual(contenu["machines"][0]["com_key"], MASQUE)

    def test_gabarit_d_empreinte_et_secrets_masques(self):
        sortie = json.loads(masquer({"fingerprint_template": "TPL", "password": "p", "client_secret": "s", "pin": 3}))
        self.assertEqual(sortie, {"fingerprint_template": MASQUE, "password": MASQUE, "client_secret": MASQUE, "pin": 3})

    def test_message_non_json_jamais_recopie(self):
        sortie = masquer("comKey=" + COMKEY)
        self.assertNotIn(COMKEY, sortie)
        self.assertIn("non JSON", sortie)

    def test_commande_masquee(self):
        cmd = ["ConsoleApp1.exe", "10.0.0.1", "4370", "12", "C:\\tmp", COMKEY]
        self.assertEqual(commande_masquee(cmd, 5)[-1], MASQUE)
        self.assertEqual(commande_masquee(cmd, 5)[:5], cmd[:5])
        self.assertEqual(commande_masquee(cmd[:5] + ["0"], 5)[-1], "0")
        self.assertEqual(cmd[-1], COMKEY)


class TestMachine(unittest.TestCase):
    def test_comkey_absente_de_la_representation(self):
        m = AccessMachine(1, "Entrée", "10.0.0.1", 4370, "actif", "C3", 1, 0, 0, 0, list(DoorType)[0], int(COMKEY))
        self.assertEqual(m.comKey, int(COMKEY))
        self.assertNotIn(COMKEY, repr(m))
        self.assertNotIn(COMKEY, str([m]))


class TestAppels(unittest.TestCase):
    def test_journaux_des_messages_masques(self):
        main = lire(PONT, "main.py")
        self.assertIn("from new access request topic from tenant %s: %s\", tenant, masquer(message))", main)
        self.assertIn("from fingerprint actions topic from tenant %s: %s\", tenant, masquer(message))", main)

    def test_comkey_jamais_journalisee(self):
        self.assertNotIn("appliqué pour %s : %s\", ip, com_key", lire(PONT, "services", "MonitorZkem.py"))
        self.assertIn("commande_masquee(cmd, len(cmd) - 1)", lire(PONT, "services", "zkem_adapter.py"))


class TestVersion(unittest.TestCase):
    def test_version_du_pont_vient_d_electron(self):
        self.assertIn('BRIDGE_VERSION = version_poste("remote-1")', lire(PONT, "remote_commands.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
