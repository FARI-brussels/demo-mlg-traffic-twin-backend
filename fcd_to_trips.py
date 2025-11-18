#!/usr/bin/env python3
import sys
import json
import math
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Callable
import tempfile
import os


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert SUMO FCD XML to Deck.gl TripsLayer JSON")
    parser.add_argument("input_xml", help="Path to SUMO FCD XML (e.g., fcd_with.out.xml)")
    parser.add_argument("output_json", help="Output JSON path (e.g., frontend/trips_with.json)")
    parser.add_argument("--min-points", type=int, default=2, help="Minimum points per trip to include")
    parser.add_argument("--dedupe", action="store_true", help="Remove consecutive duplicate coordinates")
    parser.add_argument("--network-xml", dest="network_xml", default=None, help="Path to network.net.xml for lane speed limits")
    # New metadata args
    parser.add_argument("--insertion-rate", dest="insertion_rate", type=int, default=None, help="Insertion rate used in simulation")
    parser.add_argument(
        "--closed-edges",
        dest="closed_edges",
        default=None,
        help="Comma-separated closed edge IDs; pass empty string for none",
    )
    return parser.parse_args()


def almost_equal(a: float, b: float, eps: float = 1e-9) -> bool:
    return abs(a - b) <= eps


def _build_lane_speed_map(network_xml_path: Optional[str]) -> Dict[str, float]:
    """
    Parse network.net.xml and build a dictionary mapping lane_id -> max_speed.
    Returns empty dict if file is not provided or parsing fails.
    """
    if not network_xml_path:
        return {}
    
    lane_speeds: Dict[str, float] = {}
    
    try:
        # Use iterparse for memory efficiency with large network files
        context = ET.iterparse(network_xml_path, events=("start", "end"))
        _, _ = next(context)  # Skip root element
        
        for event, elem in context:
            if event == "end" and elem.tag == "edge":
                # Process all lanes in this edge
                for lane in elem.findall("lane"):
                    lane_id = lane.get("id")
                    speed_str = lane.get("speed")
                    if lane_id and speed_str:
                        try:
                            lane_speeds[lane_id] = float(speed_str)
                        except ValueError:
                            pass
                # Clear element to free memory
                elem.clear()
        
        sys.stderr.write(f"Loaded {len(lane_speeds)} lane speed limits from network.net.xml\n")
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to parse network XML: {e}\n")
        return {}
    
    return lane_speeds


def convert(input_xml: str, output_json: str, min_points: int = 2, dedupe: bool = False, network_xml: Optional[str] = None, insertion_rate: Optional[int] = None, closed_edges: Optional[List[str]] = None, max_vehicles_in_memory: int = 10000) -> None:
    # tracks[vehicle_id] -> list of (lon, lat, time, speed, angle, lane_id)
    tracks: Dict[str, List[Tuple[float, float, float, float, float, str]]] = defaultdict(list)
    
    # Track last seen time for each vehicle to identify inactive ones
    last_seen: Dict[str, float] = {}
    
    # Temporary file to store completed trips
    temp_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl', encoding='utf-8')
    temp_filename = temp_file.name

    # Streaming parse to keep memory reasonable
    # The structure is <fcd-export> -> <timestep time="..."> -> <vehicle id="..." x="..." y="..." speed="..." .../>
    context = ET.iterparse(input_xml, events=("start", "end"))
    _, _ = next(context)  # get root element

    num_timesteps = 0
    num_points = 0
    num_flushed = 0
    current_time = 0.0
    time_window = 60.0  # Flush vehicles not seen in last 60 seconds

    def flush_inactive_vehicles(current_t: float, force_all: bool = False):
        nonlocal num_flushed
        to_remove = []
        for vid, last_t in list(last_seen.items()):
            if force_all or (current_t - last_t > time_window):
                to_remove.append(vid)
        
        for vid in to_remove:
            if vid in tracks:
                path = tracks[vid]
                # Write as JSON line
                temp_file.write(json.dumps({"id": vid, "path": path}) + "\n")
                del tracks[vid]
                del last_seen[vid]
                num_flushed += 1

    for event, elem in context:
        if event == "end" and elem.tag == "timestep":
            time_attr = elem.get("time")
            if time_attr is None:
                elem.clear()
                continue
            try:
                t = float(time_attr)
                current_time = t
            except ValueError:
                elem.clear()
                continue

            for veh in elem.findall("vehicle"):
                vid = veh.get("id")
                xs = veh.get("x")
                ys = veh.get("y")
                ss = veh.get("speed")
                ang = veh.get("angle")
                lane = veh.get("lane", "")  # Extract lane ID
                if vid is None or xs is None or ys is None:
                    continue
                try:
                    lon = float(xs)
                    lat = float(ys)
                    spd = float(ss) if ss is not None else 0.0
                    angle = float(ang) if ang is not None else 0.0
                except ValueError:
                    continue

                pt = (lon, lat, t, spd, angle, lane)
                if dedupe and tracks[vid]:
                    last = tracks[vid][-1]
                    if (almost_equal(last[0], lon) and
                        almost_equal(last[1], lat) and
                        almost_equal(last[2], t) and
                        almost_equal(last[3], spd) and
                        almost_equal(last[4], angle) and
                        last[5] == lane):
                        continue
                tracks[vid].append(pt)
                last_seen[vid] = t
                num_points += 1

            num_timesteps += 1
            
            # Periodically flush inactive vehicles to temp file
            if len(tracks) > max_vehicles_in_memory:
                flush_inactive_vehicles(current_time)
                sys.stderr.write(f"Flushed to temp file. Active vehicles: {len(tracks)}\n")
            
            # free memory as we go
            elem.clear()

    # Flush all remaining vehicles
    flush_inactive_vehicles(current_time, force_all=True)
    temp_file.close()

    # Build lane speed map from network.net.xml
    lane_speeds = _build_lane_speed_map(network_xml)

    # Now read back from temp file and build final output
    trips = []
    kept = 0
    
    with open(temp_filename, 'r', encoding='utf-8') as f:
        for line in f:
            trip_data = json.loads(line)
            vid = trip_data["id"]
            path = trip_data["path"]
            
            if len(path) < min_points:
                continue
                
            out_path = []
            for (lon, lat, t, spd, angle, lane_id) in path:
                # Direct lookup of max speed using lane ID
                vmax = lane_speeds.get(lane_id) if lane_speeds else None
                ratio = (spd / vmax) if (vmax is not None and vmax > 1e-6) else 0.0
                out_path.append([lon, lat, t, spd, ratio, angle])
            
            trips.append({
                "id": str(vid),
                "path": out_path,
            })
            kept += 1

    # Clean up temp file
    os.unlink(temp_filename)

    # Compose output with metadata
    metadata = {
        "insertion_rate": insertion_rate,
        "closed_edges": list(closed_edges) if closed_edges else [],
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump({"metadata": metadata, "trips": trips}, f, separators=(",", ":"))

    # Print a brief report to stderr
    sys.stderr.write(f"Parsed timesteps: {num_timesteps}\n")
    sys.stderr.write(f"Collected points: {num_points}\n")
    sys.stderr.write(f"Vehicles flushed: {num_flushed}, trips kept: {kept}\n")



def main() -> None:
    args = parse_args()
    # Parse closed edges string into list
    closed_edges_list: List[str] = []
    if args.closed_edges:
        closed_edges_list = [s for s in args.closed_edges.split(",") if s]
    convert(
        args.input_xml,
        args.output_json,
        min_points=args.min_points,
        dedupe=args.dedupe,
        network_xml=args.network_xml,
        insertion_rate=args.insertion_rate,
        closed_edges=closed_edges_list,
    )


if __name__ == "__main__":
    import time
    start_time = time.time()
    main() 
    end_time = time.time()
    print(f"Execution time: {end_time - start_time:.2f} seconds")