"""Graph tools served over MCP, and an agent that picks them, acts on the graph, and explains itself.

Every tool runs real, parameterized Cypher (or a FalkorDB algorithm) against the user's own graph and returns
the query it ran plus the node ids it touched, so the UI can highlight the evidence.
"""
import asyncio
import json
import os
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from mcp import Client
from mcp.server.mcpserver import MCPServer

try:
    from backend.config import settings
    from backend.graph import GENERAL, MarketGraph, entity_id
    from backend.agent import INJECTION_RULE
    from backend.telemetry import agent_span
    from backend.tabular import evaluate_market_saturation
except ImportError:
    from ideator.backend.config import settings
    from ideator.backend.graph import GENERAL, MarketGraph, entity_id
    from ideator.backend.agent import INJECTION_RULE
    from ideator.backend.telemetry import agent_span
    from ideator.backend.tabular import evaluate_market_saturation

logger = logging.getLogger("ideator.tools")
MAX_STEPS = 4
REQUIRED = ("complaint_hotspots", "predict_price")  # Gemma sometimes skips these; the answer is weak without them


def _price_args(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Price a product one feature richer than the average competitor, fixing the top complaint."""
    rows = next((h["result"].get("rows") or [] for h in history if h["tool"] == "market_overview"), [])
    avg = sum(len(r.get("features") or []) for r in rows) / max(1, len(rows))
    return {"feature_count": round(avg) + 1, "complaints_addressed": 1}


def build_server(graph: MarketGraph, idea_raw: str, idea_text: str,
                 search_fn: Optional[Callable[[str], Dict[str, Any]]] = None, actions: bool = True) -> MCPServer:
    """One MCP server per idea: tools are bound to this user's graph and idea, so the agent cannot read other tenants."""
    idea_node = entity_id("idea", idea_raw)  # same id scheme MarketGraph.insert_market_nodes uses
    searches_left = [1]
    server = MCPServer("ideator-graph", instructions="Market graph of one startup idea: competitors, prices, features, user complaints.")

    def run(cypher: str, params: Dict[str, Any], cols: List[str]) -> Dict[str, Any]:
        if not (graph.is_live and graph._graph):
            return {"error": "FalkorDB offline", "cypher": cypher, "rows": []}
        t = time.perf_counter()
        res = graph._graph.query(cypher, {"idea": idea_node, "general": GENERAL, **params})
        rows = [dict(zip(cols, r)) for r in res.result_set]
        ids = sorted({v for r in rows for k, v in r.items() if k.endswith("_id") and isinstance(v, str)}
                     | {i for r in rows for k, v in r.items() if k.endswith("_ids") for i in (v or [])})
        return {"cypher": cypher.strip(), "ms": round((time.perf_counter() - t) * 1000, 1), "rows": rows, "node_ids": ids}

    @server.tool()
    def market_overview() -> Dict[str, Any]:
        """List every competitor of the idea with its price, number of user complaints and features. Start here."""
        return run("""
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor) WHERE c.name <> $general
OPTIONAL MATCH (c)-[:PRICED_AT]->(p:Pricing)
OPTIONAL MATCH (c)-[:DRAWS_COMPLAINT]->(g:Grievance)
OPTIONAL MATCH (c)-[:OFFERS]->(f:Feature)
RETURN c.id, c.name, max(p.amount_usd), count(DISTINCT g), collect(DISTINCT f.name)
ORDER BY count(DISTINCT g) DESC""", {}, ["competitor_id", "name", "price_usd", "complaints", "features"])

    @server.tool()
    def complaint_hotspots() -> Dict[str, Any]:
        """Multi-hop: idea -> competitors -> their complaints -> the feature each complaint is about. Groups by feature,
        skipping complaints the idea already SOLVES. The top row is the most painful unserved need."""
        return run("""
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor)-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature)
WHERE NOT (u)-[:SOLVES]->(g)
RETURN f.id, f.name, count(DISTINCT g), collect(DISTINCT c.name), collect(g.text)[0..3], collect(g.id)
ORDER BY count(DISTINCT g) DESC""", {}, ["feature_id", "feature", "complaints", "competitors", "quotes", "grievance_ids"])

    @server.tool()
    def central_features(limit: int = 5) -> Dict[str, Any]:
        """FalkorDB PageRank over the market graph. High score = a feature many competitors offer AND many complaints
        point at, i.e. the contested ground everyone fights over."""
        return run("""
CALL algo.pageRank(null, null) YIELD node, score
WITH node, score WHERE node:Feature
RETURN node.id, node.name, round(score * 1000) / 1000.0 ORDER BY score DESC LIMIT $limit""",
                   {"limit": int(limit)}, ["feature_id", "feature", "pagerank"])

    @server.tool()
    def market_segments() -> Dict[str, Any]:
        """FalkorDB label propagation community detection. Competitors in the same community share features and
        complaints; a community with many complaints and few competitors is an opening."""
        return run("""
CALL algo.labelPropagation({nodeLabels: ['Competitor', 'Feature', 'Grievance']}) YIELD node, communityId
WITH communityId, collect(node) AS members
RETURN communityId,
       [m IN members WHERE m:Competitor | m.name],
       [m IN members WHERE m:Feature | m.name],
       size([m IN members WHERE m:Grievance]),
       [m IN members | m.id]
ORDER BY size([m IN members WHERE m:Grievance]) DESC""", {}, ["community", "competitors", "features", "complaints", "member_ids"])

    @server.tool()
    def evidence_for(feature: str) -> Dict[str, Any]:
        """Every path proving users struggle with a feature: competitor -> complaint (with source URL) -> feature."""
        return run("""
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor)-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature)
WHERE toLower(f.name) CONTAINS toLower($feature)
RETURN c.id, c.name, g.id, g.text, g.url, f.id""", {"feature": feature},
                   ["competitor_id", "competitor", "grievance_id", "quote", "source_url", "feature_id"])

    @server.tool()
    def predict_price(feature_count: int, complaints_addressed: int = 0) -> Dict[str, Any]:
        """TabPFN regression trained on the paid competitors stored in FalkorDB (the market_history graph):
        predicts the monthly price paid products charge with this many features and fixed complaints, plus the share of free apps."""
        if not (graph.is_live and graph._falkor):
            return {"error": "FalkorDB offline", "rows": []}
        cypher = "MATCH (s:Sample) WHERE s.price IS NOT NULL RETURN s.n_features, s.n_complaints, s.price"  # free (0) is real data; unknown is not
        data = graph._falkor.select_graph("market_history").query(cypher).result_set
        if len(data) < 5:
            return {"error": f"only {len(data)} competitors in history; need 5", "cypher": cypher, "rows": []}
        key = settings.tabpfn_api_key or os.getenv("TABPFN_API_KEY")
        if not key or os.getenv("PYTEST_CURRENT_TEST"):
            return {"error": "TabPFN key not set", "cypher": cypher, "rows": []}
        import numpy as np
        import tabpfn_client
        tabpfn_client.set_access_token(key)
        free_share = sum(1 for r in data if not r[2]) / len(data)
        paid = [r for r in data if r[2]]
        # most apps are free: learn what PAID products charge, report the free share. TabPFN rejects constant features.
        data = paid if len(paid) >= 3 and len({(r[0], r[1]) for r in paid}) > 1 else data
        x = np.array([[r[0], r[1]] for r in data], dtype=float)
        y = np.array([r[2] for r in data], dtype=float)
        median = float(np.median([r[2] for r in paid])) if paid else 0.0
        method = "TabPFN"
        try:
            model = tabpfn_client.TabPFNRegressor()
            model.fit(x, y)
            pred = float(model.predict(np.array([[feature_count, complaints_addressed]], dtype=float))[0])
        except Exception as e:  # too little variety in the history: say so and use the paid median, never fail the step
            logger.warning("TabPFN predict failed: %s", e)
            pred, method = median, "median of paid competitors (TabPFN needs more varied history)"
        return {"cypher": cypher, "rows": [{"predicted_price_usd": round(pred, 2), "trained_on": len(data), "method": method,
                                            "market_median_usd": round(median, 2),
                                            "free_share": round(free_share, 2)}], "node_ids": []}

    @server.tool()
    def saturation_score() -> Dict[str, Any]:
        """EXPERIMENTAL, unvalidated heuristic: how crowded the market is (0-100) and a suggested entry price, from live graph counts."""
        stats = run("""
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor) WHERE c.name <> $general
OPTIONAL MATCH (c)-[:PRICED_AT]->(p:Pricing)
OPTIONAL MATCH (c)-[:DRAWS_COMPLAINT]->(g:Grievance)
RETURN count(DISTINCT c), avg(p.amount_usd), count(DISTINCT g)""", {}, ["competitors", "avg_price", "complaints"])
        if stats.get("error") or not stats["rows"]:
            return stats
        r = stats["rows"][0]
        stats["tabpfn"] = evaluate_market_saturation(competitor_count=r["competitors"], median_price_usd=None if r["avg_price"] is None else float(r["avg_price"]),
                                                     complaint_count=r["complaints"], has_open_source=1, review_volume=250)
        return stats

    @server.tool()
    def search_more_complaints(query: str) -> Dict[str, Any]:
        """ACTION: run a live web search for more user complaints and add them to the graph. Use when evidence for
        a feature is thin (fewer than 2 quotes). Costs a paid search, so use at most once."""
        if not search_fn:
            return {"error": "search unavailable", "rows": []}
        if searches_left[0] <= 0:  # each search costs paid API calls: hard cap in code, not just in the prompt
            return {"error": "search already used this turn", "rows": []}
        searches_left[0] -= 1
        added = [c for c in search_fn(query).get("complaints", []) if c.get("text")]
        cypher = graph.insert_market_nodes(idea_raw, idea_text, [], added)
        return {"cypher": "\n".join(q for q in cypher if "Grievance" in q)[:600], "added": len(added), "rows": [
            {"competitor": c.get("competitor"), "quote": c.get("text"), "source_url": c.get("source_url")} for c in added],
            "node_ids": [entity_id("griev", c["text"]) for c in added]}

    @server.tool()
    def record_decision(decision: str, because_ids: List[str]) -> Dict[str, Any]:
        """ACTION: write a product decision into the graph, linked by BECAUSE edges to the node ids that justify it
        (complaints, competitors, features from earlier tool results). Always end by recording one decision."""
        d_id = entity_id("decision", f"{idea_node}:{decision}")
        out = run("""
MATCH (u:UserIdea {id: $idea})
MERGE (d:Decision {id: $d_id}) SET d.text = $text, d.at = timestamp()
MERGE (d)-[:DECIDED_FOR]->(u)
WITH d UNWIND $because AS bid
MATCH (n {id: bid}) MERGE (d)-[:BECAUSE]->(n)
RETURN d.id, count(n), collect(n.id)""", {"d_id": d_id, "text": decision, "because": list(because_ids)},
                  ["decision_id", "linked", "linked_ids"])
        graph.add_decision(idea_node, d_id, decision, list(because_ids))
        return out

    if not actions:  # read-only turns (questions) cannot write to the graph or spend on searches
        server.remove_tool("record_decision")
        server.remove_tool("search_more_complaints")
    return server


async def run_graph_agent(server: MCPServer, goal: str, ask_llm: Callable[[str], Optional[Dict[str, Any]]],
                          on_step: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """ReAct loop over MCP: list tools, let Gemma pick one per step, feed back the observation, stop on a final answer.
    Without an LLM it follows a fixed evidence plan so the graph work (and the demo) never silently disappears."""
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        catalog = "\n".join(f"- {t.name}({', '.join((t.input_schema or {}).get('properties', {}))}): {t.description}" for t in tools)
        history: List[Dict[str, Any]] = []
        fallback = [("market_overview", {}), ("complaint_hotspots", {}), ("central_features", {}), ("predict_price", None), ("saturation_score", {})]
        for step in range(MAX_STEPS + 1):
            missing = [r for r in REQUIRED if r in {t.name for t in tools} and r not in {h["tool"] for h in history}]
            forced = bool(missing) and MAX_STEPS - step <= len(missing)  # budget only fits the required tools: skip the LLM call
            obs ="\n".join(f"STEP {i + 1}: {h['tool']}({json.dumps(h['args'])}) -> {json.dumps(h['result'].get('rows', h['result']))[:800]}"
                            for i, h in enumerate(history))
            choice = await asyncio.to_thread(ask_llm, 
                "You are a market-intelligence agent working ONLY through these graph tools (served over MCP):\n"
                f"{catalog}\n\nGOAL: <user_input>{goal}</user_input>\n{INJECTION_RULE}\n\nObservations so far:\n{obs or 'none yet'}\n\n"
                "Pick the next tool, or finish. If record_decision is listed, finish only after calling it. Reply with JSON only, either\n"
                '{"thought": "why this tool", "tool": "name", "args": {}}\n'
                'or {"final": "markdown answer citing quotes and numbers from the observations", "cited_ids": ["node ids"]}'
            ) if step < MAX_STEPS and not forced else None
            if not choice and not forced:  # no LLM (or step budget spent): deterministic plan, ends with a recorded decision
                done = {h["tool"] for h in history}
                nxt = None if step >= MAX_STEPS else next(((n, a) for n, a in fallback if n not in done), None)  # budget spent: go record
                if nxt is None and "record_decision" not in done and "record_decision" in {t.name for t in tools}:
                    top = next((h["result"]["rows"][0] for h in history if h["tool"] == "complaint_hotspots" and h["result"].get("rows")), None)
                    nxt = ("record_decision", {"decision": f"Lead with {top['feature']}: {top['complaints']} complaints across {', '.join(top['competitors'])}",
                                               "because_ids": top["grievance_ids"] + [top["feature_id"]]}) if top else None
                if nxt is None:
                    break
                if nxt[1] is None:
                    nxt = (nxt[0], _price_args(history))
                choice = {"thought": "fixed evidence plan (no LLM available)", "tool": nxt[0], "args": nxt[1]}
            if missing and (forced or "final" in choice or choice.get("tool") == "record_decision"):
                # enforce in code, not the prompt: no answer or decision without the hotspots and the TabPFN price
                choice = {"thought": f"required before deciding: {missing[0]}", "tool": missing[0],
                          "args": _price_args(history) if missing[0] == "predict_price" else {}}
            if "final" in choice:
                return {"final": choice["final"], "cited_ids": choice.get("cited_ids", []), "steps": history}
            name, args = choice.get("tool"), choice.get("args") or {}
            if name not in {t.name for t in tools}:
                history.append({"tool": str(name), "args": args, "thought": choice.get("thought", ""), "result": {"error": "unknown tool"}})
                continue
            try:
                with agent_span("gen_ai.execute_tool", f"execute_tool {name}", {"gen_ai.tool.name": name, "gen_ai.tool.input": json.dumps(args)[:500]}):
                    res = await client.call_tool(name, args)
                result = res.structured_content or json.loads(res.content[0].text)
                result = result.get("result", result)
            except Exception as e:  # a bad tool call is an observation, not a crash
                result = {"error": str(e)}
            entry = {"tool": name, "args": args, "thought": choice.get("thought", ""), "result": result}
            history.append(entry)
            on_step(entry)
        return {"final": "", "cited_ids": [], "steps": history}
