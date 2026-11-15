import ast
import os
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)


def sources_du_pont():
    for dossier, _, fichiers in os.walk(PONT):
        if any(part in dossier for part in ("venv", "tests", "build", "dist", "__pycache__")):
            continue
        for nom in fichiers:
            if nom.endswith(".py"):
                chemin = os.path.join(dossier, nom)
                with open(chemin, encoding="utf-8") as f:
                    yield chemin, f.read()


def fonction(source, nom):
    arbre = ast.parse(source)
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.FunctionDef) and noeud.name == nom:
            return ast.get_source_segment(source, noeud)
    return None


class TestPhotosDeVisage(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PONT, "main.py"), encoding="utf-8") as f:
            self.main = f.read()

    def test_plus_aucun_circuit_kafka_des_photos(self):
        for chemin, source in sources_du_pont():
            for interdit in ("launch_publish_photo", "finish_publish_photo", "PhotoPublishThread", "publish_photo_kafka"):
                self.assertNotIn(interdit, source, "%s dans %s" % (interdit, chemin))

    def test_plus_aucune_lecture_de_la_photo_publique(self):
        for chemin, source in sources_du_pont():
            self.assertNotIn("/public/faceid-photo", source, chemin)
            self.assertNotIn("download_faceid_photo", source, chemin)
        self.assertFalse(os.path.exists(os.path.join(PONT, "services", "documentManagerService.py")))

    def test_envoi_sans_photo_refuse_en_400(self):
        corps = fonction(self.main, "upload_face_multipart")
        self.assertIsNotNone(corps)
        manquante = corps.index('"Photo manquante"')
        self.assertIn("}), 400", corps[manquante:manquante + 40])
        self.assertLess(manquante, corps.index("file.read()"))

    def test_photo_lue_sur_la_pointeuse_rendue_sans_publication(self):
        corps = fonction(self.main, "process_user_photo")
        self.assertIsNotNone(corps)
        self.assertIn('"photo": encoded', corps)
        self.assertNotIn("produce(", corps)

    def test_route_locale_get_face_conservee(self):
        self.assertIn("/getFace/", self.main)


if __name__ == "__main__":
    unittest.main(verbosity=2)
