"""
Merge tunnel slopes and underground segments in a SUMO network.

Many SUMO networks generated from OSM split tunnels into three consecutive edges
per driving direction: the entry slope, the underground tunnel section, and the
exit slope. Movement between those sections is mediated by tiny artificial
junctions that only serve to connect the split edges. This module provides the
core logic to collapse such triplets into a single edge while preserving
per-lane geometry and metadata.
"""

from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from copy import deepcopy
from typing import Iterable, List, Optional, Tuple
import xml.etree.ElementTree as ET

Coord = Tuple[float, float]


def parse_shape(shape_str: Optional[str]) -> List[Coord]:
    if not shape_str:
        return []
    coords: List[Coord] = []
    for token in shape_str.strip().split():
        if not token:
            continue
        x_str, y_str = token.split(",")
        coords.append((float(x_str), float(y_str)))
    return coords


def format_shape(points: Iterable[Coord]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def polyline_length(points: Iterable[Coord]) -> float:
    pts = list(points)
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        total += math.hypot(x2 - x1, y2 - y1)
    return total


def append_points(base: List[Coord], addition: Iterable[Coord], tolerance: float = 0.5) -> None:
    points = list(addition)
    if not points:
        return
    if not base:
        base.extend(points)
        return
    if math.hypot(base[-1][0] - points[0][0], base[-1][1] - points[0][1]) <= tolerance:
        base.extend(points[1:])
    else:
        base.extend(points)


def get_edge_shape(edge: ET.Element) -> List[Coord]:
    shape_attr = edge.attrib.get("shape")
    if shape_attr:
        return parse_shape(shape_attr)
    lane_shapes = [parse_shape(lane.attrib.get("shape")) for lane in edge.findall("lane")]
    lane_shapes = [shape for shape in lane_shapes if shape]
    if not lane_shapes:
        return []
    first_len = len(lane_shapes[0])
    if all(len(shape) == first_len for shape in lane_shapes):
        averaged: List[Coord] = []
        for pts in zip(*lane_shapes):
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            averaged.append((sum(xs) / len(xs), sum(ys) / len(ys)))
        return averaged
    return lane_shapes[0]


def lane_attribute_union(lanes: Iterable[ET.Element], attr: str) -> Optional[str]:
    values = []
    for lane in lanes:
        val = lane.attrib.get(attr)
        if not val:
            continue
        if attr in {"disallow", "allow"}:
            tokens = val.split()
            for token in tokens:
                if token not in values:
                    values.append(token)
        else:
            if val not in values:
                values.append(val)
    if not values:
        return None
    if attr in {"disallow", "allow"}:
        return " ".join(values)
    return values[0]


def collect_lane_params(lanes: Iterable[ET.Element]) -> OrderedDict[str, str]:
    merged: OrderedDict[str, str] = OrderedDict()
    for lane in lanes:
        for param in lane.findall("param"):
            key = param.attrib.get("key")
            if not key:
                continue
            if key == "origId":
                continue
            val = param.attrib.get("value", "")
            if key not in merged:
                merged[key] = val
    return merged


def merge_orig_ids(lanes: Iterable[ET.Element]) -> Optional[str]:
    seen: OrderedDict[str, None] = OrderedDict()
    for lane in lanes:
        for param in lane.findall("param"):
            if param.attrib.get("key") != "origId":
                continue
            for token in param.attrib.get("value", "").split():
                seen.setdefault(token, None)
    if not seen:
        return None
    return " ".join(seen.keys())


def build_lane(lanes: List[ET.Element]) -> ET.Element:
    base_lane = lanes[0]
    new_lane = deepcopy(base_lane)
    merged_points: List[Coord] = []
    for lane in lanes:
        append_points(merged_points, parse_shape(lane.attrib.get("shape")))
    if merged_points:
        new_lane.attrib["shape"] = format_shape(merged_points)
        new_lane.attrib["length"] = f"{polyline_length(merged_points):.2f}"
    speeds = [float(lane.attrib["speed"]) for lane in lanes if "speed" in lane.attrib]
    if speeds:
        new_lane.attrib["speed"] = f"{min(speeds):.2f}"
    for key in ("disallow", "allow"):
        union_val = lane_attribute_union(lanes, key)
        if union_val:
            new_lane.attrib[key] = union_val
        elif key in new_lane.attrib:
            del new_lane.attrib[key]
    merged_params = collect_lane_params(lanes)
    merged_orig = merge_orig_ids(lanes)
    for param in list(new_lane.findall("param")):
        new_lane.remove(param)
    for key, val in merged_params.items():
        param_elem = ET.Element("param", {"key": key, "value": val})
        new_lane.append(param_elem)
    if merged_orig:
        new_lane.append(ET.Element("param", {"key": "origId", "value": merged_orig}))
    return new_lane


def has_param(edge: ET.Element, key: str, value: Optional[str] = None) -> bool:
    for param in edge.findall("param"):
        if param.attrib.get("key") != key:
            continue
        if value is None or param.attrib.get("value") == value:
            return True
    return False


def merge_edge_params(edge: ET.Element, sources: List[ET.Element]) -> None:
    merged: OrderedDict[str, str] = OrderedDict()
    for source in sources:
        for param in source.findall("param"):
            key = param.attrib.get("key")
            if not key or key == "lanes":
                continue
            if key not in merged:
                merged[key] = param.attrib.get("value", "")
    for param in list(edge.findall("param")):
        edge.remove(param)
    for key, val in merged.items():
        edge.append(ET.Element("param", {"key": key, "value": val}))


def update_lanes(edge: ET.Element, lane_groups: List[List[ET.Element]]) -> None:
    for lane in list(edge.findall("lane")):
        edge.remove(lane)
    for group in lane_groups:
        edge.append(build_lane(group))


def replace_junction_incoming(junction: ET.Element, old_edge_id: str, new_edge_id: str) -> None:
    inc = junction.attrib.get("incLanes")
    if not inc:
        return
    tokens = inc.split()
    changed = False
    for idx, token in enumerate(tokens):
        if token.startswith(f"{old_edge_id}_"):
            suffix = token[len(old_edge_id) + 1 :]
            tokens[idx] = f"{new_edge_id}_{suffix}"
            changed = True
    if changed:
        junction.attrib["incLanes"] = " ".join(tokens)


def dedupe_connections(root: ET.Element) -> None:
    seen = set()
    for conn in list(root.findall("connection")):
        key = (
            conn.attrib.get("from"),
            conn.attrib.get("to"),
            conn.attrib.get("fromLane"),
            conn.attrib.get("toLane"),
            conn.attrib.get("via"),
        )
        if key in seen:
            root.remove(conn)
        else:
            seen.add(key)


def build_adjacency(edges: dict[str, ET.Element]) -> Tuple[defaultdict[str, List[ET.Element]], defaultdict[str, List[ET.Element]]]:
    incoming: defaultdict[str, List[ET.Element]] = defaultdict(list)
    outgoing: defaultdict[str, List[ET.Element]] = defaultdict(list)
    for edge in edges.values():
        if edge.get("function"):
            continue
        frm = edge.attrib.get("from")
        to = edge.attrib.get("to")
        if frm:
            outgoing[frm].append(edge)
        if to:
            incoming[to].append(edge)
    return incoming, outgoing


def build_internal_edge_index(edges: dict[str, ET.Element]) -> defaultdict[str, List[str]]:
    mapping: defaultdict[str, List[str]] = defaultdict(list)
    for edge_id in edges:
        if not edge_id.startswith(":"):
            continue
        node_id = edge_id.split("_", 1)[0][1:]
        mapping[node_id].append(edge_id)
    return mapping


def merge_triplet(
    root: ET.Element,
    edges: dict[str, ET.Element],
    junctions: dict[str, ET.Element],
    internal_edges: defaultdict[str, List[str]],
    triplet: Tuple[str, str, str],
) -> Tuple[str, str, str]:
    edge_in_id, edge_mid_id, edge_out_id = triplet
    edge_in = edges[edge_in_id]
    edge_mid = edges[edge_mid_id]
    edge_out = edges[edge_out_id]
    node_start = edge_mid.attrib["from"]
    node_end = edge_mid.attrib["to"]
    final_node = edge_out.attrib["to"]
    lane_in = edge_in.findall("lane")
    lane_mid = edge_mid.findall("lane")
    lane_out = edge_out.findall("lane")
    lane_count = len(lane_in)
    lane_groups: List[List[ET.Element]] = [
        [lane_in[idx], lane_mid[idx], lane_out[idx]] for idx in range(lane_count)
    ]
    update_lanes(edge_in, lane_groups)
    merge_edge_params(edge_in, [edge_mid, edge_in, edge_out])
    edge_in.append(ET.Element("param", {"key": "lanes", "value": str(lane_count)}))
    edge_in.attrib["to"] = final_node
    for attr_key, attr_val in edge_mid.attrib.items():
        if attr_key in {"id", "from", "to"}:
            continue
        if attr_key == "shape":
            continue
        edge_in.attrib[attr_key] = attr_val
    merged_shape: List[Coord] = []
    for shape in (get_edge_shape(edge_in), get_edge_shape(edge_mid), get_edge_shape(edge_out)):
        append_points(merged_shape, shape)
    if merged_shape:
        edge_in.attrib["shape"] = format_shape(merged_shape)
    replace_junction_incoming(junctions[final_node], edge_out_id, edge_in_id)
    internal_to_remove: set[str] = set()
    for node in (node_start, node_end):
        junction = junctions.pop(node, None)
        if junction is not None:
            root.remove(junction)
        for internal_id in internal_edges.pop(node, []):
            internal_edge = edges.pop(internal_id, None)
            if internal_edge is not None:
                root.remove(internal_edge)
                internal_to_remove.add(internal_id)
    edges.pop(edge_mid_id, None)
    edges.pop(edge_out_id, None)
    root.remove(edge_mid)
    root.remove(edge_out)
    for conn in list(root.findall("connection")):
        frm = conn.attrib.get("from")
        to = conn.attrib.get("to")
        via = conn.attrib.get("via")
        if frm in internal_to_remove or to in internal_to_remove:
            root.remove(conn)
            continue
        if frm == edge_out_id:
            conn.attrib["from"] = edge_in_id
        if frm == edge_mid_id or to == edge_mid_id or to == edge_out_id:
            root.remove(conn)
            continue
        if via and (via.startswith(f":{node_start}") or via.startswith(f":{node_end}")):
            root.remove(conn)
            continue
    dedupe_connections(root)
    return edge_in_id, edge_mid_id, edge_out_id


def identify_candidates(
    edges: dict[str, ET.Element],
    junctions: dict[str, ET.Element],
) -> List[Tuple[str, str, str]]:
    incoming, outgoing = build_adjacency(edges)
    candidates: List[Tuple[str, str, str]] = []
    for edge_id, edge in edges.items():
        if edge.get("function"):
            continue
        if not has_param(edge, "tunnel", "yes"):
            continue
        lane_count = len(edge.findall("lane"))
        if lane_count == 0:
            continue
        start = edge.attrib.get("from")
        end = edge.attrib.get("to")
        if not start or not end:
            continue
        if start not in junctions or end not in junctions:
            continue
        incoming_edges = [e for e in incoming[start] if e.attrib.get("id") != edge_id]
        outgoing_edges_from_start = [e for e in outgoing[start] if e.attrib.get("id") != edge_id]
        outgoing_edges = [e for e in outgoing[end] if e.attrib.get("id") != edge_id]
        incoming_edges_to_end = [e for e in incoming[end] if e.attrib.get("id") != edge_id]
        if len(incoming_edges) != 1:
            continue
        if outgoing_edges_from_start:
            continue
        if len(outgoing_edges) != 1:
            continue
        if incoming_edges_to_end:
            continue
        edge_in = incoming_edges[0]
        edge_out = outgoing_edges[0]
        if edge_in.get("function") or edge_out.get("function"):
            continue
        if len(edge_in.findall("lane")) != lane_count or len(edge_out.findall("lane")) != lane_count:
            continue
        candidates.append((edge_in.attrib["id"], edge.attrib["id"], edge_out.attrib["id"]))
    return candidates


def merge_tunnels(tree: ET.ElementTree) -> List[Tuple[str, str, str]]:
    root = tree.getroot()
    edges = {edge.attrib["id"]: edge for edge in root.findall("edge") if "id" in edge.attrib}
    junctions = {j.attrib["id"]: j for j in root.findall("junction") if "id" in j.attrib}
    internal_index = build_internal_edge_index(edges)
    candidates = identify_candidates(edges, junctions)
    merged: List[Tuple[str, str, str]] = []
    for triplet in candidates:
        edge_in_id, edge_mid_id, edge_out_id = triplet
        if edge_in_id not in edges or edge_mid_id not in edges or edge_out_id not in edges:
            continue
        merged_triplet = merge_triplet(root, edges, junctions, internal_index, triplet)
        merged.append(merged_triplet)
    return merged

