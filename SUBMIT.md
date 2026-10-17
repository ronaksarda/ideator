# Submit checklist (local file, not committed)

## What the product does, in one line
Type a startup idea. Ideator searches the real market, builds a FalkorDB graph, lets a Gemma agent reason over it
with MCP tools, and walks you through 5 decisions (who pays, the pain, the edge, the price, version 1) until you
have a plan you can build.

## How a user progresses (3–4 turns)
1. **Turn 1:** idea in one line. You get competitors with real prices, sourced user complaints, the market map,
   and a build brief with 2/5 slots decided by the evidence (pain, price).
2. **Turn 2:** click "Decide next". It asks who pays first and suggests an answer from the evidence. Answer it.
3. **Turn 3–4:** same for "why you beat them" and "version 1". Each answer is checked against the graph, and the
   agent saves a decision linked to its evidence.
4. **Done:** the Action plan tab opens with your plan in one paragraph, the 5/5 brief, and a numbered build order.
   Download keeps it all as Markdown.

## Graph Hacks (WeMakeDevs × FalkorDB) — due Oct 18, 11:59 PM IST
- [ ] Push the repo public (I can do it: `gh repo create ideator --public --source . --push`).
- [ ] Upload `demo/ideator-demo.mp4` (55s) (YouTube unlisted or Loom).
- [ ] In `README.md`, on the `Demo video:` line, replace `_add link_` with the video link and the repo link.
- [ ] On the submission form: pick **Track 01: Agents That Act on Connected Data**. Paste the repo, video and a
      2-line description (use the line above).

## DEV Hacktoberfest Week 2 "Side Hustle" — due Oct 18, 11:59 PM PDT
- [ ] Draft is in `docs/dev-post.md`. Read it once and edit it into your own voice.
- [ ] Add 3 screenshots where it says `[SCREENSHOT: ...]`:
      the Action plan tab, a Temporal UI page (http://localhost:8233), and a Sentry trace (check sentry.io first:
      nobody has confirmed the traces arrive there yet).
- [ ] Replace `[VIDEO]` and `[REPO]`.
- [ ] Keep the AI disclosure at the bottom. Claim only: Gemma, SerpApi, TabPFN, ElevenLabs, Sentry, Temporal.
- [ ] Read the DEV Official Rules once: can one project enter both events? Not checked yet.

## Before every demo or run
```bash
docker compose up -d falkordb temporal
```
```bash
ollama serve
```
Then start the app: `python -m uvicorn ideator.backend.main:app --port 8000` and open http://localhost:8000.
If Docker won't start: rename `%LOCALAPPDATA%\docker-secrets-engine` and retry.
