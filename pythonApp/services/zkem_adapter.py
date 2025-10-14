# adapters/zkem_adapter.py
import gc
import logging
import ctypes
import os
import subprocess
import time
import sys
from datetime import datetime
from pathlib import Path
from typing import Union

import pythoncom
import pywintypes
import win32com.client

from services.adapters import DeviceAdapter


class ZkemAdapter(DeviceAdapter):
    """
    Implémentation stand-alone (zkemkeeper.dll)
    — handle non utilisé, mais conservé pour compatibilité.
    """

    def __init__(self, machine):
        super().__init__(machine)  # crée self.handle = None
        self.ip, self.port = machine.addresseip, int(machine.port)
        self.mn = 1  # n° machine interne
        self.zk = win32com.client.Dispatch("zkemkeeper.ZKEM")
        self.connected = False  # indicateur interne

    # ------------------------------------------------------------
    # Connexion / déconnexion
    # ------------------------------------------------------------

    def connect(self, max_attempts: int = 3) -> bool:
        if self.connected:                  # déjà OK
            return True

        for n in range(1, max_attempts + 1):
            if self.zk.Connect_Net(self.ip, self.port):
                self.connected = True
                logging.info("✅ ZKEM connecté %s", self.ip)
                return True

            # échec : on récupère le code d'erreur puis on attend
            err_code = zkem_last_error(self.zk)
            logging.warning("ZKEM retry %s/%s (%s:%s) err=%s",
                            n, max_attempts, self.ip, self.port, err_code)
            time.sleep(0.0001)

        # toutes les tentatives échouent
        return False

    def disconnect(self):
        if self.connected:
            self.zk.Disconnect()
            self.connected = False
            logging.info("🔌 ZKEM déconnecté %s", self.ip)

    def _ensure_conn(fn):
        def wrapper(self, *a, **kw):
            if not self.connected:
                self.connect()
            return fn(self, *a, **kw)

        return wrapper

    @_ensure_conn
    def add_user(self, pin, name ,card_no, start_time, end_time) -> bool:
        """Ajout + règle de validité"""
        self.zk.EnableDevice(self.mn, False)
        if card_no:
            self.zk.SetStrCardNumber(str(card_no))
        ok = self.zk.SSR_SetUserInfo(self.mn, pin, name, "",0, True)
        if not ok:
            logging.error("SSR_SetUserInfo KO (pin=%s)", pin)
            self.zk.EnableDevice(self.mn, True)
            return False

        try:
            s = datetime.strptime(start_time, "%Y%m%d").strftime("%Y-%m-%d 00:00:00")
            e = datetime.strptime(end_time, "%Y%m%d").strftime("%Y-%m-%d 23:59:59")
            ok = self.zk.SetUserValidDate(self.mn, int(pin), True, 1, s, e)
            if not ok:
                logging.error("SetUserValidDate KO (pin=%s)", pin)
        except Exception as ex:
            logging.exception("Date format err: %s", ex)
            ok = False

        self.zk.EnableDevice(self.mn, True)
        return ok

    @_ensure_conn  # réutilise le décorateur existant
    def upload_user_photo(self, pin: str, photo_path: str) -> bool:
        """
        Envoie la photo faciale (JPG) à CETTE machine, afin que le
        terminal crée automatiquement le template visage de l’utilisateur.

        :param pin:        badge/pin utilisateur
        :param photo_path: chemin complet du fichier JPG (peut être mal nommé)
        :return:           True si l’upload a réussi, False sinon
        """
        # 1) Vérifications préalables --------------------------------------
        if not os.path.isfile(photo_path):
            logging.error("❌ Fichier introuvable : %s", photo_path)
            return False

        correct_name = f"verify_biophoto_9_{pin}.jpg"
        temp_dir = os.path.dirname(photo_path)
        correct_path = os.path.join(temp_dir, correct_name)

        # 2) Renomme si besoin (la machine exige ce nom) --------------------
        try:
            if photo_path != correct_path:
                os.replace(photo_path, correct_path)
                logging.info("📸 Photo renommée ➜ %s", correct_path)
        except Exception as ex:
            logging.error("❌ Impossible de renommer la photo : %s", ex)
            return False

        # 3) Envoi à la machine via zkemkeeper -----------------------------
        try:
            self.zk.EnableDevice(self.mn, False)
            success = self.zk.SendUserFacePhoto(self.mn, correct_path)
            if success:
                logging.info("✅ FaceID envoyé pour PIN %s (%s)", pin, self.ip)
            else:
                logging.error("❌ SendUserFacePhoto KO (PIN %s) err=%s",
                              pin, zkem_last_error(self.zk))
        except Exception as ex:
            logging.warning("⚠️ Exception SendUserFacePhoto : %s", ex)
            success = False
        finally:
            # On réactive le lecteur dans tous les cas
            try:
                self.zk.RefreshData(self.mn)
                self.zk.EnableDevice(self.mn, True)
            except Exception:
                pass

        return bool(success)

    @_ensure_conn
    def download_user_photo(self, pin: str, path: str) -> bool:
        """
        Télécharge la photo/empreinte faciale (JPG) via l’utilitaire C#
        « ConsoleApp1.exe » qui appelle GetUserFacePhotoByName.

        :param pin:      code utilisateur
        :param path:     chemin complet où copier la photo finale (…\\<pin>.jpg)
        :return:         True si OK, False sinon
        """
        if hasattr(sys, '_MEIPASS'):
            base_path = Path(sys._MEIPASS) / "getuserfacephoto"
        else:
            base_path = Path(__file__).resolve().parents[1] / "getuserfacephoto"
        exe_path= base_path / "ConsoleApp1.exe"

        if not exe_path.exists():
            logging.error("❌ ConsoleApp1.exe introuvable : %s", exe_path)
            return False

        # dossier temporaire où ConsoleApp1 place le fichier
        temp_dir = Path(path).parent
        temp_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(exe_path),
            self.machine.addresseip,          # IP
            str(self.machine.port),           # port
            str(pin),                         # user PIN
            str(temp_dir)                     # dossier destination
        ]

        logging.info("📡 [FACE] Lancement exe : %s", " ".join(cmd))
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            logging.debug("STDOUT: %s", res.stdout.strip())
            logging.debug("STDERR: %s", res.stderr.strip())

            if res.returncode != 0:
                logging.error("❌ ConsoleApp1 a retourné %s", res.returncode)
                return False

            # le .exe sauve le fichier sous <pin>.jpg dans temp_dir
            src_file = temp_dir / f"verify_biophoto_9_{pin}.jpg"
            dest_file = Path(path)

            if not src_file.exists():
                logging.error("❌ Fichier JPG non créé : %s", src_file)
                return False

            # copie / écrase si déjà présent
            src_file.replace(dest_file)
            logging.info("✅ Photo visage téléchargée ➜ %s", dest_file)
            return True

        except subprocess.TimeoutExpired:
            logging.error("⏳ ConsoleApp1.exe a dépassé 30 s")
            return False
        except Exception as ex:
            logging.exception("⚠️ Exception download_face : %s", ex)
            return False

    @_ensure_conn
    def delete_user(self, pin, finger_id=None) -> bool:
        """Supprime l'utilisateur (empreintes incluses)"""
        return self.zk.SSR_DeleteEnrollData(self.mn, pin, 1)

    @_ensure_conn
    def authorize_user(self, pin) -> bool:
        """Pas d'équivalent précis dans zkemkeeper ; toujours True."""
        return True

    @_ensure_conn
    def unauthorize_user(self, pin) -> bool:
        """Ajout + règle de validité"""
        try:
            s = datetime.strptime("20200101", "%Y%m%d").strftime("%Y-%m-%d 00:00:00")
            e = datetime.strptime('20200102', "%Y%m%d").strftime("%Y-%m-%d 23:59:59")
            ok = self.zk.SetUserValidDate(self.mn, int(pin), True, 1, s, e)
            if not ok:
                logging.error("SetUserValidDate KO (pin=%s)", pin)
        except Exception as ex:
            logging.exception("Date format err: %s", ex)
            ok = False

        self.zk.EnableDevice(self.mn, True)
        return ok


def zkem_last_error(zk) -> Union[int, str]:
    """
    Lecture robuste du code d'erreur, toutes versions SDK.
    """
    try:                                    # firmware récent
        return int(zk.GetLastError())
    except (TypeError, pywintypes.com_error):
        err = ctypes.c_long()
        try:                                # firmware ancien
            zk.GetLastError(err)
            return err.value
        except Exception:
            return "?"
