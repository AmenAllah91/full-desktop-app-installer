import socket
import threading

throttle_event = threading.Event()


def tcp_reachable(ip: str, port, timeout: float = 1.5) -> bool:
    """
    Sonde rapide de joignabilité, à utiliser AVANT d'entrer dans le SDK.

    Une machine éteinte est une situation normale chez les clients. Sans ce
    filtre, Connect_Net part sur ses 3 tentatives avec le timeout interne du
    SDK — des dizaines de secondes — tout en retenant zk_sdk_lock, ce qui gèle
    le pont entier pour une seule machine absente.
    """
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, TypeError):
        return False

# Le SDK zkemkeeper n'est pas sûr en multithread. Deux threads qui entrent
# simultanément dans la DLL — typiquement Connect_Net depuis un thread de
# requête Flask pendant que le thread RT pompe ses messages COM sur le même
# terminal — provoquent une violation d'accès qui tue le process d'un coup,
# sans exception ni traceback Python. Observé en production le 2026-08-14 :
# 8 arrêts brutaux sur 272 appels à connect(), la dernière ligne journalisée
# étant systématiquement « ComKey appliqué », juste avant Connect_Net.
#
# Ce verrou sérialise TOUS les accès au SDK, quel que soit l'objet COM ou le
# terminal visé. C'est un palliatif : la vraie réponse reste un thread
# propriétaire par machine, seul autorisé à parler à son terminal.
zk_sdk_lock = threading.RLock()
