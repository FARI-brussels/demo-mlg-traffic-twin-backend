# Use Ubuntu 22.04 as base (required for SUMO PPA)
FROM ubuntu:22.04

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SUMO_HOME=/usr/share/sumo \
    PORT=8000 \
    PATH="/app/.venv/bin:$PATH"

# Install system dependencies (keep default Python for apt tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    software-properties-common \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install SUMO from PPA FIRST (before changing Python)
RUN add-apt-repository -y ppa:sumo/stable && \
    apt-get update && \
    apt-get install -y --no-install-recommends sumo sumo-tools && \
    rm -rf /var/lib/apt/lists/*

# Now install Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster package management (using system pip)
RUN pip3 install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy only dependency files first (better layer caching)
COPY pyproject.toml uv.lock* ./

# Create virtual environment with Python 3.11 and install dependencies
RUN python3.11 -m venv /app/.venv && \
    . /app/.venv/bin/activate && \
    uv pip install -e . 2>/dev/null || pip install -e .

# Copy application code (after dependencies for better caching)
COPY *.py ./
COPY utils/ ./utils/
COPY brussels.geojson ./

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/docs || exit 1

# Run the application with production settings
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
