import os
import json
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# Import from our custom modules
from extract_osm import (
    parse_bbox_from_payload,
    fetch_osm_with_osmget,
    generate_geojson_from_net
)
from get_osiris_closed_edges import fetch_closed_edges_from_brussels_api
from calculate_metrics import (
    calculate_metrics,
    calculate_multi_scenario_comparison
)


# Scenario model for multi-scenario simulation
class ScenarioInput(BaseModel):
    """Input model for a single scenario in multi-scenario simulation."""
    name: str
    description: Optional[str] = None
    closed_edges: List[str] = []
    
    @field_validator('name')
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('Scenario name cannot be empty')
        return v.strip()
    
    @field_validator('closed_edges', mode='before')
    @classmethod
    def ensure_list(cls, v):
        if v is None:
            return []
        return v


MAX_SCENARIOS = 5
# Note: generate_filter_polygon module is available if needed for FCD filtering
from utils.run_utils import run_python_script, ensure_env, run
from utils.network_utils import generate_network, extract_subnetwork, extract_demand_for_subnetwork
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
    }
    
    if mode == SimulationMode.MICROSCOPIC:
        # Standard microscopic simulation paths
        paths.update({
            "sumo_network": output_dir / "osm.net.xml",
        })
    
    elif mode == SimulationMode.MESOSCOPIC:
        # Mesoscopic simulation paths
        paths.update({
            "sumo_network": output_dir / "osm.net.xml",
        })
    
    elif mode == SimulationMode.HYBRID:
        # Hybrid simulation paths
        meso_dir = output_dir / "mesoscopic"
        micro_dir = output_dir / "microscopic"
        meso_dir.mkdir(parents=True, exist_ok=True)
        micro_dir.mkdir(parents=True, exist_ok=True)
        
        paths.update({
            "full_network": output_dir / "full_network.net.xml",
            "sumo_network": output_dir / "full_network.net.xml",
            "sub_network": output_dir / "subnetwork.net.xml",
            "meso_dir": meso_dir,
            "micro_dir": micro_dir,
        })
    
    return paths


def build_scenario_paths(
    output_dir: Path, 
    scenario_name: str, 
    mode: SimulationMode
) -> Dict[str, Path]:
    """Build output paths for a specific scenario.
    
    Args:
        output_dir: Base output directory
        scenario_name: Name of the scenario (sanitized for filesystem)
        mode: Simulation mode
    
    Returns:
        Dictionary of paths for this scenario's outputs
    """
    # Sanitize scenario name for filesystem
    safe_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in scenario_name)
    scenario_dir = output_dir / f"scenario_{safe_name}"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    
    paths = {
        "scenario_dir": scenario_dir,
        "rerouter_file": scenario_dir / "rerouters.xml",
        "fcd_trips_json": output_dir / f"fcd_trips_{safe_name}.json",
        "congestion_map": output_dir / f"congestion_{safe_name}.geojson",
    }
    
    if mode == SimulationMode.MICROSCOPIC:
        paths.update({
            "fcd": scenario_dir / "fcd.out.xml",
            "tripinfo": scenario_dir / "tripinfo.xml",
            "edgedata": scenario_dir / "edgedata.xml",
        })
    
    elif mode == SimulationMode.MESOSCOPIC:
        paths.update({
            "tripinfo": scenario_dir / "tripinfo.xml",
            "edgedata": scenario_dir / "edgedata.xml",
            "vehroute": scenario_dir / "vehroute.xml",
        })
    
    elif mode == SimulationMode.HYBRID:
        meso_dir = scenario_dir / "mesoscopic"
        micro_dir = scenario_dir / "microscopic"
        meso_dir.mkdir(parents=True, exist_ok=True)
        micro_dir.mkdir(parents=True, exist_ok=True)
        
        paths.update({
            "meso_tripinfo": meso_dir / "tripinfo.xml",
            "meso_edgedata": meso_dir / "edgedata.xml",
            "meso_vehroute": meso_dir / "vehroute.xml",
            "micro_routes": micro_dir / "routes.xml",
            "micro_rerouter_file": micro_dir / "rerouters.xml",
            "micro_fcd": micro_dir / "fcd.out.xml",
            "micro_tripinfo": micro_dir / "tripinfo.xml",
            "micro_edgedata": micro_dir / "edgedata.xml",
        })
    
    return paths







def compile_metrics_report_multi_scenario(
    scenario_outputs: List[Dict[str, Any]],
    routes_xml: Path,
    simulation_mode: str,
    begin_time: int,
    end_time: int,
    insertion_rate: int,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compile metrics report from multiple scenario simulation outputs.
    
    Args:
        scenario_outputs: List of dicts with keys:
            - name: scenario name
            - description: optional description
            - closed_edges: list of closed edge IDs
            - tripinfo_path: Path to tripinfo XML
            - edgedata_path: Path to edgedata XML
        routes_xml: Path to routes XML file
        simulation_mode: Simulation mode used
        begin_time: Simulation begin time
        end_time: Simulation end time
        insertion_rate: Vehicle insertion rate
        extra_metadata: Additional metadata to include
    
    Returns:
        Dict with scenarios sorted by total_delay_vh (best first) and comparisons
    """
    # Calculate metrics for each scenario
    scenario_metrics = []
    for scenario in scenario_outputs:
        print(f"---- calculating metrics for scenario: {scenario['name']} ----")
        metrics = calculate_metrics(
            tripinfo_path=scenario['tripinfo_path'],
            edgedata_path=scenario['edgedata_path'],
            routes_path=routes_xml,
            baseline_tripinfo_path=None,  # Will recalculate detours after sorting
        )
        scenario_metrics.append({
            "name": scenario['name'],
            "description": scenario.get('description'),
            "closed_edges": scenario['closed_edges'],
            "metrics": metrics,
            "tripinfo_path": scenario['tripinfo_path'],  # Keep for detour calculation
        })
    
    # Sort by total_delay_vh (ascending - lower delay is better)
    scenario_metrics.sort(key=lambda x: x['metrics']['total_delay_vh'])
    
    # The best scenario (lowest delay) is now first
    best_scenario = scenario_metrics[0]
    best_tripinfo = best_scenario['tripinfo_path']
    
    # Recalculate metrics with detour relative to best scenario
    for i, scenario in enumerate(scenario_metrics):
        if i == 0:
            # Best scenario - no detour calculation needed
            scenario['metrics']['avg_detour_km'] = 0.0
            scenario['rank'] = 1
        else:
            # Calculate detour relative to best scenario
            metrics_with_detour = calculate_metrics(
                tripinfo_path=scenario['tripinfo_path'],
                edgedata_path=scenario_outputs[i]['edgedata_path'] if i < len(scenario_outputs) else scenario['tripinfo_path'].parent / "edgedata.xml",
                routes_path=routes_xml,
                baseline_tripinfo_path=best_tripinfo,
            )
            scenario['metrics']['avg_detour_km'] = metrics_with_detour['avg_detour_km']
            scenario['rank'] = i + 1
        
        # Remove internal path from output
        del scenario['tripinfo_path']
    
    # Calculate comparisons (deltas from best scenario)
    comparisons = calculate_multi_scenario_comparison(
        [s['metrics'] for s in scenario_metrics]
    )
    
    # Build metadata
    metadata = {
        "simulation_mode": simulation_mode,
        "begin_time": begin_time,
        "end_time": end_time,
        "simulation_duration_s": end_time - begin_time,
        "insertion_rate": insertion_rate,
        "num_scenarios": len(scenario_metrics),
        "best_scenario": best_scenario['name'],
    }
    
    if extra_metadata:
        metadata.update(extra_metadata)
    
    return {
        "scenarios": scenario_metrics,
        "comparisons": comparisons,
        "metadata": metadata,
    }


def create_simulation_output_zip_multi_scenario(
    scenario_outputs: List[Dict[str, Any]],
    metrics_report: Dict[str, Any],
    output_dir: Path,
) -> BytesIO:
    """Create ZIP file with multi-scenario simulation outputs.
    
    Args:
        scenario_outputs: List of dicts with keys:
            - name: scenario name
            - fcd_trips_json: Path to FCD trips JSON
            - congestion_map: Path to congestion GeoJSON
        metrics_report: Compiled metrics report
        output_dir: Output directory for metrics JSON
    
    Returns:
        In-memory ZIP file
    """
    # Write metrics to JSON file
    metrics_json_path = output_dir / "metrics.json"
    with open(metrics_json_path, "w", encoding="utf-8") as mf:
        json.dump(metrics_report, mf, indent=2)
    
    # Create in-memory ZIP
    memory_file = BytesIO()
    with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
        zf.write(metrics_json_path, arcname="metrics.json")
        
        for scenario in scenario_outputs:
            safe_name = "".join(
                c if c.isalnum() or c in '-_' else '_' 
                for c in scenario['name']
            )
            
            # Add FCD trips JSON
            if scenario.get('fcd_trips_json') and scenario['fcd_trips_json'].exists():
                zf.write(
                    scenario['fcd_trips_json'], 
                    arcname=f"fcd_trips_{safe_name}.json"
                )
            
            # Add congestion map
            if scenario.get('congestion_map') and scenario['congestion_map'].exists():
                zf.write(
                    scenario['congestion_map'], 
                    arcname=f"congestion_{safe_name}.geojson"
                )
    
    memory_file.seek(0)
    return memory_file


def generate_congestion_map(
    base_dir: Path, 
    network_xml: Path, 
    edgedata: Path, 
    output: Path
) -> None:
    """Generate congestion map GeoJSON from edgedata for a single scenario."""
    script = base_dir / "generate_congestion_map.py"
    if not script.exists():
        raise FileNotFoundError(f"Required script not found: {script}")
    
    run_python_script(script, [str(network_xml), str(edgedata), str(output)])


def convert_fcd_to_json(
    base_dir: Path,
    fcd_xml: Path,
    trips_json: Path,
    network_xml: Path,
    insertion_rate: int,
    closed_edges: List[str],
    begin_time: int,
    end_time: int,
    scenario_name: str = ""
) -> None:
    """Convert FCD XML to JSON for a single scenario."""
    fcd_to_trips = base_dir / "fcd_to_trips.py"

    if not fcd_to_trips.exists():
        raise FileNotFoundError(f"Required script not found: {fcd_to_trips}")

    print(f"---- converting FCD to JSON for scenario: {scenario_name} ----")
    run_python_script(
        fcd_to_trips,
        [
            str(fcd_xml),
            str(trips_json),
            "--network-xml", str(network_xml),
            "--insertion-rate", str(insertion_rate),
            "--closed-edges", ",".join(closed_edges) if closed_edges else "",
            "--begin-time", str(begin_time),
            "--end-time", str(end_time),
        ],
    )


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
    scenarios: str = Form(...),
    routes_zip: Optional[UploadFile] = File(None),
    fcdFilterShape: Optional[str] = Form(None),
    simulation_mode: str = Form(SIMULATION_MODE.value)
) -> Any:
    """
    Run a SUMO simulation pipeline to produce traffic analysis outputs.
    
    Accepts up to 5 scenarios, each with different edge closures.
    Results are sorted by total_delay_vh (best scenario first) and deltas
    are calculated relative to the best scenario.
    
    Parameters:
        scenarios: JSON array of scenario objects, each containing:
            - name (str, required): Unique name for the scenario
            - description (str, optional): Description of the scenario
            - closed_edges (list[str], optional): List of edge IDs to close
        
        Example scenarios payload:
        [
            {"name": "baseline", "description": "No closures", "closed_edges": []},
            {"name": "option_a", "description": "Close main street", "closed_edges": ["edge1"]},
            {"name": "option_b", "closed_edges": ["edge2", "edge3"]}
        ]
    
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
    
    Returns a ZIP with:
        - fcd_trips_{scenario_name}.json for each scenario (microscopic/hybrid modes)
        - congestion_{scenario_name}.geojson for each scenario
        - metrics.json with all scenarios sorted by performance
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

        # Parse scenarios JSON
        try:
            scenarios_raw = json.loads(scenarios)
            if not isinstance(scenarios_raw, list):
                raise ValueError("scenarios must be a list")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in scenarios")
        
        # Validate scenarios
        if len(scenarios_raw) == 0:
            raise HTTPException(status_code=400, detail="At least one scenario is required")
        if len(scenarios_raw) > MAX_SCENARIOS:
            raise HTTPException(
                status_code=400, 
                detail=f"Maximum {MAX_SCENARIOS} scenarios allowed, got {len(scenarios_raw)}"
            )
        
        # Parse and validate each scenario
        scenarios_list: List[ScenarioInput] = []
        scenario_names = set()
        for i, s in enumerate(scenarios_raw):
            try:
                scenario = ScenarioInput(**s)
                if scenario.name in scenario_names:
                    raise HTTPException(
                        status_code=400, 
                        detail=f"Duplicate scenario name: '{scenario.name}'"
                    )
                scenario_names.add(scenario.name)
                scenarios_list.append(scenario)
            except Exception as e:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Invalid scenario at index {i}: {str(e)}"
                )
        
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
                # MULTI-SCENARIO SIMULATION WORKFLOW
                # ==============================================================
                
                print("\n" + "=" * 60)
                print(f"RUNNING {len(scenarios_list)} SCENARIOS IN {mode.value.upper()} MODE")
                print("=" * 60 + "\n")
                
                # Validate hybrid mode requirements
                if mode == SimulationMode.HYBRID and not fcd_filter_shape_dict:
                    raise HTTPException(
                        status_code=400, 
                        detail="fcd_filter_shape is required for hybrid mode. Please provide centerLon, centerLat, and radiusKm."
                    )
                
                # For hybrid mode, extract subnetwork once (shared across scenarios)
                sub_network_path = None
                if mode == SimulationMode.HYBRID:
                    print("---- extracting subnetwork for hybrid mode ----")
                    sub_network_path = paths["sub_network"]
                    extract_subnetwork(
                        full_network=net_path,
                        output_network=sub_network_path,
                        fcd_filter_shape=fcd_filter_shape_dict,
                        buffer_km=0
                    )
                
                # Store results for each scenario
                scenario_outputs: List[Dict[str, Any]] = []
                scenario_metrics_data: List[Dict[str, Any]] = []
                
                # Run simulation for each scenario
                for scenario_idx, scenario in enumerate(scenarios_list):
                    print(f"\n{'=' * 60}")
                    print(f"SCENARIO {scenario_idx + 1}/{len(scenarios_list)}: {scenario.name}")
                    print(f"Closed edges: {scenario.closed_edges}")
                    print("=" * 60)
                    
                    # Build paths for this scenario
                    s_paths = build_scenario_paths(
                        paths["output_dir"], 
                        scenario.name, 
                        mode
                    )
                    
                    closed_edges_list = [str(e) for e in scenario.closed_edges]
                    
                    if mode == SimulationMode.MICROSCOPIC:
                        # Generate rerouters if closed edges exist
                        if closed_edges_list:
                            print(f"---- generating rerouters for {scenario.name} ----")
                            generate_rerouters(
                                network=net_path,
                                closed_edges=closed_edges_list,
                                begin=begin_time,
                                end=end_time,
                                out_xml=s_paths["rerouter_file"],
                            )
                        
                        # Run microscopic simulation
                        print(f"---- running microscopic simulation for {scenario.name} ----")
                        run_sumo_microscopic(
                            network=net_path,
                            routes_xml=routes_xml,
                            begin=begin_time,
                            end=end_time,
                            fcd_out=s_paths["fcd"],
                            tripinfo_out=s_paths["tripinfo"],
                            edgedata_out=s_paths["edgedata"],
                            rerouter_xml=s_paths["rerouter_file"] if closed_edges_list else None,
                        )
                        
                        # Convert FCD to JSON
                        convert_fcd_to_json(
                            base_dir=base_dir,
                            fcd_xml=s_paths["fcd"],
                            trips_json=s_paths["fcd_trips_json"],
                            network_xml=net_path,
                            insertion_rate=insertion_rate,
                            closed_edges=closed_edges_list,
                            begin_time=begin_time,
                            end_time=end_time,
                            scenario_name=scenario.name,
                        )
                        
                        # Generate congestion map
                        print(f"---- generating congestion map for {scenario.name} ----")
                        generate_congestion_map(
                            base_dir=base_dir,
                            network_xml=net_path,
                            edgedata=s_paths["edgedata"],
                            output=s_paths["congestion_map"],
                        )
                        
                        # Store paths for metrics calculation
                        scenario_metrics_data.append({
                            "name": scenario.name,
                            "description": scenario.description,
                            "closed_edges": closed_edges_list,
                            "tripinfo_path": s_paths["tripinfo"],
                            "edgedata_path": s_paths["edgedata"],
                        })
                        
                        scenario_outputs.append({
                            "name": scenario.name,
                            "fcd_trips_json": s_paths["fcd_trips_json"],
                            "congestion_map": s_paths["congestion_map"],
                        })
                    
                    elif mode == SimulationMode.MESOSCOPIC:
                        # Generate rerouters if closed edges exist
                        if closed_edges_list:
                            print(f"---- generating rerouters for {scenario.name} ----")
                            generate_rerouters(
                                network=net_path,
                                closed_edges=closed_edges_list,
                                begin=begin_time,
                                end=end_time,
                                out_xml=s_paths["rerouter_file"],
                            )
                        
                        # Run mesoscopic simulation
                        print(f"---- running mesoscopic simulation for {scenario.name} ----")
                        run_sumo_mesoscopic(
                            network=net_path,
                            routes_xml=routes_xml,
                            begin=begin_time,
                            end=end_time,
                            tripinfo_out=s_paths["tripinfo"],
                            edgedata_out=s_paths["edgedata"],
                            vehroute_out=s_paths["vehroute"],
                            rerouter_xml=s_paths["rerouter_file"] if closed_edges_list else None,
                        )
                        
                        # Generate congestion map
                        print(f"---- generating congestion map for {scenario.name} ----")
                        generate_congestion_map(
                            base_dir=base_dir,
                            network_xml=net_path,
                            edgedata=s_paths["edgedata"],
                            output=s_paths["congestion_map"],
                        )
                        
                        # Mesoscopic doesn't produce FCD, create empty file
                        empty_trips = {
                            "trips": [], 
                            "metadata": {
                                "note": "Mesoscopic simulation does not produce FCD trajectories",
                                "scenario": scenario.name
                            }
                        }
                        with open(s_paths["fcd_trips_json"], "w", encoding="utf-8") as f:
                            json.dump(empty_trips, f, indent=2)
                        
                        # Store paths for metrics calculation
                        scenario_metrics_data.append({
                            "name": scenario.name,
                            "description": scenario.description,
                            "closed_edges": closed_edges_list,
                            "tripinfo_path": s_paths["tripinfo"],
                            "edgedata_path": s_paths["edgedata"],
                        })
                        
                        scenario_outputs.append({
                            "name": scenario.name,
                            "fcd_trips_json": s_paths["fcd_trips_json"],
                            "congestion_map": s_paths["congestion_map"],
                        })
                    
                    elif mode == SimulationMode.HYBRID:
                        # HYBRID MODE: Mesoscopic + Microscopic
                        
                        # Phase 1: Mesoscopic simulation on full network
                        if closed_edges_list:
                            print(f"---- generating rerouters for {scenario.name} (mesoscopic) ----")
                            generate_rerouters(
                                network=net_path,
                                closed_edges=closed_edges_list,
                                begin=begin_time,
                                end=end_time,
                                out_xml=s_paths["rerouter_file"],
                            )
                        
                        print(f"---- running mesoscopic simulation for {scenario.name} ----")
                        run_sumo_mesoscopic(
                            network=net_path,
                            routes_xml=routes_xml,
                            begin=begin_time,
                            end=end_time,
                            tripinfo_out=s_paths["meso_tripinfo"],
                            edgedata_out=s_paths["meso_edgedata"],
                            vehroute_out=s_paths["meso_vehroute"],
                            rerouter_xml=s_paths["rerouter_file"] if closed_edges_list else None,
                        )
                        
                        # Phase 2: Extract demand for microscopic simulation
                        print(f"---- extracting demand for {scenario.name} ----")
                        vehicles_extracted = extract_demand_for_subnetwork(
                            vehroute_xml=s_paths["meso_vehroute"],
                            full_network=net_path,
                            sub_network=sub_network_path,
                            output_routes=s_paths["micro_routes"],
                            fcd_filter_shape=fcd_filter_shape_dict,
                            begin=begin_time,
                            end=end_time,
                        )
                        
                        # Phase 3: Microscopic simulation on subnetwork
                        # Filter closed edges to subnetwork
                        import sys
                        sumo_tools = Path(os.environ["SUMO_HOME"]) / "tools"
                        if str(sumo_tools) not in sys.path:
                            sys.path.append(str(sumo_tools))
                        import sumolib
                        
                        sub_net = sumolib.net.readNet(str(sub_network_path))
                        sub_edge_ids = set(edge.getID() for edge in sub_net.getEdges())
                        micro_closed_edges = [e for e in closed_edges_list if e in sub_edge_ids]
                        
                        micro_rerouter_path = None
                        if micro_closed_edges:
                            print(f"---- generating rerouters for {scenario.name} (microscopic) ----")
                            generate_rerouters(
                                network=sub_network_path,
                                closed_edges=micro_closed_edges,
                                begin=begin_time,
                                end=end_time,
                                out_xml=s_paths["micro_rerouter_file"],
                            )
                            micro_rerouter_path = s_paths["micro_rerouter_file"]
                        
                        if vehicles_extracted > 0:
                            print(f"---- running microscopic simulation for {scenario.name} ----")
                            run_sumo_microscopic(
                                network=sub_network_path,
                                routes_xml=s_paths["micro_routes"],
                                begin=begin_time,
                                end=end_time,
                                fcd_out=s_paths["micro_fcd"],
                                tripinfo_out=s_paths["micro_tripinfo"],
                                edgedata_out=s_paths["micro_edgedata"],
                                rerouter_xml=micro_rerouter_path,
                            )
                        else:
                            print(f"---- WARNING: No vehicles extracted for {scenario.name} ----")
                            with open(s_paths["micro_fcd"], 'w') as f:
                                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<fcd-export/>\n')
                        
                        # Convert FCD to JSON
                        convert_fcd_to_json(
                            base_dir=base_dir,
                            fcd_xml=s_paths["micro_fcd"],
                            trips_json=s_paths["fcd_trips_json"],
                            network_xml=sub_network_path,
                            insertion_rate=insertion_rate,
                            closed_edges=closed_edges_list,
                            begin_time=begin_time,
                            end_time=end_time,
                            scenario_name=scenario.name,
                        )
                        
                        # Generate congestion map from mesoscopic data (full network)
                        print(f"---- generating congestion map for {scenario.name} ----")
                        generate_congestion_map(
                            base_dir=base_dir,
                            network_xml=net_path,
                            edgedata=s_paths["meso_edgedata"],
                            output=s_paths["congestion_map"],
                        )
                        
                        # Store paths for metrics calculation (using mesoscopic for metrics)
                        scenario_metrics_data.append({
                            "name": scenario.name,
                            "description": scenario.description,
                            "closed_edges": closed_edges_list,
                            "tripinfo_path": s_paths["meso_tripinfo"],
                            "edgedata_path": s_paths["meso_edgedata"],
                        })
                        
                        scenario_outputs.append({
                            "name": scenario.name,
                            "fcd_trips_json": s_paths["fcd_trips_json"],
                            "congestion_map": s_paths["congestion_map"],
                        })
                
                # Compile metrics report with all scenarios
                print("\n" + "=" * 60)
                print("COMPILING METRICS REPORT")
                print("=" * 60)
                
                metrics_report = compile_metrics_report_multi_scenario(
                    scenario_outputs=scenario_metrics_data,
                    routes_xml=routes_xml,
                    simulation_mode=mode.value,
                    begin_time=begin_time,
                    end_time=end_time,
                    insertion_rate=insertion_rate,
                    extra_metadata={
                        "fcd_filter_shape": fcd_filter_shape_dict,
                    } if fcd_filter_shape_dict else None,
                )
                
                # Create output ZIP
                memory_file = create_simulation_output_zip_multi_scenario(
                    scenario_outputs=scenario_outputs,
                    metrics_report=metrics_report,
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

