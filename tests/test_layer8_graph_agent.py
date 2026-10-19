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


def test_hotspots_ignore_other_ideas_solves(graph):
    """A grievance solved by a different idea must still show up for this one."""
    graph.insert_market_nodes("idea_t", "Meal planner", COMPS, CMPS)
    graph._graph.query("""
MATCH (c:Competitor {name: 'PlateJoy'})-[:DRAWS_COMPLAINT]->(g:Grievance)
MERGE (other:UserIdea {id: 'other_idea'}) MERGE (other)-[:TARGETS]->(c) MERGE (other)-[:SOLVES]->(g)""")
    server = build_server(graph, "idea_t", "Meal planner")
    hot = asyncio.run(run_graph_agent(server, "x", lambda p: None, lambda s: None))
    rows = next(s for s in hot["steps"] if s["tool"] == "complaint_hotspots")["result"]["rows"]
    assert rows[0]["feature"] == "Grocery lists" and rows[0]["complaints"] == 2
    # the same idea solving it does hide it
    graph._graph.query("""
MATCH (u:UserIdea {id: $u}), (g:Grievance)<-[:DRAWS_COMPLAINT]-(:Competitor {name: 'PlateJoy'}) MERGE (u)-[:SOLVES]->(g)""",
                       {"u": next(iter(graph._graph.query("MATCH (u:UserIdea) WHERE u.id <> 'other_idea' RETURN u.id").result_set))[0]})
    out = asyncio.run(run_graph_agent(server, "x", lambda p: None, lambda s: None))
    rows = next(s for s in out["steps"] if s["tool"] == "complaint_hotspots")["result"]["rows"]
    assert rows[0]["complaints"] == 1


def test_unknown_price_is_not_free():
    from ideator.backend.graph import MarketGraph
    g = MarketGraph()
    g.insert_market_nodes("idea_p", "x", [{"name": "A", "price_usd": None, "features": []},
                                          {"name": "B", "price_usd": 0.0, "features": []}], [])
    amounts = [n["amount_usd"] for n in g._mem_nodes.values() if n.get("type") == "pricing"]
    assert amounts == [0.0]  # B is free (has a Pricing node), A is unknown (none)


def test_agent_rejects_unknown_tool(graph):
    graph.insert_market_nodes("idea_t", "Meal planner", COMPS, CMPS)
    picks = iter([{"tool": "drop_database", "args": {}}, {"final": "done", "cited_ids": []}])
    out = asyncio.run(run_graph_agent(build_server(graph, "idea_t", "Meal planner"), "x",
                                      lambda p: next(picks, {"final": "done", "cited_ids": []}), lambda s: None))
    assert out["steps"][0]["result"] == {"error": "unknown tool"} and out["final"] == "done"
    # an early "final" cannot skip the required tools: hotspots and the TabPFN price always run first
    assert {"complaint_hotspots", "predict_price"} <= {s["tool"] for s in out["steps"]}
