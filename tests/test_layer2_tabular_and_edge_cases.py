import pytest
from ideator.backend.graph import MarketGraph
from ideator.backend.tabular import evaluate_market_saturation


def test_edge_case_special_characters_and_emojis():
    graph = MarketGraph()
    idea_id = "special_char_test_99"
    weird_idea_text = "Test 'Idea' with / \\ & quotes \" and emojis 🚀🔥💡"
    
    competitors = [
        {
            "name": "Weird's & Co. / 100% \"Fast\" 🚀",
            "url": "https://example.com/special",
            "snippet": "Handles 'quoting' & \\backslashes\\ gracefully.",
            "pricing_usd": 99.0,
            "features": ["Slash / Handling", "Quote 'Escaping' ⚡"]
        }
    ]
    complaints = [
        {
            "competitor": "Weird's & Co. / 100% \"Fast\" 🚀",
            "text": "Doesn't work with emoji keys 🛑 & drops \\paths\\!",
            "feature": "Slash / Handling",
            "source_url": "https://reddit.com/r/testing"
        }
    ]

    # Must ingest without any exception
    graph.insert_market_nodes(idea_id, weird_idea_text, competitors, complaints)

    # Validate elements query
    elements = graph.get_full_graph_elements(idea_id)
    assert len(elements["nodes"]) > 0
    assert len(elements["edges"]) > 0

    # Ensure node IDs are clean hex strings with prefix
    for node in elements["nodes"]:
        n_id = node["data"]["id"]
        assert " " not in n_id
        assert "'" not in n_id
        assert "\\" not in n_id
        assert "/" not in n_id
        assert "🚀" not in n_id

    # Unserved wedge query works
    wedge = graph.query_unserved_wedge(idea_id)
    assert len(wedge) >= 1
    assert wedge[0]["complaint_count"] >= 1


def test_edge_case_empty_features_and_complaints_fallback():
    graph = MarketGraph()
    idea_id = "empty_payload_idea"
    idea_text = "Barebones idea with zero scraped features"
    
    # Empty features and empty complaints
    competitors = [
        {
            "name": "Silent Competitor",
            "url": "",
            "snippet": "No features listed",
            "pricing_usd": 0.0,
            "features": []
        }
    ]
    complaints = []

    graph.insert_market_nodes(idea_id, idea_text, competitors, complaints)

    # No complaints in, no wedge out: the graph must not invent a placeholder complaint or feature
    wedge = graph.query_unserved_wedge(idea_id)
    assert wedge == []
    assert not any(n.get("type") == "grievance" for n in graph._mem_nodes.values())


def test_tabpfn_high_density_vector():
    # High-density competitor vector: [14, 89.0, 42, 0, 1500]
    high_density_v = [14, 89.0, 42, 0, 1500]
    result = evaluate_market_saturation(high_density_v)

    assert result["risk_tier"] in ("HIGH", "CRITICAL")
    assert result["confidence"] >= 0.70
    assert result["saturation_index"] >= 65.0
    # Price recommendation undercuts median price
    assert result["recommended_entry_price"] <= 89.0 * 0.45
    assert result["recommended_entry_price"] >= 9.0


def test_tabpfn_ultra_niche_vector():
    # Ultra-niche vector: [1, 0.0, 8, 1, 10]
    niche_v = [1, 0.0, 8, 1, 10]
    result = evaluate_market_saturation(niche_v)

    assert result["risk_tier"] == "LOW"
    assert result["confidence"] >= 0.70
    assert result["saturation_index"] <= 35.0
    assert result["recommended_entry_price"] >= 19.0

