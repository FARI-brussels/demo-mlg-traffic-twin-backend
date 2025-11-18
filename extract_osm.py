"""
OSM Network Extraction Utilities

This module provides functionality to extract OpenStreetMap data
for a given bounding box and convert it to SUMO network format.
"""

import os
import json
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional


def parse_bbox_from_payload(payload: Dict[str, Any]) -> List[float]:
    """Parse a bounding box from JSON payload.

    Accepts either:
      - corners: List[{lat, lon}] (at least 4 points, we take min/max)
      - bbox: [min_lon, min_lat, max_lon, max_lat]
      - bbox: {min_lon, min_lat, max_lon, max_lat}
    Returns [min_lon, min_lat, max_lon, max_lat]
    """
    def to_float(v: Any, name: str) -> float:
        try:
            return float(v)
        except Exception:
            raise ValueError(f"Invalid numeric value for {name}")

    if isinstance(payload.get("corners"), list) and len(payload["corners"]) >= 4:
        lats: List[float] = []
        lons: List[float] = []
        for idx, pt in enumerate(payload["corners"]):
            if not isinstance(pt, dict):
                raise ValueError(f"Corner at index {idx} must be an object with 'lat' and 'lon'")
            lat = to_float(pt.get("lat"), f"corners[{idx}].lat")
            lon = to_float(pt.get("lon"), f"corners[{idx}].lon")
            lats.append(lat)
            lons.append(lon)
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        bbox = [min_lon, min_lat, max_lon, max_lat]
    else:
        bbox_val = payload.get("bbox")
        if isinstance(bbox_val, list) and len(bbox_val) == 4:
            bbox = [to_float(bbox_val[0], "min_lon"), to_float(bbox_val[1], "min_lat"), to_float(bbox_val[2], "max_lon"), to_float(bbox_val[3], "max_lat")]
        elif isinstance(bbox_val, dict):
            bbox = [
                to_float(bbox_val.get("min_lon"), "min_lon"),
                to_float(bbox_val.get("min_lat"), "min_lat"),
                to_float(bbox_val.get("max_lon"), "max_lon"),
                to_float(bbox_val.get("max_lat"), "max_lat"),
            ]
        else:
            raise ValueError("Missing or invalid 'corners' or 'bbox' in request body")

    min_lon, min_lat, max_lon, max_lat = bbox
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0 and -90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ValueError("Coordinates out of bounds")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("Invalid bbox: expected min_lon < max_lon and min_lat < max_lat")
    return [min_lon, min_lat, max_lon, max_lat]


def ensure_osmium() -> None:
    """Ensure the 'osmium' CLI is available in PATH."""
    from backend import shutil_which
    if not shutil_which("osmium"):
        raise FileNotFoundError("Required binary 'osmium' not found in PATH")


def extract_osm_from_pbf(pbf_path: Path, bbox: List[float], out_path: Path) -> None:
    """Use osmium to extract an OSM XML subset from a PBF file for the given bbox."""
    ensure_osmium()
    min_lon, min_lat, max_lon, max_lat = bbox
    cmd = [
        "osmium", "extract",
        "-b", f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "-o", str(out_path),
        "-O",
        "-f", "osm",
        str(pbf_path),
    ]
    subprocess.run(cmd, check=True)


from typing import List
from pathlib import Path
import os
import subprocess

def fetch_osm_with_osmget(bbox: List[float], work_dir: Path) -> Path:
    """Download OSM XML for bbox using SUMO's osmGet.py into work_dir and return the file path."""
    if not os.environ.get("SUMO_HOME"):
        os.environ["SUMO_HOME"] = "/usr/share/sumo"
    
    # Define the expected output path
    output_filename = "osm_bbox.osm.xml"
    output_path = work_dir / output_filename

    min_lon, min_lat, max_lon, max_lat = bbox
    tool = Path(os.environ["SUMO_HOME"]) / "tools/osmGet.py"
    
    # Run downloader in the working directory
    subprocess.run(
        ["python", str(tool), f"--bbox={min_lon},{min_lat},{max_lon},{max_lat}"], 
        cwd=str(work_dir), 
        check=True
    )
    # Check for the expected output file and return it
    if output_path.exists():
        return output_path
    
    # If the file doesn't exist, raise an error
    raise FileNotFoundError(f"osmGet.py did not produce the expected file '{output_filename}' in the working directory: {work_dir}")


def generate_geojson_from_net(net_xml: Path, out_geojson: Path) -> None:
    """Convert SUMO .net.xml to GeoJSON using net2geojson script."""
    tool = "net2geojson.py"
    cmd = ["python", str(tool), "-n", str(net_xml), "-o", str(out_geojson), "-x", "-l", "-e", "--junctions"]
    subprocess.run(cmd, check=True)

