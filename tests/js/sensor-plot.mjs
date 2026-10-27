import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {navigateSensorRange, reduceSensorPoints, sensorTimeRange}
  from '../../reactor/server/static/sensor-plot-data.js';

const now=20000, earliest=1000;
assert.deepEqual(sensorTimeRange({mode:'preset',windowSeconds:3600,
  start:5000,end:null,earliest,now}),[16400,20000],
  'preset ignores stale manual inputs');
assert.deepEqual(sensorTimeRange({mode:'manual',windowSeconds:3600,
  start:5000,end:null,earliest,now}),[5000,20000],
  'start-only manual range extends to the live edge, beyond the hour preset');
assert.deepEqual(sensorTimeRange({mode:'manual',windowSeconds:3600,
  start:5000,end:9000,earliest,now}),[5000,9000],
  'manual end freezes the selected period');
assert.deepEqual(sensorTimeRange({mode:'manual',windowSeconds:3600,
  start:null,end:9000,earliest,now}),[1000,9000],
  'end-only manual range starts at the oldest available sample');
assert.deepEqual(navigateSensorRange([100,200],'ArrowLeft'),[80,180],
  'left arrow pans by one fifth of the visible span');
assert.deepEqual(navigateSensorRange([100,200],'ArrowUp'),[120,200],
  'up arrow zooms in around the right edge');
assert.deepEqual(navigateSensorRange([100,200],'ArrowDown'),[75,200],
  'down arrow zooms out');

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
const popup = await readFile(new URL('../../reactor/server/static/sensor-plot.js',import.meta.url),'utf8');
const gaugeMarkup = control.split('function gauges(s){')[1].split('function aux(s){')[0];
const auxMarkup = control.split('function aux(s){')[1].split('function valveId(s){')[0];
assert.match(page,/id="sensorCanvas"/, 'popup contains its own canvas');
assert.match(page,/id="yLog"/, 'popup exposes a log axis control');
assert.match(page,/id="manualMode"/, 'popup labels the custom time mode');
assert.match(popup,/activateMode\("manual"\)/, 'manual edit and zoom disable the preset');
assert.match(popup,/window\.addEventListener\("mouseup"/, 'drag-to-zoom finishes outside the canvas');
assert.match(popup,/navigateSensorRange\(bounds\(\),event\.key\)/,
  'keyboard pan and zoom use the active time range');
assert.match(popup,/&start=\$\{encodeURIComponent\(from\)\}&end=/,
  'manual time ranges request older data from the server');
assert.match(popup,/availableFrom=data\.availableFrom/,
  'popup uses the archive start rather than the current hour as its limit');
assert.match(gaugeMarkup,/data-plot-key="gauge\.\$\{esc\(g\.id\)\}"/,
  'configured pressure gauge values open the popup');
assert.doesNotMatch(auxMarkup,/data-plot-key="gauge\./,
  'auxiliary temperature tiles never receive a pressure gauge link');
assert.match(auxMarkup,/if\(tc && valEl\)/,
  'only thermocouple auxiliary values get popup links');

// Exercise the actual popup controls with a read-only, fake browser surface.
class Element {
  constructor(){
    this.value=''; this.checked=false; this.dataset={}; this.listeners={};
    this.classList={values:new Set(),toggle(name,on){
      if(on) this.values.add(name); else this.values.delete(name);
    }};
  }
  addEventListener(name,fn){this.listeners[name]=fn;}
  setAttribute(){}
  getBoundingClientRect(){return {left:0};}
}
globalThis.HTMLInputElement=class extends Element {};
const elements=new Map();
for(const id of ['sensorCanvas','xMin','xMax','yMin','yMax','yLog',
  'manualMode','pointCount','plotMessage','hoverReadout','connection',
  'sensorTitle','plotTitle','yLabel'])
  elements.set(id,['xMin','xMax','yMin','yMax','yLog'].includes(id)
    ? new HTMLInputElement() : new Element());
const canvas=elements.get('sensorCanvas');
canvas.clientWidth=800; canvas.clientHeight=400;
const noop=()=>{};
canvas.getContext=()=>({setTransform:noop,fillRect:noop,fillText:noop,
  strokeRect:noop,beginPath:noop,moveTo:noop,lineTo:noop,stroke:noop,
  arc:noop,fill:noop});
const buttons=[300,900,3600].map(seconds=>{
  const button=new Element(); button.dataset.window=String(seconds); return button;
});
globalThis.document={getElementById:id=>elements.get(id),
  querySelectorAll:selector=>selector==='[data-window]' ? buttons : [],title:''};
const windowListeners={};
globalThis.window={devicePixelRatio:1,
  addEventListener:(name,fn)=>{windowListeners[name]=fn;}};
globalThis.location={search:'?key=aux.bakeout2',protocol:'http:',host:'localhost:8000'};
globalThis.WebSocket=class {close(){}};
const liveNow=Date.now()/1000;
const requests=[];
globalThis.fetch=async url=>{
  requests.push(url);
  return {ok:true,json:async()=>({key:'aux.bakeout2',label:'Bakeout temp 2',
    unit:'C',pressure:false,availableFrom:liveNow-7200,
    samples:[[liveNow-600,20],[liveNow-300,23],[liveNow,25]]})};
};
await import('../../reactor/server/static/sensor-plot.js');
await new Promise(resolve=>setImmediate(resolve));
const xMin=elements.get('xMin'), xMax=elements.get('xMax');
xMin.value='2026-10-09T16:15:00';
xMin.listeners.input();
assert.ok(!buttons.some(button=>button.classList.values.has('active')),
  'typing a manual start clears the active hour preset');
await new Promise(resolve=>setTimeout(resolve,210));
assert.match(requests.at(-1),/&start=.*&end=/,
  'start-only selection fetches its full time span from the archive');
assert.equal(xMax.value,'', 'manual start leaves the end open for live follow');
buttons[0].listeners.click();
assert.equal(xMin.value,'', 'choosing a preset clears the manual start');
canvas.listeners.mouseenter();
canvas.listeners.mousedown({button:0,clientX:180,preventDefault:noop});
canvas.listeners.mousemove({currentTarget:canvas,clientX:440});
windowListeners.mouseup({clientX:440});
assert.ok(xMin.value && xMax.value, 'drag zoom writes a bounded custom range');
const before=xMin.value;
windowListeners.keydown({key:'ArrowLeft',target:canvas,preventDefault:noop});
assert.notEqual(xMin.value,before, 'left arrow pans the custom range');
console.log('PASS sensor plot reduction, gaps, controls and scoped Hardware links');
