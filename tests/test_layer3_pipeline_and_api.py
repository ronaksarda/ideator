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


def test_pipeline_dashboard(client):
    response = client.get("/pipeline")
    assert response.status_code == 200
    assert "Ideator" in response.text
    assert "Terminal Execution Stream" in response.text
    assert "Node Inspector" in response.text
    assert "Cross-Session Memory" in response.text
    assert "Tabular Market Sizing" in response.text
    assert "cytoscape" in response.text.lower()


def test_sse_pipeline_stream_observable(client):
    idea = "Local-first SQLite API proxy with zero telemetry"
    response = client.post("/api/analyze", json={"idea": idea})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    events = []
    for line in response.text.split("\n\n"):
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            events.append(payload)

    stages = [e["stage"] for e in events]
    assert "LOG_STREAM" in stages
    assert "GRAPH_INGEST" in stages
    assert "CYPHER_TRAVERSAL" in stages
    assert "TABULAR_EVAL" in stages
    assert "CROSS_SESSION_MEMORY" in stages
    assert "PIPELINE_COMPLETE" in stages

    # Check search event source mode badge
    log_ev = next(e for e in events if e["stage"] == "LOG_STREAM")
    assert log_ev["source_badge"] in ("LIVE_SERPAPI", "DETERMINISTIC_FALLBACK")
    assert len(log_ev["competitors"]) > 0

    # Check Cypher traversal metadata (Track 01)
    trav_ev = next(e for e in events if e["stage"] == "CYPHER_TRAVERSAL")
    assert "cypher_query" in trav_ev
    assert "MATCH (u:UserIdea" in trav_ev["cypher_query"]
    assert "unserved_wedge_feature" in trav_ev

    # Check Graph payload & color codes + inspector metadata
    graph_event = next(e for e in events if e["stage"] == "GRAPH_INGEST")
    nodes = graph_event["cytoscape"]["nodes"]
    assert len(nodes) > 0
    first_node = nodes[0]["data"]
    assert "cypher_query" in first_node
    assert "incoming_edges" in first_node
    assert "outgoing_edges" in first_node

    # Check Tabular payload
    tabular_event = next(e for e in events if e["stage"] == "TABULAR_EVAL")
    assert "feature_vector" in tabular_event
    assert "saturation_index" in tabular_event
    assert "risk_tier" in tabular_event

    # Check Memory payload (Track 02)
    mem_ev = next(e for e in events if e["stage"] == "CROSS_SESSION_MEMORY")
    assert "memory_ledger" in mem_ev
    assert "past_sessions" in mem_ev

    # Check Complete Blueprint payload
    complete_event = next(e for e in events if e["stage"] == "PIPELINE_COMPLETE")
    b = complete_event["blueprint"]
    assert "the_wedge" in b
    assert "mvp_scope_cuts" in b
    assert len(b["mvp_scope_cuts"]) >= 2
    assert "monetization_structure" in b
