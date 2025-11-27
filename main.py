import os
import json
import shlex
import tempfile
import subprocess
import threading
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from typing import List, Optional, Dict, Any
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
from generate_filter_polygon import generate_circle_polygon, create_poly_xml

GEOJSON_PATH = "net.geojson"

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


def build_output_paths(output_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {
        "output_dir": output_dir,
        "sumo_network": output_dir / "osm.net.xml",
        "routes_xml": output_dir / "routes.xml",
        "trips_xml": output_dir / "trips.xml",
        "rerouter_file": output_dir / "rerouters.xml",
        "fcd_with": output_dir / "fcd_with.out.xml",
        "fcd_wout": output_dir / "fcd_without.out.xml",
        "tripinfo_with": output_dir / "tripinfo_with.xml",
        "tripinfo_wout": output_dir / "tripinfo_without.xml",
        "edgedata_with": output_dir / "edgedata_with.xml",
        "edgedata_wout": output_dir / "edgedata_without.xml",
        "fcd_trips_json_with": output_dir / "fcd_trips_with.json",
        "fcd_trips_json_wout": output_dir / "fcd_trips_without.json",
        "congestion_map_with": output_dir / "congestion_with.geojson",
        "congestion_map_wout": output_dir / "congestion_without.geojson",
    }
    return paths


def generate_network(osm_file: Path, net_out: Path) -> None:
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


def generate_rerouters(network: Path, closed_edges: List[str], begin: int, end: int, out_xml: Path) -> None:
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


def run_sumo(
    network: Path,
    routes_xml: Path,
    begin: int,
    end: int,
    fcd_out: Path,
    tripinfo_out: Path,
    edgedata_out: Path,
    rerouter_xml: Optional[Path] = None,
    fcd_filter_shape: Optional[Dict[str, float]] = None,
    output_dir: Optional[Path] = None
) -> None:
    cmd = [
        "sumo",
        "-n", str(network),
        "-r", str(routes_xml),
        "-e", str(end),
        "-b", str(begin),
        "--fcd-output", str(fcd_out),
        "--fcd-output.geo", "true",
        "--tripinfo-output", str(tripinfo_out),
        "--tripinfo-output.write-unfinished", "true",
        "--edgedata-output", str(edgedata_out),
        "--device.emissions.probability", "1.0",
        #"--device.fcd.period", "1.5",
        #"--time-to-teleport.disconnected", "-1",  # This is the correct option
         "--ignore-route-errors", # This line is now removed
    ]
    
    # Collect all additional files (rerouter and poly filter)
    additional_files = []
    
    if rerouter_xml and rerouter_xml.exists():
        additional_files.append(str(rerouter_xml))
    
    # Add FCD filter shape if provided
    poly_file = None
    if fcd_filter_shape and output_dir:
        poly_file = output_dir / "fcd_filter.poly.xml"
        shape_id = "fcd_filter_circle"
        
        create_poly_xml(
            poly_file,
            shape_id,
            fcd_filter_shape['centerLon'],
            fcd_filter_shape['centerLat'],
            fcd_filter_shape['radiusKm'],
            network  # Pass the network file for coordinate conversion
        )
        
        additional_files.append(str(poly_file))
    
    # Add all additional files as a comma-separated list
    if additional_files:
        cmd.extend(["-a", ",".join(additional_files)])
    
    # Add filter-shapes option if poly file was created
    if poly_file:
        cmd.extend(["--fcd-output.filter-shapes", "fcd_filter_circle"])
    
    run(cmd)


def run_python_script(script_path: Path, args: List[str]) -> None:
    cmd = ["python", str(script_path), *args]
    run(cmd)


def generate_congestion_maps(base_dir: Path, network_xml: Path, edgedata_with: Path, edgedata_wout: Path, output_with: Path, output_wout: Path) -> None:
    """Generate congestion map GeoJSONs from edgedata."""
    script = base_dir / "generate_congestion_map.py"
    if not script.exists():
        raise FileNotFoundError(f"Required script not found: {script}")
    
    # Generate congestion map for scenario with closures
    run_python_script(script, [str(network_xml), str(edgedata_with), str(output_with)])
    
    # Generate congestion map for scenario without closures
    run_python_script(script, [str(network_xml), str(edgedata_wout), str(output_wout)])


def convert_fcd_to_outputs(base_dir: Path, fcd_with: Path, fcd_wout: Path, trips_json_with: Path, trips_json_wout: Path, insertion_rate: int, closed_edges: List[str], network_xml: Path) -> None:
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

# Error handling setup
@app.exception_handler(subprocess.CalledProcessError)
async def subprocess_error_handler(request: Request, exc: subprocess.CalledProcessError):
    cmd_list = exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]
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
    tb = traceback.format_exc()
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
    fcd_filter_shape: Optional[str] = Form(None)
) -> Any:
    """
    Run a minimal SUMO pipeline to produce four files and return them as a ZIP.
    """
    # Base dir is the directory containing this file
    base_dir = Path(__file__).resolve().parent

    try:
        ensure_env()
        
        # Validate network_zip
        if not network_zip.filename:
             raise HTTPException(status_code=400, detail="No network file selected")

        # Parse JSON fields from Form data
        try:
            closed_edges_list = json.loads(closed_edges)
            if not isinstance(closed_edges_list, list):
                raise ValueError("closed_edges must be a list of strings")
        except json.JSONDecodeError:
             raise HTTPException(status_code=400, detail="Invalid JSON in closed_edges")
        
        fcd_filter_shape_dict = None
        if fcd_filter_shape:
            try:
                fcd_filter_shape_dict = json.loads(fcd_filter_shape)
                if not isinstance(fcd_filter_shape_dict, dict):
                    raise ValueError("fcd_filter_shape must be an object")
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON in fcd_filter_shape")

        # Use global lock to enforce serial execution
        with SIMULATION_LOCK:
            # Use temporary directory for request isolation
            with tempfile.TemporaryDirectory() as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                
                # Setup output paths in temp dir
                paths = build_output_paths(tmp_dir)
                
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
                
                # 1) Rerouters (only if closed edges provided)
                print("---- generating rerouters ----")
                generate_rerouters(
                    network=net_path,
                    closed_edges=[str(e) for e in closed_edges_list],
                    begin=begin_time,
                    end=end_time,
                    out_xml=paths["rerouter_file"],
                )
                
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

                print("---- running sumo with closed edges ----")
                # 3) SUMO simulations
                run_sumo(
                    network=net_path,
                    routes_xml=routes_xml,
                    begin=begin_time,
                    end=end_time,
                    fcd_out=paths["fcd_with"],
                    tripinfo_out=paths["tripinfo_with"],
                    edgedata_out=paths["edgedata_with"],
                    rerouter_xml=paths["rerouter_file"] if closed_edges_list else None,
                    fcd_filter_shape=fcd_filter_shape_dict,
                    output_dir=paths["output_dir"],
                    
                )
                print("---- running sumo without closed edges ----")
                run_sumo(
                    network=net_path,
                    routes_xml=routes_xml,
                    begin=begin_time,
                    end=end_time,
                    fcd_out=paths["fcd_wout"],
                    tripinfo_out=paths["tripinfo_wout"],
                    edgedata_out=paths["edgedata_wout"],
                    rerouter_xml=None,
                    fcd_filter_shape=fcd_filter_shape_dict,
                    output_dir=paths["output_dir"],
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
                print("---- calculating metrics without closed edges ----")
                metrics_without = calculate_metrics(
                    tripinfo_path=paths["tripinfo_wout"],
                    edgedata_path=paths["edgedata_wout"],
                    routes_path=paths["routes_xml"],
                    baseline_tripinfo_path=None,
                )
                print("---- calculating metrics with closed edges ----")    
                metrics_with = calculate_metrics(
                    tripinfo_path=paths["tripinfo_with"],
                    edgedata_path=paths["edgedata_with"],
                    routes_path=paths["routes_xml"],
                    baseline_tripinfo_path=paths["tripinfo_wout"],
                )
                
                comparison = calculate_scenario_comparison(metrics_with, metrics_without)
                
                # Compile all metrics into a comprehensive report
                metrics_report = {
                    "scenario_without_closures": metrics_without,
                    "scenario_with_closures": metrics_with,
                    "comparison": comparison,
                    "metadata": {
                        "begin_time": begin_time,
                        "end_time": end_time,
                        "simulation_duration_s": end_time - begin_time,
                        "insertion_rate": insertion_rate,
                        "closed_edges": [str(e) for e in closed_edges_list],
                        "num_closed_edges": len(closed_edges_list),
                    }
                }
                
                # Write metrics to JSON file
                metrics_json_path = paths["output_dir"] / "metrics.json"
                with open(metrics_json_path, "w", encoding="utf-8") as mf:
                    json.dump(metrics_report, mf, indent=2)

                # Create in-memory ZIP of all output files
                memory_file = BytesIO()
                with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
                    zf.write(paths["fcd_trips_json_with"], arcname=paths["fcd_trips_json_with"].name)
                    zf.write(paths["fcd_trips_json_wout"], arcname=paths["fcd_trips_json_wout"].name)
                    zf.write(metrics_json_path, arcname="metrics.json")
                    zf.write(paths["congestion_map_with"], arcname="congestion_with.geojson")
                    zf.write(paths["congestion_map_wout"], arcname="congestion_without.geojson")
                memory_file.seek(0)

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

