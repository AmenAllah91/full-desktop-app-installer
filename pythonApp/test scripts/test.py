#!/usr/bin/env python3
"""
Scanner ZKTeco Standalone utilisant le SDK officiel zkemkeeper.dll via COM.

PRÉREQUIS (Windows uniquement) :
  1. Télécharger le Standalone SDK officiel de ZKTeco
  2. Enregistrer la DLL :
     - Copier zkemkeeper.dll (+ dépendances) dans C:\\Windows\\SysWOW64\\
     - Exécuter en admin : regsvr32 C:\\Windows\\SysWOW64\\zkemkeeper.dll
     (ou utiliser Register_SDK_x64.bat fourni avec le SDK)
  3. Installer pywin32 :
     pip install pywin32

USAGE :
  python find_zkteco_zkem.py
  python find_zkteco_zkem.py 192.168.1 1 255
"""

import sys
import time

try:
    import win32com.client
    import pythoncom
except ImportError:
    print("[ERROR] pywin32 non installé.")
    print("[INFO]  Installation : pip install pywin32")
    sys.exit(1)

ZKTECO_PORT = 4370


def test_zkteco_connection(ip_address, port=ZKTECO_PORT):
    """
    Tente de se connecter à un appareil ZKTeco via le SDK officiel zkemkeeper.
    Retourne (True, device_info) si succès, (False, None) sinon.
    """
    zk = None
    try:
        # Instancier l'objet COM du SDK officiel ZKTeco
        zk = win32com.client.Dispatch('zkemkeeper.ZKEM.1')

        # Connect_Net est la méthode standard du SDK pour TCP/IP
        connected = zk.Connect_Net(ip_address, port)

        if not connected:
            return False, None

        # Connexion réussie -> récupérer quelques infos pour valider
        device_info = {}
        machine_number = 1

        try:
            serial_ok, serial = zk.GetSerialNumber(machine_number, "")
            if serial_ok:
                device_info['serial'] = serial
        except Exception:
            pass

        try:
            name_ok, device_name = zk.GetProductCode(machine_number, "")
            if name_ok:
                device_info['product'] = device_name
        except Exception:
            pass

        try:
            fw_ok, firmware = zk.GetFirmwareVersion(machine_number, "")
            if fw_ok:
                device_info['firmware'] = firmware
        except Exception:
            pass

        return True, device_info

    except pythoncom.com_error as e:
        if zk is None:
            print(f"\n[ERROR] Impossible d'instancier zkemkeeper.ZKEM.1")
            print(f"        La DLL est-elle bien enregistrée ?")
            print(f"        Détail : {e}")
            sys.exit(1)
        return False, None
    except Exception:
        return False, None
    finally:
        if zk is not None:
            try:
                zk.Disconnect()
            except Exception:
                pass


def find_zkteco_device(network_base="192.168.1", start_ip=1, end_ip=255):
    """
    Scanne la plage d'IPs et s'arrête au premier appareil ZKTeco trouvé.
    """
    print(f"[*] === Scanner ZKTeco (SDK officiel zkemkeeper) ===")
    print(f"[*] Reseau : {network_base}.{start_ip} -> {network_base}.{end_ip}")
    print(f"[*] Port   : {ZKTECO_PORT}\n")

    start_time = time.time()

    for i in range(start_ip, end_ip + 1):
        ip = f"{network_base}.{i}"
        sys.stdout.write(f"[+] Test {ip:18s} ... ")
        sys.stdout.flush()

        success, info = test_zkteco_connection(ip)

        if success:
            elapsed = time.time() - start_time
            print("OK CONNECTE !")
            print(f"\n{'=' * 60}")
            print(f"Machine ZKTeco trouvee !")
            print(f"  IP       : {ip}")
            print(f"  Port     : {ZKTECO_PORT}")
            if info.get('product'):
                print(f"  Modele   : {info['product']}")
            if info.get('serial'):
                print(f"  Serial   : {info['serial']}")
            if info.get('firmware'):
                print(f"  Firmware : {info['firmware']}")
            print(f"  Duree    : {elapsed:.2f}s")
            print(f"{'=' * 60}")
            return ip
        else:
            print("KO")

    elapsed = time.time() - start_time
    print(f"\n[!] Aucune machine ZKTeco trouvee ({elapsed:.2f}s)")
    return None


if __name__ == "__main__":
    network_base = sys.argv[1] if len(sys.argv) > 1 else "192.168.1"
    start_ip = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end_ip = int(sys.argv[3]) if len(sys.argv) > 3 else 255

    # Initialiser COM pour le thread courant
    pythoncom.CoInitialize()
    try:
        device_ip = find_zkteco_device(network_base, start_ip, end_ip)
    finally:
        pythoncom.CoUninitialize()

    if device_ip:
        print(f"\n>>> IP DE LA MACHINE : {device_ip}")
        sys.exit(0)
    else:
        sys.exit(1)