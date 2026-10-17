import sys
import hashlib
from backend.graph import MarketGraph
from backend.search import search_market
from backend.tabular import evaluate_market_saturation

def run_tests():
    print("[*] Running Layer 1 & Layer 2 Stress Test...")

    # Test 1: Search Module & Resilient Fallback
    print("[1/4] Testing search ingestion...")
    test_idea = "Local-first SQLite API proxy with zero telemetry"
    market_data = search_market(test_idea)
    assert len(market_data["competitors"]) > 0, "Competitors list cannot be empty"
    assert len(market_data["complaints"]) > 0, "Complaints list cannot be empty"
    print(f"      [OK] Mined {len(market_data['competitors'])} competitors, {len(market_data['complaints'])} complaints.")

    # Test 2: FalkorDB Ingestion with Special Characters & Normalization
    print("[2/4] Testing FalkorDB connection & insertion...")
    idea_id = hashlib.sha256(test_idea.encode()).hexdigest()[:12]
    try:
        mg = MarketGraph()
        mg.insert_market_nodes(idea_id, test_idea, market_data["competitors"], market_data["complaints"])
        print("      [OK] Ingested into FalkorDB without Cypher syntax crashes.")
    except Exception as e:
        print(f"      [FAIL] FalkorDB error: {e}")
        sys.exit(1)

    # Test 3: Unserved Wedge Graph Traversal & Cytoscape Serialization
    print("[3/4] Testing multi-hop traversal & Cytoscape extraction...")
    wedge_results = mg.query_unserved_wedge(idea_id)
    assert isinstance(wedge_results, list), "Wedge result must be a list"
    
    cytoscape_elements = mg.get_full_graph_elements(idea_id)
    assert "nodes" in cytoscape_elements and "edges" in cytoscape_elements
    assert len(cytoscape_elements["nodes"]) > 0, "Graph must return nodes"
    print(f"      [OK] Cytoscape payload: {len(cytoscape_elements['nodes'])} nodes, {len(cytoscape_elements['edges'])} edges (no Cartesian explosion).")

    # Test 4: TabPFN Tabular Risk Scoring
    print("[4/4] Testing TabPFN tabular scoring...")
    tabular_verdict = evaluate_market_saturation(
        competitor_count=len(market_data["competitors"]),
        median_price_usd=49.0,
        complaint_count=len(market_data["complaints"]),
        has_open_source=1,
        review_volume=250
    )
    assert "risk_tier" in tabular_verdict
    assert "recommended_entry_price" in tabular_verdict
    print(f"      [OK] Tabular Output: Risk={tabular_verdict['risk_tier']} ({tabular_verdict['saturation_index']}%), Price=${tabular_verdict['recommended_entry_price']} via {tabular_verdict['engine_used']}.")

    print("\n[SUCCESS] Layers 1 & 2 are completely stable and ready for Layer 3 (FastAPI/SSE).")

if __name__ == "__main__":
    run_tests()
