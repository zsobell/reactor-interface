import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {reduceSensorPoints} from '../../reactor/server/static/sensor-plot-data.js';

const points = Array.from({length:3000}, (_,i) => [i, 10]);
points[100][1]=1;
points[1400][1]=100;
points[2200][1]=null;
const reduced = reduceSensorPoints(points,0,2999,200);
assert.ok(reduced.length <= 601, 'display has bounded point count');
assert.ok(reduced.some(point => point[0] === 100 && point[1] === 1),
  'a low spike between screen pixels is retained');
assert.ok(reduced.some(point => point[0] === 1400 && point[1] === 100),
  'a high spike between screen pixels is retained');
assert.ok(reduced.some(point => point[0] === 2200 && point[1] === null),
  'an unavailable reading marks a gap');
assert.deepEqual(reduced.at(-1),[2999,10], 'newest reading is visible');
assert.deepEqual(reduceSensorPoints(points,90,102,200),points.slice(90,103),
  'a short zoom window retains every point for hover');

const page = await readFile(new URL('../../reactor/server/static/sensor-plot.html',import.meta.url),'utf8');
const control = await readFile(new URL('../../reactor/server/static/control.js',import.meta.url),'utf8');
const gaugeMarkup = control.split('function gauges(s){')[1].split('function aux(s){')[0];
const auxMarkup = control.split('function aux(s){')[1].split('function valveId(s){')[0];
assert.match(page,/id="sensorCanvas"/, 'popup contains its own canvas');
assert.match(page,/id="yLog"/, 'popup exposes a log axis control');
assert.match(gaugeMarkup,/data-plot-key="gauge\.\$\{esc\(g\.id\)\}"/,
  'configured pressure gauge values open the popup');
assert.doesNotMatch(auxMarkup,/data-plot-key="gauge\./,
  'auxiliary temperature tiles never receive a pressure gauge link');
assert.match(auxMarkup,/if\(tc && valEl\)/,
  'only thermocouple auxiliary values get popup links');
console.log('PASS sensor plot reduction, gaps, controls and scoped Hardware links');
