// Official Tabler Icons outline paths: table, table-shortcut, brand-python, folder-open (MIT).
const ICONS = {
  "folder-open": "M5 19l2.757 -7.351a1 1 0 0 1 .936 -.649h12.307a1 1 0 0 1 .986 1.164l-.996 5.211a2 2 0 0 1 -1.964 1.625h-14.026a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2h4l3 3h7a2 2 0 0 1 2 2v2",
  "source": "M3 13v-8a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-8 M3 10h18 M10 3v11 M2 22l5 -5 M7 21.5v-4.5h-4.5",
  "seeder": "M12 9h-7a2 2 0 0 0 -2 2v4a2 2 0 0 0 2 2h3 M12 15h7a2 2 0 0 0 2 -2v-4a2 2 0 0 0 -2 -2h-3 M8 9v-4a2 2 0 0 1 2 -2h4a2 2 0 0 1 2 2v5a2 2 0 0 1 -2 2h-4a2 2 0 0 0 -2 2v5a2 2 0 0 0 2 2h4a2 2 0 0 0 2 -2v-4 M11 6l0 .01 M13 18l0 .01",
  "model": "M3 5a2 2 0 0 1 2 -2h14a2 2 0 0 1 2 2v14a2 2 0 0 1 -2 2h-14a2 2 0 0 1 -2 -2v-14 M3 10h18 M10 3v18"
};
const viewport = document.getElementById('graph');
const canvas = document.getElementById('canvas');
const nodes = new Map();
const edges = [];
const cells = new Map();
const links = new Map();
const incoming = new Map();
const outgoing = new Map();
let paper, graph;
let selectedId = null;
let viewMode = 'project';
let columnData = null, selectedColumnId = null;
const columnRows = new Map(), columnIncoming = new Map(), columnOutgoing = new Map();
const columnLinks = [];
let cllError = '', cllLoading = false, cllRequest = 0;
let clickTimer = null;
let scale = 1, translateX = 0, translateY = 0, fittedScale = 1;
let drag = null, suppressClick = false;
function walk(start, adjacency, direction) {
  const related = new Set([start]), paths = new Set(), pending = [start];
  while (pending.length) {
    for (const edge of adjacency.get(pending.pop())) {
      paths.add(edge.id);
      const neighbor = edge[direction];
      if (!related.has(neighbor)) { related.add(neighbor); pending.push(neighbor); }
    }
  }
  return {nodes: related, edges: paths};
}
function highlight(id = null) {
  const up = id && nodes.get(id).type === 'model' ? walk(id, incoming, 'source') : {nodes: new Set(), edges: new Set()};
  const down = id ? walk(id, outgoing, 'target') : {nodes: new Set(), edges: new Set()};
  for (const [key, cell] of cells) {
    const selected = key === id, upstream = up.nodes.has(key), downstream = down.nodes.has(key);
    cell.attr('body/stroke', selected ? '#2563eb' : upstream ? '#06b6d4' : downstream ? '#d946ef' : '#b3bfce');
    cell.attr('body/strokeWidth', selected ? 3 : upstream || downstream ? 2 : 1);
    cell.attr('root/opacity', !id || selected || upstream || downstream ? 1 : 0.4);
    const view = paper.findViewByModel(cell);
    view.el.classList.toggle('is-selected', selected);
    view.el.classList.toggle('is-related', !selected && (upstream || downstream));
  }
  for (const edge of edges) {
    const link = links.get(edge.id), upstream = up.edges.has(edge.id), downstream = down.edges.has(edge.id);
    const color = upstream ? '#06b6d4' : downstream ? '#d946ef' : '#b8c3d1';
    link.attr('line/stroke', color);
    link.attr('line/strokeWidth', upstream || downstream ? 2 : 1);
    link.attr('line/targetMarker/fill', color);
    link.attr('line/targetMarker/stroke', color);
    link.attr('root/opacity', !id || upstream || downstream ? 1 : 0.1);
    const view = paper.findViewByModel(link);
    view.el.classList.toggle('is-highlighted', upstream || downstream);
    view.el.classList.toggle('is-dimmed', Boolean(id) && !upstream && !downstream);
  }
}
function resetSelection() {
  clearTimeout(clickTimer);
  selectedId = null; cllError = ''; cllLoading = false; cllRequest++;
  highlight(); renderPanel();
}

function ranks(ids, pairs, allowSchemaCycles = false) {
  const depth = new Map(ids.map(id => [id, 0]));
  const degree = new Map(ids.map(id => [id, 0]));
  const next = new Map(ids.map(id => [id, []]));
  for (const [source, target] of pairs) { degree.set(target, degree.get(target) + 1); next.get(source).push(target); }
  const queue = ids.filter(id => degree.get(id) === 0);
  for (let i = 0; i < queue.length; i++) {
    const source = queue[i];
    for (const target of next.get(source)) {
      depth.set(target, Math.max(depth.get(target), depth.get(source) + 1));
      degree.set(target, degree.get(target) - 1);
      if (degree.get(target) === 0) queue.push(target);
    }
  }
  if (queue.length !== ids.length) {
    // A valid Model DAG can re-enter a schema. Use stable schema columns in that case.
    if (allowSchemaCycles) return new Map(ids.map((id, index) => [id, index]));
    throw new Error('Lineage contains a cycle; the simple DAG layout cannot render it.');
  }
  return depth;
}
let DATA;
const panel = document.getElementById('panel-content');
const MONOSPACE = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
const iconKey = node => node.origin === 'source' ? 'source' : node.type;
// Tuple identities preserve catalogs and punctuation without ambiguous dotted keys.
const modelId = model => JSON.stringify(['MODEL', model.catalog || '', model.schema, model.name]);
const schemaId = node => JSON.stringify([node.catalog || '', node.schema]);
const schemaLabel = node => [node.catalog, node.schema].filter(Boolean).join('.');
const refId = ref => ref.type === 'SEEDER' ? JSON.stringify(['SEEDER', ref.id]) : modelId(ref);
let activeRun = null, runLoading = false, runError = '', runRequest = 0;
const runResults = new Map(), runQuality = new Map();
const runSelect = document.getElementById('run-selector');
const RUN_COLORS = {
  SUCCESS: {fill: '#f0fdf4', text: '#166534'},
  FAILED: {fill: '#fef2f2', text: '#b91c1c'},
  SKIPPED: {fill: '#fffbeb', text: '#92400e'},
  'NO RESULT': {fill: '#f8fafc', text: '#64748b'},
};
const durationText = value => value == null ? 'Not recorded' : `${value.toFixed(3)} s`;
const timestampText = value => value == null ? 'Not recorded' : value;
const runDate = value => new Date(value).toLocaleString(undefined, {timeZoneName: 'short'});
function indexRun(run) {
  activeRun = run; runResults.clear(); runQuality.clear();
  if (!run) return;
  for (const result of run.models) runResults.set(modelId(result), {...result, label: [result.schema, result.name].join('.')});
  for (const result of run.seeders) runResults.set(refId({type: 'SEEDER', id: result.name}), {...result, label: result.name});
  for (const group of run.quality) runQuality.set(modelId(group.model), group.checks);
}
function renderRunSummary() {
  const summary = document.getElementById('run-summary');
  summary.replaceChildren();
  const message = document.getElementById('run-message');
  message.textContent = runError || (runLoading ? 'Loading execution…' : '');
  message.setAttribute('role', runError ? 'alert' : 'status');
  document.getElementById('graph-context').textContent = viewMode === 'cll'
    ? 'Current column lineage · execution overlay is on the project DAG'
    : activeRun ? 'Current project DAG · historical execution overlay' : 'Current project DAG · no execution overlay';
  if (!activeRun) return;
  element('p', `${activeRun.status} · ${durationText(activeRun.duration)}`, summary, 'font-semibold text-slate-900');
  element('p', runDate(activeRun.started_at), summary);
  element('p', activeRun.run_id, summary, 'break-all font-mono text-slate-500');
  element('p', 'Results belong to this execution. Nodes, dependencies and column lineage reflect the current project.', summary);
  element('p', 'SUCCESS · FAILED · SKIPPED · NO RESULT', summary, 'font-mono text-[10px]');
  const unmatched = [...runResults.keys()].filter(id => !nodes.has(id)).length;
  if (unmatched) {
    const button = element('button', `Historical results without a current node (${unmatched})`, summary, 'text-left font-medium text-blue-700 hover:underline');
    button.onclick = () => { if (viewMode === 'cll') returnToProject(); resetSelection(); };
  }
}
function applyRunOverlay() {
  if (viewMode !== 'project') return;
  for (const [id, cell] of cells) {
    const status = activeRun ? runResults.get(id)?.status || 'NO RESULT' : '';
    const colors = RUN_COLORS[status] || RUN_COLORS['NO RESULT'];
    cell.attr({body: {fill: status ? colors.fill : '#fff'}, title: {y: status ? 29 : 40},
      executionStatus: {text: status, fill: colors.text}});
    const view = paper.findViewByModel(cell);
    view.el.setAttribute('data-execution-status', status);
    view.el.setAttribute('aria-label', `${nodes.get(id).label}${status ? ` · ${status}` : ''}`);
  }
}
function renderResult(parent, result, checks = []) {
  element('p', result.status, parent, 'mb-3 font-mono text-xs font-semibold');
  const list = element('dl', null, parent, 'space-y-2 text-xs');
  const fields = [
    ['Started', timestampText(result.started_at)], ['Finished', timestampText(result.finished_at)],
    ['Duration', durationText(result.duration)],
  ];
  if ('materialization_duration' in result) fields.push(['Materialization', durationText(result.materialization_duration)]);
  if ('execution_duration' in result) fields.push(['Seeder execution', durationText(result.execution_duration)]);
  for (const [label, value] of fields) {
    element('dt', label, list, 'text-slate-500');
    element('dd', value, list, 'break-words font-mono');
  }
  if (result.error) element('pre', result.error, parent, 'mt-4 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-red-200 bg-red-50 p-3 text-xs text-red-800');
  if (checks.length) {
    element('h4', 'Quality checks', parent, 'mb-3 mt-5 text-xs font-semibold');
    for (const check of checks) {
      const block = element('div', null, parent, 'mb-3 rounded border border-slate-300 bg-white p-3 text-xs');
      element('p', `${check.name} · ${check.status} · ${check.severity}`, block, 'break-words font-semibold');
      element('p', `Observed: ${JSON.stringify(check.observed)} · Expected: ${check.expected}`, block, 'mt-2 break-words');
      element('p', `Duration: ${durationText(check.duration)}`, block, 'mt-2 text-slate-500');
    }
  }
}
function renderNodeExecution(id) {
  if (!activeRun) return;
  const block = section('Historical execution result');
  const result = runResults.get(id);
  if (result) renderResult(block, result, runQuality.get(id));
  else element('p', 'NO RESULT · No result recorded for this node in this execution.', block, 'text-xs text-slate-500');
}
function renderUnmatchedResults() {
  if (!activeRun) return;
  const unmatched = [...runResults].filter(([id]) => !nodes.has(id));
  const block = section(`Historical results without a current node (${unmatched.length})`);
  element('p', 'Matched by dataset identity or Seeder path against the current project DAG.', block, 'mb-4 text-xs text-slate-500');
  for (const [id, result] of unmatched) {
    const details = element('details', null, block, 'mb-3 rounded border border-slate-300 bg-white p-3');
    element('summary', `${result.label} · ${result.status}`, details, 'cursor-pointer break-words font-mono text-xs');
    const content = element('div', null, details, 'mt-4');
    renderResult(content, result, runQuality.get(id));
  }
}
async function selectRun(runId) {
  const request = ++runRequest;
  indexRun(null); runError = ''; runLoading = Boolean(runId);
  applyRunOverlay(); renderRunSummary();
  if (viewMode === 'project') renderPanel();
  if (!runId) return;
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not load execution');
    if (request !== runRequest) return;
    indexRun(body);
  } catch (error) {
    if (request !== runRequest) return;
    runError = `Could not load execution: ${error.message}`;
  } finally {
    if (request === runRequest) {
      runLoading = false; applyRunOverlay(); renderRunSummary();
      if (viewMode === 'project') renderPanel();
    }
  }
}
async function loadRuns() {
  try {
    const response = await fetch('/api/runs');
    const runs = await response.json();
    if (!response.ok) throw new Error(runs.detail || 'Could not load execution history');
    for (const run of runs) {
      const option = element('option', `${runDate(run.started_at)} · ${run.status} · ${durationText(run.duration)}`, runSelect);
      option.value = run.run_id;
    }
    runSelect.disabled = runs.length === 0;
    renderRunSummary();
    if (!runs.length) document.getElementById('run-message').textContent = 'No recorded executions.';
  } catch (error) {
    runError = `Could not load execution history: ${error.message}`;
    renderRunSummary();
  }
}
runSelect.addEventListener('change', () => selectRun(runSelect.value));
function prepareLineage() {
  for (const record of DATA.models) {
    nodes.set(modelId(record), {...record, type: 'model', label: [record.catalog, record.schema, record.name].filter(Boolean).join('.')});
  }
  for (const record of DATA.seeders) {
    nodes.set(refId({type: 'SEEDER', id: record.id}), {...record, type: 'seeder', label: record.id});
  }
  DATA.lineage.forEach((edge, index) => edges.push({id: `edge_${index}`, source: refId(edge.source), target: refId(edge.target)}));
  for (const id of nodes.keys()) { incoming.set(id, []); outgoing.set(id, []); }
  // Incoming means dependencies; outgoing means dependents.
  for (const edge of edges) { incoming.get(edge.target).push(edge); outgoing.get(edge.source).push(edge); }
}
function element(tag, text, parent, className = '') {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  node.className = className;
  parent.append(node);
  return node;
}
function section(title) {
  const block = element('section', null, panel, 'border-t border-slate-300/70 py-6');
  element('h3', title, block, 'mb-4 text-sm font-semibold text-slate-900');
  return block;
}
function connections(title, ids) {
  const block = section(title);
  if (!ids.length) element('p', 'None', block, 'text-xs text-slate-500');
  for (const id of ids) {
    const button = element('button', nodes.get(id).label, block, 'block w-full min-w-0 cursor-pointer truncate py-2 text-left text-xs font-mono font-medium text-slate-700 hover:text-blue-600');
    button.title = nodes.get(id).label;
    button.onclick = () => selectNode(id);
  }
}
function panelIcon(name) {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({viewBox: '0 0 24 24', width: 40, height: 40, fill: 'none', stroke: '#2563eb', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'})) icon.setAttribute(key, value);
  const path = document.createElementNS(icon.namespaceURI, 'path');
  path.setAttribute('d', ICONS[name]); icon.append(path); panel.append(icon);
}
function renderPanel() {
  if (!DATA) return;
  panel.replaceChildren();
  const node = nodes.get(selectedId);
  if (!node) {
    panelIcon('folder-open');
    element('h2', 'Project summary', panel, 'mb-6 mt-4 text-lg font-semibold text-slate-900');
    const counts = [
      ['Models', DATA.models.filter(n => n.origin === 'internal').length],
      ['Seeders', DATA.seeders.length],
      ['Schemas', new Set(DATA.models.map(schemaId)).size],
      ['Dependencies', edges.length],
    ];
    const list = element('dl', null, panel, 'space-y-4');
    for (const [label, value] of counts) {
      const row = element('div', null, list, 'flex justify-between');
      element('dt', label, row); element('dd', value, row, 'font-semibold text-slate-900');
    }
    renderUnmatchedResults();
    return;
  }
  panelIcon(iconKey(node));
  if (node.type === 'seeder') {
    element('p', 'seeder', panel, 'mt-4 text-xs text-slate-500');
  } else {
    element('p', [node.catalog, node.schema].filter(Boolean).join('.'), panel, 'mt-4 font-mono text-xs text-slate-500');
  }
  const title = element('h2', node.name, panel, 'mb-6 mt-1 min-w-0 truncate font-mono text-lg font-semibold text-slate-900');
  title.title = node.name;
  renderNodeExecution(selectedId);
  if (node.type === 'seeder') {
    connections(`Outputs (${node.outputs.length})`, node.outputs.map(modelId));
  } else {
    if (node.origin === 'internal') {
      element('p', cllLoading ? 'Resolving column lineage…' : 'Double-click this Model to explore column lineage.', panel, 'mb-6 text-xs text-slate-500');
      if (cllError) {
        const block = section('Column lineage could not be resolved');
        const error = element('p', cllError, block, 'whitespace-pre-wrap break-words text-xs text-red-700');
        error.setAttribute('role', 'alert');
      }
    }
    if (node.origin !== 'source') connections('Upstream', incoming.get(selectedId).map(e => e.source));
    connections('Downstream', outgoing.get(selectedId).map(e => e.target));
  }
}
function selectNode(id) {
  clearTimeout(clickTimer);
  cllError = ''; cllLoading = false; cllRequest++;
  selectedId = selectedId === id ? null : id;
  highlight(selectedId); renderPanel();
}
function defineCard() {
  return joint.dia.Element.define('lineage.Card', {}, {markup: [
    {tagName: 'rect', selector: 'body'}, {tagName: 'rect', selector: 'iconBackground'},
    {tagName: 'path', selector: 'icon'}, {tagName: 'text', selector: 'title'},
    {tagName: 'text', selector: 'executionStatus'},
  ]});
}
function truncateText(text, width, measure) {
  if (measure.measureText(text).width <= width) return text;
  while (text && measure.measureText(text + '...').width > width) text = text.slice(0, -1);
  return text + '...';
}
function addSvgTitle(view, selector, text) {
  const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
  title.textContent = text;
  view.findNode(selector).append(title);
}
function createNode(id, node, Card) {
  const measure = document.createElement('canvas').getContext('2d');
  measure.font = `600 14px ${MONOSPACE}`;
  const minWidth = 240, maxWidth = 400;
  const width = Math.ceil(Math.min(maxWidth, Math.max(minWidth, measure.measureText(node.name).width + 92)));
  const cell = new Card({z: 2, size: {width, height: 72}, attrs: {
    body: {width: 'calc(w)', height: 'calc(h)', rx: 10, ry: 10, fill: '#fff', stroke: '#b3bfce', strokeWidth: 1},
    iconBackground: {x: 16, y: 16, width: 40, height: 40, rx: 8, fill: '#dbeafe'},
    icon: {d: ICONS[iconKey(node)], transform: 'translate(24 24)', fill: 'none', stroke: '#2563eb', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round'},
    title: {text: truncateText(node.name, width - 88, measure), x: 72, y: 40, fontSize: 14, fontWeight: 600, fontFamily: MONOSPACE, fill: '#0f172a'},
    executionStatus: {text: '', x: 72, y: 52, fontSize: 10, fontWeight: 600, fontFamily: MONOSPACE},
  }});
  cells.set(id, cell);
}
function layoutGraph() {
  const schemas = [...new Set([...nodes.values()].filter(n => n.type === 'model').map(schemaId))].sort();
  const schemaPairs = new Map();
  for (const edge of edges) {
    const source = nodes.get(edge.source), target = nodes.get(edge.target);
    if (source.type === 'model' && target.type === 'model' && schemaId(source) !== schemaId(target)) {
      const pair = [schemaId(source), schemaId(target)];
      schemaPairs.set(JSON.stringify(pair), pair);
    }
  }
  // Schemas occupy dependency columns; each column's containers form vertical bands.
  const schemaRanks = ranks(schemas, [...schemaPairs.values()], true);
  const groups = schemas.map(schema => {
    const ids = [...nodes].filter(([,node]) => node.type === 'model' && schemaId(node) === schema).map(([id]) => id);
    const localEdges = edges.filter(e => ids.includes(e.source) && ids.includes(e.target)).map(e => [e.source, e.target]);
    const localRanks = ranks(ids, localEdges);
    const columns = Array.from({length: Math.max(...localRanks.values()) + 1}, () => []);
    for (const id of ids) columns[localRanks.get(id)].push(id);
    const widths = columns.map(column => Math.max(...column.map(id => cells.get(id).size().width)));
    const heights = columns.map(column => column.reduce((height, id) => height + cells.get(id).size().height + 26, 0) - 26);
    return {
      schema, label: schemaLabel(nodes.get(ids[0])), ids, columns, widths,
      width: widths.reduce((a, b) => a + b, 0) + (columns.length - 1) * 64 + 64,
      height: Math.max(...heights) + 84,
      rank: schemaRanks.get(schema),
    };
  });
  const seeders = [...nodes].filter(([,node]) => node.type === 'seeder').map(([id]) => id);
  let x = seeders.length ? Math.max(...seeders.map(id => cells.get(id).size().width)) + 100 : 0;
  let totalHeight = 0;
  for (let rank = 0; rank <= Math.max(-1, ...schemaRanks.values()); rank++) {
    const band = groups.filter(group => group.rank === rank);
    let y = 0;
    for (const group of band) {
      const container = new joint.shapes.standard.Rectangle({type: 'lineage.Schema', z: 0,
        position: {x,y}, size: {width: group.width, height: group.height}, attrs: {
        root: {pointerEvents: 'none'},
        body: {rx: 10, ry: 10, fill: '#eef2f6', fillOpacity: 1, stroke: '#e2e8f0', strokeWidth: 0.5},
        label: {text: group.label, x: 32, y: 22, textAnchor: 'start', textVerticalAnchor: 'middle', fontFamily: MONOSPACE, fontSize: 11, fontWeight: 400, fill: '#526176'},
      }});
      graph.addCell(container);
      let columnX = x + 32;
      group.columns.forEach((column, index) => {
        let rowY = y + 56;
        for (const id of column) {
          const cell = cells.get(id);
          cell.position(columnX + (group.widths[index] - cell.size().width) / 2, rowY);
          graph.addCell(cell);
          container.embed(cell);
          rowY += cell.size().height + 26;
        }
        columnX += group.widths[index] + 64;
      });
      y += group.height + 36;
    }
    totalHeight = Math.max(totalHeight, y - 36);
    x += Math.max(...band.map(group => group.width)) + 100;
  }
  const seederHeight = seeders.reduce((height, id) => height + cells.get(id).size().height + 26, 0) - 26;
  let seederY = Math.max(0, (totalHeight - seederHeight) / 2);
  for (const id of seeders) {
    const cell = cells.get(id);
    cell.position(0, seederY);
    graph.addCell(cell);
    seederY += cell.size().height + 26;
  }
}
// Short horizontal ends join a cubic with horizontal tangents: one uninterrupted path.
function continuousLink(source, target, vertices, options) {
  const lead = Math.min(20, Math.max(0, (target.x - source.x) / 4));
  const startX = source.x + lead;
  const endX = target.x - lead;
  const handle = (endX - startX) / 2;
  const path = joint.g.Path.parse(
    `M ${source.x} ${source.y} L ${startX} ${source.y} ` +
    `C ${startX + handle} ${source.y} ${endX - handle} ${target.y} ${endX} ${target.y} ` +
    `L ${target.x} ${target.y}`
  );
  return options.raw ? path : path.serialize();
}
function renderProjectGraph() {
  cells.clear(); links.clear();
  const Card = defineCard();
  const namespace = {...joint.shapes, lineage: {Card, Schema: joint.shapes.standard.Rectangle}};
  if (!paper) {
    graph = new joint.dia.Graph({}, {cellNamespace: namespace});
    paper = new joint.dia.Paper({el: viewport, model: graph, cellViewNamespace: namespace,
      width: viewport.clientWidth, height: viewport.clientHeight, interactive: false, async: false,
      background: {color: 'transparent'},
    });
  } else {
    graph.clear();
    paper.off('blank:pointerdblclick');
  }
  for (const [id,node] of nodes) createNode(id,node,Card);
  layoutGraph();
  for (const edge of edges) {
    const link = new joint.shapes.standard.Link({z: 1, source: {id: cells.get(edge.source).id, anchor: {name: 'right'}}, target: {id: cells.get(edge.target).id, anchor: {name: 'left'}}, attrs: {
      line: {stroke: '#b8c3d1', strokeWidth: 1, targetMarker: {type: 'path', d: 'M 4 -2 0 0 4 2 z', fill: '#b8c3d1', stroke: '#b8c3d1'}},
    }});
    link.router('normal');
    link.connector(continuousLink);
    graph.addCell(link);
    links.set(edge.id, link);
  }
  for (const [id,cell] of cells) {
    const view = paper.findViewByModel(cell);
    view.el.classList.add('lineage-node');
    view.el.setAttribute('data-logical-id', id);
    view.el.setAttribute('tabindex', '0');
    view.el.setAttribute('role', 'button');
    view.el.setAttribute('aria-label', nodes.get(id).label);
    addSvgTitle(view, 'title', nodes.get(id).name);
    view.el.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectNode(id); }
    });
  }
  for (const cell of graph.getElements()) if (cell.get('type') === 'lineage.Schema') paper.findViewByModel(cell).el.classList.add('schema-container');
  applyRunOverlay(); renderRunSummary();
  fitGraph();
  paper.on('blank:pointerdblclick', () => {
    resetSelection();
    fitGraph();
  });
}
function applyTransform() {
  if (!paper) return;
  paper.scale(scale,scale);
  paper.translate(translateX,translateY);
  canvas.style.backgroundSize = `${18 * scale}px ${18 * scale}px`;
  canvas.style.backgroundPosition = `${translateX}px ${translateY}px`;
}
function zoomTo(nextScale, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
  if (!paper) return;
  nextScale = Math.max(Math.min(0.3,fittedScale), Math.min(2.5,nextScale));
  const ratio = nextScale / scale;
  translateX = x - (x-translateX)*ratio;
  translateY = y - (y-translateY)*ratio;
  scale = nextScale;
  applyTransform();
}
function fitGraph() {
  if (!paper || !viewport.clientWidth || !viewport.clientHeight) return;
  const bounds = paper.getContentArea();
  const padding = 28;
  fittedScale = Math.min(2.5, Math.max(1,viewport.clientWidth-padding*2)/Math.max(1,bounds.width), Math.max(1,viewport.clientHeight-padding*2)/Math.max(1,bounds.height));
  scale = fittedScale;
  translateX = (viewport.clientWidth-bounds.width*scale)/2-bounds.x*scale;
  translateY = (viewport.clientHeight-bounds.height*scale)/2-bounds.y*scale;
  applyTransform();
}
viewport.addEventListener('wheel', event => {
  if (!paper) return;
  event.preventDefault();
  if (!event.deltaY) return;
  const bounds = viewport.getBoundingClientRect();
  zoomTo(scale+(event.deltaY<0 ? 0.15 : -0.15),event.clientX-bounds.left,event.clientY-bounds.top);
}, {passive:false});
viewport.addEventListener('pointerdown', event => {
  if (!paper || event.button!==0 || !event.isPrimary || drag) return;
  suppressClick = false;
  const pan = !event.target.closest('.lineage-node, [data-column-id]');
  drag = {id:event.pointerId,x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,pan};
  if (pan) { event.preventDefault(); viewport.setPointerCapture(event.pointerId); viewport.classList.add('panning'); }
}, true);
viewport.addEventListener('pointermove', event => {
  if (!drag || drag.id!==event.pointerId) return;
  if (Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>5) suppressClick = true;
  if (drag.pan && suppressClick) { translateX+=event.clientX-drag.x; translateY+=event.clientY-drag.y; applyTransform(); }
  drag.x=event.clientX; drag.y=event.clientY;
});
function endDrag(event) {
  if (!drag || drag.id!==event.pointerId) return;
  if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
  drag=null;
  viewport.classList.remove('panning');
}
window.addEventListener('pointerup',endDrag);
window.addEventListener('pointercancel',endDrag);
viewport.addEventListener('lostpointercapture',endDrag);
viewport.addEventListener('click', event => {
  if (suppressClick && event.detail > 0) { event.preventDefault(); event.stopImmediatePropagation(); return; }
  if (viewMode === 'cll') {
    // Temporary column-click profiling, separate from initial CLL-load timers.
    const profile = new URLSearchParams(location.search).has('cllColumnProfile') ? {started: performance.now(), ms: {}, input: 'click'} : null;
    const row = event.target.closest('[data-column-id]');
    if (row) row.focus({preventScroll: true});
    if (profile) profile.ms.row_lookup_and_focus = performance.now() - profile.started;
    selectColumn(row ? row.getAttribute('data-column-id') : null, profile);
    return;
  }
  clearTimeout(clickTimer);
  const target = event.target.closest('.lineage-node');
  if (!target) { resetSelection(); return; }
  const id = target.getAttribute('data-logical-id');
  const node = nodes.get(id);
  if (node.type === 'model' && node.origin === 'internal' && event.detail > 0) {
    // Wait for a possible double-click so an already selected Model never flashes off.
    if (event.detail === 1) clickTimer = setTimeout(() => selectNode(id), 300);
  } else { selectNode(id); }
}, true);
viewport.addEventListener('dblclick', event => {
  if (viewMode !== 'project' || suppressClick) return;
  const target = event.target.closest('.lineage-node');
  if (!target) return;
  const id = target.getAttribute('data-logical-id');
  const node = nodes.get(id);
  if (node.type !== 'model' || node.origin !== 'internal') return;
  clearTimeout(clickTimer);
  openModelLineage(id);
});
viewport.addEventListener('dragstart',event => event.preventDefault());
new ResizeObserver(() => { if (paper) { paper.setDimensions(viewport.clientWidth,viewport.clientHeight); applyTransform(); } }).observe(viewport);
async function openModelLineage(id) {
  // Temporary profiling: enable with ?cllProfile=1; remove with the report harness.
  const profile = new URLSearchParams(location.search).has('cllProfile') ? {model: id, ms: {}, started: performance.now()} : null;
  selectedId = id;
  const node = nodes.get(id), request = ++cllRequest;
  cllError = ''; cllLoading = true;
  highlight(id); renderPanel();
  try {
    const params = new URLSearchParams({catalog: node.catalog || '', schema: node.schema, model: node.name});
    const fetchStarted = profile && performance.now();
    const response = await fetch(`/api/cll?${params}`);
    if (profile) profile.ms.fetch_headers = performance.now() - fetchStarted;
    const jsonStarted = profile && performance.now();
    const body = await response.json();
    if (profile) {
      profile.ms.json_handling = performance.now() - jsonStarted;
      profile.ms.fetch_body_complete = performance.now() - fetchStarted;
      profile.renderStarted = performance.now();
    }
    if (!response.ok) throw new Error(body.detail || 'Could not load column lineage');
    if (request !== cllRequest || viewMode !== 'project') return;
    renderColumnGraph(body, profile);
    renderCllPanel();
    if (profile) {
      profile.ms.frontend_render = performance.now() - profile.renderStarted;
      profile.ms.transition_total = performance.now() - profile.started;
      profile.size = {datasets: body.datasets.length, columns: body.columns.length, edges: body.edges.length};
      delete profile.started; delete profile.renderStarted;
      window.cllProfiles ||= []; window.cllProfiles.push(profile);
      console.log('[CLL PROFILE]', JSON.stringify(profile));
    }
  } catch (failure) {
    if (request !== cllRequest) return;
    cllError = failure.message;
    renderPanel();
  } finally {
    if (request === cllRequest) {
      cllLoading = false;
      if (viewMode === 'project') renderPanel();
    }
  }
}
const datasetId = ref => JSON.stringify([ref.catalog, ref.schema, ref.model]);
const columnId = ref => JSON.stringify([ref.catalog, ref.schema, ref.model, ref.column]);
const datasetLabel = ref => [ref.catalog, ref.schema, ref.model].filter(Boolean).join('.');
function renderCllPanel(profile = null) {
  let profileMark = profile && performance.now();
  panel.replaceChildren();
  if (profile) { profile.ms.panel_replace_children = performance.now() - profileMark; profileMark = performance.now(); }
  const back = element('button', '← Project', panel, 'mb-6 text-sm font-medium text-blue-600 hover:text-blue-800');
  back.onclick = returnToProject;
  const selected = columnRows.get(selectedColumnId);
  if (!selected) {
    element('h2', 'Column lineage', panel, 'mb-3 text-lg font-semibold text-slate-900');
    const context = element('p', datasetLabel(columnData.root), panel, 'mb-6 truncate font-mono text-xs');
    context.title = datasetLabel(columnData.root);
    element('p', 'Select a column to inspect its lineage.', panel, 'text-xs text-slate-500');
    return;
  }
  panelIcon('model');
  const context = element('p', datasetLabel(selected.ref), panel, 'mt-4 truncate font-mono text-xs text-slate-500');
  context.title = datasetLabel(selected.ref);
  const title = element('h2', selected.ref.column, panel, 'mb-6 mt-1 truncate font-mono text-lg font-semibold text-slate-900');
  title.title = selected.ref.column;
  const block = section('Column lineage');
  if (profile) { profile.ms.panel_dom_construction = performance.now() - profileMark; profileMark = performance.now(); }
  element('pre', selected.text, block, 'max-h-[60vh] overflow-auto rounded border border-slate-300 bg-white p-3 font-mono text-xs leading-6');
  if (profile) {
    profile.ms.panel_pre_text_and_insertion = performance.now() - profileMark;
    profile.lineage_text_characters = (selected.text || '').length;
  }
}
function selectColumn(id, profile = null) {
  if (!profile && new URLSearchParams(location.search).has('cllColumnProfile')) profile = {started: performance.now(), ms: {}, input: 'selectColumn'};
  let profileMark = profile && performance.now();
  selectedColumnId = selectedColumnId === id ? null : id;
  const selected = selectedColumnId;
  const empty = () => ({nodes: new Set(), edges: new Set()});
  const up = selected ? walk(selected, columnIncoming, 'source') : empty();
  if (profile) { profile.ms.upstream_traversal = performance.now() - profileMark; profileMark = performance.now(); }
  const down = selected ? walk(selected, columnOutgoing, 'target') : empty();
  if (profile) { profile.ms.downstream_traversal = performance.now() - profileMark; profileMark = performance.now(); }
  const datasetHighlights = new Map();
  for (const [key, row] of columnRows) {
    let highlights = datasetHighlights.get(row.cell);
    if (!highlights) {
      highlights = {attrs: {}, rows: []};
      datasetHighlights.set(row.cell, highlights);
    }
    const color = key === selected ? '#2563eb' : up.nodes.has(key) ? '#06b6d4' : down.nodes.has(key) ? '#d946ef' : null;
    highlights.attrs[`row${row.index}`] = {
      fill: color ? key === selected ? '#dbeafe' : up.nodes.has(key) ? '#ecfeff' : '#fdf4ff' : 'none',
      stroke: color || 'none',
    };
    highlights.attrs[`text${row.index}`] = {fill: color || '#334155'};
    highlights.attrs[`group${row.index}`] = {opacity: !selected || color ? 1 : 0.25};
    highlights.rows.push({index: row.index, pressed: key === selected});
  }
  for (const [cell, highlights] of datasetHighlights) {
    cell.attr(highlights.attrs);
    const view = paper.findViewByModel(cell);
    for (const row of highlights.rows) {
      view.findNode(`group${row.index}`).setAttribute('aria-pressed', String(row.pressed));
    }
  }
  if (profile) { profile.ms.row_highlight_updates = performance.now() - profileMark; profileMark = performance.now(); }
  for (const edge of columnLinks) {
    const related = up.edges.has(edge.id) || down.edges.has(edge.id);
    const color = up.edges.has(edge.id) ? '#06b6d4' : down.edges.has(edge.id) ? '#d946ef' : '#b8c3d1';
    edge.link.attr('line/stroke', color);
    edge.link.attr('line/strokeWidth', related ? 2 : 1);
    edge.link.attr('line/targetMarker/fill', color);
    edge.link.attr('line/targetMarker/stroke', color);
    edge.link.attr('root/opacity', !selected || related ? 1 : 0.1);
    const view = paper.findViewByModel(edge.link);
    view.el.classList.toggle('is-highlighted', related);
    view.el.classList.toggle('is-dimmed', Boolean(selected) && !related);
  }
  if (profile) { profile.ms.edge_highlight_updates = performance.now() - profileMark; profileMark = performance.now(); }
  renderCllPanel(profile);
  if (profile) {
    profile.ms.panel_render = performance.now() - profileMark;
    profile.ms.sync_total = performance.now() - profile.started;
    profile.ms.jointjs_svg_updates = profile.ms.row_highlight_updates + profile.ms.edge_highlight_updates;
    profile.selected = selected ? columnRows.get(selected).ref : null;
    profile.root = columnData.root;
    profile.counts = {
      visible_rows: columnRows.size, edges: columnLinks.length,
      upstream_visited_nodes: up.nodes.size, downstream_visited_nodes: down.nodes.size,
      upstream_related_columns: up.nodes.size - (selected ? 1 : 0), downstream_related_columns: down.nodes.size - (selected ? 1 : 0),
      upstream_edges: up.edges.size, downstream_edges: down.edges.size,
      rows_updated: columnRows.size, edges_updated: columnLinks.length,
      find_view_by_model_calls: datasetHighlights.size + columnLinks.length,
      jointjs_attr_calls: datasetHighlights.size + columnLinks.length * 5,
      dataset_row_attr_calls: datasetHighlights.size,
      row_level_attr_calls: 0,
      row_dom_set_attribute_calls: columnRows.size,
      edge_dom_class_list_toggle_calls: columnLinks.length * 2,
      explicit_inline_style_writes: 0,
      panel_elements: panel.querySelectorAll('*').length,
    };
    requestAnimationFrame(() => {
      profile.ms.first_frame = performance.now() - profile.started;
      requestAnimationFrame(() => {
        profile.ms.second_frame = performance.now() - profile.started;
        profile.ms.sync_to_first_frame = profile.ms.first_frame - profile.ms.sync_total;
        profile.ms.first_to_second_frame = profile.ms.second_frame - profile.ms.first_frame;
        delete profile.started;
        window.cllColumnProfiles ||= []; window.cllColumnProfiles.push(profile);
        console.log('[CLL COLUMN PROFILE]', JSON.stringify(profile));
      });
    });
  }
}
function returnToProject() {
  viewMode = 'project'; columnData = null; selectedColumnId = null;
  columnRows.clear(); columnLinks.length = 0; columnIncoming.clear(); columnOutgoing.clear();
  renderProjectGraph(); highlight(selectedId); renderPanel();
}
function layoutColumnLineage(datasets, edges) {
  const ids = [...datasets.keys()].sort();
  const pairs = new Map();
  for (const edge of edges) {
    const pair = [datasetId(edge.source), datasetId(edge.target)];
    if (pair[0] !== pair[1]) pairs.set(JSON.stringify(pair), pair);
  }
  const depth = ranks(ids, [...pairs.values()]);
  let x = 0;
  for (let rank = 0; rank <= Math.max(...depth.values()); rank++) {
    const band = ids.filter(id => depth.get(id) === rank);
    let y = 0;
    for (const id of band) {
      const cell = datasets.get(id).cell;
      cell.position(x, y); y += cell.size().height + 36;
    }
    x += Math.max(...band.map(id => datasets.get(id).cell.size().width)) + 100;
  }
}
function createColumnCard(dataset) {
  const measure = document.createElement('canvas').getContext('2d');
  measure.font = `12px ${MONOSPACE}`;
  const headerHeight = 72, headerGap = 4, rowHeight = 40, bottomPadding = 6;
  const columnsY = headerHeight + headerGap;
  const minWidth = 264, maxWidth = 400;
  const context = [dataset.catalog, dataset.schema].filter(Boolean).join('.');
  const columnWidth = Math.max(0, ...dataset.columns.map(text => measure.measureText(text).width + 32));
  measure.font = `11px ${MONOSPACE}`;
  const contextWidth = measure.measureText(context).width + 92;
  measure.font = `600 14px ${MONOSPACE}`;
  const titleWidth = measure.measureText(dataset.model).width + 92;
  const width = Math.ceil(Math.min(maxWidth, Math.max(minWidth, titleWidth, contextWidth, columnWidth)));
  const title = truncateText(dataset.model, width - 88, measure);
  measure.font = `11px ${MONOSPACE}`;
  const subtitle = truncateText(context, width - 88, measure);
  measure.font = `12px ${MONOSPACE}`;
  const markup = [
    {tagName: 'rect', selector: 'body'}, {tagName: 'rect', selector: 'iconBackground'},
    {tagName: 'path', selector: 'icon'}, {tagName: 'text', selector: 'title'}, {tagName: 'text', selector: 'context'},
  ];
  const attrs = {
    body: {width: 'calc(w)', height: 'calc(h)', rx: 10, ry: 10, fill: '#fff', stroke: '#b3bfce'},
    iconBackground: {x: 16, y: 16, width: 40, height: 40, rx: 8, fill: '#dbeafe'},
    icon: {d: ICONS.model, transform: 'translate(24 24)', fill: 'none', stroke: '#2563eb', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round'},
    title: {text: title, x: 72, y: 32, fontFamily: MONOSPACE, fontSize: 14, fontWeight: 600, fill: '#0f172a'},
    context: {text: subtitle, x: 72, y: 50, fontFamily: MONOSPACE, fontSize: 11, fill: '#526176'},
  };
  const ports = [];
  dataset.columns.forEach((column, index) => {
    const y = columnsY + index * rowHeight;
    const centerY = y + rowHeight / 2;
    markup.push({tagName: 'g', selector: `group${index}`, children: [
      {tagName: 'rect', selector: `rowBackground${index}`},
      {tagName: 'rect', selector: `row${index}`, attributes: {class: 'column-highlight'}}, {tagName: 'text', selector: `text${index}`},
    ]});
    attrs[`group${index}`] = {cursor: 'pointer'};
    attrs[`rowBackground${index}`] = {x: 1, y, width: width - 2, height: rowHeight, fill: '#fff', stroke: '#e2e8f0', strokeWidth: 0.5};
    attrs[`row${index}`] = {x: 5, y: y + 2, width: width - 10, height: rowHeight - 4, rx: 6, ry: 6, fill: 'none', stroke: 'none', strokeWidth: 1};
    attrs[`text${index}`] = {text: truncateText(column, width - 24, measure), x: 12, y: centerY + 4, fontFamily: MONOSPACE, fontSize: 12, fill: '#334155'};
    for (const side of ['in', 'out']) ports.push({id: JSON.stringify([column, side]), group: 'row', args: {x: side === 'in' ? 0 : width, y: centerY}});
  });
  return new joint.dia.Element({type: 'cll.Dataset', z: 2, size: {width, height: columnsY + dataset.columns.length * rowHeight + bottomPadding}, markup, attrs,
    ports: {groups: {row: {position: {name: 'absolute'}, attrs: {circle: {r: 0, magnet: true}}, markup: [{tagName: 'circle', selector: 'circle'}]}}, items: ports},
  });
}
function renderColumnGraph(data, profile = null) {
  let profileMark = profile && performance.now();
  const datasets = new Map(data.datasets.map(dataset => [datasetId(dataset), {...dataset, cell: createColumnCard(dataset)}]));
  if (profile) { profile.ms.jointjs_cell_creation = performance.now() - profileMark; profileMark = performance.now(); }
  // Build before replacing the project canvas so layout errors leave it intact.
  layoutColumnLineage(datasets, data.edges);
  if (profile) { profile.ms.layout = performance.now() - profileMark; profileMark = performance.now(); }
  graph.clear(); paper.off('blank:pointerdblclick');
  columnRows.clear(); columnLinks.length = 0; columnIncoming.clear(); columnOutgoing.clear();
  columnData = data; selectedColumnId = null; viewMode = 'cll';
  renderRunSummary();
  graph.addCells([...datasets.values()].map(dataset => dataset.cell));
  const texts = new Map(data.columns.map(ref => [columnId(ref), ref.text]));
  for (const dataset of datasets.values()) {
    const view = paper.findViewByModel(dataset.cell);
    addSvgTitle(view, 'title', dataset.model); addSvgTitle(view, 'context', datasetLabel(dataset));
    dataset.columns.forEach((column, index) => {
      const ref = {catalog: dataset.catalog, schema: dataset.schema, model: dataset.model, column};
      const id = columnId(ref), row = view.findNode(`group${index}`);
      addSvgTitle(view, `group${index}`, column);
      row.setAttribute('data-column-id', id); row.setAttribute('tabindex', '0');
      row.setAttribute('role', 'button'); row.setAttribute('aria-label', `${datasetLabel(ref)}.${column}`);
      row.setAttribute('aria-pressed', 'false');
      row.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectColumn(id); }
      });
      columnRows.set(id, {ref, cell: dataset.cell, index, text: texts.get(id)});
      columnIncoming.set(id, []); columnOutgoing.set(id, []);
    });
  }
  if (profile) { profile.ms.graph_construction = performance.now() - profileMark; profileMark = performance.now(); }
  data.edges.forEach((edge, index) => {
    const endpoint = (ref, side) => ({id: datasets.get(datasetId(ref)).cell.id, port: JSON.stringify([ref.column, side])});
    const link = new joint.shapes.standard.Link({z: 1, source: endpoint(edge.source, 'out'), target: endpoint(edge.target, 'in'), attrs: {
      line: {stroke: '#b8c3d1', strokeWidth: 1, targetMarker: {type: 'path', d: 'M 4 -2 0 0 4 2 z', fill: '#b8c3d1', stroke: '#b8c3d1'}},
    }});
    link.connector(continuousLink); graph.addCell(link);
    const record = {id: index, source: columnId(edge.source), target: columnId(edge.target), link};
    columnLinks.push(record); columnIncoming.get(record.target).push(record); columnOutgoing.get(record.source).push(record);
  });
  if (profile) { profile.ms.link_creation = performance.now() - profileMark; profileMark = performance.now(); }
  paper.on('blank:pointerdblclick', fitGraph);
  fitGraph();
  if (profile) profile.ms.fit = performance.now() - profileMark;
}
async function start() {
  const message = document.getElementById('message');
  try {
    if (!window.joint) throw new Error('JointJS requires internet access to its CDN');
    const response = await fetch('/api/project');
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not load project');
    DATA = body;
    prepareLineage(); renderProjectGraph(); renderPanel();
    message.textContent = nodes.size ? '' : 'This project has no Models or Seeders.';
    loadRuns();
  } catch (error) {
    message.textContent = `Could not render lineage: ${error.message}`;
    message.setAttribute('role', 'alert');
  }
}
start();
