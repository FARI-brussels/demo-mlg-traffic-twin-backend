"""
Tests for the /simulate endpoint with multi-scenario support.
"""
import json
import zipfile
from io import BytesIO
from pathlib import Path
import os
import pytest
from fastapi.testclient import TestClient

from main import app


# Test fixtures
@pytest.fixture
def client():
    """Create a test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture
def network_zip():
    """Create a zip file containing the test network."""
    network_path = Path(__file__).parent / "networks" / "network.net.xml"
    
    if not network_path.exists():
        pytest.skip(f"Test network not found at {network_path}")
    
    # Create in-memory zip
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(network_path, arcname="network.net.xml")
    memory_file.seek(0)
    
    return memory_file


@pytest.fixture
def scenarios():
    """Test scenarios with different edge closures."""
    return [
        {"name": "baseline", "description": "No closures", "closed_edges": []},
        {"name": "option_a", "description": "Close main street", "closed_edges": ["150275999#0", "70724347#0"]},
        {"name": "option_b", "closed_edges": ["781742860#6", "-781742860#8"]}
    ]


@pytest.fixture
def fcd_filter_shape():
    """FCD filter shape for hybrid mode."""
    return {
        "centerLon": 4.37298625,
        "centerLat": 50.827623,
        "radiusKm": 0.7864077861964932
    }


class TestSimulateEndpoint:
    """Tests for the /simulate endpoint."""
    
    def test_simulate_hybrid_mode_multi_scenario(
        self, 
        client, 
        network_zip, 
        scenarios, 
        fcd_filter_shape
    ):
        """Test simulation with multiple scenarios in hybrid mode."""
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 3600,
                "insertion_rate": 3000,
                "scenarios": json.dumps(scenarios),
                "simulation_mode": "hybrid",
                "fcdFilterShape": json.dumps(fcd_filter_shape),
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 200, f"Response failed: {response.text}"
        assert response.headers["content-type"] == "application/zip"
        
        # Extract and verify the response zip
        response_zip = zipfile.ZipFile(BytesIO(response.content))
        file_list = response_zip.namelist()
        
        # Check that metrics.json exists
        assert "metrics.json" in file_list, f"metrics.json not found in {file_list}"
        
        # Check that we have output files for each scenario
        for scenario in scenarios:
            safe_name = "".join(
                c if c.isalnum() or c in '-_' else '_' 
                for c in scenario['name']
            )
            assert f"fcd_trips_{safe_name}.json" in file_list, \
                f"fcd_trips_{safe_name}.json not found in {file_list}"
            assert f"congestion_{safe_name}.geojson" in file_list, \
                f"congestion_{safe_name}.geojson not found in {file_list}"
        
        # Parse and validate metrics.json
        metrics_content = response_zip.read("metrics.json")
        metrics = json.loads(metrics_content)
        
        # Verify structure
        assert "scenarios" in metrics, "metrics should contain 'scenarios' key"
        assert "comparisons" in metrics, "metrics should contain 'comparisons' key"
        assert "metadata" in metrics, "metrics should contain 'metadata' key"
        
        # Verify scenarios are sorted by total_delay_vh (ascending)
        scenarios_result = metrics["scenarios"]
        assert len(scenarios_result) == len(scenarios), \
            f"Expected {len(scenarios)} scenarios, got {len(scenarios_result)}"
        
        # Check that scenarios are sorted by delay (best first)
        delays = [s["metrics"]["total_delay_vh"] for s in scenarios_result]
        assert delays == sorted(delays), \
            f"Scenarios should be sorted by total_delay_vh (ascending): {delays}"
        
        # Verify each scenario has required fields
        for i, s in enumerate(scenarios_result):
            assert "name" in s, f"Scenario {i} missing 'name'"
            assert "metrics" in s, f"Scenario {i} missing 'metrics'"
            assert "closed_edges" in s, f"Scenario {i} missing 'closed_edges'"
            assert "rank" in s, f"Scenario {i} missing 'rank'"
            assert s["rank"] == i + 1, f"Scenario {i} should have rank {i + 1}, got {s['rank']}"
        
        # Verify comparisons
        comparisons = metrics["comparisons"]
        assert len(comparisons) == len(scenarios), \
            f"Expected {len(scenarios)} comparisons, got {len(comparisons)}"
        
        # First comparison should be the best (is_best=True)
        assert comparisons[0]["is_best"] is True, "First scenario should be marked as best"
        assert comparisons[0]["delay_increase_vh"] == 0, "Best scenario should have 0 delay increase"
        
        # Other comparisons should not be best
        for comp in comparisons[1:]:
            assert comp["is_best"] is False, "Only first scenario should be marked as best"
        
        # Verify metadata
        metadata = metrics["metadata"]
        assert metadata["simulation_mode"] == "hybrid"
        assert metadata["begin_time"] == 0
        assert metadata["end_time"] == 3600
        assert metadata["insertion_rate"] == 3000
        assert metadata["num_scenarios"] == len(scenarios)
        assert "best_scenario" in metadata
        
        print(f"\n✅ Test passed!")
        print(f"Best scenario: {metadata['best_scenario']}")
        print(f"Scenario results (sorted by delay):")
        for s in scenarios_result:
            print(f"  {s['rank']}. {s['name']}: {s['metrics']['total_delay_vh']:.2f} veh·h delay")

    def test_simulate_microscopic_mode_multi_scenario(
        self, 
        client, 
        network_zip, 
        scenarios
    ):
        """Test simulation with multiple scenarios in microscopic mode."""
        # Reset network_zip position
        network_zip.seek(0)
        
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 600,  # Shorter for faster test
                "insertion_rate": 1000,
                "scenarios": json.dumps(scenarios),
                "simulation_mode": "microscopic",
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 200, f"Response failed: {response.text}"
        
        # Parse response
        response_zip = zipfile.ZipFile(BytesIO(response.content))
        metrics_content = response_zip.read("metrics.json")
        metrics = json.loads(metrics_content)
        
        assert metrics["metadata"]["simulation_mode"] == "microscopic"
        assert len(metrics["scenarios"]) == len(scenarios)
        
        print(f"\n✅ Microscopic mode test passed!")

    def test_simulate_mesoscopic_mode_multi_scenario(
        self, 
        client, 
        network_zip, 
        scenarios
    ):
        """Test simulation with multiple scenarios in mesoscopic mode."""
        # Reset network_zip position
        network_zip.seek(0)
        
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 600,  # Shorter for faster test
                "insertion_rate": 1000,
                "scenarios": json.dumps(scenarios),
                "simulation_mode": "mesoscopic",
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 200, f"Response failed: {response.text}"
        
        # Parse response
        response_zip = zipfile.ZipFile(BytesIO(response.content))
        metrics_content = response_zip.read("metrics.json")
        metrics = json.loads(metrics_content)
        
        assert metrics["metadata"]["simulation_mode"] == "mesoscopic"
        assert len(metrics["scenarios"]) == len(scenarios)
        
        print(f"\n✅ Mesoscopic mode test passed!")


class TestSimulateValidation:
    """Tests for input validation."""
    
    def test_empty_scenarios_rejected(self, client, network_zip):
        """Test that empty scenarios list is rejected."""
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 3600,
                "insertion_rate": 3000,
                "scenarios": json.dumps([]),
                "simulation_mode": "mesoscopic",
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 400
        assert "At least one scenario" in response.json()["detail"]

    def test_too_many_scenarios_rejected(self, client, network_zip):
        """Test that more than 5 scenarios is rejected."""
        network_zip.seek(0)
        
        many_scenarios = [
            {"name": f"scenario_{i}", "closed_edges": []}
            for i in range(6)
        ]
        
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 3600,
                "insertion_rate": 3000,
                "scenarios": json.dumps(many_scenarios),
                "simulation_mode": "mesoscopic",
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 400
        assert "Maximum 5 scenarios" in response.json()["detail"]

    def test_duplicate_scenario_names_rejected(self, client, network_zip):
        """Test that duplicate scenario names are rejected."""
        network_zip.seek(0)
        
        duplicate_scenarios = [
            {"name": "baseline", "closed_edges": []},
            {"name": "baseline", "closed_edges": ["edge1"]},
        ]
        
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 3600,
                "insertion_rate": 3000,
                "scenarios": json.dumps(duplicate_scenarios),
                "simulation_mode": "mesoscopic",
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 400
        assert "Duplicate scenario name" in response.json()["detail"]

    def test_hybrid_mode_requires_fcd_filter_shape(self, client, network_zip):
        """Test that hybrid mode requires fcdFilterShape."""
        network_zip.seek(0)
        
        response = client.post(
            "/simulate",
            data={
                "begin_time": 0,
                "end_time": 3600,
                "insertion_rate": 3000,
                "scenarios": json.dumps([{"name": "baseline", "closed_edges": []}]),
                "simulation_mode": "hybrid",
                # Missing fcdFilterShape
            },
            files={
                "network_zip": ("network.zip", network_zip, "application/zip"),
            },
        )
        
        assert response.status_code == 400
        assert "fcd_filter_shape is required for hybrid mode" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

