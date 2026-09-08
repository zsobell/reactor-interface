import assert from 'node:assert/strict';
import {createLiveCharts} from '../../reactor/server/static/live-charts.js';

const windowEvents = {};
globalThis.window = {devicePixelRatio: 1, addEventListener: (n, fn) => windowEvents[n] = fn};
const elements = new Map();
let painted = 0;
const context = new Proxy({}, {get: (_, key) => key === 'measureText'
  ? text => ({width: text.length * 6}) : () => {painted++;}, set: () => true});
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    events: {}, value: '3', checked: false, innerHTML: '', clientWidth: 640, clientHeight: 320,
    addEventListener(n, fn) {this.events[n] = fn;}, getContext: () => context,
    getBoundingClientRect: () => ({left: 0}),
  });
  return elements.get(id);
}
const trend = [{t: 100, pressure: 1e-6, current: 1e-3, stage_temp: 20, bubbler_temp: 30,
                mfc_ar: 4}, {t: 101, pressure: null, current: 2e-3, mfc_ar: 5}];
const charts = createLiveCharts({$, trend, num: String, clock: String, sci: String,
  fmtCurrent: v => ({text: String(v), unit: 'A'}), currentUnit: () => ({mul: 1, unit: 'A'}),
  esc: String, setHtml: (el, html) => el.innerHTML = html});
charts.update({marks: [{t: 100, id: 'plasma', state: false, reason: 'reignite'}],
  run_valves: {plasma: 'plasma'}, mfcs: [{id: 'ar', label: 'Argon'}]});
charts.drawAllCharts();
assert.deepEqual(charts.smoothing(), {on: false, n: 3}, "page can read chart smoothing settings");
assert.ok(painted > 0, 'all three charts draw with the extracted dependencies');
assert.match($('mfcLegend').innerHTML, /Argon/, 'telemetry updates chart labels');
const canvas = $('chart');
canvas.events.mouseenter();
canvas.events.mousedown({clientX: 100, preventDefault() {}});
canvas.events.mousemove({clientX: 300});
windowEvents.mouseup();
assert.match($('legend').innerHTML, /paused/, 'dragging freezes the selected chart');
assert.doesNotMatch($('mfcLegend').innerHTML, /paused/, 'other chart remains live');
$('followBtn').onclick();
assert.doesNotMatch($('legend').innerHTML, /paused/, 'follow resumes live rendering');
windowEvents.keydown({key: 'ArrowLeft', preventDefault() {}});
assert.match($('legend').innerHTML, /paused/, 'keyboard panning works after extraction');
console.log('PASS live chart rendering, telemetry labels, independent zoom, follow and keyboard');
