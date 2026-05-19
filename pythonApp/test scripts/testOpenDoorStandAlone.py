import sys
import pythoncom
import win32com.client


class StandaloneDevice:
    def __init__(self):
        pythoncom.CoInitialize()
        self.zk = win32com.client.Dispatch("zkemkeeper.ZKEM.1")
        self.connected = False

    def connect(self, ip, port=4370, comm_key=123456):
        self.zk.SetCommPassword(comm_key)
        ok = self.zk.Connect_Net(ip, port)
        if not ok:
            raise RuntimeError(f"Impossible de se connecter à {ip}:{port}")
        self.connected = True
        print(f"✅ Connecté à {ip}:{port}")

    def open_door(self, delay_seconds=5, machine_number=1):
        if not self.connected:
            raise RuntimeError("Non connecté à l'appareil")
        delay = int(delay_seconds * 10)
        ok = self.zk.ACUnlock(machine_number, delay)
        if not ok:
            raise RuntimeError("ACUnlock a échoué")
        print(f"🔓 Porte ouverte pendant {delay_seconds}s")

    def disconnect(self):
        if self.connected:
            self.zk.Disconnect()
            self.connected = False
            print("🔌 Déconnecté")

    def __del__(self):
        self.disconnect()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    # Usage : python open_door.py 192.168.1.229 5 123456
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.228"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    comm_key = int(sys.argv[3]) if len(sys.argv) > 3 else 123456

    dev = StandaloneDevice()
    try:
        dev.connect(ip, comm_key=comm_key)
        dev.open_door(delay_seconds=duration)
    finally:
        dev.disconnect()