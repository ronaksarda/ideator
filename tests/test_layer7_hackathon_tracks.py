import uuid
import pytest
from ideator.backend.graph import MarketGraph
from fastapi.testclient import TestClient
from ideator.backend.main import app


def test_track01_multihop_reasoning_and_pagerank():
    """Track 01: Agents That Act on Connected Data - Multi-hop traversal and PageRank."""
    graph = MarketGraph()
    idea_id = "track01_test_idea"
    idea_text = "AI Whiteboard Tutor"
    
    competitors = [
        {"name": "Synthesis Tutor", "url": "https://example.com/synth", "price_usd": 45.0, "features": ["Virtual Whiteboard", "Cloud Sync"]},
        {"name": "Khanmigo", "url": "https://example.com/khan", "price_usd": 4.0, "features": ["Virtual Whiteboard", "Socratic Method"]}
    ]
    complaints = [
        {"competitor": "Synthesis Tutor", "feature": "Virtual Whiteboard", "text": "Whiteboard lacks freehand geometry controls", "source": "reddit"},
        {"competitor": "Synthesis Tutor", "feature": "Cloud Sync", "text": "Sync drops student connection during live lesson", "source": "reddit"}
    ]
    
    # 1. Ingest graph
    graph.insert_market_nodes(idea_id, idea_text, competitors, complaints)
    
    # 2. Multi-hop unserved wedge query
    wedge = graph.query_unserved_wedge(idea_id)
    assert len(wedge) >= 1
    assert wedge[0]["complaint_count"] >= 1
    
    # 3. PageRank ranking algorithm
    ranking = graph.run_pagerank_ranking(idea_id)
    assert len(ranking) >= 1
    assert "competitor" in ranking[0]
    assert "score" in ranking[0]
    assert ranking[0]["score"] > 0


def test_track02_agent_memory_and_persistent_history():
    """Track 02: Agent Memory and Coordination - Persistent conversation history and session restore."""
    client = TestClient(app)
    graph = MarketGraph()
    session_id = f"sess_track02_{uuid.uuid4().hex[:8]}"  # FalkorDB persists between runs
    
    # 1. Append chat messages to memory
    m1 = graph.append_chat_message(session_id, "user", "I want to build an AI Tutor on Whiteboard")
    m2 = graph.append_chat_message(session_id, "assistant", "Analyzed market. Primary competitor is Synthesis Tutor ($45/mo).")
    
    assert m1["id"].startswith("msg_")
    assert m2["role"] == "assistant"
    
    # 2. Retrieve persistent history
    history = graph.get_chat_history(session_id)
    assert len(history) == 2
    assert history[0]["text"] == "I want to build an AI Tutor on Whiteboard"
    assert history[1]["role"] == "assistant"
    
    # 3. Sessions listing endpoint
    sessions = graph.list_all_sessions()
    assert any(s["session_id"] == session_id for s in sessions)
    
    res = client.get("/api/sessions")
    assert res.status_code == 200
    assert "sessions" in res.json()
