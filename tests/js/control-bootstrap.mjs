import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const html = await readFile(new URL('../../reactor/server/static/index.html', import.meta.url), 'utf8');
const elements = new Map();
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
    appendChild() {}, setAttribute() {}, getBoundingClientRect() {return {left: 0};},
  };
}
for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  elements.set(match[3], element(match[1], match[2]));
}
globalThis.document = {
  getElementById: id => elements.get(id) || null,
  querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  createElement: tag => element(tag), documentElement: element('html'), body: {appendChild() {}},
};
const windowEvents = {};
globalThis.window = {addEventListener(name, fn) {windowEvents[name] = fn;}, devicePixelRatio: 1};
globalThis.location = {protocol: 'https:', host: 'reactor.test'};
const stored = new Map();
globalThis.localStorage = {getItem: k => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v)};
const restartStored = new Map([['reactor.restart.success', '2.0.2']]);
globalThis.sessionStorage = {getItem: k => restartStored.get(k) ?? null,
  setItem: (k, v) => restartStored.set(k, v), removeItem: k => restartStored.delete(k)};
const requested = [];
const hcpesPlan = {schema_version:1, id:'current-hcpes-plan', name:'Current HCPES template',
  description:'', revision:1, builtin:true, prestart_recipe_id:'current-hcpes-prestart',
  stage_polarity:1, settings:{establishment:{stable_window_s:20,maximum_wait_s:60,max_drift_a_per_min:.0001},
    parameter_change:{stable_window_s:3,maximum_wait_s:10,max_drift_a_per_min:.0003},
    parameter_settle_s:3, condition_settle_mode:'time',
    plasma_min_current_a:.0001, recovery_window_s:30, reignite_pulse_s:1,
    reignite_settle_s:1, qualified_samples:5}, axes:[]};
globalThis.fetch = async url => {
  requested.push(url);
  if(url === '/api/hcpes/capabilities')
    return {ok:true, json:async () => ({schema_version:1, axes:[]})};
  if(url === '/api/hcpes/plans')
    return {ok:true, json:async () => ({schema_version:1, selected_id:hcpesPlan.id,
      plans:[hcpesPlan], campaigns:[]})};
  if(url === '/api/hcpes/preview')
    return {ok:true, json:async () => ({plan:hcpesPlan, axes:[], nesting:[],
      estimate:{points:1,parameter_changes:0,qualified_samples:5,
        best_case_s:20,startup_timeout_case_s:60}, cleanup:{}})};
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
