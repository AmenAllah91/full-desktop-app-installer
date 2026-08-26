
# adapters/zkem_adapter.py
import base64
import logging
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Union
from win32com.client import VARIANT


import pythoncom
import pywintypes
import win32com.client

from services.adapters import DeviceAdapter
from services.common import zk_sdk_lock, tcp_reachable
from services.zkem_timezone import appliquer_timezone, ecrire_creneau


class ZkemAdapter(DeviceAdapter):
    """
    Implémentation stand-alone (zkemkeeper.dll)
    — handle non utilisé, mais conservé pour compatibilité.
    """

    def __init__(self, machine):
        super().__init__(machine)  # crée self.handle = None
        self.ip, self.port = machine.addresseip, int(machine.port)
        self.com_key = getattr(machine, "comKey", None) or 0
        self.mn = 1  # n° machine interne
        self.zk = win32com.client.Dispatch("zkemkeeper.ZKEM")
        self.connected = False  # indicateur interne

    # ------------------------------------------------------------
    # Connexion / déconnexion
    # ------------------------------------------------------------

    def connect(self, max_attempts: int = 3) -> bool:
        # Appliquer le comKey (communication password) si défini sur la machine
        com_key = getattr(self.machine, 'comKey', 0) or 0
        if com_key:
            try:
                self.zk.SetCommPassword(com_key)
                logging.info("🔑 ComKey appliqué pour %s", self.ip)
            except Exception as e:
                logging.warning("⚠️ SetCommPassword failed for %s: %s", self.ip, e)

        if self.com_key:
            try:
                self.zk.SetCommPassword(int(self.com_key))
            except Exception as ex:
                logging.warning("⚠️ SetCommPassword impossible pour %s : %s", self.ip, ex)

        for n in range(1, max_attempts + 1):
            try:
                if self.zk.Connect_Net(self.ip, self.port):
                    self.connected = True
                    logging.info("✅ ZKEM connecté %s", self.ip)
                    return True
            except Exception as e:
                logging.warning("⚠️ Connect_Net exception %s:%s : %s", self.ip, self.port, e)

            self.connected = False
            try:
                self.zk.Disconnect()
            except Exception:
                pass

            err_code = zkem_last_error(self.zk)
            logging.warning("ZKEM retry %s/%s (%s:%s) err=%s",
                            n, max_attempts, self.ip, self.port, err_code)
            time.sleep(0.5)

        return False

    def disconnect(self):
        if self.connected:
            self.zk.Disconnect()
            self.connected = False
            logging.info("🔌 ZKEM déconnecté %s", self.ip)

    def _ensure_conn(fn):
        """
        Garantit que CET objet COM est connecté avant tout appel SDK.

        On ne se fie surtout pas à self.connected : MonitorZkem le met à True
        quand SON propre objet COM se connecte, alors que self.zk — un objet
        distinct — n'a jamais eu Connect_Net. Le drapeau annonçait donc
        « connecté » sur un objet qui ne l'était pas, et tout appel venant de
        Flask (photo, porte, empreintes) échouait sans raison visible. Ça ne
        marchait que si process_device_queue avait connecté l'adapter juste
        avant pour une tâche Kafka — d'où ~50 % d'échecs apparemment aléatoires.

        connect() rejoue Connect_Net à chaque fois, exactement comme le fait
        déjà process_device_queue avant chaque tâche.

        Le tout sous zk_sdk_lock : la connexion et l'opération doivent former un
        bloc indivisible, sinon un autre thread peut entrer dans le SDK entre
        les deux et faire planter le process.
        """
        def wrapper(self, *a, **kw):
            # Sonde TCP AVANT de prendre le verrou. Une machine éteinte est un
            # cas normal en salle : sans ce filtre, Connect_Net partirait sur
            # ses 3 tentatives (timeout SDK) en retenant zk_sdk_lock, gelant les
            # monitors de toutes les autres machines pour une seule absente.
            if not tcp_reachable(self.ip, self.port):
                raise ConnectionError(
                    f"ZKEM {self.ip}:{self.port} injoignable (TCP)"
                )

            with zk_sdk_lock:
                if not self.connect():
                    raise ConnectionError(
                        f"ZKEM {self.ip}:{self.port} injoignable "
                        f"(err={zkem_last_error(self.zk)})"
                    )
                return fn(self, *a, **kw)

        return wrapper

    @_ensure_conn
    def add_user(self, pin, name, card_no, start_time, end_time,
                 timezone_slot=None, weekly_schedule=None) -> bool:
        """Ajout + règle de validité + créneau horaire hebdomadaire.

        timezone_slot / weekly_schedule sont facultatifs : un cloud plus
        ancien que ce pont ne les envoie pas, et l'adhérent garde alors
        le groupe 1 (24/7) — exactement le comportement d'avant.
        """
        self.zk.EnableDevice(self.mn, False)
        if card_no:
            self.zk.SetStrCardNumber(str(card_no))
            # user privileges 0 : user normal , 1 : enroller , 2 : admin , 3 : superadmin
        ok = self.zk.SSR_SetUserInfo(self.mn, pin, name, "", 3, True)
        if not ok:
            logging.error("SSR_SetUserInfo KO (pin=%s) err=%s",
                          pin, zkem_last_error(self.zk))
            self.connected = False
            try:
                self.zk.Disconnect()
            except Exception:
                pass
            self.zk.EnableDevice(self.mn, True)
            return False

        try:
            s = datetime.strptime(start_time, "%Y%m%d").strftime("%Y-%m-%d 00:00:00")
            e = datetime.strptime(end_time, "%Y%m%d").strftime("%Y-%m-%d 23:59:59")
            ok = self.zk.SetUserValidDate(self.mn, int(pin), 1, 1, s, e)
            if not ok:
                logging.error("SetUserValidDate KO (pin=%s)", pin)
        except Exception as ex:
            logging.exception("Date format err: %s", ex)
            ok = False

        # Le créneau horaire se pose APRÈS la période de validité, et sous le
        # même EnableDevice(False) : les deux forment la règle d'accès de
        # l'adhérent, un badge présenté entre les deux lirait une règle à
        # moitié écrite.
        if ok:
            ok = appliquer_timezone(self.zk, self.mn, pin, timezone_slot,
                                    weekly_schedule, self.machine.alias)

        # RefreshData : sans lui la pointeuse continue de servir sa copie en
        # mémoire, et le calendrier fraîchement écrit n'est pris en compte
        # qu'au redémarrage de l'appareil.
        try:
            self.zk.RefreshData(self.mn)
        except Exception as ex:
            logging.warning("RefreshData après add_user : %s", ex)

        self.zk.EnableDevice(self.mn, True)
        return ok

    @_ensure_conn
    def update_timezone(self, timezone_slot, weekly_schedule) -> bool:
        """Réécrit un calendrier sans toucher à aucun adhérent.

        Les utilisateurs déjà placés dans le groupe correspondant changent
        d'horaire du même coup — c'est tout l'intérêt : une modification de
        timezone s'applique sans repousser un seul accès.
        """
        self.zk.EnableDevice(self.mn, False)
        try:
            ok = ecrire_creneau(self.zk, self.mn, timezone_slot,
                                weekly_schedule, self.machine.alias)
        finally:
            try:
                self.zk.RefreshData(self.mn)
            except Exception as ex:
                logging.warning("RefreshData après update_timezone : %s", ex)
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

    def resource_path(self,relative_path: str, subfolder: str = None) -> str:
        if getattr(sys, 'frozen', False):
            # PyInstaller onefile : les datas sont extraites dans _MEIPASS,
            # pas à côté de l'exe. onedir : _MEIPASS == dossier de l'exe.
            base_dir = getattr(sys, '_MEIPASS', None) or os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(base_dir)
        if subfolder:
            base_dir = os.path.join(base_dir, subfolder)

        return os.path.join(base_dir, relative_path)

    @_ensure_conn
    def download_user_photo(self, pin: str, path: str) -> bool:
        """
        Télécharge la photo/empreinte faciale (JPG) via l’utilitaire C#
        « ConsoleApp1.exe » qui appelle GetUserFacePhotoByName.

        :param pin:      code utilisateur
        :param path:     chemin complet où copier la photo finale (…\\<pin>.jpg)
        :return:         True si OK, False sinon
        """
        base_path = Path(self.resource_path("", subfolder="getuserfacephoto"))
        exe_path= base_path / "ConsoleApp1.exe"

        if not exe_path.exists():
            logging.error("❌ ConsoleApp1.exe introuvable : %s", exe_path)
            return False

        # dossier temporaire où ConsoleApp1 place le fichier
        temp_dir = Path(path).parent
        temp_dir.mkdir(parents=True, exist_ok=True)

        # 5e argument attendu par l'exe : le comKey.
        # "0" = aucun mot de passe de communication (sinon l'exe retombe
        # sur sa valeur par défaut codée en dur, 654321).
        try:
            com_key_arg = str(int(self.com_key or 0))
        except (TypeError, ValueError):
            com_key_arg = "0"

        cmd = [
            str(exe_path),
            str(self.ip),                     # IP
            str(self.port),                   # port
            str(pin),                         # user PIN
            # normpath : retire le "\" final que list2cmdline échapperait
            os.path.normpath(str(temp_dir)),
            com_key_arg                       # comKey
        ]

        logging.info("📡 [FACE] Lancement exe : %s", subprocess.list2cmdline(cmd))
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",             # l'exe écrit de l'UTF-8 (emojis)
                errors="replace",
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

    def _extract_template_from_com_result(self, res):
        """
        res peut être:
        - bool
        - tuple: (success, template, length) ou (success, flag, template, length) ...
        On extrait la string template de façon robuste.
        """
        if res is None:
            return None

        if isinstance(res, tuple) and len(res) >= 2:
            success = bool(res[0])
            if not success:
                return None

            # le template est généralement le seul "str" du tuple
            for item in res[1:]:
                if isinstance(item, str) and item.strip():
                    return item.strip()
            return None

        # si jamais le COM renvoie juste True/False
        if isinstance(res, bool):
            return None

        return None

    def _read_template_str(self, user_id: str, finger_id: int):
        """
        Tente SSR_GetUserTmpStr puis GetUserTmpExStr avec plusieurs signatures possibles.
        Retourne template base64 ou None.
        """
        # 1) SSR_GetUserTmpStr (souvent dispo)
        if hasattr(self.zk, "SSR_GetUserTmpStr"):
            m = getattr(self.zk, "SSR_GetUserTmpStr")
            variants = [
                (int(self.mn), str(user_id), int(finger_id)),
                (int(self.mn), str(user_id), int(finger_id), "", 0),
            ]
            for args in variants:
                try:
                    res = m(*args)
                    tpl = self._extract_template_from_com_result(res)
                    if tpl:
                        return tpl
                except TypeError:
                    continue
                except Exception as e:
                    logging.debug(f"SSR_GetUserTmpStr fail args={args}: {e}")

        # 2) GetUserTmpExStr (souvent dispo si SetUserTmpExStr existe)
        if hasattr(self.zk, "GetUserTmpExStr"):
            m = getattr(self.zk, "GetUserTmpExStr")
            variants = [
                (int(self.mn), int(user_id), int(finger_id)),
                (int(self.mn), int(user_id), int(finger_id), 0, "", 0),
            ]
            for args in variants:
                try:
                    res = m(*args)
                    tpl = self._extract_template_from_com_result(res)
                    if tpl:
                        return tpl
                except TypeError:
                    continue
                except Exception as e:
                    logging.debug(f"GetUserTmpExStr fail args={args}: {e}")

        return None

    @_ensure_conn
    def get_fingerprints(self, pin: str) -> list[dict]:
        """
        Standalone SDK : on teste FingerID 0..9.
        """
        pin_str = str(pin)
        out = []

        for fid in range(0, 10):
            tpl = self._read_template_str(pin_str, fid)
            if tpl:
                out.append({
                    "fingerId": fid,
                    "template": tpl,     # base64 (compatible avec SetUserTmpExStr)
                    "source": "STANDALONE"
                })

        return out

    @_ensure_conn
    def delete_user(self, pin, finger_id=None) -> bool:
        """Supprime l'utilisateur (empreintes incluses)"""
        return self.zk.SSR_DeleteEnrollData(self.mn, pin, 1)

    @_ensure_conn
    def authorize_user(self, pin,
                       timezone_slot=None, weekly_schedule=None) -> bool:
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

    @staticmethod
    def bytes_to_long_array(template_bytes) -> list[int]:

        """
        Convert fingerprint bytes into list of 32-bit unsigned integers for SetEnrollData.
        Each int is 4 bytes in little-endian order.
        """
        # Make sure it's plain bytes
        if not isinstance(template_bytes, (bytes, bytearray)):
            template_bytes = bytes(template_bytes)

        # Pad to multiple of 4
        pad_len = (4 - len(template_bytes) % 4) % 4
        template_bytes += b'\x00' * pad_len

        longs = []
        for i in range(0, len(template_bytes), 4):
            val = int.from_bytes(template_bytes[i:i + 4], byteorder='little', signed=False)
            longs.append(val)

        return longs

    @_ensure_conn
    def add_fingerprint(self, user_id: str, fingerprint_template: bytes, finger_id: int) -> bool:
        """
        Add a fingerprint template to the ZK device for a specific user.

        Args:
            user_id: User PIN/ID (string, e.g. "1001")
            fingerprint_template: Captured fingerprint template (bytes, ZK format)
            finger_id: Finger ID (0-9, where 0=Thumb)
        """
        if not self.zk:
            logging.error(" ZK device not connected")
            return False

        try:
            name, password, privilege, enabled = "", "", 0, True
            exists = self.zk.SSR_GetUserInfo(self.mn, str(user_id), name, password, privilege, enabled)
            if not exists[0]:
                logging.info(f" User {user_id} does not exist, creating...")
                ok = self.zk.SSR_SetUserInfo(self.mn, str(user_id), f"User{user_id}", "", 0, True)
                if not ok:
                    logging.error(f"Failed to create user {user_id}")
                    return False
            if hasattr(fingerprint_template, 'tobytes'):
                fingerprint_bytes = fingerprint_template.tobytes()
            elif hasattr(fingerprint_template, 'data'):
                fingerprint_bytes = fingerprint_template.data
            else:
                fingerprint_bytes = bytes(fingerprint_template)

            fingerprint_template_base64 = base64.b64encode(fingerprint_bytes).decode('utf-8')

            success = self.zk.SetUserTmpExStr(
                int(self.mn),  # LONG dwMachineNumber
                int(user_id),  # LONG dwEnrollNumber
                int(finger_id),  # LONG dwFingerIndex
                int(1),  # LONG Flag
                fingerprint_template_base64  # BSTR TmpData
            )

            if success:
                logging.info(f"Fingerprint added successfully for User={user_id}, FingerID={finger_id}")
                return True
            else:
                logging.error(f"Failed to add fingerprint for User={user_id}, FingerID={finger_id} (err={zkem_last_error(self.zk)})")
                return False

        except Exception as e:
            logging.exception(f"Exception in add_fingerprint for User={user_id}: {e}")
            return False

    @_ensure_conn
    def delete_fingerprint(self, pin: str, finger_id: int) -> bool:
        """
        Delete a specific fingerprint template from the ZK device.

        Args:
            pin: User PIN/ID (string, e.g. "1001")
            finger_id: Finger ID to delete (0-9, where 0=Thumb)

        Returns:
            True if fingerprint was deleted successfully, False otherwise
        """
        if not self.zk:
            logging.error("❌ ZK device not connected")
            return False

        try:
            self.zk.EnableDevice(self.mn, False)
            success = self.zk.SSR_DeleteEnrollDataExt(
                self.mn,  # Machine number
                str(pin),  # User PIN
                int(finger_id)  # Finger ID (0-9)
            )

            if success:
                logging.info(f"Fingerprint deleted for PIN={pin}, FingerID={finger_id}")

            return True

        except Exception as e:
            logging.exception(f"Exception in delete_fingerprint for PIN={pin}, FingerID={finger_id}: {e}")
            return False

        finally:
            try:
                self.zk.RefreshData(self.mn)
                self.zk.EnableDevice(self.mn, True)
            except Exception:
                pass

    @_ensure_conn
    def open_door(self, duration_seconds: float = 5.0) -> bool:
        """
        Ouvre la porte (relais) du terminal standalone.

        :param duration_seconds: durée d'ouverture en secondes
        """
        try:
            delay = int(max(1, duration_seconds) * 10)  # doc : Delay/10 = secondes
            ok = self.zk.ACUnlock(self.mn, delay)
            if not ok:
                # zkem_last_error est définie plus bas dans CE module : l'import
                # "from MonitorZkem import ..." pointait vers un module inexistant
                # et masquait le vrai code d'erreur par un ModuleNotFoundError.
                err = zkem_last_error(self.zk)
                logging.error("❌ ACUnlock KO %s err=%s", self.ip, err)
                return False

            logging.info("✅ Porte ouverte sur standalone %s pendant %ss", self.ip, duration_seconds)
            return True

        except Exception as exc:
            logging.exception("❌ Exception ACUnlock sur %s : %s", self.ip, exc)
            return False

_LAST_ERROR_PROBE_LOGGED = False


def zkem_last_error(zk) -> Union[int, str]:
    """
    Lecture robuste du code d'erreur, toutes versions SDK.
    """
    # GetLastError rend le code par PARAMÈTRE DE SORTIE. pywin32 le renvoie
    # comme valeur de retour, à condition qu'on fournisse un argument
    # d'emplacement — n'importe quel entier fait l'affaire.
    #
    # Les deux formes essayées auparavant échouaient toutes les deux, d'où
    # 24 erreurs sur 24 en « err=? » depuis le 2026-08-14 :
    #   - sans argument      -> com_error 0x8002000F « Paramètre non facultatif »
    #   - VARIANT VT_BYREF   -> TypeError au marshaling
    # Vérifié le 2026-08-18 sur un ZKEM réel, en liaison tardive comme précoce.
    try:
        return int(zk.GetLastError(0))
    except (TypeError, pywintypes.com_error) as ex_direct:
        first = ex_direct

    # Reliquat : certaines versions n'acceptent que la forme sans argument.
    try:
        return int(zk.GetLastError())
    except Exception as ex_byref:
        # Les deux formes échouent sur le SDK déployé (constaté en prod le
        # 2026-08-14 : 24 erreurs sur 24 en "?"). On journalise la vraie cause
        # UNE fois, pour pouvoir enfin corriger l'appel plutôt que deviner.
        global _LAST_ERROR_PROBE_LOGGED
        if not _LAST_ERROR_PROBE_LOGGED:
            _LAST_ERROR_PROBE_LOGGED = True
            logging.warning(
                "⚠️ GetLastError inexploitable sur ce SDK — appel direct: %r | "
                "VARIANT byref: %r", first, ex_byref
            )
        return "?"