import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler


# Cadence de rappel d'une saturation RAM qui dure, en secondes. Sans ce garde-fou,
# un poste durablement à 90 % produisait un avertissement toutes les 30 s.
_SATURATION_REMINDER = 300
_saturation_state = {"active": False, "last_warn": 0.0}


def log_memory_usage(logger=None, message=""):
    """
    Journalise l'occupation mémoire, en ne parlant que lorsqu'il y a un problème.

    La sonde périodique tournant toutes les 30 s, une ligne INFO systématique
    représentait près de 3 000 lignes par jour pour une information qui n'intéresse
    que dans deux cas : une saturation, ou une mesure demandée explicitement à
    l'occasion d'un incident (log_memory_usage(message="task_#57_fail")).

    Règle appliquée :
      - saturation qui apparaît, ou qui dure depuis _SATURATION_REMINDER → WARNING
      - appel explicite (message autre que "periodic")                  → INFO
      - relevé périodique nominal                                       → DEBUG
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    try:
        import psutil
        proc = psutil.Process()
        rss_mb = proc.memory_info().rss / 1024 / 1024
        percent = psutil.virtual_memory().percent
        saturated = percent >= 90

        now = time.time()
        if saturated:
            first = not _saturation_state["active"]
            due = now - _saturation_state["last_warn"] >= _SATURATION_REMINDER
            _saturation_state["active"] = True
            if first or due:
                _saturation_state["last_warn"] = now
                logger.warning("⚠️ SATURATION RAM — MEM %s | RSS=%.0f MB | "
                               "System RAM=%s%%", message, rss_mb, percent)
                return
        elif _saturation_state["active"]:
            _saturation_state["active"] = False
            logger.info("✅ RAM revenue sous le seuil — System RAM=%s%%", percent)
            return

        level = logger.debug if message == "periodic" else logger.info
        level("MEM %s | RSS=%.0f MB | System RAM=%s%%", message, rss_mb, percent)

    except ImportError:
        logger.debug("psutil not available, skipping memory log")
    except Exception:
        logger.debug("Could not read memory info")


def get_logs_dir():
    app_data = os.environ.get('APPDATA')
    if not app_data:
        app_data = os.path.expanduser('~\\AppData\\Roaming')
    logs_dir = os.path.join(app_data, 'desktop-app', 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def setup_logging(level=None, log_to_file=True, log_to_console=True):
    # Niveau pilotable sans rebuild, via LOG_LEVEL dans le .env (DEBUG, INFO…).
    #
    # Le chemin nominal est volontairement muet — renouvellements de session C3,
    # relevés mémoire — pour que le journal ne contienne que des anomalies. Quand
    # il faut diagnostiquer chez un client, LOG_LEVEL=DEBUG rend toute cette trace
    # sans avoir à livrer une version spéciale.
    if level is None:
        wanted = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        level = getattr(logging, wanted, None)
        if not isinstance(level, int):
            level = logging.INFO

    # La console Windows est en cp1252 : sans ça, chaque message contenant un
    # emoji lève un UnicodeEncodeError que logging recrache en traceback sur
    # stderr — capturé par Electron comme une erreur du pont. Le handler fichier
    # est déjà en UTF-8, seule la sortie console était concernée.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    root_logger.handlers.clear()

    formatter = logging.Formatter(
        '%(asctime)s — %(levelname)s — %(name)s — %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    if log_to_file:
        logs_dir = get_logs_dir()
        log_path = os.path.join(logs_dir, 'app.log')
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    return root_logger


def get_logger(name):
    return logging.getLogger(name)


def start_memory_monitor(interval=60, stop_event=None, on_saturation=None):
    """
    Start a daemon thread that logs memory usage periodically.
    If on_saturation callback is provided, calls it when system RAM >= 90%.
    """
    def _monitor():
        while (stop_event is None) or (not stop_event.is_set()):
            log_memory_usage(message="periodic")
            if on_saturation:
                try:
                    import psutil
                    if psutil.virtual_memory().percent >= 90:
                        on_saturation()
                except Exception:
                    pass
            time.sleep(interval)
    t = threading.Thread(target=_monitor, daemon=True, name="MemoryMonitor")
    t.start()
    return t
