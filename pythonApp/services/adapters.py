# services/adapters.py
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


class PlcommAdapter(DeviceAdapter):  # ✅ hérite !
    def __init__(self, machine):
        super().__init__(machine)

    # ------------- connexion
    def connect(self, max_attempts: int = 3):
        if self.handle:
            return self.handle
        ip, port = self.machine.addresseip, self.machine.port
        params = (f"protocol=TCP,ipaddress={ip},port={port},"
                  "timeout=4000,passwd=").encode()
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

    def unauthorize_user(self, pin):
        return self._del(b"userauthorize", f"Pin={pin}")

    def download_user_photo(self, pin: str, path: str) -> bool:
        return self;