# Traffic Twin Backend

This repository contains the backend for the Traffic Twin application, a FastAPI-based service that generates and simulates traffic scenarios using SUMO (Simulation of Urban MObility).

## Prerequisites

Before running the application, ensure you have the following installed:

1.  **Python 3.9+**
2.  **uv** (Python package manager - recommended):
    *   **Linux/macOS**:
        ```bash
        curl -LsSf https://astral.sh/uv/install.sh | sh
        ```
        Or using pip:
        ```bash
        pip install uv
        ```
    *   **Windows**:
        ```powershell
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        ```
    *   For more installation options, visit the [uv documentation](https://github.com/astral-sh/uv).
3.  **SUMO (Simulation of Urban MObility)**:
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

        If you want to install dev dependencies as well (for testing and stuff);
        ```bash
        uv sync --extra dev
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

## Docker Deployment

Deploy the application using **Docker Compose** with **Caddy** as a reverse proxy.

### Prerequisites

1.  **Docker Engine** (version 20.10+): [Installation guide](https://docs.docker.com/engine/install/)
2.  **Docker Compose**: Included with Docker Desktop or install via `sudo apt-get install docker-compose-plugin`

### Quick Start

```bash
# Clone and enter the repository
git clone <repository_url>
cd demo-mlg-traffic-twin-backend

# Build and start services
docker compose up -d

# View logs
docker compose logs -f
```

Access the application:
-   **Via Caddy**: `http://localhost` (port 80)
-   **Direct backend**: `http://localhost:8000`
-   **API Docs**: `http://localhost/docs`

### Configuring Caddy

Edit `Caddyfile` for your domain (for production with automatic SSL):

```caddy
your-domain.com {
    reverse_proxy backend:8000 {
        header_up Host {host}
        header_up X-Real-IP {remote}
        header_up X-Forwarded-For {remote}
        header_up X-Forwarded-Proto {scheme}
    }
    encode gzip
}
```

Reload Caddy after changes:
```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

### Docker Commands

```bash
docker compose up -d                    # Start services
docker compose down                     # Stop and remove containers
docker compose logs -f                  # Follow all logs
docker compose logs -f backend          # Follow backend logs
docker compose build --no-cache         # Rebuild from scratch
docker compose restart                  # Restart all services
docker compose ps                       # Check status
```

### Volumes & Data Persistence

| Volume | Purpose |
|--------|---------|
| `caddy_data` | SSL certificates (Let's Encrypt) |
| `caddy_config` | Caddy configuration |

> **Note**: Simulation outputs are returned directly in the API response (as ZIP files) and use temporary directories that are cleaned up automatically.

### Environment Variables

Customize via `docker-compose.yml` or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Backend port |
| `SUMO_HOME` | `/usr/share/sumo` | SUMO installation path |
| `DEBUG` | `false` | Debug mode |

### Features

-   **Non-root user**: Backend runs as `appuser` for security
-   **Health checks**: Automatic container health monitoring
-   **Log rotation**: Prevents disk space exhaustion
-   **Restart policies**: Automatic recovery from failures
-   **Automatic SSL**: Caddy provisions Let's Encrypt certificates for valid domains
-   **Optimized build**: Multi-layer caching, minimal image size

### Troubleshooting

```bash
# Check container health
docker compose ps

# View detailed logs
docker compose logs --tail=100 backend

# Check resource usage
docker stats

# Inspect container
docker compose exec backend python --version
docker compose exec backend ls -la /app

# Test backend directly
curl http://localhost:8000/docs
```

## Project Structure

*   `main.py`: Main FastAPI application entry point.
*   `extract_osm.py`: Utilities for downloading and processing OpenStreetMap data.
*   `calculate_metrics.py`: Calculates traffic metrics from simulation outputs.
*   `get_osiris_closed_edges.py`: Fetches real-time traffic data.
*   `network_generator/`: Contains scripts and data for network generation.






