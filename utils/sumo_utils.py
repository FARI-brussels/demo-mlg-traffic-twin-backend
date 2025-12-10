from pathlib import Path
from typing import List
from utils.run_utils import run
import os
def generate_rerouters(network: Path, closed_edges: List[str], begin: int, end: int, out_xml: Path) -> None:
    """Generate a rerouter file for a given network and closed edges."""
    if not closed_edges:
        return
    tool = Path(os.environ["SUMO_HOME"]) / "tools/generateRerouters.py"
    joined = ",".join(closed_edges)
    cmd = [
        "python", str(tool),
        "-n", str(network),
        "-x", joined,
        "-b", str(begin),
        "-e", str(end),
        "-o", str(out_xml),
    ]
    run(cmd)


def generate_random_trips(network: Path, begin: int, end: int, insertion_rate: int, routes_xml: Path, trips_xml: Path) -> None:
    tool = Path(os.environ["SUMO_HOME"]) / "tools/randomTrips.py"
    cmd = [
        "python", str(tool),
        "-n", str(network),
        f"--insertion-rate={insertion_rate}",
        "-b", str(begin),
        "-e", str(end),
        "-r", str(routes_xml),
        "-o", str(trips_xml),
        "--fringe-factor", "2",
    ]
    run(cmd)



