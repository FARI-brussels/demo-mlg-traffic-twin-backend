import subprocess
from pathlib import Path
import requests
import xml.etree.ElementTree as ET
from merge_tunnels import merge_tunnels

def download_osm(url: str, output_path: Path) -> None:
    """Download an OSM .pbf file from a URL if it doesn’t already exist."""
    if output_path.exists():
        print(f"[INFO] {output_path.name} already exists, skipping download.")
        return
    print(f"[INFO] Downloading {url} ...")
    response = requests.get(url, stream=True)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"[INFO] Downloaded {output_path.name}")

def extract_region(osm_input: Path, poly_file: Path, osm_output: Path) -> None:
    """Extract a region defined by a .poly file using osmconvert."""
    print(f"[INFO] Extracting region from {osm_input.name} using {poly_file.name}")
    cmd = [
        "osmconvert",
        str(osm_input),
        f"-B={poly_file}",
        f"-o={osm_output}",
    ]
    subprocess.run(cmd, check=True)
    print(f"[INFO] Created {osm_output.name}")

def generate_network(osm_file: Path, net_out: Path) -> None:
    """Generate a SUMO network (.net.xml) from an OSM file using netconvert."""
    print(f"[INFO] Generating SUMO network from {osm_file.name}")
    cmd = [
        "netconvert",
        "--osm", str(osm_file),
        "-o", str(net_out),
        "--geometry.remove",               # use this instead of --ramps.no
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--tls.default-type", "actuated",
        "--keep-edges.by-vclass", "bus,private",
        "--remove-edges.isolated",
        "--output.street-names",
        "--output.original-names",
        "--osm.extra-attributes", "all",
    ]
    subprocess.run(cmd, check=True)
    print(f"[INFO] Generated network: {net_out.name}")

def merge_tunnel_triplets(net_input: Path, net_output: Path) -> None:
    """Merge tunnel slope/tunnel/slope triplets into single edges."""
    print(f"[INFO] Merging tunnel triplets in {net_input.name}")
    tree = ET.parse(net_input)
    merged_triplets = merge_tunnels(tree)
    if not merged_triplets:
        print("[INFO] No tunnel triplets found to merge.")
        # Still write the tree to the output
        ET.indent(tree, space="    ")
        tree.write(net_output, encoding="UTF-8", xml_declaration=True)
    else:
        print(f"[INFO] Merged {len(merged_triplets)} tunnel triplets:")
        for edge_in_id, edge_mid_id, edge_out_id in merged_triplets:
            print(f"       {edge_in_id} + {edge_mid_id} + {edge_out_id} -> {edge_in_id}")
        ET.indent(tree, space="    ")
        tree.write(net_output, encoding="UTF-8", xml_declaration=True)
    print(f"[INFO] Wrote merged network to {net_output.name}")

if __name__ == "__main__":
    # Paths and constants
    osm_url = "https://download.geofabrik.de/europe/belgium-latest.osm.pbf"
    belgium_osm = Path("belgium-latest.osm.pbf")
    brussels_poly = Path("brussels.poly")  # Must exist beforehand
    brussels_osm = Path("brussels.osm")
    brussels_net_temp = Path(".brussels.net.xml")  # Temporary file
    brussels_net_final = Path("brussels.net.xml")  # Final output

    # Run the workflow
    download_osm(osm_url, belgium_osm)
    extract_region(belgium_osm, brussels_poly, brussels_osm)
    generate_network(brussels_osm, brussels_net_temp)
    merge_tunnel_triplets(brussels_net_temp, brussels_net_final)
    # Clean up temporary network file
    brussels_net_temp.unlink()
    print(f"[INFO] Workflow complete: {brussels_net_final}")
