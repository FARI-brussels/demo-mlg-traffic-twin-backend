import os
import json
import shlex
import tempfile
import subprocess
import threading
import logging
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from typing import List, Optional, Dict, Any
from enum import Enum
import traceback

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Json

# Import from our custom modules
from extract_osm import (
    parse_bbox_from_payload,
    fetch_osm_with_osmget,
    generate_geojson_from_net
)
from get_osiris_closed_edges import fetch_closed_edges_from_brussels_api
from calculate_metrics import (
    calculate_metrics,
    calculate_scenario_comparison
)
# Note: generate_filter_polygon module is available if needed for FCD filtering
from utils.run_utils import run_python_script, ensure_env, shutil_which, run
from utils.network_utils import generate_network, extract_subnetwork
from utils.sumo_utils import generate_rerouters, generate_random_trips

GEOJSON_PATH = "net.geojson"

# Simulation mode enumeration
class SimulationMode(str, Enum):
    HYBRID = "hybrid"
    MICROSCOPIC = "microscopic"
    MESOSCOPIC = "mesoscopic"

# Default simulation mode
SIMULATION_MODE = SimulationMode.HYBRID

app = FastAPI(title="Traffic Scenario Generator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global lock for simulation to queue requests and prevent concurrent SUMO issues
SIMULATION_LOCK = threading.Lock()



def build_output_paths(output_dir: Path, mode: SimulationMode = SimulationMode.MICROSCOPIC) -> Dict[str, Path]:
    """Build output paths based on simulation mode.
    
    Args:
        output_dir: Base output directory
        mode: Simulation mode (microscopic, mesoscopic, or hybrid)
    
    Returns:
        Dictionary of output paths appropriate for the simulation mode
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Common paths for all modes
    paths = {
        "output_dir": output_dir,
        "routes_xml": output_dir / "routes.xml",
        "trips_xml": output_dir / "trips.xml",
        "rerouter_file": output_dir / "rerouters.xml",
        "fcd_trips_json_with": output_dir / "fcd_trips_with.json",
        "fcd_trips_json_wout": output_dir / "fcd_trips_without.json",
        "congestion_map_with": output_dir / "congestion_with.geojson",
        "congestion_map_wout": output_dir / "congestion_without.geojson",
    }
    
    if mode == SimulationMode.MICROSCOPIC:
        # Standard microscopic simulation paths
        paths.update({
            "sumo_network": output_dir / "osm.net.xml",
            "fcd_with": output_dir / "fcd_with.out.xml",
            "fcd_wout": output_dir / "fcd_without.out.xml",
            "tripinfo_with": output_dir / "tripinfo_with.xml",
            "tripinfo_wout": output_dir / "tripinfo_without.xml",
            "edgedata_with": output_dir / "edgedata_with.xml",
            "edgedata_wout": output_dir / "edgedata_without.xml",
        })
    
    elif mode == SimulationMode.MESOSCOPIC:
        # Mesoscopic simulation paths
        paths.update({
            "sumo_network": output_dir / "osm.net.xml",
            "tripinfo_with": output_dir / "tripinfo_with.xml",
            "tripinfo_wout": output_dir / "tripinfo_without.xml",
            "edgedata_with": output_dir / "edgedata_with.xml",
            "edgedata_wout": output_dir / "edgedata_without.xml",
            "vehroute_with": output_dir / "vehroute_with.xml",
            "vehroute_wout": output_dir / "vehroute_without.xml",
        })
    
    elif mode == SimulationMode.HYBRID:
        # Hybrid simulation paths with mesoscopic and microscopic subdirectories
        meso_dir = output_dir / "mesoscopic"
        micro_dir = output_dir / "microscopic"
        meso_dir.mkdir(parents=True, exist_ok=True)
        micro_dir.mkdir(parents=True, exist_ok=True)
        
        paths.update({
            # Full network
            "full_network": output_dir / "full_network.net.xml",
            "sumo_network": output_dir / "full_network.net.xml",  # Alias for compatibility
            
            # Mesoscopic simulation outputs (with closures)
            "meso_tripinfo_with": meso_dir / "tripinfo_with.xml",
            "meso_edgedata_with": meso_dir / "edgedata_with.xml",
            "meso_vehroute_with": meso_dir / "vehroute_with.xml",
            # Mesoscopic simulation outputs (without closures)
            "meso_tripinfo_wout": meso_dir / "tripinfo_without.xml",
            "meso_edgedata_wout": meso_dir / "edgedata_without.xml",
            "meso_vehroute_wout": meso_dir / "vehroute_without.xml",
            
            # Subnetwork
            "sub_network": output_dir / "subnetwork.net.xml",
            
            # Extracted demand for microscopic simulation
            "micro_routes_with": micro_dir / "routes_with.xml",
            "micro_routes_wout": micro_dir / "routes_without.xml",
            
            # Rerouter for microscopic simulation (subnetwork-specific)
            "micro_rerouter_file": micro_dir / "rerouters.xml",
            
            # Microscopic simulation outputs (with closures)
            "micro_fcd_with": micro_dir / "fcd_with.out.xml",
            "micro_tripinfo_with": micro_dir / "tripinfo_with.xml",
            "micro_edgedata_with": micro_dir / "edgedata_with.xml",
            # Microscopic simulation outputs (without closures)
            "micro_fcd_wout": micro_dir / "fcd_without.out.xml",
            "micro_tripinfo_wout": micro_dir / "tripinfo_without.xml",
            "micro_edgedata_wout": micro_dir / "edgedata_without.xml",
        })
    print(paths)
    return paths







def compile_metrics_report(
    tripinfo_with: Path,
    tripinfo_wout: Path,
    edgedata_with: Path,
    edgedata_wout: Path,
    routes_xml: Path,
    simulation_mode: str,
    begin_time: int,
    end_time: int,
    insertion_rate: int,
    closed_edges_list: List[str],
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compile metrics report from simulation outputs."""
    print("---- calculating metrics without closed edges ----")
    metrics_without = calculate_metrics(
        tripinfo_path=tripinfo_wout,
        edgedata_path=edgedata_wout,
        routes_path=routes_xml,
        baseline_tripinfo_path=None,
    )
    
    print("---- calculating metrics with closed edges ----")
    metrics_with = calculate_metrics(
        tripinfo_path=tripinfo_with,
        edgedata_path=edgedata_with,
        routes_path=routes_xml,
        baseline_tripinfo_path=tripinfo_wout,
    )
    
    comparison = calculate_scenario_comparison(metrics_with, metrics_without)
    
    metadata = {
        "simulation_mode": simulation_mode,
        "begin_time": begin_time,
        "end_time": end_time,
        "simulation_duration_s": end_time - begin_time,
        "insertion_rate": insertion_rate,
        "closed_edges": [str(e) for e in closed_edges_list],
        "num_closed_edges": len(closed_edges_list),
    }
    
    if extra_metadata:
        metadata.update(extra_metadata)
    
    return {
        "scenario_without_closures": metrics_without,
        "scenario_with_closures": metrics_with,
        "comparison": comparison,
        "metadata": metadata,
    }


def create_simulation_output_zip(
    fcd_trips_json_with: Path,
    fcd_trips_json_wout: Path,
    metrics_report: Dict[str, Any],
    congestion_map_with: Path,
    congestion_map_wout: Path,
    output_dir: Path,
) -> BytesIO:
    """Create ZIP file with simulation outputs."""
    # Write metrics to JSON file
    metrics_json_path = output_dir / "metrics.json"
    with open(metrics_json_path, "w", encoding="utf-8") as mf:
        json.dump(metrics_report, mf, indent=2)
    
    # Create in-memory ZIP
    memory_file = BytesIO()
    with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
        zf.write(fcd_trips_json_with, arcname="fcd_trips_with.json")
        zf.write(fcd_trips_json_wout, arcname="fcd_trips_without.json")
        zf.write(metrics_json_path, arcname="metrics.json")
        zf.write(congestion_map_with, arcname="congestion_with.geojson")
        zf.write(congestion_map_wout, arcname="congestion_without.geojson")
    memory_file.seek(0)
    
    return memory_file


def generate_congestion_maps(base_dir: Path, network_xml: Path, edgedata_with: Path, edgedata_wout: Path, output_with: Path, output_wout: Path) -> None:
    """Generate congestion map GeoJSONs from edgedata."""
    script = base_dir / "generate_congestion_map.py"
    if not script.exists():
        raise FileNotFoundError(f"Required script not found: {script}")
    
    # Generate congestion map for scenario with closures
    run_python_script(script, [str(network_xml), str(edgedata_with), str(output_with)])
    
    # Generate congestion map for scenario without closures
    run_python_script(script, [str(network_xml), str(edgedata_wout), str(output_wout)])


def convert_fcd_to_outputs(base_dir: Path, fcd_with: Path, fcd_wout: Path, trips_json_with: Path, trips_json_wout: Path, insertion_rate: int, closed_edges: List[str], network_xml: Path, begin_time: int, end_time: int) -> None:
    # Local conversion scripts live next to this backend
    fcd_to_trips = base_dir / "fcd_to_trips.py"

    if not fcd_to_trips.exists():
        raise FileNotFoundError(f"Required script not found: {fcd_to_trips}")

    trips_args_common = ["--network-xml", str(network_xml)]
    print("---- converting fcd to json with closed edges ----")
    run_python_script(
        fcd_to_trips,
        [
            str(fcd_with),
            str(trips_json_with),
            *trips_args_common,
            "--insertion-rate", str(insertion_rate),
            "--closed-edges", ",".join(closed_edges),
            "--begin-time", str(begin_time),
            "--end-time", str(end_time),
        ],
    )
    print("---- converting fcd to json without closed edges ----")
    run_python_script(
        fcd_to_trips,
        [
            str(fcd_wout),
            str(trips_json_wout),
            *trips_args_common,
            "--insertion-rate", str(insertion_rate),
            "--closed-edges", "",
        ],
    )




def extract_demand_for_subnetwork(
    vehroute_xml: Path,
    full_network: Path,
    sub_network: Path,
    output_routes: Path,
    fcd_filter_shape: Dict[str, float],
    begin: int,
    end: int
) -> int:
    """Extract demand from mesoscopic simulation for vehicles passing through area of interest.
    
    Filters vehicles whose routes pass through the subnetwork area and creates
    new route files for the microscopic simulation.
    
    IMPORTANT: Only keeps CONNECTED segments of routes within the subnetwork.
    This ensures SUMO can actually simulate the vehicles without route errors.
    
    Args:
        vehroute_xml: Path to vehroute output from mesoscopic simulation
        full_network: Path to the full network
        sub_network: Path to the extracted subnetwork
        output_routes: Path where to save extracted routes
        fcd_filter_shape: Dict with centerLon, centerLat, radiusKm
        begin: Simulation begin time
        end: Simulation end time
    
    Returns:
        Number of vehicles extracted
    """
    import sys
    import xml.etree.ElementTree as ET
    
    sumo_tools = Path(os.environ["SUMO_HOME"]) / "tools"
    if str(sumo_tools) not in sys.path:
        sys.path.append(str(sumo_tools))
    
    import sumolib
    
    # Load the subnetwork to get its edge IDs and build connectivity map
    sub_net = sumolib.net.readNet(str(sub_network))
    sub_edge_ids = set(edge.getID() for edge in sub_net.getEdges())
    
    # Build a map of edge connections for the subnetwork
    # For each edge, get the IDs of edges that can be reached from it
    edge_successors = {}
    for edge in sub_net.getEdges():
        edge_id = edge.getID()
        # Get outgoing edges from this edge's to-node
        successors = set()
        to_node = edge.getToNode()
        for outgoing in to_node.getOutgoing():
            successors.add(outgoing.getID())
        edge_successors[edge_id] = successors
    
    print(f"Subnetwork has {len(sub_edge_ids)} edges")
    
    # Parse the vehroute output and filter vehicles
    tree = ET.parse(str(vehroute_xml))
    root = tree.getroot()
    
    # Create new routes XML
    routes_root = ET.Element("routes")
    routes_root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    routes_root.set("xsi:noNamespaceSchemaLocation", "http://sumo.dlr.de/xsd/routes_file.xsd")
    
    vehicle_count = 0
    total_vehicles = 0
    vehicles_with_route_in_area = 0
    vehicles_filtered_by_time = 0
    vehicles_filtered_by_edge_count = 0
    total_segments_found = 0
    
    # Collect all valid vehicles first, then sort by departure time
    extracted_vehicles = []
    
    for vehicle in root.findall('vehicle'):
        total_vehicles += 1
        route = vehicle.find('route')
        if route is None:
            continue
        
        edges_str = route.get('edges', '')
        if not edges_str:
            continue
        
        route_edges = edges_str.split()
        
        # Check if any edge in the route is in the subnetwork
        route_in_area = any(edge in sub_edge_ids for edge in route_edges)
        
        if not route_in_area:
            continue
        
        vehicles_with_route_in_area += 1
        
        # Get vehicle departure time
        depart = float(vehicle.get('depart', 0))
        
        # Filter by time window
        if depart < begin or depart > end:
            vehicles_filtered_by_time += 1
            continue
        
        # Find all CONNECTED segments of the route within the subnetwork
        # A segment is a continuous sequence of edges where each edge connects to the next
        connected_segments = []
        current_segment = []
        
        for edge in route_edges:
            if edge in sub_edge_ids:
                if not current_segment:
                    # Start a new segment
                    current_segment = [edge]
                else:
                    # Check if this edge is connected to the previous one
                    prev_edge = current_segment[-1]
                    if edge in edge_successors.get(prev_edge, set()):
                        # Connected - extend current segment
                        current_segment.append(edge)
                    else:
                        # Not connected - save current segment and start new one
                        if len(current_segment) >= 2:
                            connected_segments.append(current_segment)
                        current_segment = [edge]
            else:
                # Edge not in subnetwork - end current segment if any
                if current_segment and len(current_segment) >= 2:
                    connected_segments.append(current_segment)
                current_segment = []
        
        # Don't forget the last segment
        if current_segment and len(current_segment) >= 2:
            connected_segments.append(current_segment)
        
        if not connected_segments:
            vehicles_filtered_by_edge_count += 1
            continue
        
        # Use the LONGEST connected segment for this vehicle
        best_segment = max(connected_segments, key=len)
        total_segments_found += len(connected_segments)
        
        # Store vehicle data for later sorting
        extracted_vehicles.append({
            "id": f"micro_{vehicle.get('id')}",
            "depart": depart,
            "type": vehicle.get('type'),
            "edges": " ".join(best_segment),
        })
        
        vehicle_count += 1
    
    # CRITICAL: Sort vehicles by departure time - SUMO requires this!
    extracted_vehicles.sort(key=lambda v: v["depart"])
    print(f"Sorted {len(extracted_vehicles)} vehicles by departure time")
    
    # Now write sorted vehicles to the routes XML
    for veh in extracted_vehicles:
        new_vehicle = ET.SubElement(routes_root, "vehicle")
        new_vehicle.set("id", veh["id"])
        new_vehicle.set("depart", str(veh["depart"]))
        
        if veh["type"]:
            new_vehicle.set("type", veh["type"])
        
        new_route = ET.SubElement(new_vehicle, "route")
        new_route.set("edges", veh["edges"])
    
    # Write output
    tree = ET.ElementTree(routes_root)
    ET.indent(tree, space="    ")
    tree.write(str(output_routes), encoding="utf-8", xml_declaration=True)
    
    # Print detailed debug info
    print(f"\n--- Demand Extraction Debug Info ---")
    print(f"Total vehicles in vehroute: {total_vehicles}")
    print(f"Vehicles with route passing through area: {vehicles_with_route_in_area}")
    print(f"Vehicles filtered by time window ({begin}-{end}): {vehicles_filtered_by_time}")
    print(f"Vehicles with no valid connected segment (< 2 edges): {vehicles_filtered_by_edge_count}")
    print(f"Total connected segments found: {total_segments_found}")
    print(f"Extracted {vehicle_count} vehicles for microscopic simulation")
    print(f"------------------------------------\n")
    
    return vehicle_count



def run_sumo_microscopic(
    network: Path,
    routes_xml: Path,
    begin: int,
    end: int,
    fcd_out: Path,
    tripinfo_out: Path,
    edgedata_out: Path,
    rerouter_xml: Optional[Path] = None,
) -> None:
    """Run SUMO in microscopic mode with full FCD output.
    
    Always calculates FCD for all vehicles with no filtering.
    """
    cmd = [
        "sumo",
        "-n", str(network),
        "-r", str(routes_xml),
        "-b", str(begin),
        "-e", str(end),
        # FCD output for all vehicles
        "--fcd-output", str(fcd_out),
        "--fcd-output.geo", "true",
        "--device.fcd.probability", "1.0",
        # Trip info and edge data
        "--tripinfo-output", str(tripinfo_out),
        "--tripinfo-output.write-unfinished", "true",
        "--edgedata-output", str(edgedata_out),
        # Emissions
        "--device.emissions.probability", "1.0",
        # Error handling
        "--ignore-route-errors",
    ]
    
    if rerouter_xml and rerouter_xml.exists():
        cmd.extend(["-a", str(rerouter_xml)])
    
    run(cmd)


# =============================================================================
# HYBRID SIMULATION MODE FUNCTIONS
# =============================================================================

def run_sumo_mesoscopic(
    network: Path,
    routes_xml: Path,
    begin: int,
    end: int,
    tripinfo_out: Path,
    edgedata_out: Path,
    vehroute_out: Path,
    rerouter_xml: Optional[Path] = None,
) -> None:
    """Run SUMO in mesoscopic mode for faster city-wide simulation.
    
    Mesoscopic mode uses a queue-based model that is much faster than microscopic
    while still providing reasonable flow estimates, route choices and OD patterns.
    """
    cmd = [
        "sumo",
        "-n", str(network),
        "-r", str(routes_xml),
        "-b", str(begin),
        "-e", str(end),
        # Enable mesoscopic mode
        "--mesosim", "true",
        # Output files
        "--tripinfo-output", str(tripinfo_out),
        "--tripinfo-output.write-unfinished", "true",
        "--edgedata-output", str(edgedata_out),
        "--vehroute-output", str(vehroute_out),
        "--vehroute-output.route-length", "true",
        "--vehroute-output.exit-times", "true",
        "--vehroute-output.write-unfinished", "true",
        # Mesoscopic parameters for reasonable traffic flow
        "--meso-junction-control", "true",
        "--meso-tls-penalty", "1.0",
        # Error handling
        "--ignore-route-errors",
    ]
    
    if rerouter_xml and rerouter_xml.exists():
        cmd.extend(["-a", str(rerouter_xml)])
    
    run(cmd)


def run_hybrid_simulation(
    base_dir: Path,
    paths: Dict[str, Path],
    net_path: Path,
    routes_xml: Path,
    begin_time: int,
    end_time: int,
    insertion_rate: int,
    closed_edges_list: List[str],
    fcd_filter_shape_dict: Dict[str, float],
) -> Dict[str, Any]:
    """Run the full hybrid simulation workflow.
    
    1. Run mesoscopic simulation for full network
    2. Extract subnetwork around area of interest
    3. Extract demand for vehicles passing through area
    4. Run microscopic simulation on subnetwork
    5. Combine outputs
    
    Returns metrics report dict.
    """
    print("=" * 60)
    print("HYBRID SIMULATION MODE")
    print("=" * 60)
    
    # ==========================================================================
    # PHASE 1: Mesoscopic simulation on full network
    # ==========================================================================
    print("\n" + "=" * 60)
    print("PHASE 1: MESOSCOPIC SIMULATION (Full Network)")
    print("=" * 60)
    
    # Generate rerouters for closed edges (if any)
    if closed_edges_list:
        print("---- generating rerouters for mesoscopic ----")
        generate_rerouters(
            network=net_path,
            closed_edges=[str(e) for e in closed_edges_list],
            begin=begin_time,
            end=end_time,
            out_xml=paths["rerouter_file"],
        )
    
    print("---- running mesoscopic simulation WITH closures ----")
    run_sumo_mesoscopic(
        network=net_path,
        routes_xml=routes_xml,
        begin=begin_time,
        end=end_time,
        tripinfo_out=paths["meso_tripinfo_with"],
        edgedata_out=paths["meso_edgedata_with"],
        vehroute_out=paths["meso_vehroute_with"],
        rerouter_xml=paths["rerouter_file"] if closed_edges_list else None,
    )
    
    print("---- running mesoscopic simulation WITHOUT closures ----")
    run_sumo_mesoscopic(
        network=net_path,
        routes_xml=routes_xml,
        begin=begin_time,
        end=end_time,
        tripinfo_out=paths["meso_tripinfo_wout"],
        edgedata_out=paths["meso_edgedata_wout"],
        vehroute_out=paths["meso_vehroute_wout"],
        rerouter_xml=None,
    )
    
    # ==========================================================================
    # PHASE 2: Extract subnetwork around area of interest
    # ==========================================================================
    print("\n" + "=" * 60)
    print("PHASE 2: SUBNETWORK EXTRACTION")
    print("=" * 60)
    
    print(f"---- extracting subnetwork around ({fcd_filter_shape_dict['centerLon']:.4f}, {fcd_filter_shape_dict['centerLat']:.4f}) with radius {fcd_filter_shape_dict['radiusKm']}km ----")
    extract_subnetwork(
        full_network=net_path,
        output_network=paths["sub_network"],
        fcd_filter_shape=fcd_filter_shape_dict,
        buffer_km=0  # Add 500m buffer around the area
    )
    
    # ==========================================================================
    # PHASE 3: Extract demand for microscopic simulation
    # ==========================================================================
    print("\n" + "=" * 60)
    print("PHASE 3: DEMAND EXTRACTION")
    print("=" * 60)
    
    print("---- extracting demand WITH closures ----")
    vehicles_with = extract_demand_for_subnetwork(
        vehroute_xml=paths["meso_vehroute_with"],
        full_network=net_path,
        sub_network=paths["sub_network"],
        output_routes=paths["micro_routes_with"],
        fcd_filter_shape=fcd_filter_shape_dict,
        begin=begin_time,
        end=end_time,
    )
    
    print("---- extracting demand WITHOUT closures ----")
    vehicles_wout = extract_demand_for_subnetwork(
        vehroute_xml=paths["meso_vehroute_wout"],
        full_network=net_path,
        sub_network=paths["sub_network"],
        output_routes=paths["micro_routes_wout"],
        fcd_filter_shape=fcd_filter_shape_dict,
        begin=begin_time,
        end=end_time,
    )
    
    # ==========================================================================
    # PHASE 4: Microscopic simulation on subnetwork
    # ==========================================================================
    print("\n" + "=" * 60)
    print("PHASE 4: MICROSCOPIC SIMULATION (Subnetwork)")
    print("=" * 60)
    
    micro_output_dir = paths["output_dir"] / "microscopic"
    
    # Filter closed edges to only those that exist in the subnetwork
    import sys
    sumo_tools = Path(os.environ["SUMO_HOME"]) / "tools"
    if str(sumo_tools) not in sys.path:
        sys.path.append(str(sumo_tools))
    import sumolib
    
    sub_net = sumolib.net.readNet(str(paths["sub_network"]))
    sub_edge_ids = set(edge.getID() for edge in sub_net.getEdges())
    
    # Filter closed edges to only those in the subnetwork
    micro_closed_edges = [e for e in closed_edges_list if e in sub_edge_ids]
    print(f"Closed edges in full network: {len(closed_edges_list)}")
    print(f"Closed edges in subnetwork: {len(micro_closed_edges)}")
    if micro_closed_edges:
        print(f"Subnetwork closed edges: {micro_closed_edges}")
    
    # Generate rerouter for subnetwork if there are closed edges in it
    micro_rerouter_path = None
    if micro_closed_edges:
        print("---- generating rerouters for microscopic subnetwork ----")
        generate_rerouters(
            network=paths["sub_network"],
            closed_edges=micro_closed_edges,
            begin=begin_time,
            end=end_time,
            out_xml=paths["micro_rerouter_file"],
        )
        micro_rerouter_path = paths["micro_rerouter_file"]
    
    if vehicles_with > 0:
        print("---- running microscopic simulation WITH closures ----")
        run_sumo_microscopic(
            network=paths["sub_network"],
            routes_xml=paths["micro_routes_with"],
            begin=begin_time,
            end=end_time,
            fcd_out=paths["micro_fcd_with"],
            tripinfo_out=paths["micro_tripinfo_with"],
            edgedata_out=paths["micro_edgedata_with"],
            rerouter_xml=micro_rerouter_path,
        )
    else:
        print("---- WARNING: No vehicles extracted for WITH closures scenario ----")
        # Create empty FCD file
        with open(paths["micro_fcd_with"], 'w') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<fcd-export/>\n')
    
    if vehicles_wout > 0:
        print("---- running microscopic simulation WITHOUT closures ----")
        run_sumo_microscopic(
            network=paths["sub_network"],
            routes_xml=paths["micro_routes_wout"],
            begin=begin_time,
            end=end_time,
            fcd_out=paths["micro_fcd_wout"],
            tripinfo_out=paths["micro_tripinfo_wout"],
            edgedata_out=paths["micro_edgedata_wout"],
            rerouter_xml=None,
        )
    else:
        print("---- WARNING: No vehicles extracted for WITHOUT closures scenario ----")
        with open(paths["micro_fcd_wout"], 'w') as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<fcd-export/>\n')
    
    # ==========================================================================
    # PHASE 5: Process outputs
    # ==========================================================================
    print("\n" + "=" * 60)
    print("PHASE 5: OUTPUT PROCESSING")
    print("=" * 60)
    
    # Convert FCD to JSON (from microscopic simulation)
    print("---- converting microscopic FCD to JSON ----")
    convert_fcd_to_outputs(
        base_dir=base_dir,
        fcd_with=paths["micro_fcd_with"],
        fcd_wout=paths["micro_fcd_wout"],
        trips_json_with=paths["fcd_trips_json_with"],
        trips_json_wout=paths["fcd_trips_json_wout"],
        insertion_rate=insertion_rate,
        closed_edges=[str(e) for e in closed_edges_list],
        network_xml=paths["sub_network"],  # Use subnetwork for coordinate conversion
        begin_time=begin_time,
        end_time=end_time,
    )
    
    # Generate congestion maps from MESOSCOPIC edgedata (full network view)
    print("---- generating congestion maps from mesoscopic data ----")
    generate_congestion_maps(
        base_dir=base_dir,
        network_xml=net_path,  # Use full network for congestion map
        edgedata_with=paths["meso_edgedata_with"],
        edgedata_wout=paths["meso_edgedata_wout"],
        output_with=paths["congestion_map_with"],
        output_wout=paths["congestion_map_wout"],
    )
    
    # Calculate metrics from MESOSCOPIC simulation (full network metrics)
    print("---- calculating metrics from mesoscopic simulation ----")
    metrics_report = compile_metrics_report(
        tripinfo_with=paths["meso_tripinfo_with"],
        tripinfo_wout=paths["meso_tripinfo_wout"],
        edgedata_with=paths["meso_edgedata_with"],
        edgedata_wout=paths["meso_edgedata_wout"],
        routes_xml=routes_xml,
        simulation_mode="hybrid",
        begin_time=begin_time,
        end_time=end_time,
        insertion_rate=insertion_rate,
        closed_edges_list=closed_edges_list,
        extra_metadata={
            "fcd_filter_shape": fcd_filter_shape_dict,
            "hybrid_details": {
                "mesoscopic_network": "full",
                "microscopic_network": "subnetwork",
                "vehicles_extracted_with_closures": vehicles_with,
                "vehicles_extracted_without_closures": vehicles_wout,
            }
        }
    )
    
    return metrics_report

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Error handling setup
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    logger.info(f"HTTPException: {exc.status_code} - {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_type": "HTTPException",
        }
    )

@app.exception_handler(subprocess.CalledProcessError)
async def subprocess_error_handler(request: Request, exc: subprocess.CalledProcessError):
    cmd_list = exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]
    logger.error(f"Subprocess error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "error_type": type(exc).__name__,
            "cmd": " ".join(cmd_list),
            "returncode": exc.returncode,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }
    )

@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    # Let FastAPI handle HTTPException properly
    if isinstance(exc, HTTPException):
        logger.info(f"HTTPException raised: {exc.status_code} - {exc.detail}")
        raise exc
    tb = traceback.format_exc()
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "error_type": type(exc).__name__,
            "error_stack": tb,
        }
    )


@app.post("/simulate")
def simulate(
    network_zip: UploadFile = File(...),
    begin_time: int = Form(...),
    end_time: int = Form(...),
    insertion_rate: int = Form(3000),
    closed_edges: str = Form("[]"),
    routes_zip: Optional[UploadFile] = File(None),
    fcdFilterShape: Optional[str] = Form(None),
    simulation_mode: str = Form(SIMULATION_MODE.value)
) -> Any:
    """
    Run a SUMO simulation pipeline to produce traffic analysis outputs.
    
    Three simulation modes are available:
    
    1. Microscopic Mode (simulation_mode="microscopic"):
       - Runs microscopic simulation on the full network
       - Best for smaller networks or when detailed trajectories are needed everywhere
       - Provides detailed FCD output with vehicle trajectories
    
    2. Mesoscopic Mode (simulation_mode="mesoscopic"):
       - Runs mesoscopic simulation on the full network
       - Faster than microscopic, uses queue-based model
       - Good for large networks, provides reasonable flow estimates
       - No detailed FCD trajectories
    
    3. Hybrid Mode (simulation_mode="hybrid", default):
       - Phase 1: Runs mesoscopic simulation on full network for city-wide flow patterns
       - Phase 2: Extracts subnetwork around area of interest (defined by fcd_filter_shape)
       - Phase 3: Extracts demand for vehicles passing through the area
       - Phase 4: Runs microscopic simulation on subnetwork for detailed FCD trajectories
       - Congestion maps and metrics come from mesoscopic (full network view)
       - FCD trajectories come from microscopic (detailed area view)
       - Requires fcd_filter_shape to be set!
    
    Returns a ZIP with: fcd_trips_with.json, fcd_trips_without.json, 
    congestion_with.geojson, congestion_without.geojson, metrics.json
    """
    # Base dir is the directory containing this file
    base_dir = Path(__file__).resolve().parent
    
    try:
        ensure_env()
        
        # Validate network_zip
        if not network_zip.filename:
             raise HTTPException(status_code=400, detail="No network file selected")

        # Validate and parse simulation mode
        try:
            mode = SimulationMode(simulation_mode)
        except ValueError:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid simulation_mode. Must be one of: {[m.value for m in SimulationMode]}"
            )

        # Parse JSON fields from Form data
        try:
            closed_edges_list = json.loads(closed_edges)
            if not isinstance(closed_edges_list, list):
                raise ValueError("closed_edges must be a list of strings")
        except json.JSONDecodeError:
             raise HTTPException(status_code=400, detail="Invalid JSON in closed_edges")
        
        fcd_filter_shape_dict = None
        if fcdFilterShape:
            try:
                fcd_filter_shape_dict = json.loads(fcdFilterShape)
                if not isinstance(fcd_filter_shape_dict, dict):
                    raise ValueError("fcd_filter_shape must be an object")
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON in fcd_filter_shape")

        # Use global lock to enforce serial execution
        with SIMULATION_LOCK:
            # Use temporary directory for request isolation
            with tempfile.TemporaryDirectory() as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                tmp_dir = Path("./output_test")
                # Setup output paths based on simulation mode
                paths = build_output_paths(tmp_dir, mode)
                
                # Extract the network XML from the uploaded ZIP
                zip_bytes = BytesIO(network_zip.file.read())
                with ZipFile(zip_bytes, 'r') as zf:
                    xml_files = [name for name in zf.namelist() if name.endswith('.net.xml')]
                    if not xml_files:
                        raise HTTPException(status_code=400, detail="No .net.xml file found in the uploaded ZIP")
                    
                    xml_filename = xml_files[0]
                    net_xml_content = zf.read(xml_filename).decode('utf-8')

                # Save the network XML to the output directory
                net_path = paths["output_dir"] / "simulation.net.xml"
                with open(net_path, "w", encoding="utf-8") as f:
                    f.write(net_xml_content)

                
                # 2) Trips and routes - check if routes_zip was provided
                print("---- handling routes ----")
                if routes_zip and routes_zip.filename:
                    print("---- extracting uploaded routes ----")
                    routes_zip_bytes = BytesIO(routes_zip.file.read())
                    
                    with ZipFile(routes_zip_bytes, 'r') as rzf:
                        routes_xml_files = [name for name in rzf.namelist() if name.endswith('.xml')]
                        if not routes_xml_files:
                            raise HTTPException(status_code=400, detail="No .xml file found in the uploaded routes ZIP")
                        
                        routes_xml_filename = routes_xml_files[0]
                        routes_xml_content = rzf.read(routes_xml_filename).decode('utf-8')
                    
                    # Save the routes XML
                    routes_xml = paths["output_dir"] / "uploaded_routes.xml"
                    with open(routes_xml, "w", encoding="utf-8") as f:
                        f.write(routes_xml_content)
                else:
                    print("---- generating random trips and routes ----")
                    generate_random_trips(
                        network=net_path,
                        begin=begin_time,
                        end=end_time,
                        insertion_rate=insertion_rate,
                        routes_xml=paths["routes_xml"],
                        trips_xml=paths["trips_xml"],
                    )
                    routes_xml = paths["routes_xml"]

                # ==============================================================
                # BRANCH: Select simulation mode workflow
                # ==============================================================
                
                if mode == SimulationMode.HYBRID:
                    # HYBRID MODE: Mesoscopic for full network + Microscopic for subnetwork
                    print("\n" + "=" * 60)
                    print("USING HYBRID SIMULATION MODE")
                    print("=" * 60 + "\n")
                    
                    # Validate that fcd_filter_shape is provided for hybrid mode
                    if not fcd_filter_shape_dict:
                        raise HTTPException(
                            status_code=400, 
                            detail="fcd_filter_shape is required for hybrid mode. Please provide centerLon, centerLat, and radiusKm."
                        )
                    
                    # Run the hybrid simulation workflow
                    metrics_report = run_hybrid_simulation(
                        base_dir=base_dir,
                        paths=paths,
                        net_path=net_path,
                        routes_xml=routes_xml,
                        begin_time=begin_time,
                        end_time=end_time,
                        insertion_rate=insertion_rate,
                        closed_edges_list=[str(e) for e in closed_edges_list],
                        fcd_filter_shape_dict=fcd_filter_shape_dict,
                    )
                    
                    memory_file = create_simulation_output_zip(
                        fcd_trips_json_with=paths["fcd_trips_json_with"],
                        fcd_trips_json_wout=paths["fcd_trips_json_wout"],
                        metrics_report=metrics_report,
                        congestion_map_with=paths["congestion_map_with"],
                        congestion_map_wout=paths["congestion_map_wout"],
                        output_dir=paths["output_dir"],
                    )
                    
                    return Response(
                        content=memory_file.getvalue(),
                        media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=simulation_outputs.zip"}
                    )
                
                elif mode == SimulationMode.MICROSCOPIC:
                    # MICROSCOPIC MODE: Microscopic simulation on full network
                    print("\n" + "=" * 60)
                    print("USING MICROSCOPIC SIMULATION MODE")
                    print("=" * 60 + "\n")
                    
                    # 1) Rerouters (only if closed edges provided)
                    print("---- generating rerouters ----")
                    generate_rerouters(
                        network=net_path,
                        closed_edges=[str(e) for e in closed_edges_list],
                        begin=begin_time,
                        end=end_time,
                        out_xml=paths["rerouter_file"],
                    )

                    print("---- running microscopic simulation with closed edges ----")
                    run_sumo_microscopic(
                        network=net_path,
                        routes_xml=routes_xml,
                        begin=begin_time,
                        end=end_time,
                        fcd_out=paths["fcd_with"],
                        tripinfo_out=paths["tripinfo_with"],
                        edgedata_out=paths["edgedata_with"],
                        rerouter_xml=paths["rerouter_file"] if closed_edges_list else None,
                    )
                    print("---- running microscopic simulation without closed edges ----")
                    run_sumo_microscopic(
                        network=net_path,
                        routes_xml=routes_xml,
                        begin=begin_time,
                        end=end_time,
                        fcd_out=paths["fcd_wout"],
                        tripinfo_out=paths["tripinfo_wout"],
                        edgedata_out=paths["edgedata_wout"],
                        rerouter_xml=None,
                    )
                    print("---- converting outputs ----")
                    # 4) Convert outputs
                    convert_fcd_to_outputs(
                        base_dir=base_dir,
                        fcd_with=paths["fcd_with"],
                        fcd_wout=paths["fcd_wout"],
                        trips_json_with=paths["fcd_trips_json_with"],
                        trips_json_wout=paths["fcd_trips_json_wout"],
                        insertion_rate=insertion_rate,
                        closed_edges=[str(e) for e in closed_edges_list],
                        network_xml=net_path,
                        begin_time=begin_time,
                        end_time=end_time,
                    )

                    # 5) Generate congestion maps
                    print("---- generating congestion maps ----")
                    generate_congestion_maps(
                        base_dir=base_dir,
                        network_xml=net_path,
                        edgedata_with=paths["edgedata_with"],
                        edgedata_wout=paths["edgedata_wout"],
                        output_with=paths["congestion_map_with"],
                        output_wout=paths["congestion_map_wout"],
                    )
                    
                    # 6) Calculate metrics and create output
                    metrics_report = compile_metrics_report(
                        tripinfo_with=paths["tripinfo_with"],
                        tripinfo_wout=paths["tripinfo_wout"],
                        edgedata_with=paths["edgedata_with"],
                        edgedata_wout=paths["edgedata_wout"],
                        routes_xml=paths["routes_xml"],
                        simulation_mode="standard_microscopic",
                        begin_time=begin_time,
                        end_time=end_time,
                        insertion_rate=insertion_rate,
                        closed_edges_list=closed_edges_list,
                    )
                    
                    memory_file = create_simulation_output_zip(
                        fcd_trips_json_with=paths["fcd_trips_json_with"],
                        fcd_trips_json_wout=paths["fcd_trips_json_wout"],
                        metrics_report=metrics_report,
                        congestion_map_with=paths["congestion_map_with"],
                        congestion_map_wout=paths["congestion_map_wout"],
                        output_dir=paths["output_dir"],
                    )

                    return Response(
                        content=memory_file.getvalue(),
                        media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=simulation_outputs.zip"}
                    )
                
                elif mode == SimulationMode.MESOSCOPIC:
                    # MESOSCOPIC MODE: Mesoscopic simulation on full network
                    print("\n" + "=" * 60)
                    print("USING MESOSCOPIC SIMULATION MODE")
                    print("=" * 60 + "\n")
                    
                    # 1) Rerouters (only if closed edges provided)
                    print("---- generating rerouters ----")
                    generate_rerouters(
                        network=net_path,
                        closed_edges=[str(e) for e in closed_edges_list],
                        begin=begin_time,
                        end=end_time,
                        out_xml=paths["rerouter_file"],
                    )

                    print("---- running mesoscopic simulation WITH closures ----")
                    # Run mesoscopic simulations
                    run_sumo_mesoscopic(
                        network=net_path,
                        routes_xml=routes_xml,
                        begin=begin_time,
                        end=end_time,
                        tripinfo_out=paths["tripinfo_with"],
                        edgedata_out=paths["edgedata_with"],
                        vehroute_out=paths["vehroute_with"],
                        rerouter_xml=paths["rerouter_file"] if closed_edges_list else None,
                    )
                    
                    print("---- running mesoscopic simulation WITHOUT closures ----")
                    run_sumo_mesoscopic(
                        network=net_path,
                        routes_xml=routes_xml,
                        begin=begin_time,
                        end=end_time,
                        tripinfo_out=paths["tripinfo_wout"],
                        edgedata_out=paths["edgedata_wout"],
                        vehroute_out=paths["vehroute_wout"],
                        rerouter_xml=None,
                    )
                    
                    # 5) Generate congestion maps
                    print("---- generating congestion maps ----")
                    generate_congestion_maps(
                        base_dir=base_dir,
                        network_xml=net_path,
                        edgedata_with=paths["edgedata_with"],
                        edgedata_wout=paths["edgedata_wout"],
                        output_with=paths["congestion_map_with"],
                        output_wout=paths["congestion_map_wout"],
                    )
                    
                    # 6) Calculate metrics
                    metrics_report = compile_metrics_report(
                        tripinfo_with=paths["tripinfo_with"],
                        tripinfo_wout=paths["tripinfo_wout"],
                        edgedata_with=paths["edgedata_with"],
                        edgedata_wout=paths["edgedata_wout"],
                        routes_xml=paths["routes_xml"],
                        simulation_mode="mesoscopic",
                        begin_time=begin_time,
                        end_time=end_time,
                        insertion_rate=insertion_rate,
                        closed_edges_list=closed_edges_list,
                    )
                    
                    # Note: Mesoscopic mode doesn't produce FCD data, so create empty JSON files
                    empty_trips = {"trips": [], "metadata": {"note": "Mesoscopic simulation does not produce FCD trajectories"}}
                    with open(paths["fcd_trips_json_with"], "w", encoding="utf-8") as f:
                        json.dump(empty_trips, f, indent=2)
                    with open(paths["fcd_trips_json_wout"], "w", encoding="utf-8") as f:
                        json.dump(empty_trips, f, indent=2)

                    memory_file = create_simulation_output_zip(
                        fcd_trips_json_with=paths["fcd_trips_json_with"],
                        fcd_trips_json_wout=paths["fcd_trips_json_wout"],
                        metrics_report=metrics_report,
                        congestion_map_with=paths["congestion_map_with"],
                        congestion_map_wout=paths["congestion_map_wout"],
                        output_dir=paths["output_dir"],
                    )

                    return Response(
                        content=memory_file.getvalue(),
                        media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=simulation_outputs.zip"}
                    )

    except subprocess.CalledProcessError as e:
        raise e
    except Exception as e:
        raise e


@app.get("/get_current_deviations")
def get_current_deviations(wfs_url: Optional[str] = None) -> Any:
    """Fetch current traffic events and return GeoJSON of closed lanes mapped to network edges."""
    print("---- getting current deviations ----")
    try:
        net_geojson_path = "brussels.geojson"
        with open(net_geojson_path, "r", encoding="utf-8") as f:
            net_geo = json.load(f)

        closed_edges = fetch_closed_edges_from_brussels_api(net_geo, wfs_url)

        return {
            "type": "FeatureCollection",
            "features": closed_edges,
        }
    except Exception as e:
        raise e


class NetworkGenerationPayload(BaseModel):
    corners: Optional[List[Dict[str, float]]] = None
    bbox: Optional[Any] = None # Can be list or dict
    output_dir: Optional[str] = None

@app.post("/generate_network_from_bounding_box")
def generate_network_from_bounding_box(payload: NetworkGenerationPayload) -> Any:
    """Generate a network from OSM data for a given bounding box."""
    try:
        ensure_env()

        # Convert pydantic model to dict for compatibility
        payload_dict = payload.model_dump()
        
        bbox = parse_bbox_from_payload(payload_dict)

        with SIMULATION_LOCK:
            with tempfile.TemporaryDirectory() as tmpd:
                tmp_path = Path(tmpd)

                osm_input_path = fetch_osm_with_osmget(bbox, tmp_path)

                # Save net.xml to the temp path
                net_xml_path = tmp_path / "uploaded.net.xml"
                generate_network(osm_input_path, net_xml_path)

                # Save geojson to the temp path
                geojson_path = tmp_path / "network.geojson"
                generate_geojson_from_net(net_xml_path, geojson_path)

                # Create metadata
                metadata = {
                    "net_xml_path": "network.net.xml"
                }
                
                # Create in-memory ZIP
                memory_file = BytesIO()
                with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
                    zf.write(net_xml_path, arcname="network.net.xml")
                    zf.write(geojson_path, arcname="network.geojson")
                    zf.writestr("metadata.json", json.dumps(metadata, indent=2))
                memory_file.seek(0)

                return Response(
                    content=memory_file.getvalue(),
                    media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=network.zip"}
                )

    except subprocess.CalledProcessError as e:
        raise e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise e


@app.post("/generate_network_geojson")
def generate_network_geojson(network_zip: UploadFile = File(...)) -> Any:
    """Generate a network geojson from uploaded zipped net.xml file."""
    try:
        if not network_zip.filename:
            raise HTTPException(status_code=400, detail="network_zip file is required")

        ensure_env()

        base_dir = Path(__file__).resolve().parent
        
        with SIMULATION_LOCK:
            with tempfile.TemporaryDirectory() as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)

                # Read and extract the ZIP file
                zip_bytes = BytesIO(network_zip.file.read())
                with ZipFile(zip_bytes, 'r') as zf:
                    # Find the .net.xml file in the zip
                    xml_files = [name for name in zf.namelist() if name.endswith('.net.xml')]
                    if not xml_files:
                         raise HTTPException(status_code=400, detail="No .net.xml file found in the uploaded ZIP")
                    
                    # Extract the first .net.xml file
                    xml_filename = xml_files[0]
                    net_xml_content = zf.read(xml_filename).decode('utf-8')

                # Save the uploaded XML to a file
                net_xml_path = tmp_dir / "uploaded.net.xml"
                with open(net_xml_path, "w", encoding="utf-8") as f:
                    f.write(net_xml_content)

                # Generate GeoJSON from the network
                geojson_path = tmp_dir / "network.geojson"
                generate_geojson_from_net(net_xml_path, geojson_path)

                # Create metadata
                metadata = {
                    "net_xml_path": "network.net.xml"
                }
                
                # Create in-memory ZIP
                memory_file = BytesIO()
                with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
                    zf.write(net_xml_path, arcname="network.net.xml")
                    zf.write(geojson_path, arcname="network.geojson")
                    zf.writestr("metadata.json", json.dumps(metadata, indent=2))
                memory_file.seek(0)

                return Response(
                    content=memory_file.getvalue(),
                    media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=network.zip"}
                )

    except subprocess.CalledProcessError as e:
        raise e
    except Exception as e:
        raise e

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

