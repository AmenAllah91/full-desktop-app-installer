import ctypes
from ctypes import c_char_p, c_int, c_long, create_string_buffer

class PullSDKController:
    def __init__(self, dll_path="plcommpro.dll"):
        # Charge la DLL PullSDK
        self.dll = ctypes.WinDLL(dll_path)
        self.handle = None

        # Définition des signatures de fonctions utilisées
        self.dll.Connect.argtypes = [c_char_p]
        self.dll.Connect.restype = ctypes.c_void_p

        self.dll.Disconnect.argtypes = [ctypes.c_void_p]
        self.dll.Disconnect.restype = None

        self.dll.ControlDevice.argtypes = [
            ctypes.c_void_p,  # handle
            c_long,           # OperationID
            c_long,           # Param1
            c_long,           # Param2
            c_long,           # Param3
            c_long,           # Param4
            c_char_p          # Options
        ]
        self.dll.ControlDevice.restype = c_int

        # Pour récupérer le code d’erreur si besoin
        self.dll.PullLastError.restype = c_int

    def connect(self, ip, port=4370, timeout=4000, password=""):
        params = (
            f"protocol=TCP,ipaddress={ip},port={port},"
            f"timeout={timeout},passwd={password}"
        )
        buf = params.encode("ascii")
        self.handle = self.dll.Connect(buf)

        if not self.handle:
            err = self.dll.PullLastError()
            raise RuntimeError(f"Connexion échouée à {ip}, code d’erreur PullSDK = {err}")

    def open_door(self, door_no=1, open_seconds=5):
        """
        Ouvre la porte door_no pendant open_seconds (1–60s).
        Pour un tourniquet tripode :
          - door_no = 1 → 'entrée'
          - door_no = 2 → 'sortie'
        …selon ton câblage.
        """
        if not self.handle:
            raise RuntimeError("Non connecté au contrôleur")

        if open_seconds <= 0:
            open_seconds = 1
        if open_seconds > 60:
            open_seconds = 60

        operation_id = 1   # Output operation
        param1 = door_no   # Door number
        param2 = 1         # 1 = door output
        param3 = open_seconds  # durée en secondes
        param4 = 0
        options = b""

        ret = self.dll.ControlDevice(
            self.handle,
            operation_id,
            param1,
            param2,
            param3,
            param4,
            options
        )

        if ret < 0:
            raise RuntimeError(f"ControlDevice a échoué, code = {ret}")

    def disconnect(self):
        if self.handle:
            self.dll.Disconnect(self.handle)
            self.handle = None


# Exemple d’utilisation :
if __name__ == "__main__":
    ip = "192.168.1.205"   # IP de ton InBio
    ctrl = PullSDKController()

    ctrl.connect(ip)
    # Entrée = porte 1, 5 secondes
    ctrl.open_door(door_no=1, open_seconds=5)
    # Sortie = porte 2, 5 secondes (si câblée ainsi)
    # ctrl.open_door(door_no=2, open_seconds=5)
    ctrl.disconnect()
