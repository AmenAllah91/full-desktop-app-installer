import ast
import os
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)
RACINE = os.path.dirname(PONT)


def lire(*chemin):
    with open(os.path.join(*chemin), encoding="utf-8", errors="replace") as f:
        return f.read()


class EcouteLocale(unittest.TestCase):

    def test_le_serveur_http_ecoute_sur_le_pc_seul_quoi_que_dise_le_env(self):
        arbre = ast.parse(lire(PONT, "main.py"))
        valeurs = [n.value for n in arbre.body
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "FLASK_HOST" for t in n.targets)]
        self.assertEqual(len(valeurs), 1)
        self.assertIsInstance(valeurs[0], ast.Constant)
        self.assertEqual(valeurs[0].value, "127.0.0.1")

    def test_le_env_par_defaut_n_ouvre_plus_le_reseau(self):
        source = lire(PONT, "main.py")
        self.assertNotIn("FLASK_HOST=0.0.0.0", source)
        self.assertIn("FLASK_HOST=127.0.0.1", source)

    def test_l_installeur_n_ouvre_plus_le_reseau(self):
        script = lire(RACINE, "build-installer.bat")
        self.assertNotIn("FLASK_HOST=0.0.0.0", script)
        self.assertIn("FLASK_HOST=127.0.0.1", script)

    def test_le_websocket_reste_sur_localhost(self):
        source = lire(PONT, "services", "websocket.py")
        self.assertIn('"localhost",\n            8765', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
