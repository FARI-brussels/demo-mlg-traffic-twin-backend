import os
import json
import shlex
import tempfile
import subprocess
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from typing import List, Optional, Dict, Any
import traceback

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

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

app = Flask(__name__)
CORS(app)

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
    exts = [""]
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


def build_output_paths(base_dir: Path, output_dir: str = "output") -> Dict[str, Path]:
    o = base_dir / output_dir
    o.mkdir(parents=True, exist_ok=True)

    paths = {
        "output_dir": o,
        "sumo_network": o / "osm.net.xml",
        "routes_xml": o / "routes.xml",
        "trips_xml": o / "trips.xml",
        "rerouter_file": o / "rerouters.xml",
        "fcd_with": o / "fcd_with.out.xml",
        "fcd_wout": o / "fcd_without.out.xml",
        "tripinfo_with": o / "tripinfo_with.xml",
        "tripinfo_wout": o / "tripinfo_without.xml",
        "edgedata_with": o / "edgedata_with.xml",
        "edgedata_wout": o / "edgedata_without.xml",
        "fcd_trips_json_with": o / "fcd_trips_with.json",
        "fcd_trips_json_wout": o / "fcd_trips_without.json",
        "congestion_map_with": o / "congestion_with.geojson",
        "congestion_map_wout": o / "congestion_without.geojson",
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


def json_error_response(e: Exception, status_code: int = 500):
    tb = traceback.format_exc()
    payload: Dict[str, Any] = {
        "error": str(e),
        "error_type": type(e).__name__,
        "error_stack": tb,
    }
    if isinstance(e, subprocess.CalledProcessError):
        cmd_list = e.cmd if isinstance(e.cmd, list) else [str(e.cmd)]
        payload.update({
            "cmd": " ".join(cmd_list),
            "returncode": e.returncode,
            "stdout": e.stdout,
            "stderr": e.stderr,
        })
    return jsonify(payload), status_code

@app.route("/simulate", methods=["POST"])
def simulate() -> Any:
    """
    Run a minimal SUMO pipeline to produce four files and return them as a ZIP:
      - fcd_trips_with.json
      - fcd_trips_without.json

    Request form-data fields:
      - network_zip: ZIP file containing network.net.xml (required)
      - begin_time: int (seconds) (required)
      - end_time: int (seconds) (required)
      - insertion_rate: int (optional, default 3000, used only if routes_zip not provided)
      - closed_edges: JSON string array (optional)
      - routes_zip: ZIP file containing routes.xml (optional, if not provided, random routes will be generated)
      - fcd_filter_shape: JSON object with centerLon, centerLat, radiusKm (optional, filters FCD output to circular area)
    """
    try:
        ensure_env()

        # Check if network zip was uploaded
        if 'network_zip' not in request.files:
            return jsonify({"error": "network_zip file is required"}), 400

        zip_file = request.files['network_zip']
        if zip_file.filename == '':
            return jsonify({"error": "No network file selected"}), 400

        # Base dir is the directory containing this file
        base_dir = Path(__file__).resolve().parent

        # Parse form data
        begin_time = int(request.form.get("begin_time"))
        end_time = int(request.form.get("end_time"))
        insertion_rate = int(request.form.get("insertion_rate", 3000))
        
        closed_edges_str = request.form.get("closed_edges", "[]")
        closed_edges = json.loads(closed_edges_str)
        if not isinstance(closed_edges, list):
            return jsonify({"error": "closed_edges must be a list of strings"}), 400
        
        # Parse optional circular filter shape for FCD filtering
        fcd_filter_shape = None
        fcd_shape_str = request.form.get("fcd_filter_shape", None)
        if fcd_shape_str:
            fcd_filter_shape = json.loads(fcd_shape_str)
            if not isinstance(fcd_filter_shape, dict):
                return jsonify({"error": "fcd_filter_shape must be an object"}), 400
        
        # Extract the network XML from the uploaded ZIP
        output_dir = "output"
        paths = build_output_paths(base_dir, output_dir)
        
        zip_bytes = BytesIO(zip_file.read())
        with ZipFile(zip_bytes, 'r') as zf:
            xml_files = [name for name in zf.namelist() if name.endswith('.net.xml')]
            if not xml_files:
                return jsonify({"error": "No .net.xml file found in the uploaded ZIP"}), 400
            
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
            closed_edges=[str(e) for e in closed_edges],
            begin=begin_time,
            end=end_time,
            out_xml=paths["rerouter_file"],
        )
        
        # 2) Trips and routes - check if routes_zip was provided
        print("---- handling routes ----")
        if 'routes_zip' in request.files and request.files['routes_zip'].filename != '':
            print("---- extracting uploaded routes ----")
            routes_zip_file = request.files['routes_zip']
            routes_zip_bytes = BytesIO(routes_zip_file.read())
            
            with ZipFile(routes_zip_bytes, 'r') as rzf:
                routes_xml_files = [name for name in rzf.namelist() if name.endswith('.xml')]
                if not routes_xml_files:
                    return jsonify({"error": "No .xml file found in the uploaded routes ZIP"}), 400
                
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
            rerouter_xml=paths["rerouter_file"] if closed_edges else None,
            fcd_filter_shape=fcd_filter_shape,
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
            fcd_filter_shape=fcd_filter_shape,
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
            closed_edges=[str(e) for e in closed_edges],
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
                "closed_edges": [str(e) for e in closed_edges],
                "num_closed_edges": len(closed_edges),
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

        return send_file(
            memory_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name="simulation_outputs.zip",
        )

    except subprocess.CalledProcessError as e:
        raise e
        return json_error_response(e, 500)
    except Exception as e:
        raise e
        return json_error_response(e, 500)


@app.route("/get_current_deviations", methods=["GET"])
def get_current_deviations() -> Any:
    """Fetch current traffic events and return GeoJSON of closed lanes mapped to network edges.

    - Downloads WFS events from mobility.brussels.
    - Filters for French consequences containing "Les deux directions fermées".
    - Matches each event point to the nearest edge from net.geojson.
    - Also includes the nearest opposite-direction edge based on id inversion.
    - Returns FeatureCollection of those edges with closure metadata.

    Optional query params:
      - net_geojson_path: override path to net.geojson (default: <this_dir>/net.geojson)
      - wfs_url: override WFS URL
    """
    try:
        net_geojson_path = "brussels.geojson"
        with open(net_geojson_path, "r", encoding="utf-8") as f:
            net_geo = json.load(f)

        wfs_url = request.args.get("wfs_url", None)
        closed_edges = fetch_closed_edges_from_brussels_api(net_geo, wfs_url)

        return jsonify({
            "type": "FeatureCollection",
            "features": closed_edges,
        })
    except Exception as e:
        return json_error_response(e, 500)


@app.route("/generate_network_from_bounding_box", methods=["POST"])
def generate_network_from_bounding_box() -> Any:
    """Generate a network from OSM data for a given bounding box.

    Request JSON fields:
      - corners: List[{lat, lon}] (at least 4) OR
      - bbox: [min_lon, min_lat, max_lon, max_lat] OR
      - bbox: {min_lon, min_lat, max_lon, max_lat}
      - output_dir: optional directory to persist the generated osm.net.xml (default 'output')
    
    Returns:
      - ZIP file containing:
        - network.net.xml: the SUMO network file
        - network.geojson: the GeoJSON representation
        - metadata.json: path information
    """
    try:
        payload = request.get_json(force=True, silent=False)
        if not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON body"}), 400

        ensure_env()

        base_dir = Path(__file__).resolve().parent
        bbox = parse_bbox_from_payload(payload)

        # Determine persistent output directory for the SUMO net
        output_dir_value = payload.get("output_dir", "output")
        output_dir_path = Path(output_dir_value)
        if not output_dir_path.is_absolute():
            output_dir_path = base_dir / output_dir_path
        output_dir_path.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpd:
            tmp_path = Path(tmpd)

            osm_input_path = fetch_osm_with_osmget(bbox, tmp_path)

            # Save net.xml to the same path as generate_network_geojson
            net_xml_path = output_dir_path / "uploaded.net.xml"
            generate_network(osm_input_path, net_xml_path)

            # Save geojson to the same path as generate_network_geojson
            geojson_path = output_dir_path / "network.geojson"
            generate_geojson_from_net(net_xml_path, geojson_path)

            # Create metadata
            metadata = {
                "net_xml_path": str(net_xml_path.relative_to(base_dir))
            }
            
            # Create in-memory ZIP
            memory_file = BytesIO()
            with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
                zf.write(net_xml_path, arcname="network.net.xml")
                zf.write(geojson_path, arcname="network.geojson")
                zf.writestr("metadata.json", json.dumps(metadata, indent=2))
            memory_file.seek(0)

            return send_file(
                memory_file,
                mimetype="application/zip",
                as_attachment=True,
                download_name="network.zip",
            )

    except subprocess.CalledProcessError as e:
        return json_error_response(e, 500)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return json_error_response(e, 500)

@app.route("/generate_network_geojson", methods=["POST"])
def generate_network_geojson() -> Any:
    """Generate a network geojson from uploaded zipped net.xml file.
    
    Request form-data:
      - network_zip: ZIP file containing network.net.xml
    
    Returns:
      - ZIP file containing:
        - network.net.xml: the SUMO network file
        - network.geojson: the GeoJSON representation
        - metadata.json: path information
    """
    try:
        # Check if a file was uploaded
        if 'network_zip' not in request.files:
            return jsonify({"error": "network_zip file is required"}), 400

        zip_file = request.files['network_zip']
        if zip_file.filename == '':
            return jsonify({"error": "No file selected"}), 400

        ensure_env()

        base_dir = Path(__file__).resolve().parent
        
        # Determine output directory
        output_dir_path = base_dir / "output"
        output_dir_path.mkdir(parents=True, exist_ok=True)

        # Read and extract the ZIP file
        zip_bytes = BytesIO(zip_file.read())
        with ZipFile(zip_bytes, 'r') as zf:
            # Find the .net.xml file in the zip
            xml_files = [name for name in zf.namelist() if name.endswith('.net.xml')]
            if not xml_files:
                return jsonify({"error": "No .net.xml file found in the uploaded ZIP"}), 400
            
            # Extract the first .net.xml file
            xml_filename = xml_files[0]
            net_xml_content = zf.read(xml_filename).decode('utf-8')

        # Save the uploaded XML to a file
        net_xml_path = output_dir_path / "uploaded.net.xml"
        with open(net_xml_path, "w", encoding="utf-8") as f:
            f.write(net_xml_content)

        # Generate GeoJSON from the network
        geojson_path = output_dir_path / "network.geojson"
        generate_geojson_from_net(net_xml_path, geojson_path)

        # Create metadata
        metadata = {
            "net_xml_path": str(net_xml_path.relative_to(base_dir))
        }
        
        # Create in-memory ZIP
        memory_file = BytesIO()
        with ZipFile(memory_file, mode="w", compression=ZIP_DEFLATED) as zf:
            zf.write(net_xml_path, arcname="network.net.xml")
            zf.write(geojson_path, arcname="network.geojson")
            zf.writestr("metadata.json", json.dumps(metadata, indent=2))
        memory_file.seek(0)

        return send_file(
            memory_file,
            mimetype="application/zip",
            as_attachment=True,
            download_name="network.zip",
        )

    except subprocess.CalledProcessError as e:
        return json_error_response(e, 500)
    except Exception as e:
        return json_error_response(e, 500)


def main():
    """Main entry point for the scenario generator backend."""
    ensure_env()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main() 