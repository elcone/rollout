// Run with: node --test tests/viewer_execution.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.attributes = {}; this._text = '';
    this.disabled = true; this.value = '';
    this.classList = {toggle() {}};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join('\n'); }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; this._text = ''; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
}

function viewer() {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
      },
      createElement: tag => new Element(tag),
      createElementNS: (_, tag) => new Element(tag),
    },
    window: {addEventListener() {}}, ResizeObserver: class {observe() {}},
    setTimeout, clearTimeout, URLSearchParams,
    fetch: async () => { throw new Error('Unexpected fetch'); },
  });
  const source = fs.readFileSync(path.join(__dirname, '../tools/viewer/static/app.js'), 'utf8');
  vm.runInContext(source.replace(/start\(\);\s*$/, ''), context);
  const evaluate = code => vm.runInContext(code, context);
  evaluate(`
    DATA = {models: [
      {schema: 'public', name: 'report', origin: 'internal'},
      {schema: 'public', name: 'new_model', origin: 'internal'},
      {schema: 'public', name: 'report', catalog: 'other', origin: 'source'},
    ], seeders: [{id: 'seeders/source.py', name: 'source.py', outputs: []}], lineage: []};
    prepareLineage();
    paper = {findViewByModel: cell => ({el: cell.el})};
    for (const id of nodes.keys()) cells.set(id, {el: document.createElement('g'), attrs: {}, attr(update) {Object.assign(this.attrs, update);}});
  `);
  return {context, evaluate, elements};
}

function run(status = 'FAILED', id = 'first') {
  return {
    run_id: id, started_at: '2026-11-04T12:00:00+00:00', finished_at: '2026-11-04T12:00:01+00:00',
    duration: 1.25, status,
    models: [{schema: 'public', name: 'report', status, started_at: null, finished_at: null,
      duration: status === 'SKIPPED' ? null : 0, materialization_duration: null,
      error: status === 'FAILED' ? '<script>bad()</script>\nFailure details' : null},
    {schema: 'old', name: 'removed', status: 'SUCCESS', duration: 0.1}],
    seeders: [{name: 'seeders/source.py', status: 'FAILED', duration: 0.5, execution_duration: 0.2, error: 'Import failed'}],
    quality: [{model: {schema: 'public', name: 'report'}, checks: [{name: 'row_count', status: 'FAIL',
      severity: 'ERROR', observed: 0, expected: '>= 1', duration: 0.001}]}],
  };
}

function activate(v, document = run()) {
  v.context.fixture = document;
  v.evaluate('indexRun(fixture); applyRunOverlay(); renderRunSummary(); renderPanel();');
}

test('history loads summaries and shows an explicit empty state', async () => {
  const v = viewer();
  v.context.fetch = async () => ({ok: true, json: async () => []});
  await v.evaluate('loadRuns()');
  assert.equal(v.elements.get('run-selector').disabled, true);
  assert.equal(v.elements.get('run-message').textContent, 'No recorded executions.');
  v.context.fetch = async () => ({ok: true, json: async () => [run()]});
  await v.evaluate('loadRuns()');
  assert.equal(v.elements.get('run-selector').disabled, false);
  assert.match(v.elements.get('run-selector').textContent, /FAILED.*1.250 s/);
  assert.equal(v.elements.get('run-selector').children[0].value, 'first');
});

test('history corruption remains visible and does not disable project navigation', async () => {
  const v = viewer();
  v.context.fetch = async () => ({ok: false, json: async () => ({detail: 'Invalid run history at line 2'})});
  await v.evaluate('loadRuns()');
  assert.match(v.elements.get('run-message').textContent, /line 2/);
  assert.equal(v.elements.get('run-message').attributes.role, 'alert');
  v.evaluate('renderPanel()');
  assert.match(v.elements.get('panel-content').textContent, /Project summary/);
});

for (const status of ['SUCCESS', 'FAILED', 'SKIPPED']) {
  test(`${status} is distinct from NO RESULT, preserves catalog identity and leaves DAG intact`, () => {
    const v = viewer();
    activate(v, run(status));
    assert.equal(v.evaluate(`cells.get(modelId({schema:'public',name:'report'})).el.attributes['data-execution-status']`), status);
    for (const ref of [{schema: 'public', name: 'new_model'}, {schema: 'public', name: 'report', catalog: 'other'}]) {
      v.context.ref = ref;
      assert.equal(v.evaluate(`cells.get(modelId(ref)).el.attributes['data-execution-status']`), 'NO RESULT');
    }
    assert.equal(v.evaluate('nodes.size'), 4);
    assert.equal(v.evaluate('edges.length'), 0);
    assert.equal(v.evaluate(`runResults.get(refId({type:'SEEDER',id:'seeders/source.py'})).status`), 'FAILED');
    assert.match(v.elements.get('graph-context').textContent, /Current project DAG.*historical/);
  });
}

test('node details preserve null/zero timings, errors as text and quality observations', () => {
  const v = viewer();
  activate(v);
  v.evaluate(`selectedId = modelId({schema:'public',name:'report'}); renderPanel();`);
  const panel = v.elements.get('panel-content');
  assert.match(panel.textContent, /FAILED/);
  assert.match(panel.textContent, /Not recorded/);
  assert.match(panel.textContent, /0.000 s/);
  assert.match(panel.textContent, /<script>bad\(\)<\/script>\nFailure details/);
  assert.match(panel.textContent, /row_count · FAIL · ERROR/);
  assert.match(panel.textContent, /Observed: 0 · Expected: >= 1/);
  v.evaluate(`selectedId = modelId({schema:'public',name:'new_model'}); renderPanel();`);
  assert.match(panel.textContent, /NO RESULT/);
  assert.doesNotMatch(panel.textContent, /SKIPPED/);
});

test('unmatched historical results are inspectable without adding nodes', () => {
  const v = viewer();
  const document = run();
  document.seeders.push({name: 'seeders/removed.py', status: 'FAILED', duration: 0.3, error: 'Old import error'});
  activate(v, document);
  const text = v.elements.get('panel-content').textContent;
  assert.match(text, /without a current node \(2\)/);
  assert.match(text, /old.removed/);
  assert.match(text, /seeders\/removed.py/);
  assert.match(text, /Old import error/);
  assert.equal(v.evaluate('nodes.size'), 4);
});

test('switching executions clears old results, and removing overlay restores cards', async () => {
  const v = viewer();
  activate(v);
  v.context.fetch = async () => ({ok: true, json: async () => run('SUCCESS', 'second')});
  await v.evaluate(`selectRun('second')`);
  assert.equal(v.evaluate('activeRun.run_id'), 'second');
  assert.match(v.elements.get('run-summary').textContent, /SUCCESS/);
  await v.evaluate(`selectRun('')`);
  assert.equal(v.evaluate('activeRun'), null);
  assert.equal(v.evaluate('runResults.size'), 0);
  assert.equal(v.evaluate(`cells.get(modelId({schema:'public',name:'report'})).attrs.body.fill`), '#fff');
  assert.equal(v.elements.get('run-summary').textContent, '');
});

test('an out-of-order response cannot replace a newer execution or a cleared overlay', async () => {
  const v = viewer();
  let resolveFirst;
  v.context.fetch = () => new Promise(resolve => {resolveFirst = resolve;});
  const first = v.evaluate(`selectRun('first')`);
  assert.match(v.elements.get('run-message').textContent, /Loading execution/);
  v.context.fetch = async () => ({ok: true, json: async () => run('SUCCESS', 'second')});
  await v.evaluate(`selectRun('second')`);
  resolveFirst({ok: true, json: async () => run()});
  await first;
  assert.equal(v.evaluate('activeRun.run_id'), 'second');
  v.context.fetch = () => new Promise(resolve => {resolveFirst = resolve;});
  const pending = v.evaluate(`selectRun('first')`);
  await v.evaluate(`selectRun('')`);
  resolveFirst({ok: true, json: async () => run()});
  await pending;
  assert.equal(v.evaluate('activeRun'), null);
});

test('failed detail requests remove stale overlay and display the API error', async () => {
  const v = viewer(); activate(v);
  v.context.fetch = async () => ({ok: false, json: async () => ({detail: 'Run no longer exists'})});
  await v.evaluate(`selectRun('gone')`);
  assert.equal(v.evaluate('activeRun'), null);
  assert.match(v.elements.get('run-message').textContent, /Run no longer exists/);
});

test('execution can change during column lineage and survives return to project', async () => {
  const v = viewer(); activate(v);
  v.evaluate(`viewMode = 'cll'; selectedId = modelId({schema:'public',name:'report'}); renderRunSummary();`);
  v.context.fetch = async () => ({ok: true, json: async () => run('SUCCESS', 'second')});
  await v.evaluate(`selectRun('second')`);
  assert.equal(v.evaluate('viewMode'), 'cll');
  assert.match(v.elements.get('graph-context').textContent, /Current column lineage/);
  v.evaluate('renderProjectGraph = () => {applyRunOverlay(); renderRunSummary();}; highlight = () => {}; returnToProject();');
  assert.equal(v.evaluate('viewMode'), 'project');
  assert.equal(v.evaluate('activeRun.run_id'), 'second');
  assert.match(v.elements.get('panel-content').textContent, /Historical execution result[\s\S]*SUCCESS/);
});
