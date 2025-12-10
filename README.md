# Traffic Twin Backend

This repository contains the backend for the Traffic Twin application, a FastAPI-based service that generates and simulates traffic scenarios using SUMO (Simulation of Urban MObility).

## Prerequisites

Before running the application, ensure you have the following installed:

1.  **Python 3.9+**
2.  **SUMO (Simulation of Urban MObility)**:
    *   **Linux (Ubuntu/Debian)**:
        ```bash
        sudo add-apt-repository ppa:sumo/stable
        sudo apt-get update
        sudo apt-get install sumo sumo-tools sumo-doc
        ```
    *   **macOS (Homebrew)**:
        ```bash
        brew install sumo
        ```
    *   **Windows**: Download the installer from the [SUMO website](https://sumo.dlr.de/docs/Downloads.php).
    *   **Environment Variable**: Ensure `SUMO_HOME` is set. On Linux, it is usually `/usr/share/sumo`.

## Installation

1.  **Clone the repository**:
    ```bash
    git clone <repository_url>
    cd demo-mlg-traffic-twin-backend
    ```

2.  **Set up a virtual environment (recommended)**:
    *   Using `uv` (recommended):
        ```bash
        uv venv
        source .venv/bin/activate
        ```
    *   Using standard `venv`:
        ```bash
        python -m venv .venv
        source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
        ```

3.  **Install dependencies**:
    *   Using `uv`:
        ```bash
        uv sync
        ```
    *   Using `pip`:
        ```bash
        pip install -e .
        ```
        *Note: The project uses `pyproject.toml`. `pip install -e .` will install the project in editable mode along with its dependencies defined in `pyproject.toml`.*

## Running the Application

To start the FastAPI server with auto-reload enabled (development mode):

```bash
# Using uv
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Using standard python/pip environment
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The server will be accessible at `http://localhost:8000`.

## API Documentation

Once the server is running, you can access the interactive API documentation:

*   **Swagger UI**: `http://localhost:8000/docs`
*   **ReDoc**: `http://localhost:8000/redoc`

## Key Endpoints

*   `POST /simulate`: Runs a traffic simulation. Requires a network file (`.net.xml` zipped) and simulation parameters. Supports both standard microscopic and hybrid mesoscopic/microscopic modes.
*   `GET /get_current_deviations`: Fetches current traffic events from the Brussels Mobility API.
*   `POST /generate_network_from_bounding_box`: Generates a SUMO network from OpenStreetMap data for a specified bounding box.
*   `POST /generate_network_geojson`: Converts a SUMO network file to GeoJSON.

## Project Structure

*   `main.py`: Main FastAPI application entry point.
*   `extract_osm.py`: Utilities for downloading and processing OpenStreetMap data.
*   `calculate_metrics.py`: Calculates traffic metrics from simulation outputs.
*   `get_osiris_closed_edges.py`: Fetches real-time traffic data.
*   `network_generator/`: Contains scripts and data for network generation.






