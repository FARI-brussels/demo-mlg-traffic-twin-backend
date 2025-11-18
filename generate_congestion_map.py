"""
Generate Congestion Map Data from SUMO EdgeData

This script processes SUMO edgedata output and network.net.xml to create
a color-coded traffic congestion map with speed/congestion metrics per edge.
Only outputs edges with data, coordinates converted to lon/lat.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import statistics
import sys


def parse_edgedata_summary(edgedata_path: Path) -> Dict[str, Dict[str, float]]:
    """Parse edgedata XML and calculate average metrics per edge.
    
    Uses streaming XML parsing to reduce memory and improve performance.
    
    Returns:
        Dictionary mapping edge_id -> {avg_speed, avg_density, avg_occupancy, avg_travel_time}
    """
    if not edgedata_path.exists():
        return {}

    # Accumulators per edge for weighted and unweighted stats
    # Each value holds running sums for both weighted-by-sampledSeconds and unweighted fallbacks
    edge_accum: Dict[str, Dict[str, float]] = {}

    # Stream parse to avoid loading entire XML into memory
    context = ET.iterparse(str(edgedata_path), events=("end",))
    for event, elem in context:
        if elem.tag == "edge":
            edge_id = elem.get('id')
            if edge_id:
                try:
                    speed = float(elem.get('speed', 0.0))
                    density = float(elem.get('density', 0.0))
                    occupancy = float(elem.get('occupancy', 0.0))
                    traveltime = float(elem.get('traveltime', 0.0))
                    sampled_seconds = float(elem.get('sampledSeconds', 0.0))
                except ValueError:
                    speed = density = occupancy = traveltime = sampled_seconds = 0.0

                acc = edge_accum.get(edge_id)
                if acc is None:
                    acc = {
                        'sum_sampled': 0.0,
                        'sum_speed_w': 0.0,
                        'sum_density_w': 0.0,
                        'sum_occupancy_w': 0.0,
                        'sum_traveltime_w': 0.0,
                        'count': 0.0,
                        'sum_speed': 0.0,
                        'sum_density': 0.0,
                        'sum_occupancy': 0.0,
                        'sum_traveltime': 0.0,
                    }
                    edge_accum[edge_id] = acc

                acc['sum_sampled'] += sampled_seconds
                acc['sum_speed_w'] += speed * sampled_seconds
                acc['sum_density_w'] += density * sampled_seconds
                acc['sum_occupancy_w'] += occupancy * sampled_seconds
                acc['sum_traveltime_w'] += traveltime * sampled_seconds

                acc['count'] += 1.0
                acc['sum_speed'] += speed
                acc['sum_density'] += density
                acc['sum_occupancy'] += occupancy
                acc['sum_traveltime'] += traveltime

            elem.clear()
        elif elem.tag == "interval":
            # Clear interval nodes as we go to free memory
            elem.clear()

    # Calculate averages per edge
    edge_summary: Dict[str, Dict[str, float]] = {}
    for edge_id, acc in edge_accum.items():
        total_sampled = acc['sum_sampled']

        if total_sampled > 0:
            avg_speed = acc['sum_speed_w'] / total_sampled
            avg_density = acc['sum_density_w'] / total_sampled
            avg_occupancy = acc['sum_occupancy_w'] / total_sampled
            avg_traveltime = acc['sum_traveltime_w'] / total_sampled
        else:
            count = acc['count'] if acc['count'] > 0 else 1.0
            avg_speed = acc['sum_speed'] / count
            avg_density = acc['sum_density'] / count
            avg_occupancy = acc['sum_occupancy'] / count
            avg_traveltime = acc['sum_traveltime'] / count

        edge_summary[edge_id] = {
            'avg_speed_ms': avg_speed,
            'avg_speed_kmh': avg_speed * 3.6,  # Convert m/s to km/h
            'avg_density': avg_density,
            'avg_occupancy': avg_occupancy,
            'avg_traveltime': avg_traveltime,
            'total_sampled_seconds': total_sampled
        }

    return edge_summary


def parse_network_location(network_xml_path: Path) -> Tuple[Tuple[float, float], str]:
    """Parse network location info for coordinate conversion.
    
    Returns:
        (netOffset_x, netOffset_y), projParameter
    """
    tree = ET.parse(str(network_xml_path))
    root = tree.getroot()
    location = root.find('location')
    
    if location is None:
        return (0.0, 0.0), "+proj=longlat +datum=WGS84"
    
    # Parse netOffset: "-588162.19,-5625039.79"
    net_offset_str = location.get('netOffset', '0.0,0.0')
    offset_parts = net_offset_str.split(',')
    net_offset = (float(offset_parts[0]), float(offset_parts[1]))
    
    # Parse projection
    proj_param = location.get('projParameter', '+proj=longlat +datum=WGS84')
    
    return net_offset, proj_param


def convert_shape_to_lonlat(shape_str: str, net_offset: Tuple[float, float], proj_param: str, transformer: Optional[Any] = None) -> List[List[float]]:
    """Convert SUMO shape string to lon/lat coordinates.
    
    Args:
        shape_str: Space-separated x,y pairs like "100.0,200.0 101.0,201.0"
        net_offset: (offset_x, offset_y) from network location
        proj_param: Projection parameter string
        transformer: Optional cached pyproj.Transformer
    
    Returns:
        List of [lon, lat] coordinate pairs
    """
    try:
        if transformer is None:
            from pyproj import Transformer
            # Create transformer from UTM to WGS84
            transformer = Transformer.from_crs(proj_param, "EPSG:4326", always_xy=True)
        
        coords: List[List[float]] = []
        offset_x, offset_y = net_offset
        for point in shape_str.split():
            x_str, y_str = point.split(',')
            x_utm = float(x_str) + offset_x
            y_utm = float(y_str) + offset_y
            lon, lat = transformer.transform(x_utm, y_utm)
            coords.append([lon, lat])
        return coords
    except ImportError:
        # Fallback: return raw coordinates if pyproj not available
        sys.stderr.write("Warning: pyproj not available, coordinates may be incorrect\n")
        coords: List[List[float]] = []
        for point in shape_str.split():
            x_str, y_str = point.split(',')
            coords.append([float(x_str), float(y_str)])
        return coords
    except Exception as e:
        sys.stderr.write(f"Warning: coordinate conversion failed: {e}\n")
        return []


def parse_network_edges(network_xml_path: Path, include_edge_ids: Optional[set] = None) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Parse network.net.xml and extract edge information.
    
    Optionally filters to only edges in include_edge_ids. Returns both the
    parsed edges and the total count of non-internal edges in the network.
    
    Returns:
        (edges_dict, total_non_internal_edges)
    """
    net_offset, proj_param = parse_network_location(network_xml_path)

    # Prepare a cached transformer if pyproj is available
    transformer: Optional[Any] = None
    try:
        from pyproj import Transformer  # type: ignore
        transformer = Transformer.from_crs(proj_param, "EPSG:4326", always_xy=True)
    except Exception:
        transformer = None

    edges: Dict[str, Dict[str, Any]] = {}
    total_non_internal_edges = 0
    
    # Use iterparse for memory efficiency
    context = ET.iterparse(str(network_xml_path), events=("start", "end"))
    _, _ = next(context)  # Skip root
    
    for event, elem in context:
        if event == "end" and elem.tag == "edge":
            edge_id = elem.get("id")
            
            # Skip internal edges (junctions)
            if not edge_id or edge_id.startswith(":"):
                elem.clear()
                continue

            # Count all real edges regardless of filter
            total_non_internal_edges += 1

            # If a filter is provided and this edge is not needed, skip heavy work
            if include_edge_ids is not None and edge_id not in include_edge_ids:
                elem.clear()
                continue
            
            # Get first lane to extract shape and speed
            lanes = elem.findall("lane")
            if not lanes:
                elem.clear()
                continue
            
            first_lane = lanes[0]
            shape_str = first_lane.get("shape")
            speed_str = first_lane.get("speed")
            
            if not shape_str or not speed_str:
                elem.clear()
                continue
            
            try:
                max_speed_ms = float(speed_str)
                max_speed_kmh = max_speed_ms * 3.6
                
                # Convert coordinates to lon/lat
                coordinates = convert_shape_to_lonlat(shape_str, net_offset, proj_param, transformer)
                
                if len(coordinates) >= 2:
                    edges[edge_id] = {
                        'geometry': {
                            'type': 'LineString',
                            'coordinates': coordinates
                        },
                        'max_speed_ms': max_speed_ms,
                        'max_speed_kmh': max_speed_kmh
                    }
            except (ValueError, Exception):
                pass
            
            elem.clear()
    
    sys.stderr.write(f"Loaded {len(edges)} edges (of {total_non_internal_edges} total) from network.net.xml\n")
    return edges, total_non_internal_edges


def classify_congestion(speed_kmh: float, max_speed: float = 50.0) -> Dict[str, Any]:
    """Classify congestion level based on speed.
    
    Args:
        speed_kmh: Average speed in km/h
        max_speed: Maximum expected speed for normalization
    
    Returns:
        Dictionary with congestion level, color, and category
    """
    if speed_kmh <= 0:
        return {
            'level': 'no_data',
            'category': 'No Data',
            'color': '#808080',
            'speed_ratio': 0
        }
    
    # Speed ratio (0-1, where 1 is free flow)
    speed_ratio = min(speed_kmh / max_speed, 1.0)
    
    # Classification based on speed ratio
    if speed_ratio >= 0.8:
        # Free flow: 80-100% of max speed
        category = 'free_flow'
        color = '#22c55e'  # Green
        label = 'Free Flow'
    elif speed_ratio >= 0.6:
        # Light congestion: 60-80% of max speed
        category = 'light'
        color = '#84cc16'  # Light green
        label = 'Light Traffic'
    elif speed_ratio >= 0.4:
        # Moderate congestion: 40-60% of max speed
        category = 'moderate'
        color = '#eab308'  # Yellow
        label = 'Moderate Congestion'
    elif speed_ratio >= 0.2:
        # Heavy congestion: 20-40% of max speed
        category = 'heavy'
        color = '#f97316'  # Orange
        label = 'Heavy Congestion'
    else:
        # Severe congestion: < 20% of max speed
        category = 'severe'
        color = '#ef4444'  # Red
        label = 'Severe Congestion'
    
    return {
        'level': category,
        'category': label,
        'color': color,
        'speed_ratio': speed_ratio
    }


def generate_congestion_geojson(
    network_xml_path: Path,
    edgedata_path: Path,
    output_path: Path
) -> Dict[str, Any]:
    """Generate a GeoJSON with congestion data for visualization.
    Only outputs edges that have traffic data.
    
    Args:
        network_xml_path: Path to network.net.xml file
        edgedata_path: Path to SUMO edgedata XML
        output_path: Path to write output GeoJSON
    
    Returns:
        Statistics about the congestion data
    """
    # Parse edge data from simulation first (to know which edges we need)
    edge_data = parse_edgedata_summary(edgedata_path)
    needed_edge_ids = set(edge_data.keys())

    # Parse network edges (only those that have data)
    network_edges, total_edges_in_network = parse_network_edges(network_xml_path, include_edge_ids=needed_edge_ids)
    
    # Statistics
    stats = {
        'total_edges_in_network': total_edges_in_network,
        'edges_with_data': 0,
        'avg_speed_kmh': 0,
        'congestion_distribution': {
            'free_flow': 0,
            'light': 0,
            'moderate': 0,
            'heavy': 0,
            'severe': 0,
            'no_data': 0
        }
    }
    
    # Build output features - only for edges with data
    output_features = []
    speeds: List[float] = []
    
    for edge_id, data in edge_data.items():
        # Skip if edge not in network
        if edge_id not in network_edges:
            continue
        
        edge_info = network_edges[edge_id]
        speed_kmh = data['avg_speed_kmh']
        speeds.append(speed_kmh)
        
        # Classify congestion
        max_speed_kmh = edge_info['max_speed_kmh']
        congestion = classify_congestion(speed_kmh, max_speed_kmh)
        
        stats['edges_with_data'] += 1
        stats['congestion_distribution'][congestion['level']] += 1
        
        # Create feature
        feature = {
            'type': 'Feature',
            'geometry': edge_info['geometry'],
            'properties': {
                'id': edge_id,
                'avg_speed_kmh': round(speed_kmh, 2),
                'max_speed_kmh': round(max_speed_kmh, 2),
                'avg_density': round(data['avg_density'], 2),
                'avg_occupancy': round(data['avg_occupancy'], 2),
                'avg_traveltime': round(data['avg_traveltime'], 2),
                'congestion_level': congestion['level'],
                'congestion_category': congestion['category'],
                'congestion_color': congestion['color'],
                'speed_ratio': round(congestion['speed_ratio'], 3)
            }
        }
        output_features.append(feature)
    
    # Calculate average speed
    if speeds:
        stats['avg_speed_kmh'] = round(statistics.mean(speeds), 2)
    
    # Create output GeoJSON
    output_geojson = {
        'type': 'FeatureCollection',
        'metadata': {
            'description': 'Traffic congestion map (edges with data only)',
            'generated_from': str(edgedata_path.name),
            'statistics': stats
        },
        'features': output_features
    }
    
    # Write to file
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_geojson, f, indent=2)
    
    return stats


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print("Usage: python generate_congestion_map.py <network.net.xml> <edgedata.xml> <output.geojson>")
        sys.exit(1)
    
    network_path = Path(sys.argv[1])
    edgedata_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])
    
    stats = generate_congestion_geojson(network_path, edgedata_path, output_path)
    
    print(f"\nCongestion Map Generated: {output_path}")
    print(f"Total edges in network: {stats['total_edges_in_network']}")
    print(f"Edges with data: {stats['edges_with_data']}")
    print(f"Average speed: {stats['avg_speed_kmh']} km/h")
    print("\nCongestion Distribution:")
    for level, count in stats['congestion_distribution'].items():
        print(f"  {level}: {count}")


