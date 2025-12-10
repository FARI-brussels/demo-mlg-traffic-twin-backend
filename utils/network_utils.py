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