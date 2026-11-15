import ast
import json
import logging
import os
import sys
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
PONT = os.path.dirname(ICI)
sys.path.insert(0, PONT)

from kafka_service.kafkaservice import KafkaService  # noqa: E402
from remote_commands import RemoteCommands  # noqa: E402
from services import identitePoste as ip  # noqa: E402

JETON = "jeton-secret-du-poste"


class FauxJeton:
    def __init__(self, valeurs):
        self.valeurs = list(valeurs)
        self.appels = 0

    def valeur(self):
        self.appels += 1
        suivant = self.valeurs.pop(0) if len(self.valeurs) > 1 else self.valeurs[0]
        if isinstance(suivant, Exception):
            raise suivant
        return suivant


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FauxProducteur:
    def __init__(self):
        self.envois = []

    def produce(self, topic, value=None, **kwargs):
        self.envois.append((topic, value, kwargs))

    def poll(self, _):
        return 0

    def flush(self, _):
        return 0


class Metadonnees:
    def __init__(self, topics):
        self.topics = {t: object() for t in topics}


class FauxAdmin:
    def __init__(self, topics):
        self.topics = list(topics)
        self.creations = []

    def list_topics(self, timeout=None):
        return Metadonnees(self.topics)

    def create_topics(self, nouvelles):
        self.creations.extend(n.topic for n in nouvelles)

        class Futur:
            def result(self, timeout=None):
                return None
        return {n.topic: Futur() for n in nouvelles}


def identite():
    return ip.IdentitePoste("empire", "yoclub-poste", "secret", "https://login/token", "3", "1003")


def entetes(jeton, horloge=None):
    return ip.EntetesKafkaPoste(identite(), jeton, "1.4.0", horloge or Horloge())


def service_kafka(fournisseur=None, admin=None):
    service = KafkaService.__new__(KafkaService)
    service.producer = FauxProducteur()
    service._admin = admin or FauxAdmin([])
    service.entetes = fournisseur
    return service


def lire(*chemin):
    with open(os.path.join(*chemin), encoding="utf-8") as f:
        return f.read()


class EntetesDuPoste(unittest.TestCase):

    def test_les_quatre_entetes_en_octets_utf8(self):
        self.assertEqual(entetes(FauxJeton([JETON]))(), [
            ("X-Yoclub-Poste", JETON.encode("utf-8")),
            ("X-Yoclub-Filiale", b"1003"),
            ("X-Yoclub-Poste-Numero", b"3"),
            ("X-Yoclub-Poste-Version", b"1.4.0"),
        ])

    def test_jeton_en_echec_trois_entetes_et_avertissement_sans_jeton(self):
        with self.assertLogs("identite_poste", level=logging.WARNING) as journal:
            obtenus = entetes(FauxJeton([ip.ErreurJeton("jeton du poste injoignable (ConnectionError)")]))()
        self.assertEqual([nom for nom, _ in obtenus],
                         ["X-Yoclub-Filiale", "X-Yoclub-Poste-Numero", "X-Yoclub-Poste-Version"])
        self.assertIn("sans preuve", "\n".join(journal.output))
        self.assertNotIn(JETON, "\n".join(journal.output))

    def test_exception_inattendue_nommee_par_son_type_seulement(self):
        with self.assertLogs("identite_poste", level=logging.WARNING) as journal:
            entetes(FauxJeton([RuntimeError(JETON)]))()
        self.assertIn("RuntimeError", "\n".join(journal.output))
        self.assertNotIn(JETON, "\n".join(journal.output))

    def test_apres_un_echec_keycloak_n_est_pas_rappele_a_chaque_message(self):
        horloge = Horloge()
        jeton = FauxJeton([ip.ErreurJeton("refusé"), JETON])
        fournisseur = entetes(jeton, horloge)
        with self.assertLogs("identite_poste", level=logging.WARNING):
            fournisseur()
        fournisseur()
        self.assertEqual(jeton.appels, 1)
        horloge.t += ip.RELANCE_JETON_KAFKA
        self.assertEqual(fournisseur()[0], ("X-Yoclub-Poste", JETON.encode("utf-8")))

    def test_le_client_du_poste_partage_son_jeton(self):
        client = ip.ClientPoste(identite(), "https://x", "1.4.0", http=object())
        fournisseur = client.entetes_kafka()
        self.assertIs(fournisseur._jeton, client._jeton)
        self.assertIn(("X-Yoclub-Poste-Version", b"1.4.0"), fournisseur._fixes)


class ProductionKafka(unittest.TestCase):

    def test_sans_fournisseur_aucun_entete(self):
        service = service_kafka()
        service.produce("rt_pointage", {"pin": 1})
        topic, valeur, kwargs = service.producer.envois[0]
        self.assertEqual((topic, json.loads(valeur)), ("rt_pointage", {"pin": 1}))
        self.assertNotIn("headers", kwargs)

    def test_avec_fournisseur_les_entetes_partent_avec_le_message(self):
        service = service_kafka(entetes(FauxJeton([JETON])))
        service.produce("rt_pointage", {"pin": 1})
        entetes_envoyes = dict(service.producer.envois[0][2]["headers"])
        self.assertEqual(entetes_envoyes["X-Yoclub-Poste"], JETON.encode("utf-8"))
        self.assertEqual(entetes_envoyes["X-Yoclub-Filiale"], b"1003")

    def test_jeton_en_echec_le_message_part_sans_preuve(self):
        service = service_kafka(entetes(FauxJeton([ip.ErreurJeton("injoignable")])))
        with self.assertLogs("identite_poste", level=logging.WARNING):
            service.produce("rt_pointage", {"pin": 1})
        self.assertEqual(len(service.producer.envois), 1)
        self.assertNotIn("X-Yoclub-Poste", dict(service.producer.envois[0][2]["headers"]))

    def test_fournisseur_en_panne_le_message_part_quand_meme(self):
        def panne():
            raise RuntimeError("panne")
        service = service_kafka(panne)
        service.produce("rt_pointage", {"pin": 1})
        self.assertEqual(len(service.producer.envois), 1)
        self.assertNotIn("headers", service.producer.envois[0][2])

    def test_le_jeton_est_pris_au_moment_de_chaque_envoi(self):
        service = service_kafka(entetes(FauxJeton(["jeton-a", "jeton-b"])))
        service.produce("rt_pointage", {"pin": 1})
        service.produce("rt_pointage", {"pin": 1})
        valeurs = [dict(e[2]["headers"])["X-Yoclub-Poste"] for e in service.producer.envois]
        self.assertEqual(valeurs, [b"jeton-a", b"jeton-b"])

    def test_file_absente_erreur_claire_et_aucune_creation(self):
        admin = FauxAdmin(["rt_pointage"])
        service = service_kafka(admin=admin)
        with self.assertLogs("kafka_service.kafkaservice", level=logging.ERROR) as journal:
            self.assertFalse(service.verifier_topic("new_access_request_empire"))
        self.assertIn("file new_access_request_empire absente : à créer par tenant-manager", journal.output[0])
        self.assertEqual(admin.creations, [])
        self.assertTrue(service.verifier_topic("rt_pointage"))


class CommandesADistance(unittest.TestCase):

    def commandes(self, poste=None, fournisseur=None, admin=None):
        r = RemoteCommands("empire", "1003", "broker", "rt_pointage", "https://x",
                           producer=FauxProducteur(), poste=poste, entetes=fournisseur,
                           admin_factory=lambda: admin)
        return r

    def test_resultat_et_presence_portent_la_preuve(self):
        r = self.commandes(poste=object(), fournisseur=entetes(FauxJeton([JETON])))
        r._send("device_result", {"commandId": "c1"})
        r._send("bridge_presence", {"tenant": "empire"})
        for topic, valeur, kwargs in r._producer.envois:
            self.assertEqual(dict(kwargs["headers"])["X-Yoclub-Poste"], JETON.encode("utf-8"))
        self.assertEqual([e[0] for e in r._producer.envois], ["device_result", "bridge_presence"])

    def test_sans_fournisseur_aucun_entete(self):
        r = self.commandes()
        r._send("device_result", {"commandId": "c1"})
        self.assertNotIn("headers", r._producer.envois[0][2])

    def test_jeton_en_echec_le_resultat_part_sans_preuve(self):
        r = self.commandes(poste=object(), fournisseur=entetes(FauxJeton([ip.ErreurJeton("refusé")])))
        with self.assertLogs("identite_poste", level=logging.WARNING):
            r._send("device_result", {"commandId": "c1"})
        self.assertNotIn("X-Yoclub-Poste", dict(r._producer.envois[0][2]["headers"]))

    def test_poste_active_ne_cree_pas_la_file_des_commandes(self):
        admin = FauxAdmin([])
        r = self.commandes(poste=object(), admin=admin)
        with self.assertLogs("remote_commands", level=logging.ERROR) as journal:
            r._ensure_topic()
        self.assertEqual(admin.creations, [])
        self.assertIn("file device_command_empire absente : à créer par tenant-manager", journal.output[0])

    def test_poste_non_active_cree_la_file_comme_avant(self):
        admin = FauxAdmin([])
        self.commandes(admin=admin)._ensure_topic()
        self.assertEqual(admin.creations, ["device_command_empire"])


class Branchements(unittest.TestCase):

    def setUp(self):
        self.arbre = ast.parse(lire(PONT, "main.py"))

    def fonction(self, nom):
        return next(n for n in self.arbre.body if isinstance(n, ast.FunctionDef) and n.name == nom)

    def test_poste_active_les_files_ne_sont_plus_creees(self):
        corps = ast.unparse(self.fonction("start_kafka_consumers"))
        self.assertIn("if POSTE is not None:\n            pointage_kafka.verifier_topic(topic)\n"
                      "        else:\n            pointage_kafka.ensure_topic(topic)", corps)

    def test_tous_les_kafka_qui_ecrivent_recoivent_les_entetes(self):
        source = ast.unparse(self.arbre)
        constructions = [n for n in ast.walk(self.arbre)
                         if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "KafkaService"]
        self.assertEqual(len(constructions), 3)
        for appel in constructions:
            self.assertIn("entetes=ENTETES_KAFKA", ast.unparse(appel))
        self.assertIn("kafka.entetes = ENTETES_KAFKA", source)
        self.assertIn("ENTETES_KAFKA = machineService.client.entetes_kafka() if POSTE is not None else None",
                      source)
        demarrages = [ast.unparse(n) for n in ast.walk(self.arbre)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "start" and getattr(n.func.value, "id", None) == "remote_commands"]
        self.assertEqual(len(demarrages), 1)
        self.assertIn("entetes=ENTETES_KAFKA", demarrages[0])

    def test_les_moniteurs_passent_par_le_kafka_partage(self):
        zkem = lire(PONT, "services", "MonitorZkem.py")
        self.assertNotIn("KafkaService(", zkem)
        self.assertIn("from services.MachineMonitor import make_rt_json, kafka", zkem)
        self.assertIn("self.kafka = kafka", zkem)
        self.assertIn("from services.MachineMonitor import make_rt_json, kafka",
                      lire(PONT, "remote_commands.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
