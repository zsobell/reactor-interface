"use strict";
import {nearestFinitePoint, resolveAxisRange} from "./analysis-plot.js";
import {navigateSensorRange, reduceSensorPoints, sensorTimeRange} from "./sensor-plot-data.js";

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const key = params.get("key") || "";
const requestedLabel = params.get("label") || "";
const points = [];
let spec = null;
let availableFrom = null;
let historyError = "";
let historyRequestId = 0;
let historyTimer = null;
let windowSeconds = 3600;
let rangeMode = "preset";
let lastReadingTime = null;
let socket = null;
let hoverX = null;
let liveDetail = "";
let graph = null;
const zoomSel = {active:false, x0:null, x1:null};
let hovering = false;

function number(text){
  if(!text.trim()) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}
function timeInput(id){
  const value = $(id).value;
  if(!value) return null;
  const seconds = new Date(value).getTime() / 1000;
  return Number.isFinite(seconds) ? seconds : null;
}
function tick(value){
  if(value === 0) return "0";
  const magnitude = Math.abs(value);
  return (magnitude < 0.001 || magnitude >= 1e5)
    ? value.toExponential(1) : String(+value.toPrecision(4));
}
function reading(value){
  if(value === 0) return "0";
  return (Math.abs(value) < 0.001 || Math.abs(value) >= 1e5)
    ? value.toExponential(3) : String(+value.toPrecision(5));
}
function timestamp(seconds){return new Date(seconds * 1000).toLocaleString();}
function localTimeInput(seconds){
  const date=new Date(seconds*1000), pad=n=>String(n).padStart(2,"0");
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function bounds(){
  return sensorTimeRange({mode:rangeMode, windowSeconds,
    start:timeInput("xMin"), end:timeInput("xMax"),
    earliest:availableFrom ?? points[0]?.[0] ?? null, now:Date.now()/1000});
}
function activateMode(mode){
  rangeMode=mode;
  for(const button of document.querySelectorAll("[data-window]")){
    const active=mode === "preset" && Number(button.dataset.window) === windowSeconds;
    button.classList.toggle("active",active);
    button.setAttribute("aria-pressed",String(active));
  }
  $("manualMode").classList.toggle("hidden",mode !== "manual");
}
function setManualRange(from,to){
  $("xMin").value=localTimeInput(from);
  $("xMax").value=localTimeInput(to);
  activateMode("manual");
  draw();
  scheduleHistory();
}
function centre(ctx, width, height, message){
  ctx.fillStyle = "#8b949e";
  ctx.textAlign = "center";
  ctx.font = "13px system-ui";
  ctx.fillText(message, width/2, height/2);
}

function draw(){
  const canvas = $("sensorCanvas");
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if(width < 2 || height < 2) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width*dpr);
  canvas.height = Math.round(height*dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.fillStyle = "#0d1117";
  ctx.fillRect(0,0,width,height);
  graph=null;
  if(!spec){ centre(ctx,width,height,"Loading sensor history…"); return; }

  const [from,to] = bounds();
  if(!(to > from)){
    $("plotMessage").textContent = "Time end must be after time start.";
    $("hoverReadout").textContent = "No sensor data";
    centre(ctx,width,height,"Choose a valid time range");
    return;
  }
  const oldest = availableFrom ?? points[0]?.[0];
  const historyNotice = rangeMode === "manual" && oldest != null && from < oldest
    ? `Available history begins ${timestamp(oldest)}.` : "";
  const visible = reduceSensorPoints(points,from,to,Math.max(80,Math.floor(width)));
  const logarithmic = $("yLog").checked;
  const good = visible.filter(([,v]) => Number.isFinite(v) && (!logarithmic || v > 0));
  $("pointCount").textContent = `${good.length} displayed points`;
  if(!good.length){
    $("plotMessage").textContent = [historyError,historyNotice,liveDetail ||
      "No valid readings in this time range."].filter(Boolean).join(" ");
    $("hoverReadout").textContent = "No sensor data in this time range";
    centre(ctx,width,height,"No valid readings in this time range");
    return;
  }
  const yMin = number($("yMin").value), yMax = number($("yMax").value);
  const explicit = [yMin,yMax].map(v => logarithmic && v !== null && v <= 0 ? null : v);
  const [y0,y1] = resolveAxisRange(explicit,good.map(([,v])=>v),logarithmic);
  const warning = logarithmic && (yMin !== null && yMin <= 0 || yMax !== null && yMax <= 0)
    ? "Log axis bounds must be positive; using automatic bound." : "";
  $("plotMessage").textContent = [historyError,warning,historyNotice,liveDetail]
    .filter(Boolean).join(" ");
  const left=80,right=18,top=16,bottom=48;
  const graphWidth=width-left-right, graphHeight=height-top-bottom;
  if(graphWidth < 40 || graphHeight < 40) return;
  const xPixel = t => left + graphWidth*(t-from)/(to-from);
  graph={left,right:left+graphWidth,from,to,
    timeAt:px=>from+(px-left)*(to-from)/graphWidth};
  const yPixel = value => {
    const v = logarithmic ? Math.log10(value) : value;
    return top + graphHeight*(1-(v-y0)/(y1-y0));
  };

  ctx.strokeStyle="#2a323d";
  ctx.lineWidth=1;
  ctx.strokeRect(left,top,graphWidth,graphHeight);
  ctx.font="11px system-ui";
  for(let i=0;i<=4;i++){
    const y=top+graphHeight*i/4;
    ctx.strokeStyle="#1b2230";
    ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(left+graphWidth,y);ctx.stroke();
    const value = y1-(y1-y0)*i/4;
    ctx.fillStyle="#8b949e";ctx.textAlign="right";ctx.textBaseline="middle";
    ctx.fillText(tick(logarithmic ? 10**value : value),left-7,y);
  }
  const timeTicks = width < 650 ? 3 : 5;
  for(let i=0;i<=timeTicks;i++){
    const t=from+(to-from)*i/timeTicks, x=xPixel(t);
    ctx.fillStyle="#8b949e";ctx.textAlign="center";ctx.textBaseline="top";
    ctx.fillText(new Date(t*1000).toLocaleTimeString(),x,top+graphHeight+7);
  }
  ctx.fillStyle="#8b949e";ctx.textAlign="center";
  ctx.fillText("Local time",left+graphWidth/2,height-14);

  ctx.strokeStyle="#58a6ff";ctx.lineWidth=1.5;
  ctx.beginPath();
  let penDown=false;
  for(const [t,value] of visible){
    if(!Number.isFinite(value) || (logarithmic && value<=0)){
      penDown=false;
      continue;
    }
    const x=xPixel(t),y=yPixel(value);
    if(!penDown) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    penDown=true;
  }
  ctx.stroke();

  if(zoomSel.active && zoomSel.x0 !== null && zoomSel.x1 !== null){
    const a=Math.max(left,Math.min(zoomSel.x0,zoomSel.x1));
    const b=Math.min(left+graphWidth,Math.max(zoomSel.x0,zoomSel.x1));
    if(b>a){
      ctx.fillStyle="rgba(88,166,255,0.16)";
      ctx.fillRect(a,top,b-a,graphHeight);
      ctx.strokeStyle="#58a6ff";
      ctx.strokeRect(a,top,b-a,graphHeight);
    }
    $("hoverReadout").textContent="Release to zoom to selected time range";
    return;
  }

  if(hoverX !== null && hoverX >= left && hoverX <= left+graphWidth){
    const target = from+(hoverX-left)*(to-from)/graphWidth;
    const series={X:visible.map(p=>p[0]),Y:visible.map(p=>p[1])};
    const nearest=nearestFinitePoint(series,target,logarithmic);
    if(nearest){
      const x=xPixel(nearest.x),y=yPixel(nearest.y);
      ctx.strokeStyle="#7d8894";ctx.lineWidth=1;
      ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,top+graphHeight);ctx.stroke();
      ctx.fillStyle="#58a6ff";ctx.beginPath();ctx.arc(x,y,4,0,2*Math.PI);ctx.fill();
      $("hoverReadout").textContent =
        `${timestamp(nearest.x)} · ${reading(nearest.y)} ${spec.unit}`;
    }
  } else {
    $("hoverReadout").textContent = "Hover over the plot to read a value";
  }
}

function append(t,value){
  if(!Number.isFinite(t)) return;
  const next=Number.isFinite(value) ? value : null;
  const last=points.at(-1);
  // A popup can remain open all day. Keep live updates compact as well.
  if(last && t-last[0] < 2 && Number.isFinite(last[1]) === Number.isFinite(next))
    points[points.length-1]=[t,next];
  else points.push([t,next]);
  draw();
}
function scheduleHistory(){
  const requestId=++historyRequestId;
  clearTimeout(historyTimer);
  if(!spec) return;
  historyTimer=setTimeout(async()=>{
    const [from,to]=bounds();
    if(!(to>from)) return;
    try{
      const url=`/api/sensor-trend?key=${encodeURIComponent(key)}`
        + `&start=${encodeURIComponent(from)}&end=${encodeURIComponent(to)}`;
      const response=await fetch(url);
      if(!response.ok) throw new Error("Sensor history is unavailable.");
      const data=await response.json();
      if(requestId!==historyRequestId) return;
      const live=points.at(-1);
      points.length=0;
      for(const [t,value] of data.samples)
        points.push([t,Number.isFinite(value) ? value : null]);
      if(live && live[0] > (points.at(-1)?.[0] ?? -Infinity)) points.push(live);
      availableFrom=data.availableFrom ?? availableFrom;
      historyError="";
      draw();
    }catch(error){
      if(requestId!==historyRequestId) return;
      historyError=error.message;
      draw();
    }
  },180);
}
function connect(){
  const protocol=location.protocol === "https:" ? "wss:" : "ws:";
  socket=new WebSocket(`${protocol}//${location.host}/ws`);
  socket.onopen=()=>{$("connection").textContent="live";};
  socket.onmessage=event=>{
    const frame=JSON.parse(event.data);
    const rd=frame.readings?.[key];
    if(!rd || rd.t === lastReadingTime) return;
    lastReadingTime=rd.t;
    liveDetail=rd.ok ? "" : (rd.detail || "Reading unavailable");
    const value=rd.ok ? frame.snapshot?.[key] : null;
    const last=points.at(-1)?.[0] ?? -Infinity;
    if(frame.t > last) append(frame.t,value);
    else draw();
  };
  socket.onclose=()=>{
    $("connection").textContent="reconnecting…";
    setTimeout(connect,2000);
  };
  socket.onerror=()=>socket.close();
}

const canvas=$("sensorCanvas");
const cssX=event=>event.clientX-canvas.getBoundingClientRect().left;
canvas.addEventListener("mouseenter",()=>{hovering=true;});
canvas.addEventListener("mousemove",event=>{
  const rect=event.currentTarget.getBoundingClientRect();
  hoverX=event.clientX-rect.left;
  if(zoomSel.active) zoomSel.x1=hoverX;
  draw();
});
canvas.addEventListener("mouseleave",()=>{hovering=false;hoverX=null;draw();});
canvas.addEventListener("mousedown",event=>{
  if(event.button !== 0 || !graph) return;
  event.preventDefault();
  zoomSel.active=true;
  zoomSel.x0=cssX(event);
  zoomSel.x1=zoomSel.x0;
});
window.addEventListener("mouseup",event=>{
  if(!zoomSel.active) return;
  const a=Math.max(graph?.left ?? 0,Math.min(zoomSel.x0,cssX(event)));
  const b=Math.min(graph?.right ?? 0,Math.max(zoomSel.x0,cssX(event)));
  zoomSel.active=false;
  zoomSel.x0=zoomSel.x1=null;
  if(graph && b-a>5)
    setManualRange(graph.timeAt(a),Math.max(graph.timeAt(a)+1,graph.timeAt(b)));
  else draw();
});
window.addEventListener("keydown",event=>{
  if(!hovering || event.target instanceof HTMLInputElement) return;
  const next=navigateSensorRange(bounds(),event.key);
  if(!next) return;
  event.preventDefault();
  setManualRange(...next);
});
for(const id of ["xMin","xMax"])
  $(id).addEventListener("input",()=>{
    activateMode($("xMin").value || $("xMax").value ? "manual" : "preset");
    draw();
    scheduleHistory();
  });
for(const id of ["yMin","yMax","yLog"])
  $(id).addEventListener("input",draw);
for(const button of document.querySelectorAll("[data-window]")){
  button.addEventListener("click",()=>{
    windowSeconds=Number(button.dataset.window);
    $("xMin").value="";$("xMax").value="";
    activateMode("preset");
    draw();
    scheduleHistory();
  });
}
window.addEventListener("resize",draw);

(async()=>{
  try{
    const response=await fetch(`/api/sensor-trend?key=${encodeURIComponent(key)}`);
    if(!response.ok) throw new Error("Sensor history is unavailable.");
    const data=await response.json();
    spec=data;
    availableFrom=data.availableFrom ?? null;
    const label=requestedLabel || data.label;
    document.title=`${label} · Reactor Interface`;
    $("sensorTitle").textContent=label;
    $("plotTitle").textContent=`${label} (${data.unit}) vs time`;
    $("yLabel").textContent=data.pressure ? "Pressure" : "Temperature";
    $("yLog").checked=!!data.pressure;
    for(const [t,value] of data.samples)
      points.push([t,Number.isFinite(value) ? value : null]);
    draw();
    connect();
    if(rangeMode === "manual") scheduleHistory();
  }catch(error){
    $("connection").textContent="unavailable";
    $("plotMessage").textContent=error.message;
    $("hoverReadout").textContent="No sensor data";
  }
})();
