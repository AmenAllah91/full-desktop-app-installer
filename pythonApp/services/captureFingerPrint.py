import base64
import io
import sys
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Le SDK du lecteur d'empreintes appartient au PILOTE, pas a l'application.
#
# L'installeur ZKFinger depose dans System32 un ensemble indissociable :
#
#     libzkfp.dll                     API principale
#     ZKFPCap.dll                     couche de capture
#     fpslib.dll                      bibliotheque d'algorithme
#     ZKFPSensors\libzklibcap.dll     plugins, charges par ZKFPCap.dll
#     ZKFPSensors\libsilkidcap.dll      depuis un sous-dossier situe A COTE
#     ZKFPSensors\libidfprcap.dll       d'elle-meme
#
# Copier les trois premieres a la racine de l'application SANS le dossier
# ZKFPSensors donne un ensemble incomplet : ZKFPCap.dll se charge, ne trouve
# pas ses plugins, et ZKFPM_Init() renvoie -1 (« Failed to initialize the
# algorithm library »). C'est exactement ce qui est arrive le 2026-08-18 —
# et le simple fait d'ajouter le dossier local en tete du chemin de recherche
# suffisait a masquer l'installation System32, pourtant complete et valide.
#
# On ne prend donc un dossier local en compte que s'il porte l'ensemble
# COMPLET. Sinon on ne touche a rien : l'ordre de recherche par defaut de
# Windows trouvera l'installation du pilote.
#
# libzkfpcsharp.dll est un cas different : c'est le wrapper C# livre avec
# l'application (et avec pyzkfp), pas un composant du pilote.
# ---------------------------------------------------------------------------
DLL_NATIVE = "libzkfp.dll"
DOSSIER_PLUGINS = "ZKFPSensors"


def _dossiers_candidats():
    """Dossiers ou une installation LOCALE complete pourrait se trouver."""
    ici = Path(__file__).resolve().parent          # .../services
    racine = ici.parent                            # .../pythonApp
    dossiers = [racine, racine / "_internal", ici]

    meipass = getattr(sys, "_MEIPASS", None)       # extraction PyInstaller
    if meipass:
        dossiers.insert(0, Path(meipass))
    if getattr(sys, "frozen", False):              # a cote de l'exe installe
        dossiers.insert(0, Path(sys.executable).resolve().parent)

    vus, uniques = set(), []
    for d in dossiers:
        if d and d.exists() and str(d) not in vus:
            vus.add(str(d))
            uniques.append(d)
    return uniques


def _ensemble_complet(dossier):
    """Vrai si ce dossier porte la DLL native ET son dossier de plugins."""
    return (dossier / DLL_NATIVE).exists() and (dossier / DOSSIER_PLUGINS).is_dir()


def trouver_dll_native():
    """Chemin d'une installation LOCALE complete, sinon None.

    None ne veut pas dire « SDK absent » : l'installation du pilote dans
    System32 reste le cas nominal et n'est pas listee ici.
    """
    for d in _dossiers_candidats():
        if _ensemble_complet(d):
            return d / DLL_NATIVE
    return None


def _prepare_dll_search_path():
    for d in _dossiers_candidats():
        if not _ensemble_complet(d):
            continue          # ensemble partiel : surtout ne pas le prioriser
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

_prepare_dll_search_path()

# ⚠️ Importer pyzkfp APRES la préparation
from pyzkfp import ZKFP2

import time
from PIL import Image
import numpy as np

from services.websocket import send_fingerprint


class FingerprintCapture:
    def __init__(self):
        self.zk = None
        self.derniere_erreur = ""     # cause exploitable pour l'appelant HTTP

    def initialize_device(self):
        """Initialize the fingerprint device"""
        try:
            _prepare_dll_search_path()
            self.zk = ZKFP2()
            self.zk.Init()

            if self.zk.GetDeviceCount() < 1:
                self.derniere_erreur = (
                    "Aucun lecteur d'empreintes detecte. La DLL est presente : "
                    "verifier que le lecteur est branche et reconnu par Windows."
                )
                logger.error(self.derniere_erreur)
                return False

            self.zk.OpenDevice(0)
            logger.info("Fingerprint device initialized successfully")
            return True

        except Exception as e:
            # Le SDK ne distingue pas « pilote absent » de « lecteur debranche » :
            # les deux ressortent en ZKFPM_Init() = -1. On oriente donc vers la
            # cause de loin la plus frequente, le pilote non installe — le lecteur
            # d'empreintes est optionnel et rare dans le parc.
            self.derniere_erreur = (
                f"Initialisation du lecteur impossible : {e}. "
                f"Le SDK ZKFinger appartient au PILOTE du lecteur, pas a "
                f"l'application : verifier que le pilote est installe sur ce poste "
                f"(il depose libzkfp.dll, ZKFPCap.dll, fpslib.dll et le dossier "
                f"{DOSSIER_PLUGINS}\\ dans System32) et que le lecteur est branche."
            )
            logger.error("Error initializing device: %s", e)
            return False

    def capture_single_template(self, attempt_num):
        """Capture a single fingerprint template and image"""
        logger.info("Scan %s/3 ...", attempt_num)
        logger.info("Place your finger on the scanner...")

        max_attempts = 30
        attempts = 0

        while attempts < max_attempts:
            try:
                capture = self.zk.AcquireFingerprint()
                if capture:
                    template, img = capture
                    logger.info("Captured template %s", attempt_num)
                    return template, img
            except Exception as e:
                logger.warning("Capture attempt failed: %s", e)

            time.sleep(1)
            attempts += 1
            if attempts % 5 == 0:
                logger.info("Still waiting... (%s/%s)", attempts, max_attempts)

        raise Exception(f"Failed to capture template {attempt_num} after {max_attempts} attempts")

    def merge_templates(self, templates):
        """Fusionne les trois captures en un gabarit d'enrolement.

        DBMerge rend un COUPLE (gabarit, longueur_reelle) — et la seconde
        valeur n'est pas un code de retour malgre son ancien nom
        `result_code` : c'est `regTempLen`, la longueur utile du gabarit.

        Elle etait ignoree, donc on transmettait le tampon complet de
        2048 octets aux pointeuses, dont ~850 octets de zeros de remplissage
        (mesure sur une capture reelle : 1198 octets utiles sur 2048).
        """
        try:
            merge_result = self.zk.DBMerge(templates[0], templates[1], templates[2])
            if not isinstance(merge_result, tuple):
                return merge_result

            gabarit = merge_result[0]
            longueur = merge_result[1] if len(merge_result) >= 2 else None

            octets = bytes(gabarit)
            if isinstance(longueur, int) and 0 < longueur <= len(octets):
                utile = octets[:longueur]
            else:
                # Repli : on retire au moins le remplissage a zero.
                utile = octets.rstrip(b"\x00") or octets
                longueur = len(utile)

            logger.info("Gabarit fusionne : %s octets utiles (tampon de %s)",
                        len(utile), len(octets))
            return utile

        except Exception as e:
            logger.error("Error merging templates: %s", e)
            logger.warning("Using first template as fallback")
            return templates[0]

    def save_template(self, template, filename="fingerprint_final.tpl"):
        """Save template to file"""
        try:
            if isinstance(template, tuple):
                template = template[0] if len(template) > 0 else b''

            if not isinstance(template, bytes):
                if hasattr(template, 'encode'):
                    template = template.encode()
                else:
                    template = bytes(template)

            with open(filename, "wb") as f:
                f.write(template)

            file_size = os.path.getsize(filename)
            logger.info("Final merged template saved as %s (%s bytes)", filename, file_size)
            return filename

        except Exception as e:
            logger.error("Error saving template: %s", e)
            return None

    def save_image(self, image_data, filename="fingerprint.bmp"):
        try:
            img_array = np.frombuffer(image_data, dtype=np.uint8)
            width, height = 300, 375
            if img_array.size != width * height:
                side = int(np.sqrt(img_array.size))
                width, height = side, side

            img_array = img_array.reshape((height, width))
            img = Image.fromarray(img_array)
            img.save(filename)
            logger.info("Fingerprint image saved as %s", filename)
            return filename
        except Exception as e:
            logger.error("Error saving image: %s", e)
            return None

    def capture_fingerprint(self, save_file=True, template_filename="fingerprint_final.tpl"):
        """
        Capture fingerprint 3 times, merge templates, save final template and images
        Returns:
            tuple: (final_template, image_filenames)
        """
        if not self.initialize_device():
            return None, None

        try:
            templates = []
            images = []
            image_files = []

            logger.info("Please scan the same finger 3 times")
            logger.info("Make sure to place your finger properly on the scanner each time")

            for i in range(3):
                template, img = self.capture_single_template(i + 1)
                templates.append(template)
                img_file = self.save_image(img, f"fingerprint_{i + 1}.bmp")
                image_files.append(img_file)
                pil_img = Image.frombytes('L', (300 , 375), img)  # 'L' = 8bit grayscale

                # Save PIL image to bytes buffer as BMP
                buffer = io.BytesIO()
                pil_img.save(buffer, format="BMP")
                img_bytes = buffer.getvalue()

                # Convert to base64
                img_b64 = base64.b64encode(img_bytes).decode('utf-8')

                # Send fingerprint step
                data = {"step": i + 1, "fingerprint": img_b64}
                send_fingerprint(data, "1003")

                if i < 2:
                    logger.info("Please lift your finger and prepare for next scan...")
                    time.sleep(2)
            logger.info("Merging templates...")
            final_template = self.merge_templates(templates)

            if final_template:
                logger.info("Templates merged successfully!")

                if save_file:
                    self.save_template(final_template, template_filename)

                return final_template, image_files
            else:
                logger.error("Failed to merge templates")
                return None, None

        except Exception as e:
            logger.error("Error during fingerprint capture: %s", e)
            return None, None

        finally:
            self.cleanup()

    def cleanup(self):
        """Clean up device resources"""
        try:
            if self.zk:
                self.zk.CloseDevice()
                self.zk.Terminate()
                logger.info("Device closed successfully")
        except Exception as e:
            logger.error("Error during cleanup: %s", e)
