import assert from 'node:assert/strict';
import {createControlTransport} from '../../reactor/server/static/control-transport.js';
import {createRunForms} from '../../reactor/server/static/control-run-forms.js';
import {createDevicePanels} from '../../reactor/server/static/control-device-panels.js';

function fakeElement(value = '') {
  return {
    value, type: 'text', checked: false, textContent: '', className: '', disabled: false,
    dataset: {}, style: {}, events: {},
    classList: {toggle() {}},
    addEventListener(name, fn) { (this.events[name] ||= new Set()).add(fn); },
    removeEventListener(name, fn) { this.events[name]?.delete(fn); },
    remove() { this.removed = true; }, focus() { this.focused = true; },
  };
}

// Transport owns exactly one reconnect timer and cancels it during disposal.
{
  const elements = new Map([['linkBadge', fakeElement()], ['shutdownBtn', fakeElement()],
    ['shutdownHint', fakeElement()]]);
  const timers = new Map(); let nextTimer = 1; const sockets = [];
  class Socket {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; }
  }
  const document = {querySelectorAll: () => [], createElement: () => fakeElement(),
    body: {appendChild() {}}};
  const transport = createControlTransport({$: id => elements.get(id), document,
    fetchImpl: async () => ({ok: true, json: async () => ({})}), WebSocketCtor: Socket,
    location: {protocol: 'https:', host: 'reactor.test'}, render() {},
    setTimeoutImpl(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearTimeoutImpl(id) { timers.delete(id); }});
  const first = transport.connect();
  assert.equal(first.url, 'wss://reactor.test/ws');
  assert.equal(transport.connect(), first, 'connect does not accumulate sockets');
  assert.equal(sockets.length, 1);
  transport.suspend();
  assert.equal(first.closed, true, 'cached-page suspension closes its live socket');
  assert.equal(timers.size, 0, 'suspension cannot schedule a reconnect');
  transport.resume();
  assert.equal(sockets.length, 2, 'restoring a cached page opens one new socket');
  const restored = sockets[1];
  restored.onclose();
  assert.equal(timers.size, 1, 'restored socket retains normal reconnect behavior');
  const [restoreTimerId, restoreReconnect] = [...timers.entries()][0];
  timers.delete(restoreTimerId); restoreReconnect();
  assert.equal(sockets.length, 3);
  sockets[2].onclose();
  transport.dispose();
  assert.equal(timers.size, 0, 'dispose cancels pending reconnect');
  assert.equal(transport.connect(), null, 'disposed transport cannot reconnect');
}

// Forms apply server state over the local cache, persist mode and remove handlers.
{
  const ids = ['modeSel','runTitle','gasWindowWord','smoothHint','reigniteHint','gasSchedHint',
    'aldParams','gasSchedSection','advSection','preSection','smoothSection',
    'p_smooth_on','p_smooth_n','p_cycles','p_dose_pressure_torr','p_dose_s','p_pump_a_s',
    'p_beam_s','p_pump_b_s','p_min_current_ua','p_sample_bias_v','p_sample_bias_polarity',
    'p_fill_pulse_on_s','p_fill_pulse_off_s','p_tolerance_pct','p_reignite_pulse_s',
    'p_reignite_settle_s','p_ar_close_delay_s','p_pre_ar_sccm','p_pre_valve_delay_s',
    'p_pre_hold_s','p_gas_overlap_s','p_mfc1_gas_enable','p_mfc1_gas_order','p_mfc1_gas_pct',
    'p_mfc1_gas_flow_sccm','p_mfc2_gas_enable','p_mfc2_gas_order','p_mfc2_gas_pct',
    'p_mfc2_gas_flow_sccm','p_run_name'];
  const elements = new Map(ids.map(id => [id, fakeElement('0')]));
  for(const gas of ['mfc1','mfc2']) elements.get(`p_${gas}_gas_enable`).type = 'checkbox';
  elements.get('p_smooth_on').type = 'checkbox';
  elements.get('modeSel').value = 'ald';
  elements.get('p_mfc1_gas_order').value = 'first';
  elements.get('p_mfc2_gas_order').value = 'second';
  const modeAld = {...fakeElement(), dataset: {mode:'ald'}};
  const modeCvd = {...fakeElement(), dataset: {mode:'cvd'}};
  const stored = new Map([['aldParams', JSON.stringify({cycles:'7'})]]);
  const posts = []; let pendingSave = null;
  const forms = createRunForms({$: id => elements.get(id),
    document: {querySelectorAll: selector => selector === '[data-mode]' ? [modeAld, modeCvd] : []},
    storage: {getItem:key => stored.get(key) ?? null, setItem:(key,value) => stored.set(key,value)},
    fetchImpl: async (url, options) => {
      if(options) { posts.push([url, JSON.parse(options.body)]); return {ok:true}; }
      return {ok:true, json:async () => ({params:{cycles:'12', dose_s:'0.25'}})};
    }, cls:(el,name,on) => { el.hidden = name === 'hidden' && on; },
    smoothing:() => ({on:false,n:5}), drawChart() {},
    setTimeoutImpl:fn => { pendingSave = fn; return 1; }, clearTimeoutImpl() { pendingSave = null; }});
  forms.mount(); forms.mount();
  assert.equal(elements.get('aldParams').events.input.size, 1, 'mount is idempotent');
  await forms.loadParams();
  assert.equal(elements.get('p_cycles').value, '12', 'server parameters override local cache');
  elements.get('modeSel').value = 'cvd';
  [...elements.get('modeSel').events.change][0]();
  assert.equal(stored.get('runMode'), 'cvd');
  assert.equal(elements.get('runTitle').textContent, 'EE-CVD run');
  assert.equal(modeAld.hidden, true); assert.equal(modeCvd.hidden, false);
  forms.saveParams(); await pendingSave();
  assert.equal(posts[0][0], '/api/run_params');
  assert.equal(posts[0][1].cycles, '12', 'saved payload contains the effective parameter values');
  forms.dispose();
  assert.equal(elements.get('aldParams').events.input.size, 0, 'dispose removes parameter handlers');
}

// Delegated panel commands preserve representatives request paths, payloads and supply ordering.
{
  const handlers = {};
  const document = {
    addEventListener(name, fn) { (handlers[name] ||= new Set()).add(fn); },
    removeEventListener(name, fn) { handlers[name]?.delete(fn); },
    querySelector: selector => selectors.get(selector) || null,
  };
  const selectors = new Map([['#mfc_ar', fakeElement('4.5')],
    ['#psuv_bias', fakeElement('120')], ['#psui_bias', fakeElement('0.2')]]);
  const calls = [], toasts = [];
  const panels = createDevicePanels({$:() => fakeElement(), document,
    cssEscape:String, esc:String, num:String, sci:String, put() {}, cls() {}, reconcile() {},
    post:async (url, body) => { calls.push([url, body]); return {setpoint_sccm:body.sccm}; },
    toast:(...args) => toasts.push(args), confirmImpl:() => true, promptImpl:() => null});
  panels.mount(); panels.mount();
  assert.equal(handlers.click.size, 2, 'mount installs one command and one rename handler');
  const click = [...handlers.click][0];
  const target = button => ({closest: selector => selector === 'button[data-act]' ? button : null});
  await click({target:target({dataset:{act:'valve',id:'dose',state:'1'}})});
  await click({target:target({dataset:{act:'mfc',id:'ar'}})});
  await click({target:target({dataset:{act:'psuset',id:'bias'}})});
  assert.deepEqual(calls, [
    ['/api/valve/dose', {state:true}],
    ['/api/mfc/ar/setpoint', {sccm:4.5}],
    ['/api/supply/bias/voltage', {volts:120}],
    ['/api/supply/bias/current', {amps:0.2}],
  ]);
  panels.dispose();
  assert.equal(handlers.click.size, 0, 'dispose removes delegated click handlers');
  assert.equal(handlers.keydown.size, 0, 'dispose removes keyboard handler');
}

// MFC telemetry keeps the requested isolation interlock visible in its live tile.
{
  const fields = new Map(['name','gas','flow','sp','fs','temp','mode','health','err','iso','bar','input']
    .map(name => [name, fakeElement()]));
  const setFlow = fakeElement();
  const tile = {classList:{toggle() {}}, querySelector(selector) {
    const field = /data-f="([^"]+)"/.exec(selector)?.[1];
    return field ? fields.get(field) : selector === '[data-act="mfc"]' ? setFlow : null;
  }};
  const container = {querySelector:() => tile};
  const panels = createDevicePanels({$:id => id === 'mfcs' ? container : fakeElement(),
    document:{addEventListener() {}, removeEventListener() {}, querySelector() {return null;}},
    cssEscape:String, esc:String, num:(value,digits=3) => Number(value).toFixed(digits),
    sci:String, put:(root,field,value) => { const el=root.querySelector(`[data-f="${field}"]`); if(el) el.textContent=value; return el; },
    cls() {}, reconcile() {}, post:async () => ({}), toast() {}, confirmImpl:() => true,
    promptImpl:() => null});
  panels.mfcs({mfcs:[{id:'ar', label:'Argon', connected:true, isolation_valve:'ar_valve',
    full_scale_sccm:10, health:{}, device_mode:'flow'}],
    valves:[{id:'ar_valve', label:'Ar isolation', open:false}],
    snapshot:{'mfc.ar.flow':2, 'mfc.ar.flow_pct':20, 'mfc.ar.setpoint':2, 'mfc.ar.temp':24}});
  assert.equal(setFlow.disabled, true, 'closed isolation valve disables the set-flow command');
  assert.equal(fields.get('input').disabled, true, 'closed isolation valve disables its input');
  assert.match(fields.get('iso').textContent, /Ar isolation is closed/);
  assert.equal(fields.get('flow').textContent, '2.000', 'live flow updates without rebuilding the tile');
}

console.log('PASS control transport lifecycle, run forms and device commands');
