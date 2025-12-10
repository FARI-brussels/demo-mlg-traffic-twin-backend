from pathlib import Path
from typing import List
import os
import subprocess
from typing import Optional

def run_python_script(script_path: Path, args: List[str]) -> None:
    cmd = ["python", str(script_path), *args]
    run(cmd)

def ensure_env() -> None:
    """Ensure environment variables and binaries we rely on are available."""
    # Default SUMO_HOME if not already set by environment
    if not os.environ.get("SUMO_HOME"):
        os.environ["SUMO_HOME"] = "/usr/share/sumo"

    # Validate required binaries and tools
    required_bins = ["netconvert", "sumo"]
    for bin_name in required_bins:
        if not shutil_which(bin_name):
            raise FileNotFoundError(f"Required binary '{bin_name}' not found in PATH")

    # Validate SUMO tool scripts
    sumo_home = Path(os.environ["SUMO_HOME"]).resolve()
    tools = {
        "generateRerouters": sumo_home / "tools/generateRerouters.py",
        "randomTrips": sumo_home / "tools/randomTrips.py",
        "osmGet": sumo_home / "tools/osmGet.py",
    }
    for tool_name, tool_path in tools.items():
        if not tool_path.exists():
            raise FileNotFoundError(f"SUMO tool '{tool_name}' not found at '{tool_path}'")


def shutil_which(cmd: str) -> Optional[str]:
    # Minimal which implementation to avoid importing shutil for a single call
    paths = os.environ.get("PATH", "").split(os.pathsep)
    for p in paths:
        candidate = Path(p) / cmd
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None



def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )