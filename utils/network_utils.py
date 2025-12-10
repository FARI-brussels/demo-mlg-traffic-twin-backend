from pathlib import Path
from utils.run_utils import run
from typing import Dict

def generate_network(osm_file: Path, net_out: Path) -> None:
    """Generate a SUMO network (.net.xml) from an OSM file using netconvert."""
    cmd = [
        "netconvert",
        "--osm", str(osm_file),
        "-o", str(net_out),
        "--geometry.remove", "--ramps.guess", "--junctions.join",
        "--tls.guess-signals", "--tls.discard-simple", "--tls.join", "--tls.default-type", "actuated",
        #"--remove-edges.by-vclass", "rail_slow,rail_fast,bicycle,pedestrian",
        "--keep-edges.by-vclass", "bus,private",
        "--remove-edges.isolated", "--output.street-names", "--output.original-names",
        "--osm.extra-attributes", "all",

    ]
    run(cmd)



def extract_subnetwork(
    full_network: Path,
    output_network: Path,
    fcd_filter_shape: Dict[str, float],
    buffer_km: float = 0
) -> None:
    """Extract a subnetwork around the area of interest using netconvert.
    
    Args:
        full_network: Path to the full network file
        output_network: Path where to save the extracted subnetwork
        fcd_filter_shape: Dict with centerLon, centerLat, radiusKm
        buffer_km: Additional buffer around the area of interest (in km)
    """
    import math
    
    center_lon = fcd_filter_shape['centerLon']
    center_lat = fcd_filter_shape['centerLat']
    radius_km = fcd_filter_shape['radiusKm'] + buffer_km
    
    # Calculate bounding box from center and radius
    # Approximate conversion: 1 degree latitude ≈ 111 km
    # 1 degree longitude ≈ 111 * cos(latitude) km
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(center_lat)))
    
    west = center_lon - lon_delta
    east = center_lon + lon_delta
    south = center_lat - lat_delta
    north = center_lat + lat_delta
    
    # Use netconvert to extract subnetwork with bounding box
    cmd = [
        "netconvert",
        "-s", str(full_network),
        "-o", str(output_network),
        # Keep edges within bounding box
        "--keep-edges.in-geo-boundary", f"{west},{south},{east},{north}",
        # Preserve edge IDs for demand matching
        "--output.original-names",
        # Keep connected network
        "--remove-edges.isolated",
    ]
    
    run(cmd)