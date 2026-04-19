
import os, sys
from pathlib import Path

def _prepare_dll_search_path():
    base_dir = getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent))
    # Windows 10+
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(base_dir)
        except Exception:
            pass
    # Fallback PATH
    os.environ["PATH"] = base_dir + os.pathsep + os.environ.get("PATH", "")

_prepare_dll_search_path()
