**Run the workflow**
   ```bash
   python download_and_filter_network.py
   ```

**Output**
   - `brussels.net.xml` - Final SUMO network with merged tunnel triplets (ramp + underground part + ramp) → single edge)

## What It Does

1. Downloads Belgium OSM data
2. Extracts Brussels region using polygon
3. Generates SUMO network with `netconvert`
4. Merges tunnel triplets (ramp + underground part + rampe) into single edges
5. Outputs single `brussels.net.xml` file

## File Details

- `download_and_filter_network.py` - Main workflow script
- `merge_tunnels.py` - Library for collapsing tunnel triplets

