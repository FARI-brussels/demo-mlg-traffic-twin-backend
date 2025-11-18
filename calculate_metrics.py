"""
SUMO Simulation Metrics Calculator

This module calculates various traffic metrics from SUMO simulation outputs:
- Total Delay, Average Travel Time, Throughput, Percentile Travel Times
- Detour Length, Queue Length, CO₂ Emissions, Transit Delay
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Any, List, Optional
import statistics


def parse_tripinfo(tripinfo_path: Path) -> Dict[str, Any]:
    """Parse tripinfo output to extract trip-level statistics.
    
    Returns:
        Dictionary with trip statistics including delays, travel times, etc.
    """
    if not tripinfo_path.exists():
        return {
            "total_vehicles": 0,
            "completed_trips": 0,
            "total_delay_s": 0.0,
            "total_travel_time_s": 0.0,
            "travel_times": [],
            "waiting_times": [],
            "route_lengths": [],
            "total_co2_mg": 0.0,
        }
    
    tree = ET.parse(str(tripinfo_path))
    root = tree.getroot()
    
    travel_times = []
    waiting_times = []
    route_lengths = []
    total_co2 = 0.0
    
    for trip in root.findall('tripinfo'):
        duration = float(trip.get('duration', 0))
        waiting_time = float(trip.get('waitingTime', 0))
        route_length = float(trip.get('routeLength', 0))
        
        travel_times.append(duration)
        waiting_times.append(waiting_time)
        route_lengths.append(route_length)
        
        # CO2 emissions (if available)
        emissions = trip.find('emissions')
        if emissions is not None:
            co2 = float(emissions.get('CO2_abs', 0))
            total_co2 += co2
    
    completed_trips = len(travel_times)
    total_travel_time = sum(travel_times)
    total_delay = sum(waiting_times)
    
    return {
        "completed_trips": completed_trips,
        "total_delay_s": total_delay,
        "total_travel_time_s": total_travel_time,
        "travel_times": travel_times,
        "waiting_times": waiting_times,
        "route_lengths": route_lengths,
        "total_co2_mg": total_co2,
    }


def parse_edgedata(edgedata_path: Path) -> Dict[str, Any]:
    """Parse edge data output for optional downstream use.

    Note: Queue-related metrics were removed from calculation/visualization.
    We keep this function for future extensions but return an empty dict
    to avoid unused/zero-valued fields.
    """
    if not edgedata_path.exists():
        return {}
    # Intentionally return empty; callers should handle missing keys
    return {}


def parse_routes_xml(routes_path: Path) -> int:
    """Parse routes.xml to count total demand (trips generated).
    
    Returns:
        Total number of vehicles/trips in the demand
    """
    if not routes_path.exists():
        return 0
    
    tree = ET.parse(str(routes_path))
    root = tree.getroot()
    
    # Count vehicles in routes file
    vehicles = root.findall('vehicle')
    trips = root.findall('trip')
    
    return len(vehicles) + len(trips)


def calculate_detour_length(
    baseline_route_lengths: List[float],
    scenario_route_lengths: List[float]
) -> float:
    """Calculate average detour length by comparing scenario to baseline.
    
    Args:
        baseline_route_lengths: Route lengths from scenario without closures
        scenario_route_lengths: Route lengths from scenario with closures
    
    Returns:
        Average detour length in km
    """
    if not baseline_route_lengths or not scenario_route_lengths:
        return 0.0
    
    # Match trips by index (assumes same trip IDs/order)
    min_len = min(len(baseline_route_lengths), len(scenario_route_lengths))
    
    detours = []
    for i in range(min_len):
        detour = scenario_route_lengths[i] - baseline_route_lengths[i]
        if detour > 0:  # Only count positive detours
            detours.append(detour / 1000.0)  # Convert m to km
    
    return statistics.mean(detours) if detours else 0.0


def calculate_metrics(
    tripinfo_path: Path,
    edgedata_path: Path,
    routes_path: Path,
    baseline_tripinfo_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Calculate comprehensive traffic metrics from SUMO outputs.
    
    Args:
        tripinfo_path: Path to tripinfo XML output
        edgedata_path: Path to edgedata XML output
        routes_path: Path to routes XML file (demand)
        baseline_tripinfo_path: Optional baseline scenario for detour calculation
    
    Returns:
        Dictionary with all calculated metrics
    """
    # Parse outputs
    trip_data = parse_tripinfo(tripinfo_path)
    edge_data = parse_edgedata(edgedata_path)
    total_demand = parse_routes_xml(routes_path)
    
    # Calculate metrics
    completed_trips = trip_data["completed_trips"]
    total_delay_s = trip_data["total_delay_s"]
    travel_times = trip_data["travel_times"]
    route_lengths = trip_data["route_lengths"]
    
    # 1. Total Delay (veh·h)
    total_delay_vh = total_delay_s / 3600.0 if total_delay_s > 0 else 0.0
    
    # 2. Average Travel Time (min/trip)
    avg_travel_time_min = (
        statistics.mean(travel_times) / 60.0 if travel_times else 0.0
    )
    
    # 3. Throughput Served (% of demand completed)
    throughput_pct = (
        (completed_trips / total_demand * 100.0) if total_demand > 0 else 0.0
    )
    
    # 4. 95th-Percentile Travel Time (reliability)
    percentile_95_min = 0.0
    if travel_times:
        sorted_times = sorted(travel_times)
        idx_95 = int(0.95 * len(sorted_times))
        percentile_95_min = sorted_times[idx_95] / 60.0
    
    # 5. Avg. Detour Length (km) for impacted O-Ds
    avg_detour_km = 0.0
    if baseline_tripinfo_path and baseline_tripinfo_path.exists():
        baseline_data = parse_tripinfo(baseline_tripinfo_path)
        avg_detour_km = calculate_detour_length(
            baseline_data["route_lengths"],
            route_lengths
        )
    
    # 6. CO₂ (kg)
    co2_kg = trip_data["total_co2_mg"] / 1_000_000.0  # Convert mg to kg
    
    # 8. Transit Delay (placeholder - would need transit-specific data)
    transit_delay_vh = 0.0
    
    # Additional useful metrics
    avg_speed_kmh = 0.0
    if travel_times and route_lengths:
        total_distance_km = sum(route_lengths) / 1000.0
        total_time_h = sum(travel_times) / 3600.0
        avg_speed_kmh = total_distance_km / total_time_h if total_time_h > 0 else 0.0
    
    return {
        "total_delay_vh": round(total_delay_vh, 2),
        "avg_travel_time_min": round(avg_travel_time_min, 2),
        "throughput_pct": round(throughput_pct, 2),
        "percentile_95_travel_time_min": round(percentile_95_min, 2),
        "avg_detour_km": round(avg_detour_km, 3),
        "co2_kg": round(co2_kg, 2),
        "transit_delay_vh": round(transit_delay_vh, 2),
        # Additional metrics
        "completed_trips": completed_trips,
        "total_demand": total_demand,
        "avg_speed_kmh": round(avg_speed_kmh, 2),
        "total_distance_km": round(sum(route_lengths) / 1000.0, 2) if route_lengths else 0.0,
    }


def calculate_scenario_comparison(
    with_closure_metrics: Dict[str, Any],
    without_closure_metrics: Dict[str, Any]
) -> Dict[str, Any]:
    """Calculate comparative metrics between scenarios.
    
    Args:
        with_closure_metrics: Metrics from scenario with road closures
        without_closure_metrics: Metrics from baseline scenario
    
    Returns:
        Dictionary with comparative metrics and deltas
    """
    def safe_delta(with_val: float, without_val: float) -> float:
        """Calculate percentage change."""
        if without_val == 0:
            return 0.0
        return ((with_val - without_val) / without_val) * 100.0
    
    return {
        "delay_increase_vh": round(
            with_closure_metrics["total_delay_vh"] - without_closure_metrics["total_delay_vh"], 
            2
        ),
        "delay_increase_pct": round(
            safe_delta(
                with_closure_metrics["total_delay_vh"],
                without_closure_metrics["total_delay_vh"]
            ),
            2
        ),
        "travel_time_increase_min": round(
            with_closure_metrics["avg_travel_time_min"] - without_closure_metrics["avg_travel_time_min"],
            2
        ),
        "travel_time_increase_pct": round(
            safe_delta(
                with_closure_metrics["avg_travel_time_min"],
                without_closure_metrics["avg_travel_time_min"]
            ),
            2
        ),
        "throughput_decrease_pct": round(
            with_closure_metrics["throughput_pct"] - without_closure_metrics["throughput_pct"],
            2
        ),
        "co2_increase_kg": round(
            with_closure_metrics["co2_kg"] - without_closure_metrics["co2_kg"],
            2
        ),
        "co2_increase_pct": round(
            safe_delta(
                with_closure_metrics["co2_kg"],
                without_closure_metrics["co2_kg"]
            ),
            2
        ),
    }


if __name__ == "__main__":
    # Example usage for testing
    import sys
    
    if len(sys.argv) < 4:
        print("Usage: python calculate_metrics.py <tripinfo.xml> <edgedata.xml> <routes.xml> [baseline_tripinfo.xml]")
        sys.exit(1)
    
    tripinfo = Path(sys.argv[1])
    edgedata = Path(sys.argv[2])
    routes = Path(sys.argv[3])
    baseline = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    
    metrics = calculate_metrics(tripinfo, edgedata, routes, baseline)
    
    import json
    print(json.dumps(metrics, indent=2))


