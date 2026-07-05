# services/adapters.py
import base64
from abc import ABC, abstractmethod
from enum import Enum
import logging, time, ctypes, os
from ctypes import *
import platform


# -------------------------------------------------------- Types machine
class MachineType(Enum):
    C3 = "C3"
    STANDALONE_NEW_FIRMWARE = "STANDALONE_NEW_FIRMWARE"


# -------------------------------------------------------- Contrat commun
class DeviceAdapter(ABC):
    def __init__(self, machine):
        self.machine = machine  # modèle AccessMachine
        self.handle = None  # pointeur PLComm ou None
        self.connected = False     # état réel de la connexion
        self.last_seen = None      # timestamp Unix dernier événement
        self.online_since = None   # timestamp Unix mise en ligne
        self.offline_since = None  # timestamp Unix mise hors-ligne
        self.reconnect_count = 0
        self.event_count = 0
        self.last_error = ""

    # ---- connexion
    @abstractmethod
    def connect(self, max_attempts: int = 3): ...

    @abstractmethod
    def disconnect(self): ...

    # ---- opérations métier
    @abstractmethod
    def add_user(self, pin, name ,card, start, end) -> bool: ...

    @abstractmethod
    def delete_user(self, pin, finger_id=None) -> bool: ...

    @abstractmethod
    def authorize_user(self, pin) -> bool: ...

    @abstractmethod
    def unauthorize_user(self, pin) -> bool: ...

    @abstractmethod
    def add_fingerprint(self,pin, fingerprint_template: bytes, finger_id: int, save_to_kafka: bool = True) -> bool: ...

    @abstractmethod
    def get_fingerprints(self, pin: str) -> list[dict]:
        """
        Retourne les templates d'empreintes pour un PIN donné.
        Format attendu: [{ "fingerId": int, "template": str, ... }, ...]
        """
        ...

# -------------------------------------------------------- PLComm adapter
if platform.system() != "Windows":
    raise EnvironmentError("❌ Le SDK plcommpro.dll est uniquement supporté sur Windows.")

# Construction dynamique du chemin vers la DLL dans System32
PLCOMPRO_URL = os.path.join(os.environ["WINDIR"], "System32", "plcommpro.dll")

# Vérifie que le fichier existe
if not os.path.exists(PLCOMPRO_URL):
    raise FileNotFoundError(f"❌ La DLL 'plcommpro.dll' est introuvable dans System32 : {PLCOMPRO_URL}")

# Chargement de la DLL
plcommpro = CDLL(PLCOMPRO_URL)
logging.info(f"✅ DLL chargée depuis : {PLCOMPRO_URL}")

# Définition des signatures des fonctions utilisées
plcommpro.Connect.argtypes = [c_char_p]
plcommpro.Connect.restype = c_void_p
plcommpro.Disconnect.argtypes = [c_void_p]
plcommpro.SetDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
plcommpro.SetDeviceData.restype = c_int
plcommpro.DeleteDeviceData.argtypes = [c_void_p, c_char_p, c_char_p, c_char_p]
plcommpro.GetDeviceData.argtypes = [
    c_void_p,
    c_char_p,
    c_int,
    c_char_p,
    c_char_p,
    c_char_p,
    c_char_p
]
plcommpro.GetDeviceData.restype = c_int
plcommpro.ControlDevice.argtypes = [
    c_void_p,   # handle (pointeur retourné par Connect)
    c_int,      # OperationID
    c_int,      # Param1
    c_int,      # Param2
    c_int,      # Param3
    c_int,      # Param4
    c_char_p    # Options
]
plcommpro.ControlDevice.restype = c_int

class PlcommAdapter(DeviceAdapter):  # ✅ hérite !
    def __init__(self, machine):
        super().__init__(machine)

    # ------------- connexion
    def connect(self, max_attempts: int = 3):
        if self.handle:
            return self.handle
        ip, port = self.machine.addresseip, self.machine.port
        com_key = getattr(self.machine, "comKey", None) or ""
        params = (f"protocol=TCP,ipaddress={ip},port={port},"
                  f"timeout=4000,passwd={com_key}").encode()
        for i in range(1, max_attempts + 1):
            logging.info("PLComm connect %s (%s/%s)", ip, i, max_attempts)
            h = plcommpro.Connect(params)
            if h:
                self.handle = h
                logging.info("✅ Connected %s (handle=%s)", ip, h)
                return h
            time.sleep(1)
        logging.error("❌ Unable to connect %s", ip)
        return None

    def disconnect(self):
        if self.handle:
            try:
                plcommpro.Disconnect(self.handle)
            finally:
                self.handle = None

    # ------------- helper appelé par DeviceContext (C3)
    def bind_handle(self, h):
        self.handle = h

    # ------------- helpers internes
    def _set(self, table: bytes, data: str) -> bool:
        if not self.handle:
            return False
        try:
            return plcommpro.SetDeviceData(
                self.handle, table, data.encode(), b"") == 0
        except OSError as exc:
            logging.error("PLComm _set crash %s : %s",
                          self.machine.alias, exc)
            self.disconnect()
            return False

    def _del(self, table: bytes, cond: str) -> bool:
        if not self.handle:
            return False
        try:
            return plcommpro.DeleteDeviceData(
                self.handle, table, cond.encode(), None) == 0
        except OSError as exc:
            logging.error("PLComm _del crash %s : %s",
                          self.machine.alias, exc)
            self.disconnect()
            return False

    def get_pin_by_card(self, card_no) -> str:
        """
        Search the user table for the given card number.
        Returns the Pin if found, else None.
        Data format: UID,CardNo,Pin,Password,Group,StartTime,EndTime,Name,SuperAuthorize
        """
        if not self.handle:
            logging.error("❌ Device not connected")
            return None

        buffer_size = 1024 * 1024  # 1 MB buffer
        buffer = create_string_buffer(buffer_size)
        card_no_clean = str(card_no).lstrip("0")
        logging.info(f"🔍 Searching for card: {card_no} (cleaned: {card_no_clean})")
        try:
            ret = plcommpro.GetDeviceData(
                self.handle,
                buffer,
                buffer_size,
                b"user",
                b"*",  # Get all fields
                b"",  # No filter - get all users
                b""
            )
            logging.debug(f"GetDeviceData returned: {ret}")
            if ret < 0:
                logging.error(f"❌ GetDeviceData failed with error: {ret}")
                return None
            raw_data = buffer.value.decode("utf-8", errors="ignore").strip()
            if not raw_data:
                logging.info("ℹ️ No user data found")
                return None
            logging.debug(f"Raw data length: {len(raw_data)} characters")
            lines = raw_data.replace('\r\n', '\n').split('\n')
            if not lines:
                logging.error("❌ No data lines found")
                return None

            header_line = lines[0].strip()
            logging.debug(f"Header: {header_line}")

            headers = [h.strip() for h in header_line.split(',')]
            logging.debug(f"Headers: {headers}")
            try:
                cardno_index = headers.index('CardNo')
                pin_index = headers.index('Pin')
                logging.debug(f"CardNo index: {cardno_index}, Pin index: {pin_index}")
            except ValueError as e:
                logging.error(f"❌ Required fields not found in header: {e}")
                return None
            for i, line in enumerate(lines[1:], 1):  # Skip header
                line = line.strip()
                if not line:
                    continue
                fields = line.split(',')
                if len(fields) <= max(cardno_index, pin_index):
                    logging.debug(f"Line {i}: Not enough fields ({len(fields)})")
                    continue
                card_from_data = fields[cardno_index].strip().lstrip('0')
                pin_from_data = fields[pin_index].strip()
                logging.debug(f"Line {i}: CardNo='{card_from_data}', Pin='{pin_from_data}'")
                if card_from_data == card_no_clean and pin_from_data and pin_from_data != '0':
                    logging.info(f"✅ Found PIN '{pin_from_data}' for card '{card_no}'")
                    return pin_from_data
            logging.warning(f"❌ Card '{card_no}' not found in {len(lines) - 1} users")
            return None
        except Exception as e:
            logging.error(f"❌ Exception in get_pin_by_card: {e}")
            return None

    def add_user(self, pin,name, card, start, end):
        # to do set the card to 0 for old user
        pin_old_user = self.get_pin_by_card(card)
        if pin_old_user and pin_old_user != pin:
            data_old_user = (f"Pin={pin_old_user}\tCardNo=0")
            self._set(b"user", data_old_user)
        data = (f"Pin={pin}\tCardNo={card}\tStartTime={start}"
                    f"\tEndTime={end}\tPassword=")
        return self._set(b"user", data)

    def delete_user(self, pin, finger_id=None):
        if finger_id is None:
            return self._del(b"userauthorize", f"Pin={pin}")
        return self._del(b"templatev10",
                         f"Pin={pin}\tFingerID={finger_id}")

    def authorize_user(self, pin):
        data = f"Pin={pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=1\r\nPin={pin}\tAuthorizeTimezoneId=1\tAuthorizeDoorId=2"
        return self._set(b"userauthorize", data)

    def add_fingerprint(self, user_id, fingerprint_template, finger_id):
        """
        Add a fingerprint template to the C3 device for a specific user.
        Includes duplicate fingerprint detection.

        Args:
            user_id: User PIN/ID
            fingerprint_template: Captured fingerprint template (bytes)
            finger_id: Finger ID (0-9)

        Returns:
            dict: {'success': bool, 'message': str, 'error_type': str}
        """
        if not self.handle:
            self.handle = self.connect()
        if not self.handle:
            logging.error("❌ Failed to connect to device")
            return {'success': False, 'message': 'Device connection failed', 'error_type': 'connection'}

        buffer_size = 10 * 1024 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)
        table_name = "user".encode('utf-8')
        filter_condition = f"Pin={user_id}\t".encode('utf-8')
        options = "".encode('utf-8')
        field_names = "*".encode('utf-8')

        # Convert fingerprint template to bytes
        if hasattr(fingerprint_template, 'tobytes'):
            fingerprint_bytes = fingerprint_template.tobytes()
        elif hasattr(fingerprint_template, 'data'):
            fingerprint_bytes = fingerprint_template.data
        else:
            fingerprint_bytes = bytes(fingerprint_template)

        fingerprint_template_base64 = base64.b64encode(fingerprint_bytes).decode('utf-8')

        # Check if user exists
        result = plcommpro.GetDeviceData(self.handle, buffer, buffer_size,
                                         table_name, field_names, filter_condition, options)

        # Create user if doesn't exist
        if result <= 0:
            logging.info(f"User {user_id} doesn't exist. Creating user...")
            user_data = f"Pin={user_id}\tStartTime=20001021\tEndTime=20251231\t".encode('utf-8')
            user_result = plcommpro.SetDeviceData(self.handle, b"user", user_data, None)

            if user_result != 0:
                logging.error(f"❌ Failed to create user {user_id}. Error code: {user_result}")
                return {'success': False, 'message': f'User creation failed: {user_result}',
                        'error_type': 'user_creation'}

            logging.info(f"✅ User {user_id} created successfully")

        # Check if this specific fingerprint slot is already occupied
        template_check = plcommpro.GetDeviceData(
            self.handle, buffer, buffer_size, b"templatev10", field_names,
            f"Pin={user_id}\tFingerID={finger_id}\t".encode('utf-8'), options
        )

        if template_check > 0:
            self.delete_fingerprint(user_id, finger_id)

        # Check for duplicate fingerprints across ALL users
        duplicate_check = self.check_duplicate_fingerprint(fingerprint_template_base64)
        if duplicate_check['is_duplicate']:
            logging.error(
                f"❌ DUPLICATE FINGERPRINT DETECTED! Already enrolled for user: {duplicate_check['existing_user']}")
            return {
                'success': False,
                'message': f"Fingerprint already enrolled for user {duplicate_check['existing_user']}",
                'error_type': 'duplicate_fingerprint',
                'existing_user': duplicate_check['existing_user'],
                'existing_finger_id': duplicate_check['finger_id']
            }

        # Add fingerprint template
        template_data = (
            f"Size={len(fingerprint_template_base64)}\t"
            f"UID={user_id}\t"
            f"Pin={user_id}\t"
            f"FingerID={finger_id}\t"
            f"Valid=1\t"
            f"Template={fingerprint_template_base64}\t"
            f"Resverd=\t"
            f"EndTag="
        ).encode("utf-8")

        result_template = plcommpro.SetDeviceData(self.handle, b"templatev10", template_data, None)

        if result_template == 0:
            logging.info(f"✅ Fingerprint added successfully for Pin={user_id}, FingerID={finger_id}")
            return True
        else:
            logging.error(f"❌ Failed to add fingerprint. Error code: {result_template}")
            error_msg = self.interpret_error_code(result_template)
            return False

    def check_duplicate_fingerprint(self, new_template_base64):
        """
        Check if the fingerprint template already exists for any user.
        Compatible CSV + KV formats returned by PullSDK.
        """
        buffer_size = 10 * 1024 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)

        result = plcommpro.GetDeviceData(
            self.handle, buffer, buffer_size,
            b"templatev10", b"*", b"", b""
        )

        if result <= 0:
            logging.info("No existing templates found or error reading templates")
            return {'is_duplicate': False, 'existing_user': None, 'finger_id': None}

        templates_data = buffer.value.decode('utf-8', errors='ignore')
        rows = self._parse_templatev10_rows(templates_data)  # ✅ support CSV + KV

        for r in rows:
            existing_template = (r.get("Template") or "").strip()
            existing_pin = (r.get("Pin") or "").strip()
            existing_fid = r.get("FingerID", "unknown")

            if existing_template and existing_template == new_template_base64:
                return {
                    'is_duplicate': True,
                    'existing_user': existing_pin,
                    'finger_id': existing_fid
                }

        return {'is_duplicate': False, 'existing_user': None, 'finger_id': None}

    def interpret_error_code(self, error_code):
        """
        Interpret ZKTeco error codes.

        Args:
            error_code: Error code from device

        Returns:
            str: Human-readable error message
        """
        error_messages = {
            -1: "General error",
            -2: "Device not connected",
            -3: "Invalid parameter",
            -4: "Operation timeout",
            -5: "Data buffer too small",
            -10: "Duplicate fingerprint detected",
            -20: "Template quality too low",
            -100: "Device memory full",
        }

        return error_messages.get(error_code, f"Unknown error code: {error_code}")

    def delete_fingerprint(self, user_id, finger_id):
        """
        Delete a specific fingerprint from a user.

        Args:
            user_id: User PIN/ID (string or int)
            finger_id: Finger ID to delete (0-9)

        Returns:
            bool: True if successful, False otherwise
        """
        # Ensure we have a connection
        if not self.handle:
            logging.warning("No handle, attempting to connect...")
            self.handle = self.connect()
            if not self.handle:
                logging.error("Failed to connect to device for delete operation")
                return False

        # Convert to string to ensure consistency
        user_id = str(user_id)
        finger_id = int(finger_id)

        logging.info(f"Attempting to delete fingerprint: PIN={user_id}, FingerID={finger_id}")

        try:
            buffer_size = 10 * 1024 * 1024
            buffer = ctypes.create_string_buffer(buffer_size)
            table_name = "templatev10".encode('utf-8')
            filter_condition = f"Pin={user_id}\t".encode('utf-8')
            options = "".encode('utf-8')
            field_names = "*".encode('utf-8')

            # Use correct case-sensitive field names: PIN (uppercase)
            check_result = plcommpro.GetDeviceData(
                self.handle, buffer, buffer_size, table_name, field_names,
                filter_condition, options
            )

            if check_result < 0:
                logging.warning(f"Fingerprint not found: PIN={user_id}, FingerID={finger_id}")
                return True  # Already deleted or doesn't exist

            logging.info(f"Fingerprint exists (found {check_result} records), proceeding with deletion")

            # Delete the fingerprint - format: "Field=Value" pairs separated by \t
            # Based on doc: data = "Pin=2" for conditions of deleting the data
            delete_filter = f"Pin={user_id}\tFingerID={finger_id}".encode('utf-8')
            logging.info(f"Delete filter: Pin={user_id}\\tFingerID={finger_id}")

            result = plcommpro.DeleteDeviceData(
                self.handle,
                b"templatev10",
                delete_filter,
                None  # Options parameter - default is null as per documentation
            )

            if result == 0:
                logging.info(f"Fingerprint deleted successfully: PIN={user_id}, FingerID={finger_id}")

                # Verify deletion
                buffer_size = 10 * 1024 * 1024
                buffer = ctypes.create_string_buffer(buffer_size)
                table_name = "templatev10".encode('utf-8')
                filter_condition = f"Pin={user_id}\t".encode('utf-8')
                options = "".encode('utf-8')
                field_names = "*".encode('utf-8')

                # Use correct case-sensitive field names: PIN (uppercase)
                verify_result = plcommpro.GetDeviceData(
                    self.handle, buffer, buffer_size, table_name, field_names,
                    filter_condition, options
                )

                if verify_result < 0:
                    logging.info("Verified: Fingerprint no longer exists")
                else:
                    logging.warning(f"Warning: Fingerprint still exists after delete (found {verify_result} records)")

                return True
            else:
                logging.error(f"Failed to delete fingerprint. Error code: {result}")
                logging.error(f"  Device: {self.machine.addresseip}")
                logging.error(f"  Handle: {self.handle}")
                logging.error(f"  Filter: PIN={user_id}\tFingerID={finger_id}")

                # Common PLComm error codes
                error_messages = {
                    -1: "General error",
                    -2: "Device not connected",
                    -3: "Invalid parameter",
                    -4: "Operation timeout",
                    -5: "Buffer too small",
                    -10: "Permission denied",
                    -100: "Table not found",
                    -101: "Invalid table name or field name (case-sensitive)",
                    -102: "Invalid field value",
                    1: "Record not found",
                    2: "Multiple records found"
                }

                if result in error_messages:
                    logging.error(f"  Error meaning: {error_messages[result]}")

                return False

        except OSError as exc:
            logging.error(f"OSError during delete_fingerprint: {exc}")
            # Connection might be broken, clear the handle
            self.disconnect()
            return False
        except Exception as exc:
            logging.exception(f"Unexpected error in delete_fingerprint: {exc}")
            return False

    def add_and_authorize_user(self, user_id, fingerprint_template, start_time, end_time, finger_id=1):
        """
        Add and authorize user - simplified version from your working code
        """
        if not self.handle:
            logging.error("Cannot add user. Device is not connected.")  # Fixed: was logging.log
            return

        # Check if user exists
        user_data_check = f"Pin={user_id}".encode('utf-8')
        result_user_exists = plcommpro.SetDeviceData(self.handle, b"user", user_data_check, None)

        if result_user_exists != 0:  # User doesn't exist
            # Add user record with simple format
            logging.info(f"User {user_id} does not exist. Adding new user.")  # Fixed: was logging.log
            user_data = f"Pin={user_id}\tStartTime={start_time}\tEndTime={end_time}\t".encode('utf-8')
            result_add_user = plcommpro.SetDeviceData(self.handle, b"user", user_data, None)

            if result_add_user == 0:
                logging.info(f"User {user_id} added successfully.")  # Fixed: was logging.log
            else:
                logging.error(f"Failed to add user. Error code: {result_add_user}")  # Fixed: was logging.log
        else:
            logging.info(f"User {user_id} already exists.")  # Fixed: was logging.log

    def unauthorize_user(self, pin):
        return self._del(b"userauthorize", f"Pin={pin}")

    def download_user_photo(self, pin: str, path: str) -> bool:
        return self;

    def _parse_templatev10_rows(self, raw: str) -> list[dict]:
        """
        PullSDK peut renvoyer:
          - CSV: header + lignes 'Size,UID,Pin,FingerID,Valid,Template,...'
          - KV:  'Pin=...\tFingerID=...\tTemplate=...'
        On supporte les 2.
        """
        if not raw:
            return []

        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = [l.strip() for l in lines if l.strip()]
        if not lines:
            return []

        out = []

        # --- Détection CSV (présence de virgules + header)
        first = lines[0]
        looks_csv = ("," in first) and ("=" not in first)

        if looks_csv:
            # header probable
            header = [h.strip() for h in first.split(",")]
            start_idx = 1

            # si le "header" est en fait déjà une ligne data (ex: commence par un nombre)
            # on fallback sur un header connu
            if header and header[0].isdigit():
                header = ["Size", "UID", "Pin", "FingerID", "Valid", "Template", "Resverd", "EndTag"]
                start_idx = 0

            for line in lines[start_idx:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 6:
                    continue
                row = dict(zip(header, parts))
                out.append(row)
            return out

        # --- Sinon KV
        for line in lines:
            fields = {}
            for part in line.split("\t"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    fields[k.strip()] = v.strip()
            if fields:
                out.append(fields)

        return out

    def get_fingerprints(self, pin: str) -> list[dict]:
        """
        C3 / inBio via PullSDK: lit templatev10 (sans filtre, plus stable),
        puis filtre côté Python.
        """
        if not self.handle:
            self.connect()
        if not self.handle:
            raise RuntimeError("❌ PullSDK: impossible de se connecter (handle nul).")

        buffer_size = 10 * 1024 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)

        # ⚠️ on évite le filter 'Pin=...' qui peut retourner -101 selon firmware
        ret = plcommpro.GetDeviceData(
            self.handle,
            buffer,
            buffer_size,
            b"templatev10",
            b"*",
            b"",
            b""
        )

        if ret <= 0:
            return []

        raw = buffer.value.decode("utf-8", errors="ignore")
        rows = self._parse_templatev10_rows(raw)

        pin_str = str(pin).strip()
        res = []

        for r in rows:
            # CSV: keys 'Pin','FingerID','Template' | KV idem
            if str(r.get("Pin", "")).strip() != pin_str:
                continue

            tpl = (r.get("Template") or "").strip()
            if not tpl:
                continue

            try:
                fid = int(str(r.get("FingerID")).strip())
            except Exception:
                continue

            size_val = r.get("Size")
            try:
                size_int = int(size_val) if size_val is not None and str(size_val).strip().isdigit() else None
            except Exception:
                size_int = None

            res.append({
                "fingerId": fid,
                "template": tpl,
                "size": size_int,
                "valid": r.get("Valid"),
                "source": "PULLSDK"
            })

        res.sort(key=lambda x: x["fingerId"])
        return res

    def open_door(self, door_no: int, duration: int = 5, event_type: int = 0) -> bool:
        """
        Ouvre une porte sur un contrôleur C3/inBio via ControlDevice.

        :param door_no: numéro de la porte (1..4)
        :param duration: durée en secondes (1..60)
        :param event_type: type d'événement dans les logs (0 = auto par status)
        """
        if not self.handle:
            # normal pour les C3 : le handle est en général fixé par DeviceContext,
            # mais si jamais ce n'est pas le cas, on tente une connexion directe.
            self.connect()

        if not self.handle:
            logging.error("❌ open_door: aucun handle disponible pour %s", self.machine.addresseip)
            return False

        # bornage de la durée
        duration = max(1, min(int(duration), 60))

        operation_id = 1      # 1 = output
        param1 = int(door_no) # index de sortie / porte
        param2 = 1            # 1 = door output
        param3 = duration     # durée en secondes
        param4 = int(event_type)  # 0 = auto selon status
        options = b""         # pas d'options

        try:
            ret = plcommpro.ControlDevice(
                self.handle,
                operation_id,
                param1,
                param2,
                param3,
                param4,
                options
            )
            if ret < 0:
                logging.error(
                    "❌ ControlDevice KO (%s door=%s, duration=%s) ret=%s",
                    self.machine.addresseip, door_no, duration, ret
                )
                return False

            logging.info(
                "✅ Porte %s ouverte pendant %s s sur %s",
                door_no, duration, self.machine.addresseip
            )
            return True

        except OSError as exc:
            logging.error(
                "❌ Exception ControlDevice pour %s : %s",
                self.machine.addresseip, exc
            )
            self.disconnect()
            return False