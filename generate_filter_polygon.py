import os
from pathlib import Path


def generate_circle_polygon(center_lon: float, center_lat: float, radius_km: float, num_points: int = 64) -> list:
    """Generate a circular polygon approximation using lat/lon coordinates.
    
    Args:
        center_lon: Center longitude
        center_lat: Center latitude
        radius_km: Radius in kilometers
        num_points: Number of points to approximate the circle
        
    Returns:
        List of (lon, lat) tuples forming the polygon
    """
    import math
    
    # Earth's radius in kilometers
    R = 6371.0
    
    points = []
    for i in range(num_points):
        # Angle in radians
        bearing = 2 * math.pi * i / num_points
        
        # Convert to radians
        lat1 = math.radians(center_lat)
        lon1 = math.radians(center_lon)
        
        # Calculate new point
        lat2 = math.asin(
            math.sin(lat1) * math.cos(radius_km / R) +
            math.cos(lat1) * math.sin(radius_km / R) * math.cos(bearing)
        )
        
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(radius_km / R) * math.cos(lat1),
            math.cos(radius_km / R) - math.sin(lat1) * math.sin(lat2)
        )
        
        # Convert back to degrees
        points.append((math.degrees(lon2), math.degrees(lat2)))
    
    return points


def create_poly_xml(poly_file: Path, shape_id: str, center_lon: float, center_lat: float, radius_km: float, network_file: Path) -> None:
    """Create a .poly.xml file with a circular polygon for FCD filtering.
    
    Args:
        poly_file: Path where to save the .poly.xml file
        shape_id: ID for the polygon shape
        center_lon: Center longitude
        center_lat: Center latitude
        radius_km: Radius in kilometers
        network_file: Path to the network file (needed for coordinate conversion)
    """
    import sys
    sumo_tools = Path(os.environ["SUMO_HOME"]) / "tools"
    if str(sumo_tools) not in sys.path:
        sys.path.append(str(sumo_tools))
    
    import sumolib
    
    # Load the network to get its coordinate system
    net = sumolib.net.readNet(str(network_file))
    
    # Generate circular polygon points in lat/lon
    points_geo = generate_circle_polygon(center_lon, center_lat, radius_km)
    
    # Convert each point from lat/lon to network coordinates
    points_net = []
    for lon, lat in points_geo:
        # sumolib uses (lon, lat) order for geo coordinates
        x, y = net.convertLonLat2XY(lon, lat)
        points_net.append((x, y))
    
    # Log conversion for debugging
    print(f"FCD Filter: Center geo ({center_lon:.6f}, {center_lat:.6f}) -> net ({points_net[0][0]:.2f}, {points_net[0][1]:.2f})")
    print(f"FCD Filter: Radius {radius_km}km with {len(points_net)} points")
    
    # Add all points to shape attribute (network coordinates)
    shape_str = ' '.join(f'{x:.2f},{y:.2f}' for x, y in points_net)
    
    # Create XML content
    xml_lines = [
        '<additional xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/additional_file.xsd">',
        f'    <poly id="{shape_id}" type="fcd_filter" color="1,0,0" fill="1" layer="0" shape="{shape_str}"/>',
        '</additional>'
    ]
    
    with open(poly_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(xml_lines))



