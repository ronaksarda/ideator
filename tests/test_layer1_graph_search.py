import pytest
from ideator.backend.search import search_market
from ideator.backend.graph import MarketGraph


def test_search_market_structure():
    query = "local-first API billing tracker"
    result = search_market(query)
    
    assert "competitors" in result
    assert "complaints" in result
    assert len(result["competitors"]) >= 3
    assert len(result["complaints"]) >= 3

    comp = result["competitors"][0]
    assert "name" in comp
    assert "pricing_usd" in comp
    assert "features" in comp
    assert isinstance(comp["features"], list)

    complaint = result["complaints"][0]
    assert "competitor" in complaint
    assert "text" in complaint
    assert "feature" in complaint


def test_graph_ingestion_and_wedge_traversal():
    query = "local-first API billing tracker"
    idea_id = "test_idea_123"
    
    market_data = search_market(query)
    graph = MarketGraph()
    
    graph.insert_market_nodes(
        idea_id=idea_id,
        idea_text=query,
        competitors=market_data["competitors"],
        complaints=market_data["complaints"]
    )
    
    # 1. Unserved Wedge traversal
    wedge = graph.query_unserved_wedge(idea_id)
    assert isinstance(wedge, list)
    assert len(wedge) > 0
    
    top_feature = wedge[0]
    assert "feature" in top_feature
    assert "complaint_count" in top_feature
    assert "competitor_count" in top_feature
    assert "avg_pricing_usd" in top_feature
    assert top_feature["complaint_count"] >= 1
    
    # Ensure descending order by complaint_count
    for i in range(len(wedge) - 1):
        assert wedge[i]["complaint_count"] >= wedge[i+1]["complaint_count"]

    # 2. Cytoscape elements serialization
    elements = graph.get_full_graph_elements(idea_id)
    assert "nodes" in elements
    assert "edges" in elements
    assert len(elements["nodes"]) > 0
    assert len(elements["edges"]) > 0
    
    # Validate cytoscape schema
    node_types = set()
    for n in elements["nodes"]:
        assert "data" in n
        assert "id" in n["data"]
        assert "label" in n["data"]
        assert "type" in n["data"]
        node_types.add(n["data"]["type"])
        
    assert "competitor" in node_types
    assert "grievance" in node_types
    assert "feature" in node_types

    edge_labels = set()
    for e in elements["edges"]:
        assert "data" in e
        assert "source" in e["data"]
        assert "target" in e["data"]
        assert "label" in e["data"]
        edge_labels.add(e["data"]["label"])
        
    assert "TARGETS" in edge_labels
    assert "DRAWS_COMPLAINT" in edge_labels
    assert "TARGETS_FEATURE" in edge_labels


def test_relevant_drops_off_topic_results():
    from ideator.backend.search import _relevant
    idea = "Cloud logging tool that caps the monthly bill for small DevOps teams"
    assert _relevant("Cloud logging costs: how to cap your DevOps bill", idea)
    assert not _relevant("NFL: National Football League Discussion", idea)
    assert not _relevant("Reddit Inc Homepage. Reddit is home to thousands of communities", idea)
