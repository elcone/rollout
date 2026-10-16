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
function resetSelection() { selectedId = null; highlight(); renderPanel(); }

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
const refId = ref => ref.type === 'SEEDER' ? JSON.stringify(['SEEDER', ref.id]) : modelId(ref);
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
      ['Schemas', new Set(DATA.models.map(n => n.schema)).size],
      ['Dependencies', edges.length],
    ];
    const list = element('dl', null, panel, 'space-y-4');
    for (const [label, value] of counts) {
      const row = element('div', null, list, 'flex justify-between');
      element('dt', label, row); element('dd', value, row, 'font-semibold text-slate-900');
    }
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
  if (node.type === 'seeder') {
    connections(`Outputs (${node.outputs.length})`, node.outputs.map(modelId));
  } else {
    if (node.origin !== 'source') connections('Upstream', incoming.get(selectedId).map(e => e.source));
    connections('Downstream', outgoing.get(selectedId).map(e => e.target));
  }
}
function selectNode(id) {
  selectedId = selectedId === id ? null : id;
  highlight(selectedId); renderPanel();
}
function defineCard() {
  return joint.dia.Element.define('lineage.Card', {}, {markup: [
    {tagName: 'rect', selector: 'body'}, {tagName: 'rect', selector: 'iconBackground'},
    {tagName: 'path', selector: 'icon'}, {tagName: 'text', selector: 'title'},
  ]});
}
function createNode(id, node, Card) {
  const measure = document.createElement('canvas').getContext('2d');
  measure.font = `600 14px ${MONOSPACE}`;
  const width = Math.ceil(Math.max(220, measure.measureText(node.name).width + 92));
  const cell = new Card({z: 2, size: {width, height: 72}, attrs: {
    body: {width: 'calc(w)', height: 'calc(h)', rx: 10, ry: 10, fill: '#fff', stroke: '#b3bfce', strokeWidth: 1},
    iconBackground: {x: 16, y: 16, width: 40, height: 40, rx: 8, fill: '#dbeafe'},
    icon: {d: ICONS[iconKey(node)], transform: 'translate(24 24)', fill: 'none', stroke: '#2563eb', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round'},
    title: {text: node.name, x: 72, y: 40, fontSize: 14, fontWeight: 600, fontFamily: MONOSPACE, fill: '#0f172a'},
  }});
  cells.set(id, cell);
}
function layoutGraph() {
  const schemas = [...new Set([...nodes.values()].filter(n => n.type === 'model').map(n => n.schema))].sort();
  const schemaPairs = new Map();
  for (const edge of edges) {
    const source = nodes.get(edge.source), target = nodes.get(edge.target);
    if (source.type === 'model' && target.type === 'model' && source.schema !== target.schema) {
      schemaPairs.set(JSON.stringify([source.schema, target.schema]), [source.schema, target.schema]);
    }
  }
  // Schemas occupy dependency columns; each column's containers form vertical bands.
  const schemaRanks = ranks(schemas, [...schemaPairs.values()], true);
  const groups = schemas.map(schema => {
    const ids = [...nodes].filter(([,node]) => node.type === 'model' && node.schema === schema).map(([id]) => id);
    const localEdges = edges.filter(e => ids.includes(e.source) && ids.includes(e.target)).map(e => [e.source, e.target]);
    const localRanks = ranks(ids, localEdges);
    const columns = Array.from({length: Math.max(...localRanks.values()) + 1}, () => []);
    for (const id of ids) columns[localRanks.get(id)].push(id);
    const widths = columns.map(column => Math.max(...column.map(id => cells.get(id).size().width)));
    const heights = columns.map(column => column.reduce((height, id) => height + cells.get(id).size().height + 26, 0) - 26);
    return {
      schema, ids, columns, widths,
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
        label: {text: group.schema, x: 32, y: 22, textAnchor: 'start', textVerticalAnchor: 'middle', fontFamily: MONOSPACE, fontSize: 11, fontWeight: 400, fill: '#526176'},
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
function renderGraph() {
  const Card = defineCard();
  const namespace = {...joint.shapes, lineage: {Card, Schema: joint.shapes.standard.Rectangle}};
  graph = new joint.dia.Graph({}, {cellNamespace: namespace});
  paper = new joint.dia.Paper({el: viewport, model: graph, cellViewNamespace: namespace,
    width: viewport.clientWidth, height: viewport.clientHeight, interactive: false, async: false,
    background: {color: 'transparent'},
  });
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
    view.el.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectNode(id); }
    });
  }
  for (const cell of graph.getElements()) if (cell.get('type') === 'lineage.Schema') paper.findViewByModel(cell).el.classList.add('schema-container');
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
  const pan = !event.target.closest('.lineage-node');
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
viewport.addEventListener('click',event => {
  if (suppressClick && event.detail>0) { event.preventDefault(); event.stopImmediatePropagation(); return; }
  const target=event.target.closest('.lineage-node');
  if (target) selectNode(target.getAttribute('data-logical-id'));
  else resetSelection();
}, true);
viewport.addEventListener('dragstart',event => event.preventDefault());
new ResizeObserver(() => { if (paper) { paper.setDimensions(viewport.clientWidth,viewport.clientHeight); applyTransform(); } }).observe(viewport);
async function start() {
  const message = document.getElementById('message');
  try {
    if (!window.joint) throw new Error('JointJS requires internet access to its CDN');
    const response = await fetch('/api/project');
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not load project');
    DATA = body;
    prepareLineage(); renderGraph(); renderPanel();
    message.textContent = nodes.size ? '' : 'This project has no Models or Seeders.';
  } catch (error) {
    message.textContent = `Could not render lineage: ${error.message}`;
    message.setAttribute('role', 'alert');
  }
}
start();
