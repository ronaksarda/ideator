---
title: "Ideator: an agent that tells you what to build, not just what exists"
tags: hacktoberfest, ai, gemma, opensource
---

<!-- Fill before publishing: [VIDEO], [REPO], screenshots marked [SCREENSHOT: ...]. Delete this comment. -->

## The problem

Every founder I know validates ideas the same way: twenty browser tabs, a few app store pages, some Reddit threads, and
a gut feeling. You come out with a list of competitors and no decision.

Chat assistants don't fix this. Ask one about your idea and you get a confident list of features. Ask again and you get
a different list. After ten messages you still don't know who pays, what to charge, or what version 1 is.

## Who would pay for this

- **Solo founders and indie hackers** before they spend a weekend building. One avoided wrong build pays for a year.
- **Product managers** who need a short, sourced brief before a planning meeting.
- **Accelerator and hackathon mentors** who review dozens of ideas a week and want the evidence, not the pitch.

## What Ideator does

You type one line. Ideator:

1. Searches Google, Google Play (real app names and prices) and Reddit through **SerpApi**.
2. Writes companies, prices, features and user complaints into a **FalkorDB** graph, one graph per session.
3. Lets a **Gemma** agent pick graph tools over **MCP**, one step at a time: market overview, multi-hop complaint
   hotspots, PageRank, community detection, evidence paths, and a **TabPFN** price prediction.
4. Saves the agent's decision back into the graph, linked by `BECAUSE` edges to the complaints that justify it.
5. Writes a report where every user quote links to its source.

[SCREENSHOT: market map with a competitor selected]

## The part I care about: it converges

Version one of Ideator had a real flaw. You could ask questions forever and never reach a conclusion. It just produced
more features.

So every reply now ends with a **build brief** of five slots:

| Slot | What it means |
|---|---|
| Who pays | the exact paying segment |
| Pain you fix | the one complaint you fix, with how many users raised it |
| Why you beat them | the edge over the named competitors |
| Price | a USD price with its reason (TabPFN when available) |
| First version | the 2-3 things v1 ships, and nothing else |

The evidence decides two slots on the first turn: the pain and the price. The other three are your call, so Ideator
asks for them one at a time, each with a suggestion taken from the evidence. Slots never empty on their own. A few
turns later you have a full brief, and the Action plan tab opens with your plan in one paragraph and a numbered build
order. That is the moment the tool becomes useful.

[SCREENSHOT: Action plan tab, brief at 5/5 "Ready to build"]

## How the graph does real work

The agent never sees raw search results. It sees the graph through tools, and each tool runs parameterized Cypher.
The most useful query is the multi-hop "complaint hotspot":

```cypher
MATCH (u:UserIdea {id: $idea})-[:TARGETS]->(c:Competitor)-[:DRAWS_COMPLAINT]->(g:Grievance)-[:TARGETS_FEATURE]->(f:Feature)
WHERE NOT (:UserIdea)-[:SOLVES]->(g)
RETURN f.name, count(DISTINCT g), collect(DISTINCT c.name)
ORDER BY count(DISTINCT g) DESC
```

It answers: which feature do users of *several* competitors complain about, that your idea doesn't fix yet. When you
refine the idea, the new version gets `SOLVES` edges, so the same query moves on to the next unmet need.

One rule lives in code, not in the prompt: the agent can't answer or record a decision until it has run the complaint
hotspots and the TabPFN price. Gemma sometimes skipped them when only asked nicely.

## TabPFN on data the app collects itself

Each analysis copies its competitors into a shared `market_history` graph. `predict_price` trains a TabPFN regressor on
the paid ones at call time, and reports what share of apps in the space are free. Most consumer apps turn out to be
free, which is itself the answer to "what can I charge".

[SCREENSHOT: agent step "TabPFN priced it at $X/mo"]

## Why open models matter here

Ideator runs on **Gemma** (`gemma-4-26b-a4b-it`), an open-weights model. When the hosted endpoint fails, short prompts
fall back to `gemma2:2b` on a local Ollama, and if no model answers at all, the report is built only from the search
data and says so. A founder's unannounced idea is sensitive. Open weights mean you can run the whole thing on your own
machine and nothing about the idea has to leave it.

## Reliability and observability

- **Temporal** runs search and report generation as retried, durable workflows.
  [SCREENSHOT: Temporal UI at localhost:8233 with Completed workflows]
- **Sentry** records one transaction per chat turn, with a span per MCP tool call.
  [SCREENSHOT: Sentry trace]
- **ElevenLabs** reads the answer aloud.

## What I'd do next

- Cache searches so a new idea takes seconds, not 2-3 minutes.
- Let two founders compare briefs for the same market in one shared graph.

## Try it

Demo: [VIDEO] · Code: [REPO]

## AI assistance disclosure

I built Ideator with help from AI coding assistants, including Claude Code. I reviewed, ran and tested the code, and
I wrote and edited this post myself.
