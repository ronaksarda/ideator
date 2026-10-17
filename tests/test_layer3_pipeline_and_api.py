import json
import pytest
from fastapi.testclient import TestClient
from ideator.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "falkordb_live" in data
    assert "source_mode" in data
    assert "memory_ledger" in data


def test_index_serves_chat_console(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Ideator" in response.text
    assert "/static/chat.js" in response.text
    assert "cytoscape" in response.text.lower()
