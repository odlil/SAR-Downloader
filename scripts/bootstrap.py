"""Create a project-local environment; never install into the user's base Python."""
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (3, 11) <= sys.version_info[:2] <= (3, 12) or sys.maxsize <= 2 ** 32:
        print("[ERROR] Use Python 3.11 (recommended) or 3.12, 64-bit.")
        return 1
    try:
        import tkinter
    except ImportError:
        print("[ERROR] Reinstall Python with Tcl/Tk support, or use the Windows portable ZIP.")
        return 1
    target = ROOT / ".venv"
    executable = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if target.exists() and not (target / "pyvenv.cfg").is_file():
        print("[ERROR] .venv exists but is not a virtual environment; it was left unchanged.")
        return 1
    try:
        if not executable.is_file():
            print("[SAR] Creating .venv ...", flush=True)
            venv.EnvBuilder(with_pip=True).create(target)
        subprocess.run([str(executable), "-B", "-c",
                        "import sys; assert (3,11)<=sys.version_info[:2]<=(3,12) and sys.maxsize>2**32"], check=True)
        probe = subprocess.run([str(executable), "-B", "-m", "pip", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode:
            subprocess.run([str(executable), "-B", "-m", "ensurepip", "--upgrade", "--default-pip"], check=True)
        requirements = ROOT / "requirements-lock.txt"
        if not requirements.is_file():
            requirements = ROOT / "requirements.txt"
        print("[SAR] Installing tested dependencies into .venv (network required) ...", flush=True)
        subprocess.run([str(executable), "-B", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                        "--only-binary=:all:", "-r", str(requirements)], check=True)
        subprocess.run([str(executable), "-B", str(ROOT / "check_environment.py"), "--native"], check=True)
        print("[SAR] Configuring the separate DEM engine ...", flush=True)
        subprocess.run([sys.executable, '-B', str(ROOT/'scripts'/'setup_dem.py'),
                        '--candidate', sys.executable], check=True)
    except (OSError, subprocess.CalledProcessError):
        print("[ERROR] Setup did not finish. Check the error above and network access to PyPI; rerun to continue.")
        return 1
    print("[SAR] Setup completed. Double-click the launcher to open SAR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
