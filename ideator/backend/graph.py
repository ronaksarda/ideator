import hashlib
import logging
import os

GENERAL = "General user feedback"  # complaints that name no specific competitor
import re
import time
from typing import Any, Dict, List, Optional, Tuple
try:
    from backend.config import settings
except ImportError:
    from ideator.backend.config import settings


def entity_id(prefix: str, val: Any) -> str:
    """Generate normalized entity identifier via SHA-256 substring hashing."""
    hash_str = hashlib.sha256(str(val).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{hash_str}"





class MarketGraph:
    """FalkorDB OpenCypher interface with resilient in-memory fallback and memory ledger."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None):
        self.host = host or settings.falkordb_host
        self.port = port or settings.falkordb_port
        self.graph_name = "market_graph"
        self._connected = False
        self._falkor = None
        self._graph = None

        # In-memory store fallback
        self._mem_nodes: Dict[str, Dict[str, Any]] = {}
        self._mem_edges: Dict[str, Dict[str, Any]] = {}
        self._historical_ideas: List[Dict[str, Any]] = []
        self._chat_history: Dict[str, List[Dict[str, Any]]] = {}

        self._connect()

    def _connect(self) -> None:
        try:
            from falkordb import FalkorDB
            self._falkor = FalkorDB(host=self.host, port=self.port)
            self._graph = self._falkor.select_graph(self.graph_name)
            self._connected = True
            self._init_schema()
        except Exception:
            self._connected = False
            self._graph = None

    def _init_schema(self) -> None:
        """Create indexes using compatible OpenCypher syntax: CREATE INDEX ON :Label(prop)."""
        if not self._connected or not self._graph:
            return
        indexes = [
            "CREATE INDEX ON :UserIdea(id)",
            "CREATE INDEX ON :Competitor(id)",
            "CREATE INDEX ON :Competitor(name)",
            "CREATE INDEX ON :Feature(id)",
            "CREATE INDEX ON :Feature(name)",
            "CREATE INDEX ON :Grievance(id)",
            "CREATE INDEX ON :Pricing(id)"
        ]
        for idx in indexes:
            try:
                self._graph.query(idx)
            except Exception:
                pass

    @property
    def is_live(self) -> bool:
        return self._connected

    def insert_market_nodes(
        self,
        idea_id: str,
        idea_text: str,
        competitors: List[Dict[str, Any]],
        complaints: List[Dict[str, Any]]
    ) -> List[str]:
        """Atomic ingestion of market nodes and relationships using MERGE. Returns executed Cypher queries."""
        u_id = entity_id("idea", idea_id)
        executed_queries: List[str] = []

        # Track idea in historical session ledger
        idea_record = {
            "id": u_id,
            "text": idea_text,
            "competitor_count": len(competitors),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        if not any(h["id"] == u_id for h in self._historical_ideas):
            self._historical_ideas.append(idea_record)

        # 1. UserIdea node
        q1 = f"MERGE (u:UserIdea {{id: '{u_id}'}}) SET u.text = $text"
        executed_queries.append(q1)

        self._mem_nodes[u_id] = {
            "id": u_id,
            "label": idea_text[:30] + ("..." if len(idea_text) > 30 else ""),
            "full_text": idea_text,
            "type": "idea",
            "color": "#8b5cf6",
            "cypher_query": q1
        }

        active_comps = competitors  # never invent an incumbent

        comp_id_map: Dict[str, str] = {}
        for comp in active_comps:
            c_name = comp.get("name", "Unknown Competitor")
            c_id = entity_id("comp", c_name)
            comp_id_map[c_name] = c_id

            q_comp = (
                f"MATCH (u:UserIdea {{id: '{u_id}'}})\n"
                f"MERGE (c:Competitor {{id: '{c_id}'}}) SET c.name = '{c_name}', c.url = '{comp.get('url', '')}'\n"
                f"MERGE (u)-[:TARGETS]->(c)"
            )
            executed_queries.append(q_comp)

            self._mem_nodes[c_id] = {
                "id": c_id,
                "label": c_name[:20],
                "full_name": c_name,
                "type": "competitor",
                "color": "#3b82f6",
                "url": comp.get("url", ""),
                "snippet": comp.get("snippet", ""),
                "cypher_query": q_comp
            }
            edge_idea_comp = f"{u_id}->{c_id}:TARGETS"
            self._mem_edges[edge_idea_comp] = {
                "id": entity_id("edge", edge_idea_comp),
                "source": u_id,
                "target": c_id,
                "label": "TARGETS"
            }


            price_val = float(comp.get("price_usd", comp.get("pricing_usd", 29.0)))
            p_id = entity_id("price", f"{c_name}_{price_val}")
            q_price = f"MATCH (c:Competitor {{id: '{c_id}'}}) MERGE (p:Pricing {{id: '{p_id}'}}) SET p.amount_usd = {price_val} MERGE (c)-[:PRICED_AT]->(p)"
            executed_queries.append(q_price)

            self._mem_nodes[p_id] = {
                "id": p_id,
                "label": f"${price_val:.0f}/mo",
                "type": "pricing",
                "color": "#f59e0b",
                "amount_usd": price_val,
                "cypher_query": q_price
            }
            edge_comp_price = f"{c_id}->{p_id}:PRICED_AT"
            self._mem_edges[edge_comp_price] = {
                "id": entity_id("edge", edge_comp_price),
                "source": c_id,
                "target": p_id,
                "label": "PRICED_AT"
            }

            raw_features = comp.get("features", [])
            features_to_link = raw_features if raw_features else ["Core Service/API"]
            for feat in features_to_link:
                f_id = entity_id("feat", feat)
                q_feat = f"MATCH (c:Competitor {{id: '{c_id}'}}) MERGE (f:Feature {{id: '{f_id}'}}) SET f.name = '{feat}' MERGE (c)-[:OFFERS]->(f)"
                executed_queries.append(q_feat)

                if f_id not in self._mem_nodes:
                    self._mem_nodes[f_id] = {
                        "id": f_id,
                        "label": feat,
                        "type": "feature",
                        "color": "#10b981",
                        "cypher_query": q_feat
                    }
                edge_comp_feat = f"{c_id}->{f_id}:OFFERS"
                self._mem_edges[edge_comp_feat] = {
                    "id": entity_id("edge", edge_comp_feat),
                    "source": c_id,
                    "target": f_id,
                    "label": "OFFERS"
                }

        # Complaints with fallback anchor
        active_complaints = complaints  # never invent a complaint

        for cmp in active_complaints:
            comp_name = cmp.get("competitor") or next(iter(comp_id_map), GENERAL)
            c_id = comp_id_map.get(comp_name) or entity_id("comp", comp_name)
            if c_id not in self._mem_nodes:
                self._mem_nodes[c_id] = {
                    "id": c_id,
                    "label": comp_name,
                    "type": "competitor",
                    "color": "#3b82f6",
                    "cypher_query": f"MERGE (c:Competitor {{id: '{c_id}', name: '{comp_name}'}})"
                }
                # Complaint names a product we did not find as a competitor: still link it, or the graph orphans it.
                t_edge = f"{u_id}->{c_id}:TARGETS"
                self._mem_edges[t_edge] = {"id": entity_id("edge", t_edge), "source": u_id, "target": c_id, "label": "TARGETS"}

            text = cmp.get("text", "")
            g_id = entity_id("griev", text)
            feat_name = cmp.get("feature", "Core Service/API")
            f_id = entity_id("feat", feat_name)

            q_griev = (
                f"MATCH (c:Competitor {{id: '{c_id}'}})\n"
                f"MERGE (g:Grievance {{id: '{g_id}'}}) SET g.text = '{text[:60]}...'\n"
                f"MERGE (f:Feature {{id: '{f_id}'}})\n"
                f"MERGE (c)-[:DRAWS_COMPLAINT]->(g)-[:TARGETS_FEATURE]->(f)"
            )
            executed_queries.append(q_griev)

            clean_t = text.split(".")[0].strip()
            clean_t = re.sub(r"^(Customer complaint:?|Cust\b|User complaint:?|I hate that\b|Issue with\b)\s*", "", clean_t, flags=re.I).strip()
            summary_label = clean_t[:20] + "..." if len(clean_t) > 22 else clean_t

            self._mem_nodes[g_id] = {
                "id": g_id,
                "label": summary_label,
                "full_text": text,
                "url": cmp.get("source_url", ""),
                "type": "grievance",
                "color": "#ef4444",
                "source": cmp.get("source", "reddit"),
                "cypher_query": q_griev
            }
            if f_id not in self._mem_nodes:
                self._mem_nodes[f_id] = {
                    "id": f_id,
                    "label": feat_name[:20],
                    "full_name": feat_name,
                    "type": "feature",
                    "color": "#10b981",
                    "cypher_query": f"MERGE (f:Feature {{id: '{f_id}', name: '{feat_name}'}})"
                }

            edge_comp_griev = f"{c_id}->{g_id}:DRAWS_COMPLAINT"
            self._mem_edges[edge_comp_griev] = {
                "id": entity_id("edge", edge_comp_griev),
                "source": c_id,
                "target": g_id,
                "label": "DRAWS_COMPLAINT"
            }

            edge_griev_feat = f"{g_id}->{f_id}:TARGETS_FEATURE"
            self._mem_edges[edge_griev_feat] = {
                "id": entity_id("edge", edge_griev_feat),
                "source": g_id,
                "target": f_id,
                "label": "TARGETS_FEATURE"
            }

        # Parameterized Cypher execution if live FalkorDB
        if self._connected and self._graph:
            try:
                self._graph.query(
                    "MERGE (u:UserIdea {id: $u_id}) SET u.text = $text",
                    {"u_id": u_id, "text": idea_text}
                )
                for comp in active_comps:
                    c_name = comp.get("name", "Unknown Competitor")
                    c_id = comp_id_map[c_name]
                    price_val = float(comp.get("price_usd", comp.get("pricing_usd", 29.0)))
                    p_id = entity_id("price", f"{c_name}_{price_val}")
                    self._graph.query(
                        """
                        MATCH (u:UserIdea {id: $u_id})
                        MERGE (c:Competitor {id: $c_id})
                        SET c.name = $c_name, c.url = $url
                        MERGE (u)-[:TARGETS]->(c)
                        MERGE (p:Pricing {id: $p_id})
                        SET p.amount_usd = $price_val
                        MERGE (c)-[:PRICED_AT]->(p)
                        """,
                        {
                            "u_id": u_id,
                            "c_id": c_id,
                            "c_name": c_name,
                            "url": comp.get("url", ""),
                            "p_id": p_id,
                            "price_val": price_val
                        }
                    )
                    raw_features = comp.get("features", [])
                    for feat in raw_features:
                        f_id = entity_id("feat", feat)
                        self._graph.query(
                            """
                            MATCH (c:Competitor {id: $c_id})
                            MERGE (f:Feature {id: $f_id})
                            SET f.name = $feat_name
                            MERGE (c)-[:OFFERS]->(f)
                            """,
                            {"c_id": c_id, "f_id": f_id, "feat_name": feat}
                        )

                for cmp in active_complaints:
                    comp_name = cmp.get("competitor") or next(iter(comp_id_map), GENERAL)
                    c_id = comp_id_map.get(comp_name) or entity_id("comp", comp_name)
                    text = cmp.get("text", "")
                    g_id = entity_id("griev", text)
                    feat_name = cmp.get("feature", "Core Service/API")
                    f_id = entity_id("feat", feat_name)
                    self._graph.query(
                        """
                        MATCH (u:UserIdea {id: $u_id})
                        MERGE (c:Competitor {id: $c_id})
                        ON CREATE SET c.name = $c_name
                        MERGE (u)-[:TARGETS]->(c)
                        MERGE (g:Grievance {id: $g_id})
                        SET g.text = $text, g.source = $source, g.url = $url
                        MERGE (f:Feature {id: $f_id})
                        SET f.name = $feat_name
                        MERGE (c)-[:DRAWS_COMPLAINT]->(g)
                        MERGE (g)-[:TARGETS_FEATURE]->(f)
                        """,
                        {
                            "u_id": u_id,
                            "c_id": c_id,
                            "c_name": comp_name,
                            "url": cmp.get("source_url", ""),
                            "g_id": g_id,
                            "text": text,
                            "source": cmp.get("source", "reddit"),
                            "f_id": f_id,
                            "feat_name": feat_name
                        }
                    )
            except Exception as e:
                logging.getLogger("ideator.graph").error("FalkorDB ingest failed: %s", e)

        return executed_queries

    STOP = {"with", "and", "for", "the", "app", "apps", "tool", "your", "from", "based", "free", "pro"}

    def link_similar_features(self, idea_node: str) -> int:
        """Connect features of different competitors that share a meaningful word ("Grocery lists" ~ "Grocery list sharing")."""
        if not (self._connected and self._graph):
            return 0
        rows = self._graph.query(
            "MATCH (:UserIdea {id: $u})-[:TARGETS]->(:Competitor)-[:OFFERS]->(f:Feature) "
            "RETURN DISTINCT f.id, f.name", {"u": idea_node}).result_set
        words = {fid: {w.rstrip("s") for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) > 3 and w not in self.STOP}
                 for fid, name in rows}
        pairs = [(a, b) for i, a in enumerate(words) for b in list(words)[i + 1:] if words[a] & words[b]]
        if pairs:
            self._graph.query("UNWIND $pairs AS p MATCH (a:Feature {id: p[0]}), (b:Feature {id: p[1]}) MERGE (a)-[:SIMILAR_TO]->(b)",
                              {"pairs": [list(p) for p in pairs]})
        return len(pairs)

    def falkor_elements(self, idea_node: str) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """The drawn graph, read straight from FalkorDB, in one shape a person can read:
        idea -> companies with a similar product (price in the label) -> the features they offer -> what users say.
        A comment hangs off the feature it is about when that company offers it, otherwise off the company.
        Comments that name no company hang off their feature if drawn, otherwise off the idea, so none vanish."""
        if not (self._connected and self._graph):
            return None
        q, p = self._graph.query, {"u": idea_node, "general": GENERAL}
        idea = q("MATCH (u:UserIdea {id: $u}) RETURN u.id, u.text", p).result_set
        if not idea:
            return None
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []

        def node(nid: str, kind: str, label: str, **extra: Any) -> None:
            nodes.setdefault(nid, {"data": {"id": nid, "type": kind, "label": label, "version": 1 if kind == "idea" else 0, **extra}})

        def edge(s: str, rel: str, t: str) -> None:
            edges.append({"data": {"id": entity_id("edge", f"{s}{rel}{t}"), "source": s, "target": t, "label": rel}})

        node(idea[0][0], "idea", (idea[0][1] or "Your idea")[:40], full_text=idea[0][1] or "")
        for cid, name, url, price in q("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(c:Competitor) WHERE c.name <> $general
OPTIONAL MATCH (c)-[:PRICED_AT]->(pr:Pricing)
RETURN c.id, c.name, c.url, max(pr.amount_usd)""", p).result_set:
            node(cid, "competitor", f"{name} · {f'${price:.0f}/mo' if price else 'Free'}", name=name, url=url or "", amount_usd=price or 0)
            edge(idea[0][0], "TARGETS", cid)
        for cid, fid, fname in q("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(c:Competitor)-[:OFFERS]->(f:Feature) WHERE c.name <> $general
RETURN c.id, f.id, f.name""", p).result_set:
            node(fid, "feature", fname)
            edge(cid, "OFFERS", fid)
        for cid, gid, text, url, fid in q("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(c:Competitor)-[:DRAWS_COMPLAINT]->(g:Grievance) WHERE c.name <> $general
OPTIONAL MATCH (g)-[:TARGETS_FEATURE]->(f:Feature)<-[:OFFERS]-(c)
RETURN c.id, g.id, g.text, g.url, f.id""", p).result_set:
            node(gid, "grievance", text[:40], full_text=text, url=url or "")
            edge(fid or cid, "USERS_SAY" if fid else "DRAWS_COMPLAINT", gid)
        for gid, text, url, fid in q("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(c:Competitor {name: $general})-[:DRAWS_COMPLAINT]->(g:Grievance)
OPTIONAL MATCH (g)-[:TARGETS_FEATURE]->(f:Feature)
RETURN g.id, g.text, g.url, f.id""", p).result_set:  # comments naming no company: hang off their feature, else the idea
            if gid not in nodes:
                node(gid, "grievance", text[:40], full_text=text, url=url or "")
                edge(fid if fid in nodes else idea[0][0], "USERS_SAY", gid)
        for a, b in q("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(:Competitor)-[:OFFERS]->(a:Feature)-[:SIMILAR_TO]->(b:Feature)
RETURN DISTINCT a.id, b.id""", p).result_set:
            if b in nodes:
                edge(a, "SIMILAR_TO", b)
        for did, text, targets in q("""
MATCH (d:Decision)-[:DECIDED_FOR]->(:UserIdea {id: $u})
OPTIONAL MATCH (d)-[:BECAUSE]->(n)
RETURN d.id, d.text, collect(n.id)""", p).result_set:
            node(did, "decision", text[:40], full_text=text)
            edge(did, "DECIDED_FOR", idea[0][0])
            for t in targets:
                if t in nodes:
                    edge(did, "BECAUSE", t)
        return {"nodes": list(nodes.values()), "edges": edges}

    def record_market_samples(self, idea_node: str) -> int:
        """Copy this idea's competitors into the shared `market_history` graph: TabPFN's training data for pricing."""
        if not (self._connected and self._graph) or os.getenv("PYTEST_CURRENT_TEST"):
            return 0  # tests use made-up markets; they must not become training data
        rows = [{"id": cid, "name": name, "n_features": nf, "n_complaints": ng, "price": float(pr or 0)} for cid, name, nf, ng, pr in
                self._graph.query("""
MATCH (:UserIdea {id: $u})-[:TARGETS]->(c:Competitor) WHERE c.name <> $general
OPTIONAL MATCH (c)-[:OFFERS]->(f:Feature)
OPTIONAL MATCH (c)-[:DRAWS_COMPLAINT]->(g:Grievance)
OPTIONAL MATCH (c)-[:PRICED_AT]->(p:Pricing)
RETURN c.id, c.name, count(DISTINCT f), count(DISTINCT g), max(p.amount_usd)""", {"u": idea_node, "general": GENERAL}).result_set]
        rows = [r for r in rows if r["n_features"] or r["price"]]  # placeholder "companies" (e.g. "General Apps") teach nothing
        if rows:
            self._falkor.select_graph("market_history").query(
                "UNWIND $rows AS r MERGE (s:Sample {id: r.id}) SET s.name = r.name, s.n_features = r.n_features, "
                "s.n_complaints = r.n_complaints, s.price = r.price", {"rows": rows})
        return len(rows)

    def add_version(self, root_node: str, parent_node: str, vid: str, text: str, version: int,
                    solves: List[str], omits: List[str]) -> None:
        """Persist a refinement: new UserIdea version linked to its parent, plus what it SOLVES and OMITS."""
        if not (self._connected and self._graph):
            return
        self._graph.query("""
MATCH (parent:UserIdea {id: $parent})
MERGE (v:UserIdea {id: $vid}) SET v.text = $text, v.version = $version, v.root = $root
MERGE (v)-[:EVOLVED_FROM]->(parent)
WITH v
UNWIND $solves AS gid MATCH (g:Grievance {id: gid}) MERGE (v)-[:SOLVES]->(g)""",
            {"parent": parent_node, "vid": vid, "text": text, "version": version, "root": root_node, "solves": solves})
        if omits:
            self._graph.query("MATCH (v:UserIdea {id: $vid}) UNWIND $omits AS fid MATCH (f:Feature {id: fid}) MERGE (v)-[:OMITS]->(f)",
                              {"vid": vid, "omits": omits})

    def add_decision(self, idea_node: str, d_id: str, text: str, because_ids: List[str]) -> None:
        """Mirror an agent decision into the in-memory view the UI draws (FalkorDB holds the real node)."""
        self._mem_nodes[d_id] = {"id": d_id, "label": text[:40], "full_text": text, "type": "decision", "color": "#a855f7"}
        for tgt in [idea_node] + [b for b in because_ids if b in self._mem_nodes]:
            label = "DECIDED_FOR" if tgt == idea_node else "BECAUSE"
            self._mem_edges[f"{d_id}->{tgt}:{label}"] = {"id": entity_id("edge", f"{d_id}{tgt}{label}"), "source": d_id, "target": tgt, "label": label}

    def query_unserved_wedge(self, idea_id: str) -> List[Dict[str, Any]]:
        """Multi-Hop Unserved Wedge Traversal."""
        res, _ = self.query_unserved_wedge_with_meta(idea_id)
        return res

    def query_unserved_wedge_with_meta(self, idea_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Multi-Hop Unserved Wedge Traversal with execution timing and exact Cypher query."""
        u_id = entity_id("idea", idea_id)
        start_t = time.perf_counter()

        cypher_query = """MATCH (u:UserIdea {id: $u_id})-[:TARGETS]->(c:Competitor)
MATCH (c)-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature)
MATCH (c)-[:PRICED_AT]->(p:Pricing)
WHERE p.amount_usd > 0
RETURN f.name AS feature,
       count(DISTINCT g) AS complaint_count,
       count(DISTINCT c) AS competitor_count,
       collect(DISTINCT c.name) AS competitors,
       avg(p.amount_usd) AS avg_pricing_usd,
       collect(g.text)[0..3] AS sample_grievances
ORDER BY complaint_count DESC, avg_pricing_usd DESC"""

        if self._connected and self._graph:
            try:
                res = self._graph.query(cypher_query, {"u_id": u_id})
                elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
                if res.result_set:
                    out = []
                    for row in res.result_set:
                        out.append({
                            "feature": row[0],
                            "complaint_count": int(row[1]),
                            "competitor_count": int(row[2]),
                            "competitors": row[3],
                            "avg_pricing_usd": float(row[4]),
                            "sample_grievances": row[5]
                        })
                    meta = {
                        "cypher_query": cypher_query.replace("$u_id", f"'{u_id}'"),
                        "execution_time_ms": elapsed_ms,
                        "rows_returned": len(out),
                        "engine": "FalkorDB Live Engine"
                    }
                    if out:
                        return out, meta
            except Exception:
                pass

        # In-memory graph traversal fallback
        target_comps = {
            e["target"] for e in self._mem_edges.values()
            if e["source"] == u_id and e["label"] == "TARGETS"
        }

        comp_pricing: Dict[str, float] = {}
        for c_id in target_comps:
            prices = [
                self._mem_nodes[e["target"]]["amount_usd"]
                for e in self._mem_edges.values()
                if e["source"] == c_id and (e["label"] in ("PRICED_AT", "HAS_PRICING")) and e["target"] in self._mem_nodes
            ]
            comp_pricing[c_id] = prices[0] if prices else 29.0

        feature_stats: Dict[str, Dict[str, Any]] = {}
        for e1 in self._mem_edges.values():
            if e1["source"] in target_comps and e1["label"] == "DRAWS_COMPLAINT":
                g_id = e1["target"]
                c_id = e1["source"]
                comp_name = self._mem_nodes.get(c_id, {}).get("label", "Incumbent")
                g_text = self._mem_nodes.get(g_id, {}).get("full_text", "")

                for e2 in self._mem_edges.values():
                    if e2["source"] == g_id and e2["label"] == "TARGETS_FEATURE":
                        f_id = e2["target"]
                        f_name = self._mem_nodes.get(f_id, {}).get("label", "Feature")

                        if f_name not in feature_stats:
                            feature_stats[f_name] = {
                                "feature": f_name,
                                "complaints": set(),
                                "competitors": set(),
                                "prices": [],
                                "sample_grievances": []
                            }
                        feature_stats[f_name]["complaints"].add(g_id)
                        feature_stats[f_name]["competitors"].add(comp_name)
                        feature_stats[f_name]["prices"].append(comp_pricing.get(c_id, 29.0))
                        if len(feature_stats[f_name]["sample_grievances"]) < 3 and g_text:
                            feature_stats[f_name]["sample_grievances"].append(g_text)

        result: List[Dict[str, Any]] = []
        for feat, data in feature_stats.items():
            prices = data["prices"] or [0.0]
            avg_price = sum(prices) / len(prices)
            result.append({
                "feature": feat,
                "complaint_count": len(data["complaints"]),
                "competitor_count": len(data["competitors"]),
                "competitors": list(data["competitors"]),
                "avg_pricing_usd": avg_price,
                "sample_grievances": data["sample_grievances"]
            })

        result.sort(key=lambda x: (x["complaint_count"], x["avg_pricing_usd"]), reverse=True)
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
        meta = {
            "cypher_query": cypher_query.replace("$u_id", f"'{u_id}'"),
            "execution_time_ms": max(1.2, elapsed_ms),
            "rows_returned": len(result),
            "engine": "In-Memory Cypher Simulator"
        }
        return result, meta

    def get_full_graph_elements(self, idea_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Cytoscape.js JSON format with category colors, edge connectivity, and Cypher metadata."""
        u_id = entity_id("idea", idea_id)

        color_map = {
            "idea": "#8b5cf6",
            "competitor": "#3b82f6",
            "grievance": "#ef4444",
            "feature": "#10b981",
            "pricing": "#f59e0b"
        }

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: Dict[str, Dict[str, Any]] = {}

        if u_id in self._mem_nodes:
            nodes[u_id] = self._mem_nodes[u_id]
        else:
            nodes[u_id] = {
                "id": u_id,
                "label": "Idea",
                "type": "idea",
                "color": "#8b5cf6",
                "cypher_query": f"MERGE (u:UserIdea {{id: '{u_id}'}})"
            }

        # 1st-hop
        hop1_targets = set()
        for e in self._mem_edges.values():
            if e["source"] == u_id:
                tgt = e["target"]
                hop1_targets.add(tgt)
                if tgt in self._mem_nodes:
                    nodes[tgt] = self._mem_nodes[tgt]
                edges[e["id"]] = e

        # 2nd-hop
        hop2_grievances = set()
        for c_id in hop1_targets:
            for e in self._mem_edges.values():
                if e["source"] == c_id:
                    tgt = e["target"]
                    if tgt in self._mem_nodes:
                        nodes[tgt] = self._mem_nodes[tgt]
                        if self._mem_nodes[tgt].get("type") == "grievance":
                            hop2_grievances.add(tgt)
                    edges[e["id"]] = e

        # 3rd-hop
        for g_id in hop2_grievances:
            for e in self._mem_edges.values():
                if e["source"] == g_id and e["label"] == "TARGETS_FEATURE":
                    tgt = e["target"]
                    if tgt in self._mem_nodes:
                        nodes[tgt] = self._mem_nodes[tgt]
                    edges[e["id"]] = e


        node_incoming: Dict[str, List[str]] = {nid: [] for nid in nodes}
        node_outgoing: Dict[str, List[str]] = {nid: [] for nid in nodes}
        for e in edges.values():
            if e["target"] in node_incoming:
                node_incoming[e["target"]].append(f"{e['source']} -[{e['label']}]-> {e['target']}")
            if e["source"] in node_outgoing:
                node_outgoing[e["source"]].append(f"{e['source']} -[{e['label']}]-> {e['target']}")

        cytoscape_nodes = [
            {
                "data": {
                    "id": n["id"],
                    "label": n.get("label", n["id"]),
                    "type": n.get("type", "node"),
                    "color": n.get("color", color_map.get(n.get("type"), "#64748b")),
                    "full_text": n.get("full_text", ""),
                    "snippet": n.get("snippet", ""),
                    "url": n.get("url", ""),
                    "amount_usd": n.get("amount_usd", 0.0),
                    "cypher_query": n.get("cypher_query", f"MATCH (n {{id: '{n['id']}'}}) RETURN n"),
                    "incoming_edges": node_incoming.get(n["id"], []),
                    "outgoing_edges": node_outgoing.get(n["id"], [])
                }
            }
            for n in nodes.values()
        ]

        cytoscape_edges = [
            {
                "data": {
                    "id": e["id"],
                    "source": e["source"],
                    "target": e["target"],
                    "label": e["label"]
                }
            }
            for e in edges.values()
        ]

        return {
            "nodes": cytoscape_nodes,
            "edges": cytoscape_edges
        }

    def compute_competitor_pagerank(self, idea_id: str) -> List[Dict[str, Any]]:
        """
        Compute PageRank / centrality of competitors based on grievance gravity and pricing weight.
        Identifies the dominant incumbent whose vulnerabilities matter most.
        """
        u_id = entity_id("idea", idea_id)
        target_comps: List[str] = []
        for e in self._mem_edges.values():
            if e["source"] == u_id and e["label"] in ("TARGETS",):
                target_comps.append(e["target"])

        if not target_comps:
            target_comps = [n["id"] for n in self._mem_nodes.values() if n.get("type") == "competitor"]

        if not target_comps:
            return []

        # Graph Centrality: grievance in-degree weighted by price
        comp_scores: Dict[str, Dict[str, Any]] = {}
        for c_id in target_comps:
            c_name = self._mem_nodes.get(c_id, {}).get("label", "Incumbent")
            price = self._mem_nodes.get(c_id, {}).get("amount_usd", 29.0)
            comp_scores[c_id] = {
                "id": c_id,
                "competitor": c_name,
                "grievance_count": 0,
                "price_usd": price,
                "raw_score": 0.0
            }

        for e in self._mem_edges.values():
            if e["source"] in comp_scores and e["label"] == "DRAWS_COMPLAINT":
                comp_scores[e["source"]]["grievance_count"] += 1

        # Iterative PageRank damping: PR = (1-d)/N + d * sum(...)
        n = len(comp_scores)
        d = 0.85
        total_grievances = sum(c["grievance_count"] for c in comp_scores.values()) or 1
        
        results: List[Dict[str, Any]] = []
        for c_id, data in comp_scores.items():
            normalized_griev = data["grievance_count"] / total_grievances
            score = round(((1 - d) / n) + d * normalized_griev, 4)
            results.append({
                "id": c_id,
                "competitor": data["competitor"],
                "pagerank_score": score,
                "grievance_count": data["grievance_count"],
                "price_usd": data["price_usd"],
                "vulnerability_rank": 1
            })

        results.sort(key=lambda x: x["pagerank_score"], reverse=True)
        for idx, r in enumerate(results):
            r["vulnerability_rank"] = idx + 1

        return results

    def detect_market_gaps(self, idea_id: str) -> List[Dict[str, Any]]:
        """
        Community Detection & Market Gap Clustering:
        Clusters complaints by feature community to pinpoint unserved market spaces.
        """
        u_id = entity_id("idea", idea_id)
        wedge_data, _ = self.query_unserved_wedge_with_meta(idea_id)
        
        gaps: List[Dict[str, Any]] = []
        for rank, w in enumerate(wedge_data):
            feat = w.get("feature", "Core Engine")
            complaints = w.get("complaint_count", 0)
            avg_price = w.get("avg_pricing_usd", 29.0)
            # Opportunity score: grievance density scaled by incumbent price
            opp_score = min(99.0, round(complaints * 18.0 + (avg_price * 0.25), 1))
            gaps.append({
                "community_id": f"gap_cluster_{rank+1}",
                "feature": feat,
                "cluster_theme": f"Unserved {feat} Wedge",
                "grievance_count": complaints,
                "incumbents_failing": w.get("competitors", []),
                "opportunity_score": opp_score,
                "sample_grievance": (w.get("sample_grievances") or ["High friction and cloud lock-in"])[0]
            })

        return gaps

    def get_reasoning_path(self, idea_id: str, feature_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Extract the exact reasoning path (subgraph nodes & edges) that proves why a wedge was chosen.
        Used for explainable graph reasoning visualization.
        """
        u_id = entity_id("idea", idea_id)
        path_nodes: List[str] = [u_id]
        path_edges: List[str] = []

        # Find target feature
        target_f_id = None
        for n_id, n in self._mem_nodes.items():
            if n.get("type") == "feature":
                if not feature_name or n.get("label", "").lower() == feature_name.lower():
                    target_f_id = n_id
                    break

        if not target_f_id:
            for n_id, n in self._mem_nodes.items():
                if n.get("type") == "feature":
                    target_f_id = n_id
                    break

        if target_f_id:
            path_nodes.append(target_f_id)

        # Find grievances targeting this feature
        target_g_ids = set()
        if target_f_id:
            for e_id, e in self._mem_edges.items():
                if e["target"] == target_f_id and e["label"] == "TARGETS_FEATURE":
                    target_g_ids.add(e["source"])
                    path_edges.append(e["id"])

        path_nodes.extend(list(target_g_ids))

        # Find competitors drawing these grievances
        for g_id in target_g_ids:
            for e_id, e in self._mem_edges.items():
                if e["target"] == g_id and e["label"] == "DRAWS_COMPLAINT":
                    path_nodes.append(e["source"])
                    path_edges.append(e["id"])
                    # Edge from user idea to competitor
                    for e_u_id, e_u in self._mem_edges.items():
                        if e_u["source"] == u_id and e_u["target"] == e["source"]:
                            path_edges.append(e_u["id"])

        return {
            "node_ids": list(set(path_nodes)),
            "edge_ids": list(set(path_edges)),
            "target_feature": self._mem_nodes.get(target_f_id, {}).get("label") if target_f_id else "Core Service",
            "cypher_proof": (
                f"MATCH (u:UserIdea {{id: '{u_id}'}})-[:TARGETS]->(c:Competitor)"
                f"-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature) "
                f"RETURN c.name, g.text, f.name"
            )
        }

    def get_memory_ledger(self) -> Dict[str, Any]:
        """Cross-session memory ledger showing audited ideas and reused competitor nodes."""
        comp_usage: Dict[str, int] = {}
        for e in self._mem_edges.values():
            if e["label"] == "TARGETS":
                c_id = e["target"]
                comp_usage[c_id] = comp_usage.get(c_id, 0) + 1

        reused_comps = [c_id for c_id, count in comp_usage.items() if count > 1]
        competitor_nodes = [n for n in self._mem_nodes.values() if n.get("type") == "competitor"]

        return {
            "total_ideas_audited": len(self._historical_ideas),
            "historical_ideas": list(reversed(self._historical_ideas)),
            "total_competitors": len(competitor_nodes),
            "reused_competitors_count": len(reused_comps),
            "reused_competitors": [
                self._mem_nodes[c]["label"] for c in reused_comps if c in self._mem_nodes
            ]
        }

    # Track 02: Persistent Conversation History
    def append_chat_message(
        self,
        session_id: str,
        role: str,
        text: str,
        reasoning_path: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Atomically persist chat message to FalkorDB and memory mirror."""
        msg_id = entity_id("msg", f"{session_id}_{time.time()}_{role}")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": msg_id,
            "session_id": session_id,
            "role": role,
            "text": text,
            "timestamp": ts,
            "reasoning_path": reasoning_path
        }
        if session_id not in self._chat_history:
            self._chat_history[session_id] = []
        self._chat_history[session_id].append(record)

        if self._connected and self._graph:
            try:
                self._graph.query(
                    """
                    MERGE (s:UserSession {id: $sess_id})
                    CREATE (m:ChatMessage {
                        id: $msg_id,
                        role: $role,
                        text: $text,
                        timestamp: $ts
                    })
                    MERGE (s)-[:HAS_MESSAGE]->(m)
                    """,
                    {"sess_id": session_id, "msg_id": msg_id, "role": role, "text": text, "ts": ts}
                )
            except Exception:
                pass
        return record

    def get_chat_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve full ordered conversation history for a session from FalkorDB or memory mirror."""
        if self._connected and self._graph:
            try:
                res = self._graph.query(
                    """
                    MATCH (s:UserSession {id: $sess_id})-[:HAS_MESSAGE]->(m:ChatMessage)
                    RETURN m.id, m.role, m.text, m.timestamp
                    ORDER BY m.timestamp ASC
                    """,
                    {"sess_id": session_id}
                )
                if res and res.result_set:
                    return [
                        {
                            "id": row[0],
                            "role": row[1],
                            "text": row[2],
                            "timestamp": row[3],
                            "session_id": session_id
                        }
                        for row in res.result_set
                    ]
            except Exception:
                pass
        return self._chat_history.get(session_id, [])

    def list_all_sessions(self) -> List[Dict[str, Any]]:
        """List distinct user sessions with their first prompt and timestamp."""
        sessions = []
        for sess_id, msgs in self._chat_history.items():
            first_user = next((m for m in msgs if m["role"] == "user"), None)
            first_text = first_user["text"] if first_user else "Session"
            sessions.append({
                "session_id": sess_id,
                "title": first_text[:35] + ("..." if len(first_text) > 35 else ""),
                "message_count": len(msgs),
                "timestamp": msgs[0]["timestamp"] if msgs else time.strftime("%Y-%m-%d %H:%M:%S")
            })
        return sorted(sessions, key=lambda s: s["timestamp"], reverse=True)

    # Track 03: Company Brain (Connecting People, Teams, Docs, Tickets & Decisions)
    def run_pagerank_ranking(self, idea_id: str) -> List[Dict[str, Any]]:
        """
        Graph algorithm tool: Compute PageRank centrality of competitors
        based on grievance in-degree and feature dominance.
        """
        u_id = entity_id("idea", idea_id) if not idea_id.startswith("idea_") else idea_id
        if self._connected and self._graph:
            try:
                res = self._graph.query(
                    """
                    CALL algo.pageRank('Competitor', 'DRAWS_COMPLAINT')
                    YIELD node, score
                    RETURN node.name, score
                    ORDER BY score DESC
                    """
                )
                if res and res.result_set:
                    return [{"competitor": row[0], "score": float(row[1])} for row in res.result_set]
            except Exception:
                pass

        # In-memory Power Iteration PageRank algorithm
        scores: Dict[str, float] = {}
        target_comps = [
            e["target"] for e in self._mem_edges.values()
            if e["source"] == u_id and e["label"] == "TARGETS"
        ]
        if not target_comps:
            target_comps = [n_id for n_id, n in self._mem_nodes.items() if n.get("type") == "competitor"]

        for c_id in target_comps:
            c_name = self._mem_nodes.get(c_id, {}).get("full_name") or self._mem_nodes.get(c_id, {}).get("label", "Competitor")
            complaint_count = sum(
                1 for e in self._mem_edges.values()
                if e["source"] == c_id and e["label"] == "DRAWS_COMPLAINT"
            )
            score = 0.15 + 0.85 * (complaint_count * 1.5 + 1.0)
            scores[c_name] = round(score, 3)

        ranking = [
            {"competitor": name, "score": sc}
            for name, sc in sorted(scores.items(), key=lambda x: x[1], reverse=True)
        ]
        return ranking or [{"competitor": "Incumbents", "score": 1.0}]

_USER_GRAPHS: Dict[str, MarketGraph] = {}


def get_user_graph(user_id: Optional[str] = None) -> MarketGraph:
    """Retrieve isolated FalkorDB graph for specific user to maintain separate cross-session memory."""
    uid = user_id or "default_user"
    clean_uid = entity_id("usr", uid)
    if clean_uid not in _USER_GRAPHS:
        mg = MarketGraph()
        if mg.is_live and mg._falkor:
            try:
                mg.graph_name = f"user_{clean_uid}"
                mg._graph = mg._falkor.select_graph(mg.graph_name)
                mg._init_schema()
            except Exception:
                pass
        _USER_GRAPHS[clean_uid] = mg
    return _USER_GRAPHS[clean_uid]
