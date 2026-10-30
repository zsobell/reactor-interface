import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const html = await readFile(new URL('../../reactor/server/static/index.html', import.meta.url), 'utf8');
const elements = new Map();
let downloadClicks = 0;
function element(tag, attrs = '') {
  return {
    tagName: tag.toUpperCase(), value: /\bvalue="([^"]*)"/.exec(attrs)?.[1] || '',
    type: /\btype="([^"]*)"/.exec(attrs)?.[1] || '', checked: /\bchecked\b/.test(attrs),
    textContent: '', innerHTML: '', children: [], dataset: {}, events: {},
    clientWidth: 0, clientHeight: 0, classList: {toggle() {}, add() {}, remove() {}},
    style: {setProperty() {}},
    addEventListener(name, fn) {this.events[name] = fn;},
    removeEventListener(name) {delete this.events[name];},
    querySelectorAll() {return [];}, querySelector() {return null;},
    appendChild(child) {this.children.push(child);}, replaceChildren(...children) {this.children=children;},
    click() {if (tag === 'a' && this.download) downloadClicks++;}, remove() {},
    setAttribute() {}, getBoundingClientRect() {return {left: 0};},
  };
}
for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  elements.set(match[3], element(match[1], match[2]));
}
const documentEvents = new Map();
globalThis.document = {
  getElementById: id => elements.get(id) || null,
  querySelectorAll: () => [],
  addEventListener(name, fn) {documentEvents.set(name,[...(documentEvents.get(name)||[]),fn]);},
  removeEventListener() {},
  createElement: tag => element(tag), documentElement: element('html'), body: {appendChild() {}},
};
const windowEvents = {};
const opened = [];
globalThis.window = {
  addEventListener(name, fn) {windowEvents[name] = fn;}, devicePixelRatio: 1,
  open(url,name){opened.push({url,name});return {focus(){}};},
};
globalThis.location = {protocol: 'https:', host: 'reactor.test'};
let lifecycleConfirm = '';
globalThis.confirm = message => {
  if (message.startsWith('Restart the reactor server?')) {
    lifecycleConfirm = message;
    return false;
  }
  return true;
};
const stored = new Map();
globalThis.localStorage = {getItem: k => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v)};
const restartStored = new Map([['reactor.restart.success', '2.0.2']]);
globalThis.sessionStorage = {getItem: k => restartStored.get(k) ?? null,
  setItem: (k, v) => restartStored.set(k, v), removeItem: k => restartStored.delete(k)};
const requested = [];
const prestartRecipe = {schema_version:1, id:'current-prestart',
  name:'Current pre-start', description:'', revision:1, builtin:true,
  parameters:[], start_steps:[], abort_steps:[]};
const hcpesPlan = {schema_version:1, id:'current-hcpes-plan', name:'Current HCPES template',
  description:'', revision:1, builtin:true, prestart_recipe_id:'current-hcpes-prestart',
  stage_polarity:1, settings:{establishment:{stable_window_s:20,maximum_wait_s:60,max_drift_a_per_min:.0001},
    parameter_change:{stable_window_s:3,maximum_wait_s:10,max_drift_a_per_min:.0003},
    parameter_settle_s:3, condition_settle_mode:'time',
    plasma_min_current_a:.0001, recovery_window_s:30, reignite_pulse_s:1,
    reignite_settle_s:1, qualified_samples:5}, axes:[]};
globalThis.fetch = async url => {
  requested.push(url);
  if(url === '/api/prestart/capabilities')
    return {ok:true, json:async () => ({schema_version:1, targets:[]})};
  if(url === '/api/prestart/recipes')
    return {ok:true, json:async () => ({schema_version:1,
      selected_id:prestartRecipe.id, recipes:[prestartRecipe]})};
  if(url === '/api/errors/clear')
    return {ok:true, json:async () => ({ok:true, cleared_at:1700000050})};
  if(url === '/api/hcpes/capabilities')
    return {ok:true, json:async () => ({schema_version:1, axes:[]})};
  if(url === '/api/hcpes/plans')
    return {ok:true, json:async () => ({schema_version:1, selected_id:hcpesPlan.id,
      plans:[hcpesPlan], campaigns:[]})};
  if(url === '/api/hcpes/preview')
    return {ok:true, json:async () => ({plan:hcpesPlan, axes:[], nesting:[],
      estimate:{points:1,parameter_changes:0,qualified_samples:5,
        best_case_s:20,startup_timeout_case_s:60}, cleanup:{}})};
  if(url === '/api/prestart/preview')
    return {ok:true, json:async () => ({name:'Current pre-start', revision:1,
      start_steps:[], abort_steps:[]})};
  return {ok: true, json: async () => ({params: {}, samples: [],
    events: url === '/api/events' ? [{t: 1700000000, kind: 'startup', message: 'seeded startup event'}] : [],
    total_s: 2505})};
};
const sockets = [];
globalThis.WebSocket = class {
  constructor(url) {this.url = url; sockets.push(this);}
  close() {this.closed = true;}
};
const control = await import('../../reactor/server/static/control.js');
await new Promise(resolve => setTimeout(resolve, 20));
assert.equal(sockets[0].url, 'wss://reactor.test/ws', 'bootstrap reaches secure telemetry connection');
assert.ok(requested.includes('/api/run_params'), 'server settings loaded');
assert.ok(requested.includes('/api/events'), 'event history loaded');
assert.match(elements.get('events').innerHTML, /seeded startup event/,
  'seeded event history renders before a new telemetry event arrives');
assert.match(elements.get('smoothHint').textContent, /off/, 'page reads extracted chart settings during bootstrap');
// A connected socket alone did not catch the telemetry crash: exercise the
// actual page render path before treating bootstrap as healthy.
const frame = {
  site:'Test reactor', t:1700000001, daq:{configured:true, inputs:10},
  snapshot:{pressure:2e-8, 'pressure.volts':2.5,
    'stage.temp':23, 'inst.ammeter':1e-6,
    'gauge.prec1_dose':0.02, 'aux.bubbler':30},
  readings:{}, stage_temp:{enabled:true,value:23},
  regulator:{running:false}, gauges:[], aux:[],
  instruments:[], power_supplies:[], mfcs:[], valves:[],
  recipe:{state:'idle', cycle:0, cycles_total:0}, prestart:{},
  logging:{active:false, run_export:{active:false}, ellipsometer:{active:false},
    hcpes:{active:false}, errors:{}},
  ellipsometer:null, events:[], errors:[], hcpes:{},
  aperture_lifetime:{},
};
let plotDraws = 0;
const runCanvas = elements.get('chart');
runCanvas.clientWidth = 500; runCanvas.clientHeight = 280;
const drawing = new Proxy({}, {get:(_target, key) => key === 'measureText'
  ? () => ({width:0}) : () => {}});
runCanvas.getContext = () => {plotDraws++; return drawing;};
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.equal(elements.get('siteName').textContent, 'Test reactor');
assert.equal(elements.get('pressure').textContent, '2.00e-8',
  'live telemetry updates the Run readout');
assert.equal(elements.get('hwPressure').textContent, '2.00e-8',
  'the same telemetry frame updates Hardware');
assert.ok(plotDraws > 0, 'the telemetry frame reaches the Run plot drawing path');
await elements.get('aldStart').onclick();
frame.recipe = {state:'running', recipe:'EE-ALD', started_at:1700000001,
  cycle:1, cycles_total:1};
sockets[0].onmessage({data:JSON.stringify(frame)});
await elements.get('restartBtn').onclick();
assert.match(lifecycleConfirm, /A RUN IS ACTIVE AND WILL BE ABORTED/,
  'server restart warning still recognizes an active run');
frame.recipe.state = 'done';
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.equal(downloadClicks, 0,
  'finishing a run does not prompt for a duplicate browser CSV');
frame.recipe = {state:'idle', cycle:0, cycles_total:0};
frame.prestart = {running:true, state:'running', phase:'striking plasma'};
frame.power_supplies = [{id:'hv', driver:'glassman_fl', label:'HV supply',
  hv_on:false, connected:true}];
assert.match(control.liveAlerts(frame)[0].msg, /HV supply is off/,
  'HV off during pre-start appears in the header');
frame.power_supplies[0].hv_on = true;
assert.equal(control.liveAlerts(frame).length, 0,
  'turning HV on clears the live warning');
frame.prestart = {running:false, state:'error', phase:'error: sensitive detail'};
frame.power_supplies = [];
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.equal(elements.get('preStatus').textContent, 'error',
  'pre-start summary does not show the error detail');
frame.prestart = {};
frame.errors = [{t:1700000002, kind:'error', message:'old failure'}];
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.match(elements.get('errors').innerHTML, /old failure/);
await elements.get('clearErrorsBtn').onclick();
assert.doesNotMatch(elements.get('errors').innerHTML, /old failure/,
  'clear button empties the displayed error log');
assert.ok(requested.includes('/api/errors/clear'), 'clear button calls the server');
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.doesNotMatch(elements.get('errors').innerHTML, /old failure/,
  'older telemetry tails cannot restore cleared errors');
frame.error_log_cleared_at = 1700000050;
frame.errors = [{t:1700000051, kind:'error', message:'new failure'}];
sockets[0].onmessage({data:JSON.stringify(frame)});
assert.match(elements.get('errors').innerHTML, /new failure/,
  'new errors still appear after clearing');
const sensor = {dataset:{plotKey:'stage.temp',plotLabel:'Stage temperature'},
  closest(selector){return selector === '[data-pane="hardware"]' ? {} :
    selector === '[data-plot-key]' ? this : null;}};
for(const listener of documentEvents.get('click')||[])
  listener({target:sensor});
assert.match(opened[0].url,/sensor-plot\.html\?key=stage\.temp&label=Stage\+temperature/,
  'clicking a Hardware temperature value opens its own sensor popup');
let prevented = false;
for(const listener of documentEvents.get('keydown')||[])
  listener({target:sensor,key:'Enter',preventDefault(){prevented=true;}});
assert.equal(prevented,true,'keyboard activation works on a sensor value');
assert.equal(opened.length,2,'keyboard activation opens the same plot');
assert.equal(restartStored.has('reactor.restart.success'), false,
  'restart-success notice is consumed once after the replacement page boots');
const oldWorkerError = {logging:{active:true,run_export:{active:false},ellipsometer:{active:false},
  hcpes:{active:false},errors:{worker:'FileExistsError: old session'}},recipe:{},
  power_supplies:[],mfcs:[],instruments:[],daq:{configured:true}};
assert.equal(control.liveAlerts(oldWorkerError).length,0,
  'latched HCPES recording error stays in history while another stream runs');
oldWorkerError.logging.hcpes.active=true;
assert.match(control.liveAlerts(oldWorkerError)[0].msg,/old session/,
  'active HCPES recording errors remain visible');
oldWorkerError.logging.hcpes.active=false;
assert.equal(control.liveAlerts(oldWorkerError).length,0,
  'HCPES error chip clears as soon as its recording owner closes');
elements.get('preSection').open = false;
await elements.get('preStartBtn').onclick();
assert.ok(requested.includes('/api/prestart/start'), 'pre-start launch is sent');
assert.equal(elements.get('preSection').open, false,
  'starting pre-start leaves the recipe section collapsed');
const formHandler = elements.get('aldParams').events.input;
windowEvents.pagehide({persisted:true});
assert.equal(sockets[0].closed, true, 'cached navigation suspends telemetry');
windowEvents.pageshow({persisted:true});
assert.equal(sockets.length, 2, 'back-forward cache restore reconnects telemetry');
assert.equal(elements.get('aldParams').events.input, formHandler,
  'cached navigation preserves the mounted form handlers');
windowEvents.pagehide({persisted:false});
windowEvents.pageshow({persisted:true});
assert.equal(sockets.length, 2, 'permanently disposed page cannot reconnect');
console.log('PASS control bootstrap, settings, chart integration and cached-page reconnect');
