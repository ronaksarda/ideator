# Ideator

Ideator is an agent that checks a startup idea against the real market. You describe an idea. It finds companies
with a similar product, what they offer and charge, and what their users complain about. All of that goes into a
FalkorDB graph. A Gemma-powered agent then works through the graph with MCP tools, decides where the opening is,
and saves that decision in the graph, linked to the evidence behind it.

Demo video: _add link_ · Live deployment: _add link, or use the local setup below_

---

## What happens when you send an idea

1. **Search.** SerpApi runs three live searches at once: Google results for competitors, Google Play for real app
   names, ratings and prices, and Reddit for user complaints. Gemma turns the raw results into clean records, using
   only what the results support.
2. **Write the graph.** Companies, prices, features and complaints are written to FalkorDB with parameterized
   `MERGE` queries, in one graph per user session. Features that share a meaningful word across companies are linked
   with `SIMILAR_TO`.
3. **Agent loop over MCP.** The graph tools are served by an MCP server (`mcp` SDK v2, `MCPServer`). The agent lists
   them through an MCP client, and Gemma picks one tool per step. Each tool's result goes back to the agent as an
   observation. The loop ends when the agent records a decision. Every step streams to the UI in plain words.
   Add `?debug=1` to the URL to see the Cypher each step ran.
4. **Act.** `record_decision` writes a `Decision` node with `BECAUSE` edges to the complaints and features that
   justify it. `search_more_complaints` can run another live search and add the results to the graph.
5. **Report.** Gemma writes a structured analysis from the graph findings: the idea, what it means, how to improve
   it, a competitor table, what users need, pricing, a build plan, and an action plan. Every user quote links to its
   source.
6. **Follow-ups.** Later messages build on the same idea. The agent runs again on the change, and the reply uses
   the conversation so far plus notes about the founder.
7. **Build brief.** Every reply ends with a five-slot brief: who pays, the pain you fix, why you beat the named
   competitors, the price and the first version. The evidence decides the pain and the price on turn 1. The other
   three are the founder's call: each turn asks one question with a suggestion from the evidence. Slots never empty
   on their own. At 5/5 the Action plan tab shows the plan in one paragraph and a numbered build order, and the
   download carries it. This is what turns a chat into a decision.

The agent cannot skip the evidence: `complaint_hotspots` and the TabPFN `predict_price` tool always run before it
answers or records a decision. This rule lives in code (`REQUIRED` in `tools.py`), not only in the prompt.

The market map you see is read back from FalkorDB with Cypher (`MarketGraph.falkor_elements`), not from a copy kept
in the app.

---

## Graph data model

```
(:UserIdea {id, text})
  -[:TARGETS]->      (:Competitor {id, name, url})
                       -[:PRICED_AT]->       (:Pricing {id, amount_usd})
                       -[:OFFERS]->          (:Feature {id, name})
                       -[:DRAWS_COMPLAINT]-> (:Grievance {id, text, url, source})
                                               -[:TARGETS_FEATURE]-> (:Feature)
(:Feature) -[:SIMILAR_TO]-> (:Feature)            similar features across companies
(:Decision {id, text, at}) -[:DECIDED_FOR]-> (:UserIdea)
(:Decision) -[:BECAUSE]-> (:Grievance | :Feature | :Competitor)
(:UserIdea) -[:SOLVES]-> (:Grievance), -[:OMITS]-> (:Feature)    written when you refine the idea
```

- **Isolation:** each session gets its own graph (`user_<hash>`), using FalkorDB's multi-graph support.
- **Indexes:** on `id` for every label, and on `name` for `Competitor` and `Feature`.

---

## Cypher queries and algorithms the agent uses

All of these are in `ideator/backend/tools.py`. Each is an MCP tool the agent can choose.

| Tool | What it does |
|---|---|
| `market_overview` | Lists each company with its price, complaint count and features |
| `complaint_hotspots` | Multi-hop query: idea, then competitor, then complaint, then the feature it's about. Groups by feature, skips complaints the idea already solves |
| `central_features` | `CALL algo.pageRank(null, null)` to find the most contested features |
| `market_segments` | `CALL algo.labelPropagation(...)` community detection over competitors, features and complaints |
| `evidence_for` | Every path that backs a feature, with source URLs |
| `predict_price` | **TabPFN regression** trained on every competitor stored in FalkorDB (`market_history` graph: features, complaints, price). Predicts what the market charges for your feature set |
| `saturation_score` | Graph counts fed into a TabPFN classifier, which returns how crowded the market is |
| `search_more_complaints` | **Action:** a live search whose results are written into the graph |
| `record_decision` | **Action:** writes a `Decision` node and its `BECAUSE` edges |

The multi-hop query:

```cypher
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor)-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature)
WHERE NOT (u)-[:SOLVES]->(g)
RETURN f.id, f.name, count(DISTINCT g), collect(DISTINCT c.name), collect(g.text)[0..3], collect(g.id)
ORDER BY count(DISTINCT g) DESC
```

Recording a decision:

```cypher
MATCH (u:UserIdea {id: $idea})
MERGE (d:Decision {id: $d_id}) SET d.text = $text, d.at = timestamp()
MERGE (d)-[:DECIDED_FOR]->(u)
WITH d UNWIND $because AS bid
MATCH (n {id: bid}) MERGE (d)-[:BECAUSE]->(n)
RETURN d.id, count(n), collect(n.id)
```

---

## Stack

- **FalkorDB:** graph store, Cypher, `algo.pageRank`, `algo.labelPropagation`.
- **MCP Python SDK v2:** serves the tools. The agent connects through an in-process `mcp.Client`.
- **Gemma 4** (`gemma-4-26b-a4b-it`, then `gemma-4-31b-it`) through the Google AI Studio API. Local `gemma2:2b`
  through Ollama is the fallback for short prompts.
- **SerpApi:** Google search, Google Play and Reddit results.
- **TabPFN** (Prior Labs): a tool inside the agent. Each analysis adds its competitors to a `market_history` graph,
  and `predict_price` trains a TabPFN regressor on that history at call time.
- **Temporal:** the two flaky, paid steps run as durable workflows (`SearchWorkflow`, `ReportWorkflow`) on a worker
  inside the app. Each retries 3 times with backoff and is visible in the Temporal UI at http://localhost:8233.
  Without a Temporal server, the steps run directly.
- **Sentry agent tracing:** one `gen_ai.invoke_agent` transaction per chat turn. Every Gemma call and every MCP tool
  call (`gen_ai.execute_tool`) is a child span with its inputs.
- **ElevenLabs:** reads any reply aloud.
- **Backend:** FastAPI with server-sent events.
- **Frontend:** plain JavaScript, Cytoscape.js with the fcose layout, marked and DOMPurify.

---

## Local setup

Ideator runs on your machine: the app, a FalkorDB graph database and (optionally) a Temporal server. Setup takes
about 10 minutes, most of it waiting for Docker images.

### 1. What you need installed

| Tool | Why | Get it |
|---|---|---|
| Python 3.12 | Runs the app | https://www.python.org/downloads/ |
| Docker Desktop | Runs FalkorDB and Temporal | https://www.docker.com/products/docker-desktop/ |
| Ollama (optional) | Local Gemma fallback when the hosted model fails | https://ollama.com/download |

### 2. Get the code and the Python packages

```bash
git clone https://github.com/ronaksarda/ideator.git
```
```bash
cd ideator
```
```bash
python -m venv .venv
```
Activate it: `.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux. Then:
```bash
pip install -r requirements.txt
```

### 3. Start the databases

Start Docker Desktop first, then:
```bash
docker compose up -d falkordb temporal
```

This starts:

| Service | Port | What it is |
|---|---|---|
| FalkorDB | 6379 | The graph database. Required: without it the agent has no tools and nothing to reason over. |
| FalkorDB Browser | http://localhost:3000 | Look at the graph the app builds (connect to `localhost:6379`). |
| Temporal | 7233 | Runs search and report as retried workflows. Optional: without it those steps run directly. |
| Temporal UI | http://localhost:8233 | See each workflow run. |

### 4. Add your API keys

Copy the example file and fill it in:
```bash
cp .env.example .env
```

| Key | Required? | What it powers | Where to get it | Without it |
|---|---|---|---|---|
| `FALKORDB_HOST`, `FALKORDB_PORT` | Yes | Graph database address | Leave `localhost` and `6379` | Nothing works |
| `GEMINI_API_KEY` | Yes | The Gemma models (`gemma-4-26b-a4b-it`, then `gemma-4-31b-it`) that run the agent and write the report | Google AI Studio: https://aistudio.google.com/apikey | A fixed evidence plan and a report built only from search data |
| `SERPAPI_API_KEY` | Yes | Live search: Google, Google Play, Reddit | https://serpapi.com/manage-api-key (free plan: 100 searches a month; one idea uses about 3) | No live market data |
| `TABPFN_API_KEY` | Recommended | `predict_price`: TabPFN price prediction | https://ux.priorlabs.ai (sign up, copy the access token) | The price step reports that the key is missing |
| `ELEVENLABS_API_KEY` | Optional | The Listen button | https://elevenlabs.io/app/settings/api-keys | Listen says the voice is unavailable |
| `SENTRY_DSN` | Optional | Tracing: one transaction per chat turn, one span per Gemma and MCP tool call | sentry.io: create a Python project, copy the DSN | No traces |
| `SESSION_SECRET` | Optional | Signs the session cookie | Any long random string | A key is generated once and saved in `ideator/data/` |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Optional | GitHub sign-in | GitHub, Settings, Developer settings, OAuth Apps (callback: `http://localhost:8000/auth/callback`) | Guest mode: each browser gets its own private sessions |

`.env` is in `.gitignore`. Never commit it.

### 5. Optional: the local fallback model

```bash
ollama pull gemma2:2b
```
```bash
ollama serve
```

### 6. Run it

```bash
python -m uvicorn ideator.backend.main:app --port 8000
```

Open http://localhost:8000 and describe an idea in one line. Add `?debug=1` to the URL to see the Cypher every
agent step runs.

### 7. Check that everything works

Tests (about 20 seconds; the graph-agent tests need FalkorDB running):
```bash
python -m pytest -q
```


### Troubleshooting

| Problem | Fix |
|---|---|
| `Connection refused` on 6379 | Docker Desktop isn't running, or run `docker compose up -d falkordb` again |
| Docker Desktop won't start on Windows | Quit Docker, rename `%LOCALAPPDATA%\docker-secrets-engine`, start Docker again |
| Report says the AI model was unavailable | Google's hosted Gemma returned errors; try again in a minute, or run Ollama for the fallback |
| "Too many requests" | Rate limits: 3 new ideas per hour, 20 messages per 10 minutes. Restarting the app resets them |
| Port 8000 in use | Add `--port 8001` to the run command |
| Mic doesn't work | Use Chrome or Edge and allow microphone access |

---

## Safety

- **Input filter, no AI:** messages are capped at 600 characters. HTML, scripts and template syntax are refused, and
  so is profanity. A new idea needs at least 3 words. See `ideator/backend/guard.py`.
- **Rate limits:** 3 new ideas per user per hour and 20 messages per 10 minutes. These live in memory, so they reset
  on restart and apply per worker.
- **Prompt injection:** user text goes into prompts inside `<user_input>` tags, with a rule that it is data, not
  instructions. The agent can only call the tools on its own MCP server, and those tools are bound to that user's
  graph. Unknown tool names are refused.
- **Output:** model output is rendered with marked and sanitized with DOMPurify. Every query the app runs against FalkorDB uses parameters. The one exception is the "write the graph" text shown for display, which is a readable copy, not what runs.

---

## Known limits

- A new idea takes about 1.5–3 minutes: live search, several Gemma agent steps, TabPFN and the report. Follow-ups
  are faster.
- When hosted Gemma returns errors, the agent falls back to a fixed evidence plan, and the report is built only
  from the search data. The UI says so.
- Some complaints don't name a specific company. They are stored under "General user feedback" and drawn off the
  feature they are about, or off the idea itself.
- `saturation_score` trains its TabPFN classifier on 4 hand-made rows; treat it as a rough signal. `predict_price`
  is the real TabPFN use: it trains on every competitor stored in FalkorDB.
- Companies with no listed price show as "Free".

## AI assistance disclosure

This project was built with AI coding assistants, including Claude Code. The team reviewed, ran and tested
the code, and can explain the graph model, the queries and the architecture.
