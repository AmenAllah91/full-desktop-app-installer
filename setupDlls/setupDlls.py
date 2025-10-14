import os
import sys
import zipfile
import subprocess
import shutil
import platform
import ctypes
from pathlib import Path


def is_admin():
    """Check if the script is running with admin/root privileges"""
    if os.name != 'nt':
        return os.geteuid() == 0
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    """Re-run the script with admin privileges"""
    if os.name != 'nt':
        print("This script is designed to run on Windows for SDK installation.")
        sys.exit(1)

    params = " ".join([f'"{arg}"' for arg in sys.argv if arg != "--elevated"]) + " --elevated"
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)


def get_system_architecture():
    """Determine if system is x86 or x64"""
    return "x64" if platform.machine().endswith('64') else "x86"


def find_batch_file(directory, pattern):
    """Find batch files matching a pattern with fuzzy matching for spacing/case"""
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
    """Run a batch file after removing pause commands"""
    print(f"Running batch file: {batch_path}")
    try:
        # Create a temporary batch file without pause commands
        with open(batch_path, 'r') as f:
            content = f.read()

        # Replace pause commands with REM (comment them out)
        modified_content = content.replace('pause', 'REM PAUSE_DISABLED')

        temp_batch_path = batch_path + '.nopause.bat'
        with open(temp_batch_path, 'w') as f:
            f.write(modified_content)

        # Run the modified batch file
        result = subprocess.run(temp_batch_path, shell=True, cwd=cwd)

        # Clean up
        os.remove(temp_batch_path)

        if result.returncode == 0:
            print(f"Successfully executed: {os.path.basename(batch_path)}")
            return True
        else:
            print(f"Failed to execute {os.path.basename(batch_path)}, return code: {result.returncode}")
            return False
    except Exception as e:
        print(f"Error executing {os.path.basename(batch_path)}: {e}")
        return False


def extract_and_run_batch():
    """Extract the DLLs zip file and run the batch files"""

    # Determine base directory
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent.parent

    print(f"Base directory: {base_dir}")

    # Path to SDK.zip in resources/dlls/
    zip_path = base_dir / "resources" / "SDK.zip"

    if not zip_path.exists():
        print(f"Error: SDK.zip file not found at expected location: {zip_path}")
        return

    print(f"Found ZIP file at: {zip_path}")

    # Create extraction directory
    extract_dir = base_dir  / "resources" / "extracted_dlls"

    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        print("Extracting DLL files...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        arch = get_system_architecture()
        print(f"Detected system architecture: {arch}")

        sdk_dir = extract_dir / "SDK" / arch
        if not sdk_dir.exists():
            print(f"Error: Could not find SDK/{arch} directory in extracted files")
            for item in extract_dir.rglob("*"):
                print(f"  - {item.relative_to(extract_dir)}")
            return

        # Run Delete_SDK_xxx.bat
        delete_pattern = f"Delete_SDK {arch}.bat"
        delete_batch = find_batch_file(sdk_dir, delete_pattern)
        if delete_batch:
            print(f"Found Delete SDK batch file: {delete_batch.name}")
            run_batch_file(str(delete_batch), str(sdk_dir))
        else:
            print(f"Warning: {delete_pattern} not found")

        # Run Register_SDK xxx.bat
        register_pattern = f"Register_SDK {arch}.bat"
        register_batch = find_batch_file(sdk_dir, register_pattern)
        if not register_batch:
            print(f"Error: {register_pattern} not found in {sdk_dir}")
            for item in sdk_dir.iterdir():
                print(f"  - {item.name}")
            return

        print(f"Found Register SDK batch file: {register_batch.name}")
        success = run_batch_file(str(register_batch), str(sdk_dir))

        if success:
            print("SDK installation completed successfully!")
        else:
            print("SDK installation encountered issues.")

    except Exception as e:
        print(f"Error occurred: {e}")


def setupDlls():
    print("SDK Installer")
    print("=============")

    if "--elevated" not in sys.argv:
        if not is_admin():
            print("Administrator privileges required for SDK installation.")
            print("Requesting administrator privileges...")
            run_as_admin()
            sys.exit(0)

    print("Running with administrator privileges")
    extract_and_run_batch()

setupDlls()