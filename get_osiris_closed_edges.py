"""
Closed Edges Detection from mobility.brussels API

This module provides functionality to fetch traffic events from
the mobility.brussels WFS API and map them to closed edges in
a SUMO network.
"""

import json
import urllib.request
from typing import List, Dict, Any, Optional


def http_get_json(url: str) -> Dict[str, Any]:
    """Fetch JSON data from a URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "scenario-generator-tt/0.1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        data = resp.read()
        return json.loads(data)


def point_to_segment_distance(lon: float, lat: float, p1: List[float], p2: List[float]) -> float:
    """Calculate the distance from a point to a line segment."""
    x, y = lon, lat
    x1, y1 = p1[0], p1[1]
    x2, y2 = p2[0], p2[1]
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        # p1 == p2
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((x - proj_x) ** 2 + (y - proj_y) ** 2) ** 0.5


def nearest_edge_feature(lon: float, lat: float, net_geojson: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find the nearest edge feature to a given point in a network GeoJSON."""
    best_feat = None
    best_dist = float("inf")
    for feat in net_geojson.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        for i in range(len(coords) - 1):
            d = point_to_segment_distance(lon, lat, coords[i], coords[i + 1])
            if d < best_dist:
                best_dist = d
                best_feat = feat
    return best_feat


def parse_edge_id(raw_id: Any) -> Optional[str]:
    """Parse an edge ID from various input types."""
    try:
        if raw_id is None:
            return None
        return str(raw_id)
    except Exception:
        return None


def invert_edge_id(edge_id: str) -> Optional[str]:
    """Invert the direction of an edge ID.
    
    Handles ids like "510702675#0" => "-510702675#0", 
    "-510702675#1" => "510702675#1"
    """
    try:
        if not edge_id:
            return None
        if edge_id.startswith("-"):
            return edge_id[1:]
        # if it already starts without '-', add it
        return f"-{edge_id}"
    except Exception:
        return None


def edge_id_base(edge_id: str) -> str:
    """Extract the base part of an edge ID (before '#')."""
    if not edge_id:
        return ""
    return edge_id.split('#')[0]


def nearest_inverse_edge(source_edge: Dict[str, Any], net_geojson: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Given a source edge feature, find the closest opposite-direction edge.

    Strategy:
      1) Build target id by inverting sign: id -> -id, preserving suffix like '#i'. 
         If exact id exists, return it.
      2) Otherwise, find all edges with base id inverted (e.g., '-12345#i' for any i) 
         and pick closest by geometry proximity.
      3) If no candidates, return None.
    """
    props = source_edge.get("properties") or {}
    src_id = parse_edge_id(props.get("id") or props.get("osm_id"))
    if not src_id:
        return None

    inverted_exact = invert_edge_id(src_id)
    inverted_base = invert_edge_id(edge_id_base(src_id))

    # Collect candidates, also try for properties.osm_id if present
    candidates: List[Dict[str, Any]] = []
    exact_match: Optional[Dict[str, Any]] = None

    for feat in net_geojson.get("features", []):
        fprops = feat.get("properties") or {}
        fid = parse_edge_id(fprops.get("id") or fprops.get("osm_id"))
        if not fid:
            continue
        if inverted_exact and fid == inverted_exact:
            exact_match = feat
            break
        if inverted_base and edge_id_base(fid) == inverted_base:
            candidates.append(feat)

    if exact_match:
        return exact_match

    # Fallback: nearest among base id-matching candidates
    if not candidates:
        return None

    # Use distance between midpoints of source and candidate
    def line_midpoint(feat: Dict[str, Any]) -> Optional[List[float]]:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            return None
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            return None
        # simple midpoint between endpoints
        x = (coords[0][0] + coords[-1][0]) * 0.5
        y = (coords[0][1] + coords[-1][1]) * 0.5
        return [x, y]

    src_mid = line_midpoint(source_edge)
    best_feat = None
    best_dist = float("inf")
    if src_mid:
        for cand in candidates:
            c_mid = line_midpoint(cand)
            if not c_mid:
                continue
            dx = src_mid[0] - c_mid[0]
            dy = src_mid[1] - c_mid[1]
            d = (dx * dx + dy * dy) ** 0.5
            if d < best_dist:
                best_dist = d
                best_feat = cand
    else:
        # No midpoint; fallback to first candidate
        best_feat = candidates[0]

    return best_feat


def fetch_closed_edges_from_brussels_api(
    net_geojson: Dict[str, Any],
    wfs_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch current traffic events and return closed lanes mapped to network edges.

    - Downloads WFS events from mobility.brussels.
    - Filters for French consequences containing "Les deux directions fermées".
    - Matches each event point to the nearest edge from net.geojson.
    - Also includes the nearest opposite-direction edge based on id inversion.
    - Returns list of edge features with closure metadata.

    Args:
        net_geojson: Network GeoJSON with edge features
        wfs_url: Optional custom WFS URL (uses default if None)

    Returns:
        List of GeoJSON features representing closed edges
    """
    if wfs_url is None:
        wfs_url = "https://data.mobility.brussels/geoserver/bm_traffic/wfs?service=wfs&version=1.1.0&request=GetFeature&typeName=bm_traffic:events&outputFormat=json&srsName=EPSG:4326"
    
    data = http_get_json(wfs_url)
    features = data.get("features", [])
    closed_edges: List[Dict[str, Any]] = []

    for feat in features:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        consequences_fr = (props.get("consequences_fr") or "").strip()
        is_active = bool(props.get("is_active", True))
        if not is_active:
            continue
        if "Les deux directions fermées" not in consequences_fr:
            continue
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or None
        if not coords or len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        edge_feat = nearest_edge_feature(lon, lat, net_geojson)
        if not edge_feat:
            continue
        # Output for primary edge
        base_props = {
            "closed": True,
            "closure_reason": consequences_fr,
            "event_id": feat.get("id"),
            "importance": props.get("importance"),
            "location_fr": props.get("location_fr"),
            "start_time": props.get("start_time"),
            "end_time": props.get("end_time"),
        }
        # add edge id to properties
        edge_props = edge_feat.get("properties") or {}
        base_props["edge_id"] = str(edge_props.get("id") or edge_props.get("osm_id") or "")
        closed_edges.append({
            "type": "Feature",
            "geometry": edge_feat.get("geometry"),
            "properties": base_props,
        })

        # Attempt to include opposite-direction edge
        inv = nearest_inverse_edge(edge_feat, net_geojson)
        if inv:
            inv_props = inv.get("properties") or {}
            inv_base_props = dict(base_props)
            inv_base_props["edge_id"] = str(inv_props.get("id") or inv_props.get("osm_id") or "")
            closed_edges.append({
                "type": "Feature",
                "geometry": inv.get("geometry"),
                "properties": inv_base_props,
            })

    return closed_edges

