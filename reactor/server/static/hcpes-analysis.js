"use strict";

/* Dedicated, read-only HCPES analysis. Kept separate from analysis.js so the
   generic run/Auger/ellipsometer plotter retains its existing data and layout. */

const POINT_LABELS = {
  signed_stage_bias_v: "Signed stage bias (V)",
  point_index: "Point index",
  acquisition_order: "Condition completion order",
  signed_order: "Signed sweep order",
  started_elapsed_s: "Point start (s)",
  ended_elapsed_s: "Point end (s)",
  stage_current_mean_a: "Stage current mean (mA)",
  stage_current_stddev_a: "Stage current standard deviation (mA)",
  stage_current_min_a: "Stage current minimum (mA)",
  stage_current_max_a: "Stage current maximum (mA)",
  observed_drift_a_per_min: "Stage-current drift (mA/min)",
  chamber_pressure_mean_torr: "Chamber pressure mean (Torr)",
  stage_temperature_mean_c: "Stage temperature mean (°C)",
  aperture_lifetime_mean_s: "Aperture lifetime mean (s)",
  ar_baratron_mean_torr: "Ar Baratron mean (Torr)",
  hv_voltage_mean_v: "HV voltage mean (V)",
  hv_current_mean_ma: "HV current mean (mA)",
};

const COLOURS = ["#58a6ff", "#f0883e", "#3fb950", "#bc8cff", "#f2cc60",
                 "#39c5cf", "#ff7b72", "#a5d6ff", "#d2a8ff", "#7ee787"];
const PHASE_COLOURS = {
  acquire: "#3fb950", settle: "#58a6ff", parameter_change: "#d29922",
  pause: "#8b949e", plasma_loss: "#f85149", reignite: "#bc8cff",
  recovery: "#f0883e", inaccessible: "#ff7b72", setup: "#39c5cf",
};

export function esc(value){
  return String(value ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

export function label(key){
  if(POINT_LABELS[key]) return POINT_LABELS[key];
  if(key.startsWith("setpoint:")){
    const target = key.slice(9).replace(/^mfc:/, "MFC ").replace(/^supply:/, "");
    const unit = key.includes("mfc:") ? "sccm"
      : /(steering|collimating)$/.test(key) ? "mA" : "V";
    return `${target.replaceAll("_", " ")} (${unit})`;
  }
  if(key.startsWith("channel:")){
    const channel=key.slice(8), name=channel.replaceAll(".", " · ");
    if(channel === "inst.ammeter") return "Stage current mean (mA)";
    if(/^psu\..+\.current$/.test(channel)) return `${name} mean (mA)`;
    if(/^hv\..+\.current$/.test(channel)) return `${name} mean (mA)`;
    return `${name} mean`;
  }
  return key.replaceAll("_", " ");
}

export function displayScale(key){
  if(/^stage_current_(mean|stddev|min|max)_a$/.test(key)) return 1000;
  if(key === "observed_drift_a_per_min") return 1000;
  if(/^setpoint:supply:(steering|collimating)$/.test(key)) return 1000;
  if(key === "channel:inst.ammeter") return 1000;
  if(/^channel:psu\..+\.current$/.test(key)) return 1000;
  return 1;
}

export function displayValue(key, value){
  if(value === null || value === undefined || value === "") return value;
  const number=Number(value);
  return Number.isFinite(number) ? number * displayScale(key) : value;
}

export function metricValue(point, key){
  if(key.startsWith("channel:"))
    return point.channel_stats?.[key.slice(8)]?.mean ?? null;
  return point[key] ?? null;
}

export function axisKeys(source){
  if(!source) return [];
  return ["signed_stage_bias_v", ...(source.fields || []).filter(
    key => key.startsWith("setpoint:") && key !== "setpoint:supply:stage_bias")];
}

export function metricKeys(source){
  if(!source) return [];
  const excluded = new Set([
    "point_index", "signed_stage_bias_v", "requested_qualified_samples",
    "actual_qualified_samples", "settled", "recovery_count", "source_polarity",
    "source_point", "started_elapsed_s", "ended_elapsed_s",
  ]);
  const keys = (source.fields || []).filter(key =>
    !key.startsWith("setpoint:") && !key.startsWith("source_")
    && !excluded.has(key)
    && (source.points || []).some(row => Number.isFinite(row[key])));
  const channels = new Set();
  for(const point of source.points || [])
    for(const key of Object.keys(point.channel_stats || {})) channels.add(`channel:${key}`);
  return [...keys, ...[...channels].sort()];
}

function sameValue(a, b){
  return String(a) === String(b);
}

export function filterPoints(points, filters){
  return points.filter(point => Object.entries(filters || {}).every(
    ([key, value]) => value === "" || sameValue(point[key], value)));
}

export function continuousOrder(points, xKey){
  return points.map((point, index) => ({point, index})).sort((a, b) => {
    const av = +a.point[xKey], bv = +b.point[xKey];
    if(av !== bv) return av - bv;
    const ao = a.point.signed_order ?? a.index;
    const bo = b.point.signed_order ?? b.index;
    return ao - bo;
  }).map(row => row.point);
}

export function sliceGroups(source, points, xKey, filters){
  const varying = axisKeys(source).filter(key => key !== xKey && !filters[key]);
  const forceSession = source?.kind === "campaign" && !source.compatible;
  const groups = new Map();
  for(const point of points){
    const parts = varying.map(key => `${label(key)}=${point[key]}`);
    if(forceSession) parts.unshift(`session=${point.source_session}`);
    const name = parts.join(" · ") || "selected conditions";
    if(!groups.has(name)) groups.set(name, []);
    groups.get(name).push(point);
  }
  return [...groups].map(([name, rows]) => ({name, points: continuousOrder(rows, xKey)}));
}

export function heatCells(points, xKey, yKey, metric){
  const cells = new Map();
  for(const point of points){
    const x = metricValue(point, xKey), y = metricValue(point, yKey);
    const value = metricValue(point, metric);
    if(!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(value)) continue;
    const key = `${x}\u0000${y}`;
    if(!cells.has(key)) cells.set(key, {x, y, values: [], points: []});
    cells.get(key).values.push(value);
    cells.get(key).points.push(point);
  }
  return [...cells.values()].map(cell => ({
    ...cell, value: cell.values.reduce((a, b) => a + b, 0) / cell.values.length,
  }));
}

export function rawCurrent(row){
  const value = row?.measurements?.["inst.ammeter"];
  return Number.isFinite(value) ? value : null;
}

const hasDom = typeof document !== "undefined";
const $ = id => hasDom ? document.getElementById(id) : null;
let SOURCE = null;
let FILTERS = {};
let RAW = null;
let hitItems = [];

function fmt(value, digits=5){
  if(value === null || value === undefined || value === "") return "—";
  if(!Number.isFinite(+value)) return "—";
  const number = +value;
  const magnitude = Math.abs(number);
  return magnitude && (magnitude < 1e-3 || magnitude >= 1e5)
    ? number.toExponential(3) : String(+number.toPrecision(digits));
}

export function pointConditionLines(source, point){
  return axisKeys(source).map(key =>
    `${label(key)}: ${fmt(displayValue(key, point?.[key]), 7)}`);
}

function options(keys, selected){
  return keys.map(key => `<option value="${esc(key)}"${key === selected ? " selected" : ""}>${esc(label(key))}</option>`).join("");
}

function uniqueValues(key){
  return [...new Set((SOURCE?.points || []).map(row => row[key]).filter(
    value => value !== null && value !== undefined))].sort((a, b) => +a - +b);
}

function toast(message, ok=false){
  document.querySelectorAll(".toast").forEach(node => node.remove());
  const node = document.createElement("div");
  node.className = `toast${ok ? " ok" : ""}`;
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), ok ? 2600 : 7000);
}

function activateTab(name){
  const hcpes = name === "hcpes";
  $("runAnalysisPane").classList.toggle("hidden", hcpes);
  $("hcpesAnalysisPane").classList.toggle("hidden", !hcpes);
  document.querySelectorAll(".analysis-tab").forEach(button =>
    button.classList.toggle("active", button.dataset.analysisTab === name));
  document.querySelectorAll(".run-analysis-control").forEach(node =>
    node.classList.toggle("hidden", hcpes));
  try{ localStorage.setItem("reactorAnalysis.activeTab", name); }catch(_){}
  if(hcpes){
    if(!$("hcpesSourceSel").options.length || !$("hcpesSourceSel").value) rescan();
    requestAnimationFrame(draw);
  }
  requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
}

async function rescan(){
  try{
    const response = await fetch("/api/hcpes/analysis/sources");
    if(!response.ok) throw new Error(`scan failed (${response.status})`);
    const data = await response.json();
    const previous = $("hcpesSourceSel").value;
    $("hcpesSourceSel").innerHTML = data.sources.length
      ? data.sources.map(source => `<option value="${esc(source.name)}">${esc(source.title)} — ${source.kind}, ${source.status}, ${source.points} points</option>`).join("")
      : `<option value="">— no HCPES bundles in ${esc(data.dir)} —</option>`;
    if(previous && [...$("hcpesSourceSel").options].some(option => option.value === previous))
      $("hcpesSourceSel").value = previous;
  }catch(error){
    $("hcpesSourceSel").innerHTML = `<option value="">— HCPES data unavailable —</option>`;
  }
}

async function loadSource(){
  const name = $("hcpesSourceSel").value;
  if(!name){ toast("No HCPES source selected."); return; }
  try{
    const response = await fetch(`/api/hcpes/analysis/source?name=${encodeURIComponent(name)}`);
    if(!response.ok) throw new Error((await response.json()).detail || `load failed (${response.status})`);
    SOURCE = await response.json();
    FILTERS = {};
    RAW = null;
    configureSource();
    toast(`Loaded ${SOURCE.title}`, true);
  }catch(error){ toast(error.message || String(error)); }
}

function configureSource(){
  const points = SOURCE.points || [];
  const rejected = points.filter(row => ["partial", "inaccessible"].includes(row.accessibility)).length;
  const unsettled = points.filter(row => row.settled === false).length;
  const sessions = new Set(points.map(row => row.source_session).filter(Boolean));
  $("hcpesSourceInfo").textContent = `${SOURCE.kind} · ${points.length} points · ${sessions.size} session${sessions.size === 1 ? "" : "s"}`;
  const alert = $("hcpesCompatibility");
  alert.classList.toggle("hidden", SOURCE.compatible !== false);
  alert.textContent = SOURCE.compatible === false
    ? `Sources are not stitched: ${(SOURCE.compatibility_issues || []).join(" · ")}` : "";
  $("hcpesSummary").innerHTML = [
    [points.length, "attempted points"],
    [points.length - rejected, "collected points"],
    [rejected, "rejected / partial"],
    [unsettled, "never settled"],
    [SOURCE.kind === "campaign" ? "− to +" : ((SOURCE.session?.stage_polarity || 1) > 0 ? "+" : "−"), "polarity"],
  ].map(([value, text]) => `<div><strong>${esc(value)}</strong><span>${esc(text)}</span></div>`).join("");

  const axes = axisKeys(SOURCE);
  $("hcpesX").innerHTML = options(axes, "signed_stage_bias_v");
  $("hcpesHeatY").innerHTML = options(axes, axes.find(key => key !== "signed_stage_bias_v") || axes[0]);
  const metrics = metricKeys(SOURCE);
  $("hcpesMetric").innerHTML = options(metrics,
    metrics.includes("stage_current_mean_a") ? "stage_current_mean_a" : metrics[0]);
  renderFilters();
  configureRawSelectors();
  updateViewControls();
  draw();
  clearRaw();
}

function renderFilters(){
  $("hcpesFilters").innerHTML = axisKeys(SOURCE).map(key => {
    const values = uniqueValues(key);
    return `<label>${esc(label(key))}<select data-filter="${esc(key)}">
      <option value="">All (${values.length})</option>
      ${values.map(value => `<option value="${esc(value)}">${esc(fmt(displayValue(key,value),7))}</option>`).join("")}
    </select></label>`;
  }).join("");
  $("hcpesFilters").querySelectorAll("select").forEach(select => {
    select.addEventListener("change", () => {
      FILTERS[select.dataset.filter] = select.value;
      draw();
    });
  });
}

function configureRawSelectors(){
  $("hcpesRawSession").innerHTML = (SOURCE.raw_sources || []).map(row =>
    `<option value="${esc(row.session_id)}">${esc(row.session_id)} (${row.polarity > 0 ? "+" : "−"})</option>`).join("");
  configureRawPoints();
}

function configureRawPoints(){
  const session = $("hcpesRawSession").value;
  const points = (SOURCE?.points || []).filter(row => row.source_session === session);
  const indexes = [...new Set(points.map(row => row.source_point))].sort((a,b) => a-b);
  $("hcpesRawPoint").innerHTML = `<option value="">all points</option>`
    + indexes.map(index => `<option value="${index}">point ${index}</option>`).join("");
}

function updateViewControls(){
  const view = $("hcpesView").value;
  $("hcpesHeatYWrap").classList.toggle("hidden", view !== "heat");
  const x = $("hcpesX");
  const previous = x.value;
  const keys = view === "trend"
    ? ["acquisition_order", "ended_elapsed_s", "started_elapsed_s"]
    : axisKeys(SOURCE);
  x.innerHTML = options(keys, keys.includes(previous) ? previous
    : (view === "trend" ? "acquisition_order" : "signed_stage_bias_v"));
  const help={
    line:"Plots the selected result against one swept parameter. Any other parameter left at All becomes a separate line.",
    heat:"Maps two swept parameters. If another parameter remains at All, each cell averages every matching condition; filter it to inspect one slice.",
    trend:"Run sequence plots the selected result in the order conditions finished. Lines connect points only within one session. Use it to spot time/order drift—not as a response-versus-one-parameter sweep. Hover a point to see every commanded condition.",
  };
  const note=$("hcpesViewHelp"); if(note) note.textContent=help[view] || "";
}

function canvasContext(canvas){
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = Math.max(2, Math.round(width * dpr));
  canvas.height = Math.max(2, Math.round(height * dpr));
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#0d1117"; g.fillRect(0, 0, width, height);
  return {g, width, height};
}

function extent(values){
  let min=Infinity,max=-Infinity;
  for(const value of values){
    if(!Number.isFinite(value)) continue;
    if(value<min) min=value;
    if(value>max) max=value;
  }
  if(min===Infinity) return [0,1];
  if(min === max){ const delta = Math.abs(min) * .05 || 1; min -= delta; max += delta; }
  const pad = (max - min) * .06;
  return [min - pad, max + pad];
}

function axes(g, width, height, xRange, yRange, xLabel, yLabel){
  const box = {left:68, right:width-22, top:20, bottom:height-48};
  const W = box.right - box.left, H = box.bottom - box.top;
  const px = value => box.left + W * (value-xRange[0])/(xRange[1]-xRange[0]);
  const py = value => box.bottom - H * (value-yRange[0])/(yRange[1]-yRange[0]);
  g.font = "10px system-ui"; g.strokeStyle = "#2a323d"; g.fillStyle = "#8b949e";
  g.strokeRect(box.left, box.top, W, H);
  for(let i=0;i<=4;i++){
    const y = box.top + H*i/4;
    g.strokeStyle = "#1b2230"; g.beginPath(); g.moveTo(box.left,y); g.lineTo(box.right,y); g.stroke();
    g.textAlign="right"; g.textBaseline="middle";
    g.fillText(fmt(yRange[1]-(yRange[1]-yRange[0])*i/4,4), box.left-7,y);
  }
  for(let i=0;i<=5;i++){
    const x = box.left + W*i/5;
    g.textAlign="center"; g.textBaseline="top";
    g.fillText(fmt(xRange[0]+(xRange[1]-xRange[0])*i/5,4),x,box.bottom+7);
  }
  g.textAlign="center"; g.fillText(xLabel, box.left+W/2,height-14);
  g.save(); g.translate(13,box.top+H/2); g.rotate(-Math.PI/2); g.fillText(yLabel,0,0); g.restore();
  return {box, px, py};
}

function draw(){
  const canvas = $("hcpesCanvas");
  const {g,width,height} = canvasContext(canvas);
  hitItems = [];
  if(!SOURCE){ centre(g,width,height,"load an HCPES session or campaign"); return; }
  const points = filterPoints(SOURCE.points || [], FILTERS);
  const view = $("hcpesView").value;
  if(view === "heat") drawHeat(g,width,height,points);
  else drawLines(g,width,height,points,view === "trend");
}

function drawLines(g,width,height,points,trend){
  const xKey = $("hcpesX").value;
  const metric = $("hcpesMetric").value;
  let groups;
  if(trend){
    const map = new Map();
    for(const point of points){
      const name = point.source_session || "session";
      if(!map.has(name)) map.set(name,[]);
      map.get(name).push(point);
    }
    groups = [...map].map(([name,rows]) => ({name,points:continuousOrder(rows,xKey)}));
  }else groups = sliceGroups(SOURCE,points,xKey,FILTERS);
  const plotPoints = groups.flatMap(group => group.points.map(point => ({
    point, x: displayValue(xKey,metricValue(point,xKey)),
    y: displayValue(metric,metricValue(point,metric)), group,
  }))).filter(row => Number.isFinite(row.x) && Number.isFinite(row.y));
  if(!plotPoints.length){ centre(g,width,height,"no qualified values match these filters"); legend([]); return; }
  const xRange=extent(plotPoints.map(row=>row.x)), yRange=extent(plotPoints.map(row=>row.y));
  const A=axes(g,width,height,xRange,yRange,label(xKey),label(metric));
  groups.forEach((group,index) => {
    const colour=COLOURS[index%COLOURS.length];
    const rows=plotPoints.filter(row=>row.group===group);
    g.strokeStyle=colour; g.lineWidth=1.7; g.beginPath();
    rows.forEach((row,i) => { const x=A.px(row.x),y=A.py(row.y); i?g.lineTo(x,y):g.moveTo(x,y); });
    g.stroke();
    for(const row of rows){
      const x=A.px(row.x),y=A.py(row.y), point=row.point;
      marker(g,x,y,colour,point);
      hitItems.push({x,y,point,value:row.y,series:group.name});
    }
  });
  legend(groups.map((group,index)=>({name:group.name,colour:COLOURS[index%COLOURS.length]})));
}

function marker(g,x,y,colour,point){
  g.save(); g.fillStyle=colour; g.strokeStyle=colour; g.lineWidth=1.6;
  if(point.accessibility === "inaccessible"){
    g.beginPath(); g.moveTo(x-4,y-4);g.lineTo(x+4,y+4);g.moveTo(x+4,y-4);g.lineTo(x-4,y+4);g.stroke();
  }else if(point.accessibility === "recovered"){
    g.beginPath();g.moveTo(x,y-4);g.lineTo(x+4,y);g.lineTo(x,y+4);g.lineTo(x-4,y);g.closePath();g.fill();
  }else{
    g.beginPath();g.arc(x,y,3.2,0,Math.PI*2);g.fill();
  }
  if(point.settled === false){
    g.strokeStyle="#f85149";g.lineWidth=2;g.beginPath();g.arc(x,y,6,0,Math.PI*2);g.stroke();
  }
  g.restore();
}

function drawHeat(g,width,height,points){
  const xKey=$("hcpesX").value, yKey=$("hcpesHeatY").value, metric=$("hcpesMetric").value;
  if(xKey===yKey){ centre(g,width,height,"choose two different parameter axes"); legend([]); return; }
  const cells=heatCells(points,xKey,yKey,metric).map(cell => ({...cell,
    x:displayValue(xKey,cell.x), y:displayValue(yKey,cell.y),
    value:displayValue(metric,cell.value),
  }));
  if(!cells.length){ centre(g,width,height,"no qualified values match these filters"); legend([]); return; }
  const xs=[...new Set(cells.map(c=>c.x))].sort((a,b)=>a-b);
  const ys=[...new Set(cells.map(c=>c.y))].sort((a,b)=>a-b);
  const left=82,right=width-74,top=18,bottom=height-54;
  const cw=(right-left)/xs.length,ch=(bottom-top)/ys.length;
  let lo=Infinity,hi=-Infinity;
  for(const cell of cells){ if(cell.value<lo)lo=cell.value;if(cell.value>hi)hi=cell.value; }
  for(const cell of cells){
    const xi=xs.indexOf(cell.x), yi=ys.indexOf(cell.y);
    const t=hi===lo?.5:(cell.value-lo)/(hi-lo);
    const x=left+xi*cw,y=bottom-(yi+1)*ch;
    g.fillStyle=heatColour(t);g.fillRect(x+1,y+1,Math.max(1,cw-2),Math.max(1,ch-2));
    if(cell.points.some(point=>point.settled===false)){
      g.strokeStyle="#f85149";g.lineWidth=2;g.strokeRect(x+2,y+2,cw-4,ch-4);
    }
    hitItems.push({x:x+cw/2,y:y+ch/2,rect:{x,y,w:cw,h:ch},point:cell.points[0],points:cell.points,
                   value:cell.value,series:`${label(xKey)} ${cell.x} · ${label(yKey)} ${cell.y}`});
  }
  g.font="10px system-ui";g.fillStyle="#8b949e";
  xs.forEach((value,i)=>{g.textAlign="center";g.fillText(fmt(value),left+(i+.5)*cw,bottom+15);});
  ys.forEach((value,i)=>{g.textAlign="right";g.fillText(fmt(value),left-7,bottom-(i+.5)*ch+3);});
  g.textAlign="center";g.fillText(label(xKey),left+(right-left)/2,height-13);
  g.save();g.translate(14,top+(bottom-top)/2);g.rotate(-Math.PI/2);g.fillText(label(yKey),0,0);g.restore();
  const grad=g.createLinearGradient(0,bottom,0,top);grad.addColorStop(0,heatColour(0));grad.addColorStop(1,heatColour(1));
  g.fillStyle=grad;g.fillRect(right+18,top,14,bottom-top);
  g.textAlign="left";g.fillText(fmt(hi),right+36,top+5);g.fillText(fmt(lo),right+36,bottom);
  legend([{name:`cell colour: ${label(metric)}`,colour:"#bc8cff"}]);
}

function heatColour(t){
  const hue=240-220*Math.max(0,Math.min(1,t));
  return `hsl(${hue} 75% 52%)`;
}

function legend(groups){
  const base=[
    {name:"recovered",colour:"#f0883e"},
    {name:"never settled (red ring)",colour:"#f85149"},
    {name:"inaccessible (×)",colour:"#ff7b72"},
  ];
  $("hcpesLegend").innerHTML=[...groups.slice(0,12),...base].map(row=>
    `<span><i style="background:${row.colour}"></i>${esc(row.name)}</span>`).join("");
}

function centre(g,width,height,message){
  g.fillStyle="#6b7683";g.font="12px system-ui";g.textAlign="center";g.textBaseline="middle";
  g.fillText(message,width/2,height/2);
}

function showHover(event){
  if(!hitItems.length) return hideHover();
  const canvas=$("hcpesCanvas"), rect=canvas.getBoundingClientRect();
  const x=event.clientX-rect.left,y=event.clientY-rect.top;
  let hit=hitItems.find(item=>item.rect && x>=item.rect.x && x<=item.rect.x+item.rect.w && y>=item.rect.y && y<=item.rect.y+item.rect.h);
  if(!hit) hit=hitItems.reduce((best,item)=>{
    const distance=(item.x-x)**2+(item.y-y)**2;
    return !best||distance<best.distance?{...item,distance}:best;
  },null);
  if(!hit || (!hit.rect && hit.distance>500)) return hideHover();
  const point=hit.point, metric=$("hcpesMetric").value;
  const text=[hit.series,`${label(metric)}: ${fmt(hit.value,7)}`,
    `Source: ${point.source_session || "—"} · point ${point.source_point || point.point_index}`,
    `Run order: ${fmt(point.acquisition_order,7)} · elapsed end: ${fmt(point.ended_elapsed_s,7)} s`,
    "CONDITIONS",...pointConditionLines(SOURCE,point),
    `OUTCOME: ${point.accessibility} · settled: ${point.settled}`,
    `Observed drift: ${fmt(displayValue("observed_drift_a_per_min",point.observed_drift_a_per_min),6)} mA/min`,
    ...(hit.points?.length > 1 ? [`Heat-map cell averages ${hit.points.length} matching conditions; filter other axes to inspect one.`] : []),
  ].join("\n");
  const hover=$("hcpesHover");hover.textContent=text;hover.classList.remove("hidden");
  hover.style.left=Math.max(4,Math.min(x+12,rect.width-440))+"px";hover.style.top=Math.max(4,y-20)+"px";
}

function hideHover(){ $("hcpesHover")?.classList.add("hidden"); }

async function loadRaw(){
  if(!SOURCE) return;
  const params=new URLSearchParams({name:SOURCE.name,session_id:$("hcpesRawSession").value,
                                    mode:$("hcpesRawMode").value});
  if($("hcpesRawPoint").value) params.set("point_index",$("hcpesRawPoint").value);
  try{
    const response=await fetch(`/api/hcpes/analysis/raw?${params}`);
    if(!response.ok) throw new Error((await response.json()).detail || "raw load failed");
    RAW=await response.json();renderRaw();
  }catch(error){toast(error.message||String(error));}
}

function clearRaw(){
  $("hcpesRawRows").innerHTML="";$("hcpesRawInfo").textContent="—";
  const {g,width,height}=canvasContext($("hcpesRawCanvas"));centre(g,width,height,"load raw intervals to inspect exclusions");
}

function renderRaw(){
  const rows=RAW.rows||[];
  $("hcpesRawInfo").textContent=`${RAW.matched} intervals${RAW.truncated?" · first 10,000 shown":""}`;
  $("hcpesRawRows").innerHTML=rows.slice(0,1500).map(row=>`<tr>
    <td>${esc(fmt(row.elapsed_s,7))}</td><td>${esc(row.point_index??"—")}</td>
    <td>${esc(row.phase)}</td><td>${esc(fmt(displayValue("stage_current_mean_a",rawCurrent(row)),7))}</td>
    <td>${esc(row.reason||"")}</td><td>${esc(row.exclusion_reason||"")}</td></tr>`).join("");
  const canvas=$("hcpesRawCanvas"),{g,width,height}=canvasContext(canvas);
  const plotted=rows.map(row=>({row,x:+row.elapsed_s,y:displayValue("stage_current_mean_a",rawCurrent(row))})).filter(row=>Number.isFinite(row.x)&&Number.isFinite(row.y));
  if(!plotted.length){centre(g,width,height,"no numeric stage-current samples in this selection");return;}
  const A=axes(g,width,height,extent(plotted.map(r=>r.x)),extent(plotted.map(r=>r.y)),"Elapsed time (s)","Stage current (mA)");
  g.strokeStyle="#8b949e";g.lineWidth=1;g.beginPath();
  plotted.forEach((item,index)=>{const x=A.px(item.x),y=A.py(item.y);index?g.lineTo(x,y):g.moveTo(x,y);});g.stroke();
  for(const item of plotted){g.fillStyle=PHASE_COLOURS[item.row.phase]||"#8b949e";g.beginPath();g.arc(A.px(item.x),A.py(item.y),3,0,Math.PI*2);g.fill();}
}

async function copyVisible(){
  if(!SOURCE) return;
  const points=filterPoints(SOURCE.points||[],FILTERS),metric=$("hcpesMetric").value,x=$("hcpesX").value;
  const lines=[["source_session","source_point",label(x),label(metric),"settled","accessibility"].join("\t")];
  for(const point of points) lines.push([point.source_session,point.source_point,
    displayValue(x,metricValue(point,x)),displayValue(metric,metricValue(point,metric)),
    point.settled,point.accessibility].map(value=>value??"").join("\t"));
  try{await navigator.clipboard.writeText(lines.join("\n"));toast(`Copied ${points.length} points`,true);}
  catch(_){toast("Could not copy visible data.");}
}

function wire(){
  document.querySelectorAll(".analysis-tab").forEach(button=>
    button.addEventListener("click",()=>activateTab(button.dataset.analysisTab)));
  $("hcpesRescan").onclick=rescan;$("hcpesLoad").onclick=loadSource;
  $("hcpesView").onchange=()=>{updateViewControls();draw();};
  for(const id of ["hcpesX","hcpesHeatY","hcpesMetric"]) $(id).onchange=draw;
  $("hcpesCanvas").addEventListener("mousemove",showHover);
  $("hcpesCanvas").addEventListener("mouseleave",hideHover);
  $("hcpesRawSession").onchange=configureRawPoints;$("hcpesRawLoad").onclick=loadRaw;
  $("hcpesCopy").onclick=copyVisible;
  $("hcpesPng").onclick=()=>{
    $("hcpesCanvas").toBlob(blob=>{
      if(!blob)return;const a=document.createElement("a");a.href=URL.createObjectURL(blob);
      a.download=`${(SOURCE?.title||"hcpes").replace(/[^\w.-]+/g,"_")}.png`;a.click();
      setTimeout(()=>URL.revokeObjectURL(a.href),3000);
    });
  };
  let timer=null;window.addEventListener("resize",()=>{clearTimeout(timer);timer=setTimeout(()=>{draw();if(RAW)renderRaw();},80);});
  let saved="run";try{saved=localStorage.getItem("reactorAnalysis.activeTab")||"run";}catch(_){}
  activateTab(saved === "hcpes" ? "hcpes" : "run");
  rescan();
}

if(hasDom && $("hcpesAnalysisPane")) wire();
