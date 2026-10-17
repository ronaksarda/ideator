(async () => {
  const $ = (id) => document.getElementById(id);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- session id (per-browser convenience; the real state is in FalkorDB) ----
  const KEY = 'ideator.session';
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  let sessionId;
  try { sessionId = localStorage.getItem(KEY); } catch (_) { /* storage blocked */ }
  if (!sessionId) { sessionId = newId(); try { localStorage.setItem(KEY, sessionId); } catch (_) {} }

  // ---- graph canvas ----
  const EDGE_WORDS = {
    TARGETS: 'competes with', PRICED_AT: 'charges', HAS_PRICING: 'charges', OFFERS: 'offers', DRAWS_COMPLAINT: 'users say',
    TARGETS_FEATURE: 'is about', USERS_SAY: 'users say', BECAUSE: 'because of', DECIDED_FOR: 'decided for', SOLVES: 'fixes', OMITS: 'drops', EVOLVED_FROM: 'evolved from', SUPERSEDES: 'replaces',
  };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const graphStyle = () => [
      { selector: 'node', style: {
        'label': 'data(label)', 'font-size': 9, 'color': css('--ink'),
        'text-background-opacity': 0,
        'text-outline-color': css('--paper'), 'text-outline-width': 2,
        'text-wrap': 'wrap', 'text-max-width': 75, 'text-valign': 'bottom', 'text-margin-y': 4,
        'width': 18, 'height': 18, 'background-color': css('--feature'),
        'transition-property': 'opacity, background-color', 'transition-duration': reducedMotion ? 0 : 400,
      } },
      { selector: 'node[type = "idea"]', style: { 'background-color': css('--accent'), 'width': 26, 'height': 26, 'shape': 'round-rectangle', 'font-weight': 600 } },
      { selector: 'node[type = "competitor"]', style: { 'background-color': css('--ink'), 'width': 20, 'height': 20 } },
      { selector: 'node[type = "grievance"]', style: { 'background-color': css('--grievance') } },
      { selector: 'node[type = "pricing"]', style: { 'background-color': css('--pricing'), 'shape': 'diamond' } },
      { selector: 'node[type = "decision"]', style: { 'background-color': '#a855f7', 'shape': 'star', 'width': 28, 'height': 28, 'font-weight': 600 } },
      { selector: 'node.evidence', style: { 'border-width': 4, 'border-color': '#a855f7' } },
      { selector: 'node.solved', style: { 'background-color': css('--solved') } },
      { selector: 'node.superseded', style: { 'opacity': 0.35 } },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': css('--accent') } },
      { selector: 'edge', style: {
        'width': 1.2, 'line-color': css('--muted'), 'curve-style': 'bezier',
        'target-arrow-shape': 'triangle', 'target-arrow-color': css('--muted'), 'arrow-scale': 0.8,
        'font-size': 9, 'color': css('--ink'),
        'text-rotation': 'autorotate', 'text-background-color': css('--panel'), 'text-background-opacity': 0.85, 'text-background-padding': 1,
      } },

      // Relationship words only on the selected node's edges, so the canvas stays readable.
      { selector: 'edge.show-label', style: { 'label': (e) => EDGE_WORDS[e.data('label')] || e.data('label'), 'width': 2.2, 'line-color': css('--accent'), 'target-arrow-color': css('--accent'), 'z-index': 10 } },
      { selector: 'edge[label = "EVOLVED_FROM"]', style: { 'width': 2, 'line-color': css('--accent'), 'target-arrow-color': css('--accent') } },
      { selector: 'edge[label = "SUPERSEDES"]', style: { 'line-style': 'dotted', 'line-color': css('--muted'), 'target-arrow-color': css('--muted') } },
      { selector: 'edge[label = "SOLVES"]', style: { 'width': 2.5, 'line-color': css('--solved'), 'target-arrow-color': css('--solved') } },
      { selector: '.faded', style: { 'opacity': 0.12, 'text-opacity': 0.12 } },
      { selector: 'edge[label = "SIMILAR_TO"]', style: { 'line-style': 'dashed', 'line-color': css('--pricing'), 'target-arrow-shape': 'none', 'width': 1.5 } },
      { selector: 'edge[label = "OMITS"]', style: { 'width': 1.5, 'line-style': 'dashed', 'line-color': css('--muted'), 'target-arrow-color': css('--muted') } },
  ];
  const cy = cytoscape({
    container: $('cy'),
    wheelSensitivity: 0.3,
    style: graphStyle(),
  });

  function markSolved() {
    // A grievance turns green once the active lineage solves it.
    cy.nodes('[type = "grievance"]').removeClass('solved');
    let node = cy.nodes('.active-version');
    const seen = new Set();
    while (node.length && !seen.has(node.id())) {
      seen.add(node.id());
      node.outgoers('edge[label = "SOLVES"]').targets().addClass('solved');
      node = node.outgoers('edge[label = "EVOLVED_FROM"]').targets();
    }
  }

  // Force-directed layout. fcose (if its CDN script loaded) guarantees no node/label overlap; cose is the fallback.
  function relayout(animate) {
    const fcose = typeof cytoscapeFcose !== 'undefined';
    cy.layout({
      name: fcose ? 'fcose' : 'cose', quality: 'proof', randomize: true, fit: true, padding: 40,
      animate: animate && !reducedMotion, animationDuration: 400,
      nodeDimensionsIncludeLabels: true, packComponents: true,
      nodeRepulsion: () => (fcose ? 9000 : 400000), idealEdgeLength: () => (fcose ? 110 : 140),
      nodeSeparation: 90, edgeElasticity: () => 0.3, gravity: 0.25, numIter: 2500,
      nodeOverlap: 60, componentSpacing: 120,
    }).run();
  }

  // Dragging a node drags everything it owns (its outgoing subtree); edges to other nodes stay attached.
  const UP_EDGES = new Set(['EVOLVED_FROM', 'SUPERSEDES']);
  let dragKids = cy.collection(), dragFrom = null;
  cy.on('grab', 'node', (evt) => {
    const root = evt.target, seen = new Set([root.id()]);
    let frontier = root, kids = cy.collection();
    while (frontier.length) {
      const next = frontier.outgoers('edge').filter((e) => !UP_EDGES.has(e.data('label'))).targets().filter((n) => !seen.has(n.id()));
      next.forEach((n) => seen.add(n.id()));
      kids = kids.union(next); frontier = next;
    }
    dragKids = kids; dragFrom = { ...root.position() };
  });
  cy.on('drag', 'node', (evt) => {
    if (!dragFrom) return;
    const p = evt.target.position(), dx = p.x - dragFrom.x, dy = p.y - dragFrom.y;
    dragKids.forEach((n) => { if (!n.grabbed()) n.shift({ x: dx, y: dy }); });
    dragFrom = { ...p };
  });
  cy.on('free', 'node', () => { dragKids = cy.collection(); dragFrom = null; });
  cy.on('select', 'node', (evt) => {
    const near = evt.target.closedNeighborhood();
    cy.elements().addClass('faded'); near.removeClass('faded');
    evt.target.connectedEdges().addClass('show-label');
  });
  cy.on('unselect', 'node', (evt) => { evt.target.connectedEdges().removeClass('show-label'); cy.elements().removeClass('faded'); });

  function applyDelta(ev) {
    const wasEmpty = cy.elements().length === 0;
    const fresh = cy.collection();
    for (const n of ev.nodes || []) {
      const existing = cy.getElementById(n.data.id);
      if (existing.length) { existing.data(n.data); continue; }
      fresh.merge(cy.add({ group: 'nodes', data: n.data }));
    }
    for (const e of ev.edges || []) {
      if (cy.getElementById(e.data.id).length) continue;
      if (!cy.getElementById(e.data.source).length || !cy.getElementById(e.data.target).length) continue;
      fresh.merge(cy.add({ group: 'edges', data: e.data }));
    }
    for (const id of ev.superseded || []) cy.getElementById(id).addClass('superseded');

    const newNodes = fresh.nodes();
    if (newNodes.length) relayout(!wasEmpty);
    if (ev.focus) {
      cy.nodes('.active-version').removeClass('active-version');
      cy.getElementById(ev.focus).addClass('active-version');
    }
    if (!reducedMotion && !wasEmpty) {
      newNodes.style('opacity', 0);
      newNodes.animate({ style: { opacity: 1 } }, { duration: 500, complete: () => newNodes.removeStyle('opacity') });
    }
    markSolved();
    $('graph-empty').hidden = cy.elements().length > 0;
  }

  // ---- inspector ----
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TYPE_NAMES = { idea: 'Idea version', competitor: 'Similar company', grievance: 'What users say', feature: 'Feature', pricing: 'Price', decision: 'Agent decision' };

  let lastScore = null;

  // Inline markdown (links) from the advisor, sanitized; plain escape when the libraries did not load.
  const mdi = (t) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parseInline(String(t ?? ''))) : esc(t));
  // Node details: built from the node's real connections in the graph, plus the advisor's analysis when it has one.
  let planItems = [];
  const lbl = (n) => n.data('name') || n.data('label');  // companies carry "Name · price" as label
  const via = (n, dir, rel, type) => (dir === 'out' ? n.outgoers(`edge[label = "${rel}"]`).targets() : n.incomers(`edge[label = "${rel}"]`).sources())
    .filter((x) => !type || x.data('type') === type);
  const planFor = (name) => planItems.find((p) => p.feature && (p.feature.toLowerCase().includes(name.toLowerCase()) || name.toLowerCase().includes(p.feature.toLowerCase())));
  const list = (xs) => xs.length ? `<ul>${xs.map((x) => `<li>${x}</li>`).join('')}</ul>` : '';
  const quote = (g) => `"${esc(g.data('full_text') || lbl(g))}"`;

  function details(n) {
    const d = n.data(), ins = d.insight || {}, rows = [];
    const add = (k, v) => { if (v && String(v).trim()) rows.push([k, v]); };
    if (d.type === 'competitor') {
      add('Price', d.amount_usd ? `$${Number(d.amount_usd).toFixed(2)}/mo` : 'Free (may have in-app purchases)');
      add('Features', list(via(n, 'out', 'OFFERS').map((f) => esc(lbl(f)))));
      add('Best at', esc(ins['Best at'])); add('Worst at', esc(ins['Worst at']));
      const said = via(n, 'out', 'DRAWS_COMPLAINT').union(via(n, 'out', 'OFFERS').outgoers('edge[label = "USERS_SAY"]').targets());
      add('What users say', list(said.map(quote)));
      const beat = planItems.filter((p) => (p.who_has_it || '').toLowerCase().includes(lbl(n).toLowerCase()));
      add('How we beat them', list(beat.map((p) => `<b>${esc(p.feature)}</b>: ${mdi(p.what_to_do)}${p.add ? ` Add: ${mdi(p.add)}.` : ''}`)));
    } else if (d.type === 'feature') {
      add('Offered by', via(n, 'in', 'OFFERS').map((c) => esc(lbl(c))).join(', '));
      add('What users say about it', list(via(n, 'out', 'USERS_SAY').map(quote)));
      const sim = n.connectedEdges('edge[label = "SIMILAR_TO"]').connectedNodes().not(n);
      add('Similar features elsewhere', list(sim.map((f) => `${esc(lbl(f))} <span class="muted">(${esc(via(f, 'in', 'OFFERS').map(lbl).join(', ') || 'from complaints')})</span>`)));
      const p = planFor(lbl(n));
      if (p) { add('The problem', mdi(p.problem)); add('What we do better', mdi(p.what_to_do)); add('Ideas to build', list((p.ideas || []).map(mdi))); add('Feature to add', mdi(p.add)); }
      add('Verdict', esc([ins.Verdict, ins.Why].filter(Boolean).join(': ')));
    } else if (d.type === 'grievance') {
      add('What the user said', quote(n));
      const f = via(n, 'in', 'USERS_SAY');
      add('About', esc(via(n, 'in', 'DRAWS_COMPLAINT').union(via(f, 'in', 'OFFERS')).map(lbl).join(', ')));
      add('Feature', esc(f.map(lbl).join(', ')));
      const p = f.length && planFor(lbl(f[0]));
      if (p) add('What we do about it', mdi(p.what_to_do));
    } else if (d.type === 'pricing') {
      const who = via(n, 'in', 'PRICED_AT').map(lbl).join(', ');
      add('Who charges this', `${esc(who)}: ${esc(lbl(n))}`);
      add('Your suggested price', ins['Your price'] ? esc(ins['Your price']) : lastScore ? `$${Math.round(lastScore.recommended_entry_price)}/mo` : '');
      add('Market range', esc(ins['Market range'])); add('Users will pay', esc(ins['Users will pay'])); add('Why', esc(ins.Why));
      add('Feature that justifies it', esc(ins['Feature to justify it']));
    } else if (d.type === 'decision') {
      add('Decision', esc(d.full_text || lbl(n)));
      add('Because of', list(via(n, 'out', 'BECAUSE').map((x) => `${esc(TYPE_NAMES[x.data('type')] || '')}: ${x.data('type') === 'grievance' ? quote(x) : esc(lbl(x))}`)));
    } else if (d.type === 'idea') {
      add('What it is', esc(ins['What it is'] || d.full_text || lbl(n)));
      add('Competitors found', via(n, 'out', 'TARGETS').map((c) => esc(lbl(c))).join(', '));
      add('Biggest openings', list(planItems.slice(0, 3).map((p) => `<b>${esc(p.feature)}</b>: ${mdi(p.what_to_do)}`)));
      add('Improve it', esc(ins['Improve it']));
      if (d.version > 1) { add('Change in this version', esc(ins.Change || d.delta)); add('Where the idea stands', esc(ins['Where the idea stands'])); }
    }
    return rows;
  }

  cy.on('tap', 'node', (evt) => {
    const n = evt.target, d = n.data(), rows = details(n);
    $('insp-title').textContent = `${TYPE_NAMES[d.type] || 'Item'}: ${d.label}`;
    $('insp-body').innerHTML = `<dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')
      || '<dt>Details</dt><dd>Nothing is connected to this item yet.</dd>'}
      ${d.url && /^https?:/.test(d.url) && !/google\.com\/search/.test(d.url) ? `<dt>Source</dt><dd><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a></dd>` : ''}</dl>`;
    $('inspector').hidden = false;
  });

  // Action plan tab: one card per opportunity, click to find it on the graph.
  window.renderPlan = (items, brief, question) => {
    planItems = items || [];
    // Build brief: the conclusion the chat converges on. Slots fill turn by turn; one question closes the next gap.
    const SLOTS = { who: 'Who pays', pain: 'Pain you fix', edge: 'Why you beat them', price: 'Price', mvp: 'First version (MVP)' };
    const b = brief || {}, done = Object.keys(SLOTS).filter((k) => b[k]).length;
    // One-paragraph plan from the brief, so the tab opens with a conclusion, not a list.
    const lc = (t) => /^[A-Z][a-z]/.test(t) ? t[0].toLowerCase() + t.slice(1) : t;  // mid-sentence, but keep acronyms
    const summary = done ? [b.mvp ? `Build ${lc(b.mvp)}` : 'Build a first version', b.who ? ` for ${lc(b.who)}.` : '.', b.price ? ` Charge ${b.price}.` : '',
      b.edge ? ` You win because: ${b.edge}.` : '', b.pain ? ` It fixes: ${b.pain}.` : ''].join('').replace(/\.\./g, '.') : '';
    const locked = done === 5;
    if (locked && !document.body.classList.contains('locked')) {  // the moment the plan is decided: mark it once
      document.body.classList.add('locked', 'sunrise');
      setTimeout(() => document.body.classList.remove('sunrise'), 2400);
      if (planItems.length) showTab('plan');
    } else if (!locked) document.body.classList.remove('locked');
    const briefHtml = Object.keys(b).length ? `<article class="plan-card brief${locked ? ' is-locked' : ''}">
      <p class="brief-kicker">${locked ? 'Plan locked' : `Your plan so far · ${done} of 5 decided`}</p>
      <h3 class="brief-title">${summary ? mdi(summary) : 'Answer a few questions and your plan writes itself here.'}</h3>
      <ol class="slots">${Object.entries(SLOTS).map(([k, l]) => `<li class="${b[k] ? 'on' : ''}"><span class="slot-l">${l}</span><span class="slot-v">${b[k] ? mdi(b[k]) : 'Open'}</span></li>`).join('')}</ol>
      <p class="brief-next">${locked ? 'Every decision is made. Download the session to keep this plan.' : `<b>Next to decide:</b> ${mdi(question || 'Ask what is still open.')}`}</p></article>` : '';
    const none = (t) => !t || /^(n\/?a|none|nobody|no one)\.?$/i.test(String(t).trim());
    // Suggested changes come from this idea's own action plan, so they name its real features and competitors.
    const strip = (t) => String(t || '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
    $('chips').innerHTML = planItems.filter((p) => !/^general/i.test(p.feature || '')).slice(0, 3).map((p) => `<button type="button" class="chip" data-text="${esc(
      `Change my idea to win on ${p.feature}${none(p.who_has_it) ? '' : ` against ${strip(p.who_has_it)}`}${(p.ideas || [])[0] ? `: ${strip(p.ideas[0])}` : p.problem ? `, where users say ${strip(p.problem)}` : ''}`.replace(/[<>]/g, '').slice(0, 550))}">Win on ${esc(p.feature)}</button>`).join('');
    if (question && done < 5) $('chips').insertAdjacentHTML('afterbegin', `<button type="button" class="chip chip-next" data-text="${esc(`${question.replace(/[<>]/g, '')} My answer: `.slice(0, 550))}">Decide next: ${esc(question.slice(0, 60))}${question.length > 60 ? '…' : ''}</button>`);
    $('chips').hidden = !planItems.length && !question;
    $('plan').innerHTML = briefHtml + (planItems.length ? `<h3 class="steps-title">Build order</h3>` + planItems.map((p, i) => `<article class="plan-card" data-i="${i}" tabindex="0">
      <h3><span class="step-n">Step ${i + 1}</span> ${esc(p.feature)}</h3>
      ${(p.ideas || []).length ? `<p><b>Build:</b></p><ul>${p.ideas.map((x) => `<li>${mdi(x)}</li>`).join('')}</ul>` : p.what_to_do ? `<p><b>Build:</b> ${mdi(p.what_to_do)}</p>` : ''}
      ${p.problem ? `<p><b>Why:</b> ${mdi(p.problem)}</p>` : ''}
      ${none(p.who_has_it) ? '<p><b>Competition:</b> nobody offers this yet</p>' : `<p><b>Beats:</b> ${mdi(p.who_has_it)}</p>`}</article>`).join('')
      : '<p class="muted">The action plan appears after your first idea is analyzed.</p>');
  };
  $('plan').addEventListener('click', (e) => {
    const card = e.target.closest('.plan-card'); if (!card || card.classList.contains('brief')) return;
    const p = planItems[card.dataset.i];
    const comp = (p.who_has_it || '').split(',')[0].trim().toLowerCase();  // topic may not be a drawn feature: fall back to the company
    let node = cy.nodes('[type = "feature"]').filter((f) => planFor(lbl(f)) === p).first();
    if (!node.length && comp) node = cy.nodes('[type = "competitor"]').filter((c) => lbl(c).toLowerCase() === comp).first();
    showTab('map');
    if (node.length) { cy.$(':selected').unselect(); node.select(); cy.animate({ center: { eles: node }, zoom: 1.4 }); node.emit('tap'); }
  });
  function showTab(t) {
    $('tab-map').setAttribute('aria-selected', t === 'map'); $('tab-plan').setAttribute('aria-selected', t === 'plan');
    $('plan').hidden = t !== 'plan';
  }
  $('tab-map').addEventListener('click', () => showTab('map'));
  $('tab-plan').addEventListener('click', () => showTab('plan'));
  window.renderPlan([]);
  $('insp-close').addEventListener('click', () => { $('inspector').hidden = true; cy.$(':selected').unselect(); });
  cy.on('tap', (evt) => { if (evt.target === cy) $('inspector').hidden = true; });

  // ---- score + lineage ----
  function renderScore(s) {
    lastScore = s;
    $('sat-value').textContent = `${s.saturation_index}%`;
    $('sat-fill').style.width = `${s.saturation_index}%`;
    $('sat-meter').setAttribute('aria-valuenow', s.saturation_index);
    const d = $('sat-delta');
    if (s.previous != null && s.previous !== s.saturation_index) {
      const diff = Math.round((s.saturation_index - s.previous) * 10) / 10;
      d.textContent = `${diff > 0 ? '+' : ''}${diff} points vs last version`;
      d.className = `sat-delta ${diff < 0 ? 'down' : 'up'}`;
    } else { d.textContent = ''; d.className = 'sat-delta'; }
    const v = s.vector;
    $('sat-meta').textContent = `v${s.version} · suggested price $${Math.round(s.recommended_entry_price)}/mo · `
      + `${v.competitor_count} direct competitors · ${s.solved} complaints fixed · ${s.omitted} features dropped`;
    if (s.lineage) renderLineage(s.lineage);
  }

  function renderLineage(items) {
    const ol = $('lineage');
    ol.innerHTML = '';
    for (const v of items) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = `v${v.version}${v.version > 1 ? ' ' + v.delta.slice(0, 32) + (v.delta.length > 32 ? '…' : '') : ''}`;
      b.title = v.delta;
      if (v.active) li.className = 'active';
      if (v.superseded) li.classList.add('superseded');
      b.addEventListener('click', () => { const n = cy.getElementById(v.node || v.id); if (n.length) { cy.animate({ center: { eles: n } }); n.emit('tap'); } });
      li.appendChild(b);
      ol.appendChild(li);
    }
  }

  // ---- chat ----
  const feed = $('feed');
  function addMsg(cls, html) {
    $('empty').hidden = true;
    const el = document.createElement('div');
    el.className = `msg ${cls}`;
    el.innerHTML = html;
    feed.appendChild(el);
    feed.scrollTop = feed.scrollHeight;
    return el;
  }

  const md = (t) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(t)) : esc(t));
  const SOURCE_LABEL = { LIVE_SERPAPI: 'Based on a live web search', LLM_SYNTHESIZED: 'AI estimate: web search was unavailable', OFFLINE_MOCK: 'Sample data: web search is not set up' };
  const INTENT_LABEL = { new_concept: 'New idea · searched the web', refine: 'Change to your idea', question: 'Answer' };

  const DEBUG = new URLSearchParams(location.search).has('debug');
  const STEP_TEXT = {
    market_overview: (ev) => `Mapped ${ev.rows.length} competitors with their prices and complaints`,
    complaint_hotspots: (ev) => ev.rows[0] ? `Biggest unmet need: "${ev.rows[0].feature}" (${ev.rows[0].complaints} complaints)` : 'Looked for unmet needs',
    central_features: (ev) => ev.rows[0] ? `Most contested feature in the market: "${ev.rows[0].feature}"` : 'Ranked the most contested features',
    market_segments: (ev) => `Grouped the market into ${ev.rows.length} segments`,
    evidence_for: (ev) => `Found ${ev.rows.length} user quotes backing "${(ev.args || {}).feature || 'this'}"`,
    predict_price: (ev) => ev.rows[0] ? `TabPFN priced it at $${ev.rows[0].predicted_price_usd}/mo (learned from ${ev.rows[0].trained_on} competitors${ev.rows[0].free_share != null ? `; ${Math.round(ev.rows[0].free_share * 100)}% of apps here are free` : ''})` : 'Predicted a market price',
    saturation_score: (ev) => ev.rows[0] ? `Scored how crowded the market is (${ev.rows[0].competitors} competitors)` : 'Scored market crowding',
    search_more_complaints: (ev) => `Searched the web for more complaints (${ev.rows.length} new)`,
    record_decision: () => 'Saved a decision, linked to the evidence behind it',
  };
  let inflight = null;

  async function send(text) {
    const btn = $('send');
    btn.disabled = true;
    addMsg('user', esc(text));
    const agent = addMsg('agent', '<div class="meta">Working…</div><div class="body"></div>');
    let cypher = [];
    const ctrl = inflight = new AbortController();  // New session / switching session aborts this stream
    try {
      const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, message: text }), signal: ctrl.signal });
      if (res.status === 401) { location.reload(); return; }
      if (!res.ok) {  // refusals (filter, rate limit) come back as {detail: "..."} meant for the user
        const why = await res.json().then((j) => j.detail).catch(() => null);
        agent.querySelector('.meta').textContent = res.status === 429 ? 'Slow down' : 'Can\'t send that';
        agent.querySelector('.body').textContent = typeof why === 'string' ? why : `Something went wrong (${res.status}). Try again.`;
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          if (!chunk.startsWith('data: ')) continue;
          const ev = JSON.parse(chunk.slice(6));
          if (ev.type === 'intent') agent.querySelector('.meta').textContent = INTENT_LABEL[ev.intent] || ev.intent;
          else if (ev.type === 'agent_step') agent.querySelector('.meta').textContent = DEBUG ? `[${ev.agent}] ${ev.step}` : 'Working on it…';
          else if (ev.type === 'cypher') cypher = cypher.concat(ev.statements);
          else if (ev.type === 'graph_delta') applyDelta(ev);
          else if (ev.type === 'tool_call') {
            // Agent trace in plain words; tool names, arguments and Cypher only in debug mode.
            let trace = agent.querySelector('.trace');
            if (!trace) { agent.querySelector('.body').insertAdjacentHTML('beforebegin', '<ol class="trace" aria-label="What the agent checked"></ol>'); trace = agent.querySelector('.trace'); }
            ev.rows = ev.rows || [];
            const said = ev.error ? 'Could not check this step' : (STEP_TEXT[ev.tool] || (() => 'Checked the market graph'))(ev);
            trace.insertAdjacentHTML('beforeend', `<li>${esc(said)}`
              + (DEBUG ? `<div class="muted small"><code>${esc(ev.tool)} ${esc(JSON.stringify(ev.args || {}))}</code> ${ev.ms ?? ''} ms. ${esc(ev.thought || '')}</div>`
                + (ev.cypher ? `<details><summary>Cypher</summary><pre>${esc(ev.cypher)}</pre></details>` : '') : '') + '</li>');
            cy.nodes('.evidence').removeClass('evidence');
            for (const id of ev.node_ids || []) cy.getElementById(id).addClass('evidence');
            agent.querySelector('.meta').textContent = `Analyzing the market graph (step ${ev.step})…`;
          }
          else if (ev.type === 'plan') window.renderPlan(ev.items, ev.brief, ev.question);
          else if (ev.type === 'node_insights') for (const [id, ins] of Object.entries(ev.insights)) cy.getElementById(id).data('insight', ins);
          else if (ev.type === 'score') renderScore(ev);
          else if (ev.type === 'reasoning_path') {
            agent.dataset.reasoningNodes = JSON.stringify(ev.node_ids || []);
            agent.dataset.reasoningEdges = JSON.stringify(ev.edge_ids || []);
            agent.dataset.proof = ev.cypher_proof || '';
          }
          else if (ev.type === 'message') {
            const body = agent.querySelector('.body');
            body.innerHTML = md(ev.text); body.classList.add('md');  // every reply may carry formatting; md() sanitizes
            if (ev.source_mode) agent.querySelector('.meta').textContent = SOURCE_LABEL[ev.source_mode] || '';
            agent.dataset.fullText = ev.text;
          }
          else if (ev.type === 'error') { agent.classList.add('error'); agent.querySelector('.body').textContent = ev.text; }
        }
      }
      
      // Action buttons: Reasoning Path & Voice Narration
      let actionsHtml = '<div class="agent-actions" style="margin-top: 10px; display: flex; gap: 8px;">';
      if (agent.dataset.reasoningNodes) {
        actionsHtml += `<button type="button" class="btn-reasoning" style="background: rgba(139, 92, 246, 0.18); border: 1px solid var(--accent); color: var(--accent); padding: 4px 10px; border-radius: 6px; font-size: 11px; cursor: pointer; font-weight: 600;">Show evidence on graph</button>`;
      }
      actionsHtml += `<button type="button" class="btn-tts" style="background: rgba(59, 130, 246, 0.15); border: 1px solid var(--ink); color: var(--ink); padding: 4px 10px; border-radius: 6px; font-size: 11px; cursor: pointer; font-weight: 500;">Play Voice (ElevenLabs)</button>`;
      actionsHtml += '</div>';
      agent.insertAdjacentHTML('beforeend', actionsHtml);

      // Event listener: Highlight reasoning path in FalkorDB graph
      const rBtn = agent.querySelector('.btn-reasoning');
      if (rBtn) {
        rBtn.addEventListener('click', () => {
          try {
            const nodeIds = JSON.parse(agent.dataset.reasoningNodes || '[]');
            const edgeIds = JSON.parse(agent.dataset.reasoningEdges || '[]');
            cy.elements().unselect();
            const targetEles = cy.collection();
            for (const nid of nodeIds) {
              const el = cy.getElementById(nid);
              if (el.length) targetEles.merge(el);
            }
            for (const eid of edgeIds) {
              const el = cy.getElementById(eid);
              if (el.length) targetEles.merge(el);
            }
            if (targetEles.length) {
              targetEles.select();
              cy.animate({ fit: { eles: targetEles, padding: 60 } }, { duration: 500 });
              rBtn.textContent = 'Reasoning Path Highlighted';
              setTimeout(() => { rBtn.textContent = 'Show evidence on graph'; }, 3000);
            }
          } catch (_) {}
        });
      }

      // Event listener: ElevenLabs voice synthesis
      const ttsBtn = agent.querySelector('.btn-tts');
      if (ttsBtn) {
        ttsBtn.addEventListener('click', async () => {
          ttsBtn.disabled = true;
          ttsBtn.textContent = 'Synthesizing Voice…';
          try {
            const rawText = agent.dataset.fullText || agent.querySelector('.body').textContent || '';
            const cleanSpeech = rawText.replace(/[*#`•]/g, '').slice(0, 400);
            const resp = await fetch('/api/tts', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ text: cleanSpeech })
            });
            if (!resp.ok) throw new Error('ElevenLabs API key required or generation failed');
            const blob = await resp.blob();
            const audioUrl = URL.createObjectURL(blob);
            const audio = new Audio(audioUrl);
            audio.play();
            ttsBtn.textContent = 'Playing…';
            audio.onended = () => { ttsBtn.textContent = 'Play Voice (ElevenLabs)'; ttsBtn.disabled = false; };
          } catch (e) {
            ttsBtn.textContent = 'Voice Unavailable (Add ELEVENLABS_API_KEY)';
            setTimeout(() => { ttsBtn.textContent = 'Play Voice (ElevenLabs)'; ttsBtn.disabled = false; }, 3500);
          }
        });
      }

      if (DEBUG && cypher.length) {
        agent.insertAdjacentHTML('beforeend', `<details style="margin-top: 8px;"><summary style="cursor: pointer; font-size: 11px; color: var(--muted);">FalkorDB Cypher Queries (${cypher.length})</summary><pre style="font-size: 11px; margin-top: 4px; padding: 6px; background: rgba(0,0,0,0.3); border-radius: 4px;">${esc(cypher.join('\n\n'))}</pre></details>`);
      }
    } catch (err) {
      if (err.name === 'AbortError') { agent.remove(); return; }
      agent.classList.add('error');
      agent.querySelector('.meta').textContent = 'Request failed';
      agent.querySelector('.body').textContent = `${err.message}. Check that the server is running, then send again.`;
    } finally {
      btn.disabled = false;
      feed.scrollTop = feed.scrollHeight;
    }
  }

  $('composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = $('msg').value.trim();
    if (!t || $('send').disabled) return;
    $('msg').value = '';
    send(t);
  });
  $('msg').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); }
  });
  // Voice input: browser speech-to-text (Chrome/Edge). Fills the box, then sends.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const rec = new SR(); rec.lang = 'en-US'; rec.interimResults = true;
    $('mic').hidden = false;
    $('mic').addEventListener('click', () => { rec.start(); $('mic').textContent = '● Listening'; });
    rec.onresult = (e) => { $('msg').value = [...e.results].map(r => r[0].transcript).join(''); };
    rec.onend = () => { $('mic').textContent = 'Mic'; if ($('msg').value.trim()) $('composer').requestSubmit(); };
    rec.onerror = (e) => { $('mic').textContent = 'Mic'; $('msg').placeholder = 'Mic error: ' + e.error; };
  }
  $('chips').addEventListener('click', (e) => {
    const c = e.target.closest('.chip'); if (!c) return;
    $('msg').value = c.dataset.text; $('msg').focus();
  });
  function resetView(id) {
    if (inflight) inflight.abort();
    sessionId = id;
    try { localStorage.setItem(KEY, sessionId); } catch (_) {}
    cy.elements().remove();
    feed.querySelectorAll('.msg').forEach((m) => m.remove());
    $('empty').hidden = false; $('graph-empty').hidden = false; $('inspector').hidden = true;
    $('lineage').innerHTML = ''; $('sat-value').textContent = '–'; $('sat-fill').style.width = '0';
    $('sat-delta').textContent = ''; $('sat-meta').textContent = 'Describe an idea to score its market.';
    $('send').disabled = false;
    if ($('sessions-select')) $('sessions-select').value = '';
    if (window.renderPlan) window.renderPlan(null);
  }
  $('download').addEventListener('click', async () => {
    const res = await fetch(`/api/session/${encodeURIComponent(sessionId)}/transcript`);
    if (!res.ok) { addMsg('agent', '<div class="meta">Download</div><div class="body">Nothing to download yet. Send a message first.</div>'); return; }
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await res.blob());
    a.download = `ideator-${sessionId.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  $('new-session').addEventListener('click', () => { resetView(newId()); $('msg').focus(); });
  // Theme: system default, or the viewer's saved choice.
  const applyTheme = (t) => {
    if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
    cy.style(graphStyle());
  };
  let theme = null;
  try { theme = localStorage.getItem('ideator.theme'); } catch (_) {}
  if (theme) applyTheme(theme);
  $('theme').addEventListener('click', () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
    theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('ideator.theme', theme); } catch (_) {}
    applyTheme(theme);
  });

  // ---- account ----
  $('logout').addEventListener('click', async () => {
    await fetch('/auth/logout', { method: 'POST' });
    location.reload();
  });
  const me = await fetch('/auth/me').then((r) => r.json()).catch(() => ({ auth_enabled: false, user: null }));
  if (me.auth_enabled && !me.user) { $('gate').hidden = false; $('console').hidden = true; return; }
  if (me.auth_enabled) {
    $('user').hidden = false; $('logout').hidden = false;
    $('user-name').textContent = me.user.login;
    if (me.user.avatar_url) $('avatar').src = me.user.avatar_url; else $('avatar').remove();
  }

  // ---- restore and session switcher (Track 02) ----
  async function restoreSession(id) {
    resetView(id);

    try {
      const s = await fetch(`/api/session/${encodeURIComponent(sessionId)}`).then((r) => r.json());
      if (!s.exists && (!s.transcript || !s.transcript.length)) return;
      if (s.nodes && s.nodes.length) {
        const active = (s.lineage || []).find((v) => v.active);
        applyDelta({ nodes: s.nodes, edges: s.edges, focus: active && (active.node || active.id),
          superseded: (s.lineage || []).filter((v) => v.superseded).map((v) => v.node || v.id) });
      }
      for (const [id, ins] of Object.entries(s.insights || {})) cy.getElementById(id).data('insight', ins);
      window.renderPlan(s.plan || [], s.brief, s.question);
      if (s.score && s.score.vector) renderScore({ ...s.score, previous: null, lineage: s.lineage || [] });
      for (const m of s.transcript || []) {
        addMsg(m.role === 'user' ? 'user' : 'agent', m.role === 'user' ? esc(m.text) : `<div class="meta">Ideator</div><div class="body md">${md(m.text)}</div>`);
      }
      if (!(s.transcript || []).length && (s.lineage || []).length) {
        addMsg('agent', `<div class="meta">Restored from graph</div><div class="body">Session has ${s.lineage.length} idea version${s.lineage.length > 1 ? 's' : ''}. Continue refining, or start a new session.</div>`);
      }
      loadSessionsList();
    } catch (_) {}
  }

  async function loadSessionsList() {
    try {
      const res = await fetch('/api/sessions').then((r) => r.json());
      const sel = $('sessions-select');
      if (!sel || !res.sessions) return;
      sel.innerHTML = '<option value="">Past sessions…</option>';
      for (const s of res.sessions) {
        const opt = document.createElement('option');
        opt.value = s.session_id;
        opt.textContent = `${s.title} · ${new Date(s.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
        if (s.session_id === sessionId) opt.selected = true;
        sel.appendChild(opt);
      }
    } catch (_) {}
  }

  const selElem = $('sessions-select');
  if (selElem) {
    selElem.addEventListener('change', (e) => {
      if (e.target.value) restoreSession(e.target.value);
    });
  }


  // Restore active session on page load
  if (!inflight) restoreSession(sessionId);  // user already sent something while the page was loading: keep it
  loadSessionsList();

  // ---- resizable split: chat on the left, map on the right; the width is remembered per browser ----
  const split = $('split'), con = $('console');
  const setLeft = (pct) => {
    pct = Math.min(75, Math.max(22, pct));
    con.style.setProperty('--left', pct + '%');
    split.setAttribute('aria-valuenow', Math.round(pct));
    cy.resize();
    try { localStorage.setItem('ideator.split', pct); } catch (_) {}
  };
  try { const saved = parseFloat(localStorage.getItem('ideator.split')); if (saved) setLeft(saved); } catch (_) {}
  split.addEventListener('pointerdown', (e) => {
    split.setPointerCapture(e.pointerId); split.classList.add('dragging'); document.body.classList.add('resizing');
    const move = (ev) => setLeft((ev.clientX - con.getBoundingClientRect().left) / con.clientWidth * 100);
    const up = () => { split.removeEventListener('pointermove', move); split.classList.remove('dragging'); document.body.classList.remove('resizing'); };
    split.addEventListener('pointermove', move);
    split.addEventListener('pointerup', up, { once: true });
  });
  split.addEventListener('keydown', (e) => {
    const cur = parseFloat(getComputedStyle(con).getPropertyValue('--left')) || 40;
    if (e.key === 'ArrowLeft') setLeft(cur - 3); else if (e.key === 'ArrowRight') setLeft(cur + 3); else return;
    e.preventDefault();
  });
  split.addEventListener('dblclick', () => setLeft(40));
})();

// ---- background: slowly shimmering random characters, like the Claude Code start screen ----
(() => {
  const c = document.getElementById('rain'); if (!c) return;
  const g = c.getContext('2d'), CH = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', S = 16;
  let cols, rows, cells;
  const fit = () => {
    c.width = innerWidth; c.height = innerHeight; cols = Math.ceil(c.width / 10); rows = Math.ceil(c.height / S);
    cells = Array.from({ length: cols * rows }, () => Math.random());
  };
  const draw = () => {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    g.fillStyle = getComputedStyle(document.body).backgroundColor; g.fillRect(0, 0, c.width, c.height);
    g.font = '13px ui-monospace, Consolas, monospace';
    for (let i = 0; i < cells.length; i++) {
      const a = cells[i] * 0.35;
      g.fillStyle = dark ? `rgba(90,140,190,${a})` : `rgba(60,90,130,${a * 0.6})`;
      g.fillText(CH[(Math.random() * CH.length) | 0], (i % cols) * 10, ((i / cols) | 0) * S + 12);
    }
  };
  fit(); draw(); addEventListener('resize', () => { fit(); draw(); });
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  setInterval(() => { for (let i = 0; i < cells.length / 20; i++) cells[(Math.random() * cells.length) | 0] = Math.random() ** 3; draw(); }, 150);
})();
