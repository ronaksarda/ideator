// Ideator Frontend Application Engine

let cy = null;

document.addEventListener('DOMContentLoaded', () => {
  initCytoscape();

  const runBtn = document.getElementById('run-audit-btn');
  runBtn.addEventListener('click', runMarketAudit);

  const resetBtn = document.getElementById('reset-zoom-btn');
  resetBtn.addEventListener('click', () => {
    if (cy) {
      cy.layout({ name: 'cose', animate: true, padding: 30 }).run();
    }
  });

  const closeInspectorBtn = document.getElementById('close-inspector-btn');
  closeInspectorBtn.addEventListener('click', () => {
    document.getElementById('inspector-card').classList.add('hidden');
  });
});

function initCytoscape() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    elements: [],
    style: [
      {
        selector: 'node',
        style: {
          'label': 'data(label)',
          'background-color': 'data(color)',
          'color': '#f8fafc',
          'font-size': '10px',
          'font-family': 'JetBrains Mono, monospace',
          'text-valign': 'bottom',
          'text-margin-y': '6px',
          'text-wrap': 'wrap',
          'text-max-width': '120px',
          'width': 28,
          'height': 28,
          'border-width': 2,
          'border-color': '#0f172a',
          'transition-property': 'background-color, border-width, width, height',
          'transition-duration': '0.2s'
        }
      },
      {
        selector: 'node[type = "idea"]',
        style: {
          'width': 38,
          'height': 38,
          'font-weight': 'bold',
          'font-size': '11px',
          'border-color': '#c084fc',
          'border-width': 3
        }
      },
      {
        selector: 'node:selected',
        style: {
          'border-color': '#ffffff',
          'border-width': 4
        }
      },
      {
        selector: 'edge',
        style: {
          'width': 1.5,
          'line-color': '#334155',
          'target-arrow-color': '#64748b',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'arrow-scale': 0.8,
          'label': 'data(label)',
          'font-size': '8px',
          'color': '#64748b',
          'text-rotation': 'autorotate',
          'text-margin-y': '-6px'
        }
      }
    ],
    layout: {
      name: 'cose',
      animate: true,
      padding: 30
    }
  });

  cy.on('tap', 'node', (evt) => {
    const node = evt.target;
    showInspector(node.data());
  });
}

function showInspector(data) {
  const card = document.getElementById('inspector-card');
  const typeBadge = document.getElementById('inspector-type');
  const title = document.getElementById('inspector-title');
  const content = document.getElementById('inspector-content');
  const meta = document.getElementById('inspector-meta');

  card.classList.remove('hidden');

  const type = data.type || 'node';
  typeBadge.textContent = type.toUpperCase();

  const colors = {
    idea: 'bg-purple-900/60 text-purple-300 border border-purple-500/30',
    competitor: 'bg-blue-900/60 text-blue-300 border border-blue-500/30',
    feature: 'bg-emerald-900/60 text-emerald-300 border border-emerald-500/30',
    grievance: 'bg-red-900/60 text-red-300 border border-red-500/30',
    pricing: 'bg-amber-900/60 text-amber-300 border border-amber-500/30'
  };
  typeBadge.className = `text-[10px] font-mono px-2 py-0.5 rounded font-semibold ${colors[type] || 'bg-slate-800 text-slate-300'}`;

  title.textContent = data.label || 'Entity';
  content.textContent = data.full_text || data.snippet || 'No additional snippet available for this node.';

  let metaHtml = `ID: <span class="text-slate-300">${data.id}</span>`;
  if (data.url) {
    metaHtml += `<br>URL: <a href="${data.url}" target="_blank" class="text-purple-400 underline hover:text-purple-300">${data.url}</a>`;
  }
  if (data.amount_usd !== undefined && data.amount_usd > 0) {
    metaHtml += `<br>Pricing: <span class="text-emerald-400 font-bold">$${data.amount_usd}/mo</span>`;
  }
  meta.innerHTML = metaHtml;
}

function addLog(text, stage = 'INFO') {
  const container = document.getElementById('log-container');
  const entry = document.createElement('div');
  const time = new Date().toLocaleTimeString();
  entry.innerHTML = `<span class="text-slate-600">[${time}]</span> <span class="text-purple-400">[${stage}]</span> ${text}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;
}

async function runMarketAudit() {
  const idea = document.getElementById('idea-input').value.trim();
  if (!idea) return;

  const runBtn = document.getElementById('run-audit-btn');
  const spinner = document.getElementById('btn-spinner');
  const badge = document.getElementById('pipeline-badge');

  runBtn.disabled = true;
  spinner.classList.remove('hidden');
  badge.textContent = 'RUNNING';
  badge.className = 'text-[10px] px-2 py-0.5 rounded bg-purple-900/50 text-purple-300 border border-purple-500/30 font-mono animate-pulse';

  document.getElementById('log-container').innerHTML = '';
  addLog(`Initiating market intelligence pipeline for: "${idea}"`);

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idea })
    });

    if (!response.ok) {
      throw new Error(`Server responded with ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n\n');
      buffer = lines.pop(); // Keep unparsed partial line

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const payload = JSON.parse(line.substring(6));
            handleEvent(payload);
          } catch (e) {
            console.error('Error parsing SSE event:', e);
          }
        }
      }
    }

    badge.textContent = 'COMPLETE';
    badge.className = 'text-[10px] px-2 py-0.5 rounded bg-emerald-900/50 text-emerald-300 border border-emerald-500/30 font-mono';
    addLog('Market audit successfully finalized.', 'SUCCESS');

  } catch (err) {
    console.error('Audit failed:', err);
    addLog(`Pipeline error: ${err.message}`, 'ERROR');
    badge.textContent = 'FAILED';
    badge.className = 'text-[10px] px-2 py-0.5 rounded bg-red-900/50 text-red-300 border border-red-500/30 font-mono';
  } finally {
    runBtn.disabled = false;
    spinner.classList.add('hidden');
  }
}

function handleEvent(payload) {
  const stage = payload.stage;
  if (payload.log) {
    addLog(payload.log, stage);
  }

  if (stage === 'GRAPH_INGEST' && payload.cytoscape) {
    if (cy) {
      cy.elements().remove();
      cy.add(payload.cytoscape);
      cy.layout({
        name: 'cose',
        animate: true,
        animationDuration: 800,
        nodeRepulsion: 4000,
        idealEdgeLength: 60,
        padding: 40
      }).run();
    }
  }

  if (stage === 'TABULAR' && payload.tabular) {
    const t = payload.tabular;
    document.getElementById('tabular-card').classList.remove('hidden');
    document.getElementById('metric-saturation').textContent = `${t.saturation_index}%`;
    document.getElementById('metric-risk').textContent = t.risk_tier;
    
    const riskBadge = document.getElementById('metric-risk');
    if (t.risk_tier === 'LOW') riskBadge.className = 'text-sm font-bold font-mono mt-0.5 text-emerald-400';
    else if (t.risk_tier === 'MODERATE') riskBadge.className = 'text-sm font-bold font-mono mt-0.5 text-yellow-400';
    else if (t.risk_tier === 'HIGH') riskBadge.className = 'text-sm font-bold font-mono mt-0.5 text-orange-400';
    else riskBadge.className = 'text-sm font-bold font-mono mt-0.5 text-red-400';

    document.getElementById('metric-price').textContent = `$${t.recommended_entry_price || t.pricing_recommendation_usd}/mo`;
  }

  if (stage === 'COMPLETE' && payload.blueprint) {
    const b = payload.blueprint;
    document.getElementById('blueprint-card').classList.remove('hidden');
    document.getElementById('blueprint-wedge').textContent = b.the_wedge;
    
    const cutsList = document.getElementById('blueprint-cuts');
    cutsList.innerHTML = '';
    (b.mvp_scope_cuts || []).forEach(cut => {
      const li = document.createElement('li');
      li.textContent = cut;
      cutsList.appendChild(li);
    });

    document.getElementById('blueprint-monetization').textContent = b.monetization_structure;
  }
}
