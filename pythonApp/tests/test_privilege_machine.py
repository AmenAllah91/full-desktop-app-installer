import os
import sys
import unittest
from unittest import mock

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)
sys.path.insert(0, PONT)

from services import privilegeMachine  # noqa: E402
from services.privilegeMachine import (  # noqa: E402
    PRIVILEGE_ADMIN, PRIVILEGE_UTILISATEUR, est_pin_employe, privilege_machine)


def lire(*chemin):
    with open(os.path.join(*chemin), encoding="utf-8") as f:
        return f.read()


class TestPrivilegeMachine(unittest.TestCase):
    def test_adherent_jamais_admin(self):
        for demande in (None, "", "ADMIN", "admin", "USER", 3, "3"):
            self.assertEqual(privilege_machine("3040", demande), PRIVILEGE_UTILISATEUR)
            self.assertEqual(privilege_machine(99999, demande), PRIVILEGE_UTILISATEUR)

    def test_employe_admin_par_defaut(self):
        self.assertEqual(privilege_machine("100000"), PRIVILEGE_ADMIN)
        self.assertEqual(privilege_machine(100042, "ADMIN"), PRIVILEGE_ADMIN)
        self.assertEqual(privilege_machine(" 100042 ", None), PRIVILEGE_ADMIN)

    def test_employe_utilisateur_si_le_serveur_le_demande(self):
        self.assertEqual(privilege_machine("100042", "USER"), PRIVILEGE_UTILISATEUR)
        self.assertEqual(privilege_machine("100042", " user "), PRIVILEGE_UTILISATEUR)

    def test_pin_illisible_jamais_admin(self):
        for pin in (None, "", "abc", "10 00 00", "1e6"):
            self.assertFalse(est_pin_employe(pin))
            self.assertEqual(privilege_machine(pin, "ADMIN"), PRIVILEGE_UTILISATEUR)

    def test_plancher_aligne_sur_le_serveur(self):
        self.assertEqual(privilegeMachine.PIN_PLANCHER_EMPLOYE, 100000)


class TestAjoutStandalone(unittest.TestCase):
    def ajouter(self, pin, privilege=None):
        from services import zkem_adapter
        adapter = zkem_adapter.ZkemAdapter.__new__(zkem_adapter.ZkemAdapter)
        adapter.zk = mock.MagicMock()
        adapter.zk.SSR_SetUserInfo.return_value = True
        adapter.zk.SetUserValidDate.return_value = True
        adapter.mn = 1
        adapter.ip = "10.0.0.1"
        adapter.port = 4370
        adapter.connected = True
        adapter.machine = mock.MagicMock(alias="essai")
        with mock.patch.object(zkem_adapter, "tcp_reachable", return_value=True), \
                mock.patch.object(zkem_adapter.ZkemAdapter, "connect", return_value=True), \
                mock.patch.object(zkem_adapter, "appliquer_timezone", return_value=True):
            ok = adapter.add_user(pin, "Nom", "", "20261101", "20261130", privilege=privilege)
        self.assertTrue(ok)
        return adapter.zk.SSR_SetUserInfo.call_args.args

    def test_adherent_ecrit_en_utilisateur_normal(self):
        self.assertEqual(self.ajouter("3040"), (1, "3040", "Nom", "", 0, True))
        self.assertEqual(self.ajouter("3040", "ADMIN")[4], 0)

    def test_employe_ecrit_en_administrateur(self):
        self.assertEqual(self.ajouter("100001")[4], 3)
        self.assertEqual(self.ajouter("100001", "USER")[4], 0)


class TestCablage(unittest.TestCase):
    def test_aucun_privilege_en_dur_dans_l_adaptateur(self):
        source = lire(PONT, "services", "zkem_adapter.py")
        self.assertNotIn('"", 3, True', source)
        self.assertIn("privilege_machine(pin, privilege)", source)

    def test_le_message_et_la_route_locale_portent_le_privilege(self):
        source = lire(PONT, "main.py")
        self.assertIn('"privilege": json_message.get("machinePrivilege")', source)
        self.assertIn('"privilege": data.get("machinePrivilege")', source)
        self.assertIn('privilege=task.get("privilege")', source)


class TestOutilDeReleve(unittest.TestCase):
    def test_classement_et_retrogradation(self):
        sys.path.insert(0, os.path.join(PONT, "migration"))
        import standalone_privileges as outil
        utilisateurs = [
            {"pin": "3040", "nom": "A", "privilege": 3, "actif": True},
            {"pin": "3041", "nom": "B", "privilege": 0, "actif": True},
            {"pin": "100001", "nom": "E", "privilege": 3, "actif": True},
        ]
        adherents, employes = outil.classer(utilisateurs)
        self.assertEqual([u["pin"] for u in adherents], ["3040"])
        self.assertEqual([u["pin"] for u in employes], ["100001"])
        z = mock.MagicMock()
        z.SSR_GetUserInfo.return_value = (True, "A", "1234", 3, True)
        z.GetStrCardNumber.return_value = (True, "0007654321")
        z.SSR_SetUserInfo.return_value = True
        self.assertEqual(outil.corriger(z, adherents), [])
        z.SetStrCardNumber.assert_called_once_with("0007654321")
        z.SSR_SetUserInfo.assert_called_once_with(1, "3040", "A", "1234", 0, True)
        z.EnableDevice.assert_called_with(1, True)


if __name__ == "__main__":
    unittest.main()
