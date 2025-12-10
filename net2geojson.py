#!/usr/bin/python3
# Eclipse SUMO, Simulation of Urban MObility; see https://eclipse.dev/sumo
# Copyright (C) 2007-2025 German Aerospace Center (DLR) and others.
# This program and the accompanying materials are made available under the
# terms of the Eclipse Public License 2.0 which is available at
# https://www.eclipse.org/legal/epl-2.0/
# This Source Code may also be made available under the following Secondary
# Licenses when the conditions for such availability set forth in the Eclipse
# Public License 2.0 are satisfied: GNU General Public License, version 2
# or later which is available at
# https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html
# SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later

# @file    net2geojson.py
# @author  Jakob Erdmann
# @date    2020-05-05
"""
This script converts a sumo network to GeoJSON and optionally includes edgeData.
Enhanced to support polygon lanes with buffering and directional arrows for deck.gl visualization.
"""
from __future__ import absolute_import, print_function

import json
import math
import os
import sys
from collections import defaultdict

from shapely.geometry import LineString, Polygon, MultiPolygon
from shapely import buffer as shapely_buffer
import pyproj

if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))
import sumolib  # noqa
import sumolib.geomhelper as gh


# Global transformer cache - initialized once per network
_transformers = {
    'to_utm': None,
    'to_wgs84': None,
    'utm_crs': None
}


def init_transformers(net):
    """Initialize UTM transformers based on network center. Call once at startup."""
    global _transformers
    
    # Get network bounding box center
    bbox = net.getBoundary()
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    center_lon, center_lat = net.convertXY2LonLat(center_x, center_y)
    
    # Create UTM projection for the network area
    utm_zone = int((center_lon + 180) / 6) + 1
    utm_crs = f"+proj=utm +zone={utm_zone} +{'south' if center_lat < 0 else 'north'} +ellps=WGS84"
    
    _transformers['to_utm'] = pyproj.Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    _transformers['to_wgs84'] = pyproj.Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True)
    _transformers['utm_crs'] = utm_crs


def transform_to_utm(lon, lat):
    """Transform lon/lat to UTM using cached transformer."""
    return _transformers['to_utm'].transform(lon, lat)


def transform_to_wgs84(x, y):
    """Transform UTM to lon/lat using cached transformer."""
    return _transformers['to_wgs84'].transform(x, y)

# Default display properties for different lane types
DISPLAY_PROPERTIES = {
    "lane": {
        "defaultFillColor": [150, 150, 150, 255],  # private lanes
        "busFillColor": [255, 255, 0, 255],        # bus-only lanes
        "tunnelFillColor": [0, 0, 255, 255],       # tunnel lanes
        "closedFillColor": [255, 0, 0, 255],       # closed lanes
        "selectedFillColor": [255, 106, 0, 255],  # selected lane
        "defaultLineColor": [0, 0, 0, 50],
        "hoveredLineColor": [0, 255, 255, 255],
    },
    "junction": {
        "fillColor": [60, 60, 60, 255],
    },
    "arrow": {
        "fillColor": [255, 255, 255, 255],  # white arrows
    },
    "arrow-stem": {
        "lineColor": [255, 255, 255, 255],  # white stem
    },
}


def parse_args():
    op = sumolib.options.ArgumentParser(description="net to geojson",
                                        usage="Usage: " + sys.argv[0] + " -n <net> <options>")
    # input
    op.add_argument("-n", "--net-file", category="input", dest="netFile", required=True, type=op.net_file,
                    help="The .net.xml file to convert")
    op.add_argument("-d", "--edgedata-file", category="input", dest="edgeData", type=op.edgedata_file,
                    help="Optional edgeData to include in the output")
    op.add_argument("-p", "--ptline-file", category="input", dest="ptlines", type=op.file,
                    help="Optional ptline information to include in the output")
    # output
    op.add_argument("-o", "--output-file", dest="outFile", category="output", required=True, type=op.file,
                    help="The geojson output file name")
    # processing
    op.add_argument("-l", "--lanes", action="store_true", default=False,
                    help="Export lane geometries (as polygons with arrows by default)")
    op.add_argument("-e", "--edges", action="store_true", default=False, help="Export edge geometries")
    op.add_argument("--junctions", action="store_true", default=False,
                    help="Export junction geometries")
    op.add_argument("-i", "--internal", action="store_true", default=False,
                    help="Export internal geometries")
    op.add_argument("-j", "--junction-coordinates", dest="junctionCoords", action="store_true", default=False,
                    help="Append junction coordinates to edge shapes")
    op.add_argument("-b", "--boundary", dest="boundary", action="store_true", default=False,
                    help="Export boundary shapes instead of center-lines")
    op.add_argument("-t", "--traffic-lights", action="store_true", default=False, dest="tls",
                    help="Export traffic light geometries")
    op.add_argument("--edgedata-timeline", action="store_true", default=False, dest="edgedataTimeline",
                    help="Exports all time intervals (by default only the first is exported)")
    op.add_argument("-x", "--extra-attributes", action="store_true", default=False, dest="extraAttributes",
                    help="Exports extra attributes from edge and lane "
                         "(such as max speed, number of lanes and allowed vehicles)")
    op.add_argument("--arrow-size", type=float, default=2.5, dest="arrowSize",
                    help="Size of the arrow in meters (default: 2.5)")

    options = op.parse_args()
    if not options.edges and not options.lanes:
        options.edges = True

    return options


def shape2json(net, geometry, isBoundary):
    lonLatGeometry = [net.convertXY2LonLat(x, y) for x, y in geometry]
    coords = [[round(x, 6), round(y, 6)] for x, y in lonLatGeometry]
    if isBoundary:
        coords = [coords]
    return {
        "type": "Polygon" if isBoundary else "LineString",
        "coordinates": coords
    }


def buffer_line_to_polygon(net, geometry, width):
    """
    Buffer a line geometry to create a polygon with the given width.
    Uses cached UTM projection for accurate meter-based buffering.
    """
    if len(geometry) < 2:
        return None
    
    # Convert SUMO coords to UTM directly (faster than going through lon/lat twice)
    utm_coords = []
    for x, y in geometry:
        lon, lat = net.convertXY2LonLat(x, y)
        utm_x, utm_y = transform_to_utm(lon, lat)
        utm_coords.append((utm_x, utm_y))
    
    # Create and buffer LineString in UTM
    line_utm = LineString(utm_coords)
    buffered_utm = shapely_buffer(line_utm, width / 2, cap_style='flat', join_style='mitre')
    
    if buffered_utm.is_empty:
        return None
    
    # Handle MultiPolygon by taking the largest polygon
    if isinstance(buffered_utm, MultiPolygon):
        buffered_utm = max(buffered_utm.geoms, key=lambda p: p.area)
    
    if not isinstance(buffered_utm, Polygon):
        return None
    
    # Transform back to WGS84
    coords = []
    for utm_x, utm_y in buffered_utm.exterior.coords:
        lon, lat = transform_to_wgs84(utm_x, utm_y)
        coords.append([round(lon, 6), round(lat, 6)])
    
    return {
        "type": "Polygon",
        "coordinates": [coords]
    }


def create_arrow_features(net, geometry, width, properties, arrow_size=2.5):
    """
    Create arrow features (triangle head + stem line) at the end of a lane
    to indicate traffic direction. Uses cached UTM transformers.
    """
    if len(geometry) < 2:
        return []
    
    # Convert last two points to UTM for accurate arrow placement
    p1_x, p1_y = geometry[-2]
    p2_x, p2_y = geometry[-1]
    
    p1_lon, p1_lat = net.convertXY2LonLat(p1_x, p1_y)
    p2_lon, p2_lat = net.convertXY2LonLat(p2_x, p2_y)
    
    p1_utm = transform_to_utm(p1_lon, p1_lat)
    p2_utm = transform_to_utm(p2_lon, p2_lat)
    
    # Calculate direction vector
    dx = p2_utm[0] - p1_utm[0]
    dy = p2_utm[1] - p1_utm[1]
    length = math.hypot(dx, dy)
    
    if length < 0.001:
        return []
    
    # Unit vectors
    ux = dx / length
    uy = dy / length
    px = -uy  # perpendicular
    py = ux
    
    # Arrow dimensions (in meters)
    arrow_width = arrow_size * 0.6
    stem_length = arrow_size * 2
    
    # Calculate arrow points in UTM
    # Tip is slightly back from the end point
    tip_x = p2_utm[0] - ux * arrow_size
    tip_y = p2_utm[1] - uy * arrow_size
    stem_base_x = tip_x - ux * stem_length
    stem_base_y = tip_y - uy * stem_length
    left_x = tip_x - ux * arrow_size + px * arrow_width
    left_y = tip_y - uy * arrow_size + py * arrow_width
    right_x = tip_x - ux * arrow_size - px * arrow_width
    right_y = tip_y - uy * arrow_size - py * arrow_width
    
    # Convert back to WGS84
    tip_lon, tip_lat = transform_to_wgs84(tip_x, tip_y)
    stem_base_lon, stem_base_lat = transform_to_wgs84(stem_base_x, stem_base_y)
    left_lon, left_lat = transform_to_wgs84(left_x, left_y)
    right_lon, right_lat = transform_to_wgs84(right_x, right_y)
    
    # Round coordinates once
    tip_coord = [round(tip_lon, 6), round(tip_lat, 6)]
    left_coord = [round(left_lon, 6), round(left_lat, 6)]
    right_coord = [round(right_lon, 6), round(right_lat, 6)]
    stem_base_coord = [round(stem_base_lon, 6), round(stem_base_lat, 6)]
    
    # Arrow head (triangle)
    arrow_feature = {
        "type": "Feature",
        "properties": {**properties, "element": "arrow"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[tip_coord, left_coord, right_coord, tip_coord]]
        }
    }
    
    # Arrow stem (line)
    stem_feature = {
        "type": "Feature",
        "properties": {**properties, "element": "arrow-stem"},
        "geometry": {
            "type": "LineString",
            "coordinates": [stem_base_coord, tip_coord]
        }
    }
    
    return [arrow_feature, stem_feature]


def get_lane_type(allow, tunnel):
    """
    Determine lane type based on permissions and attributes.
    Returns just the type string, not the full display properties.
    """
    allow_list = [s.strip() for s in (allow or "").split(",")]
    
    # Determine lane type for coloring
    if "bus" in allow_list and "private" not in allow_list:
        return "bus"
    elif tunnel == "yes":
        return "tunnel"
    return "private"


def addFeature(options, features, addLanes, arrow_features):
    geomType = 'lane' if addLanes else 'edge'
    
    # Cache edge objects to avoid repeated lookups
    edge_cache = {}
    lane_cache = {}
    
    # Pre-compute which edge IDs have data
    edgeData_keys = set(edgeData.keys())
    ptLines_keys = set(ptLines.keys())
    
    geometries = list(net.getGeometries(addLanes, options.junctionCoords))
    total = len(geometries)
    
    for idx, (id, geometry, width) in enumerate(geometries):
        if idx % 1000 == 0:
            print(f"  Processing {geomType} {idx}/{total}...", file=sys.stderr)
        
        # Get edge/lane objects from cache
        if addLanes:
            if id not in lane_cache:
                lane_cache[id] = net.getLane(id)
            lane = lane_cache[id]
            edge = lane.getEdge()
            edgeID = edge.getID()
        else:
            edgeID = id
            if edgeID not in edge_cache:
                edge_cache[edgeID] = net.getEdge(edgeID)
            edge = edge_cache[edgeID]
        
        # Build properties dict
        props = {
            "element": geomType,
            "id": id,
            "width": width,
        }
        
        # Add edge data if available
        if edgeID in edgeData_keys:
            if options.edgedataTimeline:
                props["edgeData"] = edgeData[edgeID]
            else:
                props.update(edgeData[edgeID][0])

        # Add PT line info if available
        if edgeID in ptLines_keys:
            for ptType, lines in ptLines[edgeID].items():
                props[ptType] = " ".join(sorted(lines))

        if not addLanes or not options.edges:
            props["name"] = edge.getName()
        
        # Get allow and tunnel for lanes (needed for laneType and arrows)
        allow = None
        allow_set = None
        if addLanes:
            allow_set = lane.getPermissions()
            allow = ','.join(sorted(allow_set))
            tunnel = edge._params.get("tunnel")
            props["allow"] = allow
            props["tunnel"] = tunnel
            props["laneType"] = get_lane_type(allow, tunnel)
            props["edgeId"] = edgeID
        
        if options.extraAttributes:
            props["maxSpeed"] = edge.getSpeed()
            if geomType == 'edge':
                props["numLanes"] = edge.getLaneNumber()
                permissions_union = set()
                for lane in edge.getLanes():
                    permissions_union.update(lane.getPermissions())
                props["allow"] = ",".join(sorted(permissions_union))

        # Lanes are always exported as polygons with arrows
        if addLanes:
            polygon_geom = buffer_line_to_polygon(net, geometry, width)
            if polygon_geom:
                # Create arrows for lanes that allow bus or private vehicles
                if allow_set and ("bus" in allow_set or "private" in allow_set):
                    arrows = create_arrow_features(
                        net, geometry, width, 
                        {"laneId": id, "edgeId": edgeID},
                        options.arrowSize
                    )
                    arrow_features.extend(arrows)
                
                features.append({
                    "type": "Feature",
                    "properties": props,
                    "geometry": polygon_geom
                })
        elif options.boundary:
            geometry = gh.line2boundary(geometry, width)
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": shape2json(net, geometry, options.boundary)
            })
        else:
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": shape2json(net, geometry, options.boundary)
            })


if __name__ == "__main__":
    options = parse_args()
    
    print("Loading network...", file=sys.stderr)
    net = sumolib.net.readNet(options.netFile, withInternal=options.internal)
    if not net.hasGeoProj():
        sys.stderr.write("Network does not provide geo projection\n")
        sys.exit(1)
    
    # Initialize UTM transformers once for the entire network
    print("Initializing projections...", file=sys.stderr)
    init_transformers(net)

    edgeData = defaultdict(dict)
    if options.edgeData:
        print("Loading edge data...", file=sys.stderr)
        for i, interval in enumerate(sumolib.xml.parse(options.edgeData, "interval", heterogeneous=True)):
            for edge in interval.edge:
                data = dict(edge.getAttributes())
                data["begin"] = interval.begin
                data["end"] = interval.end
                del data["id"]
                edgeData[edge.id][i] = data
            if not options.edgedataTimeline:
                break

    ptLines = defaultdict(lambda: defaultdict(set))
    if options.ptlines:
        print("Loading PT lines...", file=sys.stderr)
        for ptline in sumolib.xml.parse(options.ptlines, "ptLine", heterogeneous=True):
            if ptline.route:
                for edge in ptline.route[0].edges.split():
                    ptLines[edge][ptline.type].add(ptline.line)

    features = []
    arrow_features = []

    if options.edges:
        print("Processing edges...", file=sys.stderr)
        addFeature(options, features, False, arrow_features)
    if options.lanes:
        print("Processing lanes...", file=sys.stderr)
        addFeature(options, features, True, arrow_features)

    if options.junctions:
        print("Processing junctions...", file=sys.stderr)
        nodes = net.getNodes()
        for idx, junction in enumerate(nodes):
            shape = junction.getShape()
            # Only include junctions with valid polygon shapes (more than 2 points)
            if len(shape) > 2:
                # Convert junction shape to polygon
                coords = []
                for x, y in shape:
                    lon, lat = net.convertXY2LonLat(x, y)
                    coords.append([round(lon, 6), round(lat, 6)])
                
                features.append({
                    "type": "Feature",
                    "properties": {
                        "element": 'junction',
                        "id": junction.getID(),
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coords]
                    }
                })

    if options.tls:
        for edge in net.getEdges():
            for lane in edge.getLanes():
                nCons = len(lane.getOutgoing())
                for i, con in enumerate(lane.getOutgoing()):
                    if con.getTLSID() != "":
                        feature = {"type": "Feature"}
                        feature["properties"] = {
                            "element": 'tls_connection',
                            "id": "%s_%s" % (con.getJunction().getID(), con.getJunctionIndex()),
                            "tls": con.getTLSID(),
                            "tlIndex": con.getTLLinkIndex(),
                        }
                        barLength = lane.getWidth() / nCons
                        offset = i * barLength - lane.getWidth() * 0.5
                        prev, end = lane.getShape()[-2:]
                        geometry = [gh.add(end, gh.sideOffset(prev, end, offset)),
                                    gh.add(end, gh.sideOffset(prev, end, offset + barLength))]
                        if options.boundary:
                            geometry = gh.line2boundary(geometry, 0.2)
                        feature["geometry"] = shape2json(net, geometry, options.boundary)
                        features.append(feature)

    # Combine lane features with arrow features
    all_features = features + arrow_features
    
    print(f"Total features: {len(features)} geometries + {len(arrow_features)} arrows = {len(all_features)}", file=sys.stderr)

    geojson = {
        "type": "FeatureCollection",
        "features": all_features,
        "metadata": {
            "displayProperties": DISPLAY_PROPERTIES,
            "generatedWith": "net2geojson.py",
        }
    }
    
    print(f"Writing output to {options.outFile}...", file=sys.stderr)
    with sumolib.openz(options.outFile, 'w') as outf:
        # Use compact JSON for large files (much faster to write and smaller file size)
        if len(all_features) > 1000:
            json.dump(geojson, outf, separators=(',', ':'))
        else:
            json.dump(geojson, outf, sort_keys=True, indent=2, separators=(',', ': '))
        print(file=outf)
    
    print("Done!", file=sys.stderr)
