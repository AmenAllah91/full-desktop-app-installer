import os
import sys
import zipfile
import subprocess
import shutil
import platform
import ctypes
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

logs_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~\\AppData\\Roaming')), 'desktop-app', 'logs')
os.makedirs(logs_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(
            os.path.join(logs_dir, 'setup.log'),
            maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'
        ),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("SetupDlls")


def is_admin():
    if os.name != 'nt':
        return os.geteuid() == 0
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    if os.name != 'nt':
        logger.error("This script is designed to run on Windows for SDK installation.")
        sys.exit(1)

    params = " ".join([f'"{arg}"' for arg in sys.argv if arg != "--elevated"]) + " --elevated"
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)


def get_system_architecture():
    return "x64" if platform.machine().endswith('64') else "x86"


def find_batch_file(directory, pattern):
    for file in directory.iterdir():
        if file.is_file() and file.suffix.lower() == '.bat':
            file_lower = file.name.lower()
            pattern_lower = pattern.lower()

            if file_lower == pattern_lower:
                return file
            if file_lower.replace(" ", "") == pattern_lower.replace(" ", ""):
                return file
            if pattern_lower.replace("_", " ") in file_lower or pattern_lower.replace(" ", "_") in file_lower:
                return file
    return None


def run_batch_file(batch_path, cwd):
    logger.info("Running batch file: %s", batch_path)
    try:
        with open(batch_path, 'r') as f:
            content = f.read()

        modified_content = content.replace('pause', 'REM PAUSE_DISABLED')

        temp_batch_path = batch_path + '.nopause.bat'
        with open(temp_batch_path, 'w') as f:
            f.write(modified_content)

        result = subprocess.run(temp_batch_path, shell=True, cwd=cwd)

        os.remove(temp_batch_path)

        if result.returncode == 0:
            logger.info("Successfully executed: %s", os.path.basename(batch_path))
            return True
        else:
            logger.error("Failed to execute %s, return code: %s", os.path.basename(batch_path), result.returncode)
            return False
    except Exception as e:
        logger.error("Error executing %s: %s", os.path.basename(batch_path), e)
        return False


def extract_and_run_batch():
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent.parent

    logger.info("Base directory: %s", base_dir)

    zip_path = base_dir / "resources" / "SDK.zip"

    if not zip_path.exists():
        logger.error("SDK.zip file not found at expected location: %s", zip_path)
        return

    logger.info("Found ZIP file at: %s", zip_path)

    extract_dir = base_dir / "resources" / "extracted_dlls"

    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("Extracting DLL files...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        arch = get_system_architecture()
        logger.info("Detected system architecture: %s", arch)

        sdk_dir = extract_dir / "SDK" / arch
        if not sdk_dir.exists():
            logger.error("Could not find SDK/%s directory in extracted files", arch)
            for item in extract_dir.rglob("*"):
                logger.info("  - %s", item.relative_to(extract_dir))
            return

        delete_pattern = f"Delete_SDK {arch}.bat"
        delete_batch = find_batch_file(sdk_dir, delete_pattern)
        if delete_batch:
            logger.info("Found Delete SDK batch file: %s", delete_batch.name)
            run_batch_file(str(delete_batch), str(sdk_dir))
        else:
            logger.warning("%s not found", delete_pattern)

        register_pattern = f"Register_SDK {arch}.bat"
        register_batch = find_batch_file(sdk_dir, register_pattern)
        if not register_batch:
            logger.error("%s not found in %s", register_pattern, sdk_dir)
            for item in sdk_dir.iterdir():
                logger.info("  - %s", item.name)
            return

        logger.info("Found Register SDK batch file: %s", register_batch.name)
        success = run_batch_file(str(register_batch), str(sdk_dir))

        if success:
            logger.info("SDK installation completed successfully!")
        else:
            logger.error("SDK installation encountered issues.")

    except Exception as e:
        logger.error("Error occurred: %s", e)


def setupDlls():
    logger.info("SDK Installer")
    logger.info("=============")

    if "--elevated" not in sys.argv:
        if not is_admin():
            logger.info("Administrator privileges required for SDK installation.")
            logger.info("Requesting administrator privileges...")
            run_as_admin()
            sys.exit(0)

    logger.info("Running with administrator privileges")
    extract_and_run_batch()


setupDlls()
