import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler


def log_memory_usage(logger=None, message=""):
    """Log current process memory usage and detect RAM saturation."""
    if logger is None:
        logger = logging.getLogger(__name__)
    try:
        import psutil
        proc = psutil.Process()
        rss = proc.memory_info().rss
        rss_mb = rss / 1024 / 1024
        sys_mem = psutil.virtual_memory()
        percent = sys_mem.percent
        saturated = percent >= 90
        warning = "⚠️ SATURATION RAM" if saturated else ""
        logger.info(
            "MEM %s | RSS=%.0f MB | System RAM=%s%% %s",
            message, rss_mb, percent, warning
        )
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


def setup_logging(level=logging.INFO, log_to_file=True, log_to_console=True):
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


def start_memory_monitor(interval=60, stop_event=None):
    """Start a daemon thread that logs memory usage periodically."""
    def _monitor():
        while (stop_event is None) or (not stop_event.is_set()):
            log_memory_usage(message="periodic")
            time.sleep(interval)
    t = threading.Thread(target=_monitor, daemon=True, name="MemoryMonitor")
    t.start()
    return t
