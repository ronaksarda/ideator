"""Track 01: graph agent picks MCP tools, runs real Cypher on FalkorDB, and records a decision with evidence."""
import asyncio
import uuid

import pytest

from ideator.backend.graph import MarketGraph
from ideator.backend.tools import build_server, run_graph_agent

COMPS = [{"name": "PlateJoy", "price_usd": 9.99, "features": ["Grocery lists"]},
         {"name": "Mealime", "price_usd": 0.0, "features": ["Quick recipes"]}]
CMPS = [{"competitor": "PlateJoy", "feature": "Grocery lists", "text": "Lists are messy and duplicate items", "source_url": "https://reddit.com/a"},
        {"competitor": "Mealime", "feature": "Grocery lists", "text": "Grocery list ignores my pantry", "source_url": "https://reddit.com/b"},
        {"competitor": "Mealime", "feature": "Quick recipes", "text": "Recipes repeat every week", "source_url": "https://reddit.com/c"}]


@pytest.fixture
def graph():
    g = MarketGraph()
    if not g.is_live:
        pytest.skip("FalkorDB not running")
    g._graph = g._falkor.select_graph(f"test_{uuid.uuid4().hex[:8]}")
    yield g
    g._graph.delete()


def test_agent_runs_tools_over_mcp_and_records_decision(graph):
    graph.insert_market_nodes("idea_t", "Meal planner", COMPS, CMPS)
    steps = []
    out = asyncio.run(run_graph_agent(build_server(graph, "idea_t", "Meal planner"), "find the wedge",
                                      lambda prompt: None, steps.append))
    tools = [s["tool"] for s in out["steps"]]
    assert tools[0] == "market_overview" and tools[-1] == "record_decision"
    hot = next(s for s in out["steps"] if s["tool"] == "complaint_hotspots")["result"]
    assert hot["rows"][0]["feature"] == "Grocery lists" and hot["rows"][0]["complaints"] == 2  # multi-hop count
    assert "MATCH" in hot["cypher"]
    decision = out["steps"][-1]["result"]["rows"][0]
    assert decision["linked"] == 3  # 2 complaints + the feature, linked with BECAUSE edges in FalkorDB
    assert graph._graph.query("MATCH (:Decision)-[:BECAUSE]->(g:Grievance) RETURN count(g)").result_set[0][0] == 2
    assert len(steps) == len(out["steps"])


def test_agent_rejects_unknown_tool(graph):
    graph.insert_market_nodes("idea_t", "Meal planner", COMPS, CMPS)
    picks = iter([{"tool": "drop_database", "args": {}}, {"final": "done", "cited_ids": []}])
    out = asyncio.run(run_graph_agent(build_server(graph, "idea_t", "Meal planner"), "x",
                                      lambda p: next(picks, {"final": "done", "cited_ids": []}), lambda s: None))
    assert out["steps"][0]["result"] == {"error": "unknown tool"} and out["final"] == "done"
    # an early "final" cannot skip the required tools: hotspots and the TabPFN price always run first
    assert {"complaint_hotspots", "predict_price"} <= {s["tool"] for s in out["steps"]}
