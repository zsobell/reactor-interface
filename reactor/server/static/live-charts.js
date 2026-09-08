/** Live chart rendering and interaction, independent of control commands. */
export function createLiveCharts({$, trend, num, clock, sci, fmtCurrent,
                                  currentUnit, esc, setHtml}) {
// ---------------------------------------------------------------- chart
// Three independent charts (run monitor, MFC flow, temperatures), each with
// its own time window, hover, and drag-to-zoom - a ChartCtl per chart holds
// exactly the state that used to be the single set of `view`/`hover`/
// `zoomSel`/`GEOM` globals. Interaction (mouse + keyboard) is wired once,
// generically, in chartInteraction() below and dispatches to whichever
// chart the cursor is over.
let MARKS = [];
let RUN_PLASMA = "plasma_ground";
let MFC_IDS = [];        // populated from live state each frame
let MFC_LABELS = {};     // id -> current display label (tracks renames)
let CUR_UNIT = {mul:1e6, unit:"µA", dp:2};         // current panel's display unit

function makeChartCtl(defaultWindowS){
  return {
    view: { follow:true, windowS:defaultWindowS, endT:null },
    hover: { active:false, px:null },
    zoomSel: { active:false, x0:null, x1:null },
    geom: null,          // last-drawn geometry, for hover/zoom hit-testing
  };
}
const RUN_CHART  = makeChartCtl(300);
const MFC_CHART  = makeChartCtl(120);
const TEMP_CHART = makeChartCtl(600);

const PADL = 60, PADR = 22;
function visibleRangeFor(ctl){
  const latest = trend.length ? trend[trend.length-1].t : Date.now()/1000;
  const end = ctl.view.follow ? latest : (ctl.view.endT ?? latest);
  return { t0: end - ctl.view.windowS, t1: end };
}

function drawChart(){
  const cv = $("chart"), dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if(w < 2 || h < 2) return;      // pane hidden: a hidden canvas has no size
  cv.width = w*dpr; cv.height = h*dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,w,h);

  const ctl = RUN_CHART;
  const pad = {t:12, b:20, gap:26};
  const {t0,t1} = visibleRangeFor(ctl), span = Math.max(1, t1-t0);
  const W = w - PADL - PADR;
  const panelH = (h - pad.t - pad.b - pad.gap) / 2;
  const topY = pad.t, botY = pad.t + panelH + pad.gap;
  const X = t => PADL + W*(t-t0)/span;
  const pts = trend.filter(r => r.t >= t0-2 && r.t <= t1+2);

  // One unit for the whole current panel, chosen from the largest value in
  // view so the axis doesn't flip between mA and µA as the trace moves.
  let peak = 0;
  for(const r of pts){
    const v = r.current;
    if(typeof v === "number" && Number.isFinite(v)) peak = Math.max(peak, Math.abs(v));
  }
  CUR_UNIT = currentUnit(peak);

  // Smoothing draws the averaged trace but keeps the axis on the raw spread, so
  // the noise that was removed still occupies its share of the panel instead of
  // the remainder being stretched to fill it.
  const sm = smoothing();
  const pPanel = drawPanel(g,
    sm.on ? `PRESSURE (Torr) — smoothed ${sm.n}pt, raw scale` : "PRESSURE (Torr)",
    PADL, topY, W, panelH, sm.on ? smoothSeries(pts, "pressure", sm.n) : pts,
    X, true, [{key:"pressure", color:"#58a6ff"}], 1, pts);
  const cPanel = drawPanel(g, `SAMPLE CURRENT (${CUR_UNIT.unit})`, PADL, botY, W,
    panelH, pts, X, false, [{key:"current", color:"#3fb950"}], CUR_UNIT.mul);

  ctl.geom = { X, invX: px => t0 + (px - PADL)/W*span, PADL, W, t0, t1,
               top: topY, bottom: botY + panelH, pts, panels: [pPanel, cPanel] };

  // plasma relay flips on the current panel. A reignite is two valve commands
  // (pulse ON to interrupt, then OFF to restrike) - only the restrike, the one
  // that actually turns the beam back on, is worth flagging; the interrupt
  // pulse is drawn as nothing so one retry attempt isn't shown twice.
  for(const m of MARKS){
    if(m.id !== RUN_PLASMA || m.t < t0 || m.t > t1) continue;
    const isReignite = /reignit/i.test(m.reason||"");
    if(isReignite && m.state !== false) continue;   // skip the interrupt pulse
    const x = X(m.t), recovery = isReignite;
    g.save();
    g.strokeStyle = recovery ? "#f85149" : "#8b949e";
    g.globalAlpha = recovery ? 0.95 : 0.5;
    g.setLineDash(recovery ? [] : [3,3]); g.lineWidth = 1;
    g.beginPath(); g.moveTo(x,botY); g.lineTo(x,botY+panelH); g.stroke();
    g.restore();
  }

  g.fillStyle="#8b949e"; g.font="11px system-ui"; g.textAlign="center";
  g.fillText(clock(t0), PADL+26, h-4);
  g.fillText(clock(t1), PADL+W-26, h-4);

  drawOverlaysFor(g, w, h, ctl, s => [clock(s.t),
    "P " + sci(s.pressure, 3) + " Torr",
    "I " + (()=>{const ic=fmtCurrent(s.current); return ic.text+" "+ic.unit;})()]);

  setHtml($("legend"),
    `<span><i style="background:#58a6ff"></i>Chamber pressure</span>`
   +`<span><i style="background:#3fb950"></i>Sample current</span>`
   +`<span><i style="background:#8b949e"></i>plasma scheduled flip</span>`
   +`<span><i style="background:#f85149"></i>plasma reignite</span>`
   +(ctl.view.follow ? "" : `<span style="color:var(--warn)">paused — press Follow live</span>`));
}

/* Display-only noise reduction on the chamber-pressure trace. Centered so the
   trace doesn't lag the live value, which a trailing average would; gaps (null
   readings) are left as gaps rather than averaged across. Cheap enough to run
   every frame on a few hundred visible points. */
function smoothing(){
  const n = Math.max(1, Math.round(parseFloat($("p_smooth_n").value) || 1));
  return {on: $("p_smooth_on").checked && n > 1, n};
}
function smoothSeries(pts, key, n){
  const half = Math.floor(n / 2);
  return pts.map((row, i) => {
    const v = row[key];
    if(typeof v !== "number" || !Number.isFinite(v)) return row;   // keep the gap
    let sum = 0, count = 0;
    for(let j = Math.max(0, i-half); j <= Math.min(pts.length-1, i+half); j++){
      const w = pts[j][key];
      if(typeof w === "number" && Number.isFinite(w)){ sum += w; count++; }
    }
    return count ? {...row, [key]: sum/count} : row;
  });
}

/** `mul` rescales a linear panel into a display unit (A -> mA/µA); the axis
    labels are drawn in projected space, so they follow automatically.
    `rangePts` scales the axis off a different set than the one drawn - used so
    smoothing cannot rescale the axis to its own narrower spread, which would
    magnify what's left into looking like a bigger trend than the data shows. */
function drawPanel(g, title, x0, y0, W, H, pts, X, log, series, mul=1, rangePts=null){
  g.strokeStyle="#2a323d"; g.lineWidth=1; g.strokeRect(x0,y0,W,H);
  g.fillStyle="#8b949e"; g.font="10px system-ui"; g.textAlign="left";
  g.fillText(title, x0+5, y0+11);
  const proj = v => log ? Math.log10(Math.max(v,1e-14)) : v*mul;
  let lo=Infinity, hi=-Infinity;
  for(const r of (rangePts || pts)) for(const s of series){
    const v=r[s.key];
    if(typeof v==="number" && Number.isFinite(v)){ const q=proj(v); if(q<lo)lo=q; if(q>hi)hi=q; }
  }
  if(lo===Infinity){ lo=log?-8:-1; hi=log?0:1; }
  if(hi-lo<1e-9){ hi=lo+1; lo-=1; }
  const mg=(hi-lo)*0.1; lo-=mg; hi+=mg;
  const Y = v => y0 + H*(1-(proj(v)-lo)/(hi-lo));
  g.textAlign="right";
  for(let i=0;i<=4;i++){
    const yy=y0+H*i/4, q=hi-(hi-lo)*i/4;
    g.strokeStyle="#1b2230"; g.beginPath(); g.moveTo(x0,yy); g.lineTo(x0+W,yy); g.stroke();
    g.fillStyle="#8b949e";
    g.fillText(log ? Math.pow(10,q).toExponential(1)
      : (Math.abs(q)<1e-3 && q!==0 ? q.toExponential(1) : q.toPrecision(3)), x0-6, yy+3);
  }
  for(const s of series){
    g.strokeStyle=s.color; g.lineWidth=1.6; g.beginPath(); let drawing=false;
    for(const r of pts){
      const v=r[s.key];
      if(typeof v!=="number"||!Number.isFinite(v)){ drawing=false; continue; }
      const px=X(r.t), py=Y(v);
      drawing ? g.lineTo(px,py) : g.moveTo(px,py); drawing=true;
    }
    g.stroke();
  }
  return { y0, H, Y, series };   // for the hover readout
}

// --- MFC flow strip: own time window, hover, and drag-to-zoom, same as the
// run monitor. ---
const MFC_COLORS = {ar: "#58a6ff", h2: "#3fb950", n2: "#d29922"};
const MFC_FALLBACK_COLORS = ["#a371f7", "#f85149", "#56d4dc"];
function drawMfcChart(){
  const cv = $("mfcChart");
  if(!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if(w < 2 || h < 2) return;      // pane hidden
  cv.width = w*dpr; cv.height = h*dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,w,h);

  const ctl = MFC_CHART;
  const {t0,t1} = visibleRangeFor(ctl), span = Math.max(1, t1-t0);
  const W = w - PADL - PADR;
  const panelH = h - 24;
  const X = t => PADL + W*(t-t0)/span;
  const pts = trend.filter(r => r.t >= t0-2 && r.t <= t1+2);

  const ids = MFC_IDS.length ? MFC_IDS : Object.keys(MFC_COLORS);
  const series = ids.map((id,i) => ({
    key: "mfc_"+id,
    color: MFC_COLORS[id] || MFC_FALLBACK_COLORS[i % MFC_FALLBACK_COLORS.length],
  }));
  const panel = drawPanel(g, "FLOW (sccm)", PADL, 4, W, panelH, pts, X, false, series);

  ctl.geom = { X, invX: px => t0 + (px - PADL)/W*span, PADL, W, t0, t1,
               top: 4, bottom: 4 + panelH, pts, panels: [panel] };

  g.fillStyle="#8b949e"; g.font="11px system-ui"; g.textAlign="center";
  g.fillText(clock(t0), PADL+26, h-4);
  g.fillText(clock(t1), PADL+W-26, h-4);

  drawOverlaysFor(g, w, h, ctl, s => [clock(s.t), ...series.map(ser =>
    `${(MFC_LABELS[ser.key.slice(4)]||ser.key).slice(0,14)} `
    + (typeof s[ser.key]==="number" ? num(s[ser.key],2) : "—") + " sccm")]);

  setHtml($("mfcLegend"), series.map(s =>
    `<span><i style="background:${s.color}"></i>${esc(MFC_LABELS[s.key.slice(4)]||s.key)}</span>`
  ).join("") + (ctl.view.follow ? "" : `<span style="color:var(--warn)">paused — press Follow live</span>`));
}

// --- temperatures: stage and precursor bubbler ---
// Separate panels rather than one shared axis - the two sit at different
// temperatures and a shared scale would flatten both. Own time window, hover,
// and drag-to-zoom, independent of the run monitor.
const TEMP_SERIES = [
  {key:"stage_temp",   color:"#f0883e", title:"STAGE (°C)"},
  {key:"bubbler_temp", color:"#a371f7", title:"PRECURSOR BUBBLER (°C)"},
];
function drawTempChart(){
  const cv = $("tempChart");
  if(!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if(w < 2 || h < 2) return;      // pane hidden
  cv.width = w*dpr; cv.height = h*dpr;
  const g = cv.getContext("2d");
  g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,w,h);

  const ctl = TEMP_CHART;
  const pad = {t:6, b:20, gap:22};
  const {t0,t1} = visibleRangeFor(ctl), span = Math.max(1, t1-t0);
  const W = w - PADL - PADR;
  const panelH = (h - pad.t - pad.b - pad.gap) / 2;
  const X = t => PADL + W*(t-t0)/span;
  const pts = trend.filter(r => r.t >= t0-2 && r.t <= t1+2);

  const panels = TEMP_SERIES.map((s, i) => {
    const y0 = pad.t + i*(panelH + pad.gap);
    return drawPanel(g, s.title, PADL, y0, W, panelH, pts, X, false,
                      [{key:s.key, color:s.color}]);
  });

  ctl.geom = { X, invX: px => t0 + (px - PADL)/W*span, PADL, W, t0, t1,
               top: pad.t, bottom: pad.t + panels.length*panelH + (panels.length-1)*pad.gap,
               pts, panels };

  g.fillStyle="#8b949e"; g.font="11px system-ui"; g.textAlign="center";
  g.fillText(clock(t0), PADL+26, h-4);
  g.fillText(clock(t1), PADL+W-26, h-4);

  drawOverlaysFor(g, w, h, ctl, s => [clock(s.t), ...TEMP_SERIES.map(ser =>
    `${ser.title.replace(" (°C)","")} ${num(s[ser.key],1)} °C`)]);

  const last = trend.length ? trend[trend.length-1] : {};
  setHtml($("tempLegend"), TEMP_SERIES.map(s =>
    `<span><i style="background:${s.color}"></i>${esc(s.title.replace(" (°C)",""))}`
    + ` ${num(last[s.key], 1)} °C</span>`).join("")
    + (ctl.view.follow ? "" : `<span style="color:var(--warn)">paused — press Follow live</span>`));
}

// --- hover readout + drag-to-zoom band, generic across all 3 charts ---
function nearestSampleIn(pts, t){
  if(!pts || !pts.length) return null;
  let best = pts[0], bd = Math.abs(pts[0].t - t);
  for(const p of pts){ const d = Math.abs(p.t - t); if(d < bd){ bd = d; best = p; } }
  return best;
}
/** `lineBuilder(sample)` returns the hover-box text lines for that chart. */
function drawOverlaysFor(g, w, h, ctl, lineBuilder){
  const GEOM = ctl.geom;
  if(!GEOM) return;
  const {zoomSel, hover} = ctl;

  // drag-to-zoom band takes precedence over the hover readout
  if(zoomSel.active && zoomSel.x0 != null && zoomSel.x1 != null
     && Math.abs(zoomSel.x1 - zoomSel.x0) > 2){
    const a = Math.max(GEOM.PADL, Math.min(zoomSel.x0, zoomSel.x1));
    const b = Math.min(GEOM.PADL + GEOM.W, Math.max(zoomSel.x0, zoomSel.x1));
    g.save();
    g.fillStyle = "rgba(88,166,255,0.16)";
    g.fillRect(a, GEOM.top, b - a, GEOM.bottom - GEOM.top);
    g.strokeStyle = "#58a6ff"; g.lineWidth = 1;
    g.strokeRect(a, GEOM.top, b - a, GEOM.bottom - GEOM.top);
    g.restore();
    return;
  }

  if(!hover.active || hover.px == null
     || hover.px < GEOM.PADL || hover.px > GEOM.PADL + GEOM.W) return;
  const s = nearestSampleIn(GEOM.pts, GEOM.invX(hover.px));
  if(!s) return;
  const x = GEOM.X(s.t);

  g.save();
  g.strokeStyle = "#8b949e"; g.globalAlpha = 0.6; g.setLineDash([4,3]); g.lineWidth = 1;
  g.beginPath(); g.moveTo(x, GEOM.top); g.lineTo(x, GEOM.bottom); g.stroke();
  g.restore();

  for(const p of GEOM.panels) for(const ser of p.series){
    const v = s[ser.key];
    if(typeof v === "number" && Number.isFinite(v)){
      g.save(); g.fillStyle = ser.color;
      g.beginPath(); g.arc(x, p.Y(v), 3.5, 0, Math.PI*2); g.fill(); g.restore();
    }
  }

  const lines = lineBuilder(s);
  g.font = "11px system-ui"; g.textBaseline = "top";
  const padB = 6, lh = 14;
  const bw = Math.max(...lines.map(l => g.measureText(l).width)) + padB*2;
  const bh = lines.length*lh + padB*2;
  let bx = x + 10; if(bx + bw > w - 4) bx = x - 10 - bw;
  const by = GEOM.top + 4;
  g.save();
  g.fillStyle = "rgba(13,17,23,0.92)"; g.strokeStyle = "#2a323d";
  g.fillRect(bx, by, bw, bh); g.strokeRect(bx, by, bw, bh);
  g.fillStyle = "#e6edf3"; g.textAlign = "left";
  lines.forEach((l,i) => g.fillText(l, bx + padB, by + padB + i*lh));
  g.restore();
}

function drawAllCharts(){ drawChart(); drawMfcChart(); drawTempChart(); }

// Drag across a plot to zoom to that time range; arrow keys pan/zoom while the
// cursor is over a chart; hover shows the value under the cursor. No wheel
// handler — scrolling the page must never be hijacked by a chart. Each chart
// is wired independently via registerChart(); mouseup and keydown are single
// window-level listeners that act on whichever chart is mid-drag or hovered.
const CHARTS = [];   // {ctl, redraw}, in registration order

function registerChart(canvasId, ctl, redraw){
  const cv = $(canvasId);
  const cssX = e => e.clientX - cv.getBoundingClientRect().left;
  CHARTS.push({ctl, redraw});

  cv.addEventListener("mouseenter", () => { ctl.hover.active = true; });
  cv.addEventListener("mouseleave", () => {
    ctl.hover.active = false; ctl.hover.px = null;
    if(!ctl.zoomSel.active) redraw();
  });
  cv.addEventListener("mousemove", e => {
    ctl.hover.px = cssX(e);
    if(ctl.zoomSel.active) ctl.zoomSel.x1 = ctl.hover.px;
    redraw();
  });
  cv.addEventListener("mousedown", e => {
    e.preventDefault();
    ctl.zoomSel.active = true; ctl.zoomSel.x0 = cssX(e); ctl.zoomSel.x1 = ctl.zoomSel.x0;
  });
}

window.addEventListener("mouseup", () => {
  for(const {ctl, redraw} of CHARTS){
    if(!ctl.zoomSel.active) continue;
    ctl.zoomSel.active = false;
    const a = Math.min(ctl.zoomSel.x0, ctl.zoomSel.x1);
    const b = Math.max(ctl.zoomSel.x0, ctl.zoomSel.x1);
    ctl.zoomSel.x0 = ctl.zoomSel.x1 = null;
    if(ctl.geom && b - a > 5){                    // a real drag -> zoom to it
      const nt0 = ctl.geom.invX(a), nt1 = ctl.geom.invX(b);
      ctl.view.windowS = Math.max(1, nt1 - nt0);
      ctl.view.endT = nt1; ctl.view.follow = false;
    }
    redraw();
  }
});
window.addEventListener("keydown", e => {
  const active = CHARTS.find(c => c.ctl.hover.active);
  if(!active) return;
  const {ctl, redraw} = active;
  const {t0,t1} = visibleRangeFor(ctl), span = t1 - t0;
  let handled = true;
  if(e.key === "ArrowLeft"){ ctl.view.follow = false; ctl.view.endT = (ctl.view.endT ?? t1) - span*0.2; }
  else if(e.key === "ArrowRight"){ ctl.view.follow = false; ctl.view.endT = (ctl.view.endT ?? t1) + span*0.2; }
  else if(e.key === "ArrowUp"){ ctl.view.follow = false; ctl.view.endT = ctl.view.endT ?? t1; ctl.view.windowS = Math.max(1, span*0.8); }
  else if(e.key === "ArrowDown"){ ctl.view.follow = false; ctl.view.endT = ctl.view.endT ?? t1; ctl.view.windowS = Math.min(3600, span*1.25); }
  else handled = false;
  if(handled){ e.preventDefault(); redraw(); }
});

/** Wires a chart's Follow-live button and window-size <select>. */
function wireChartControls(followId, selId, ctl, redraw){
  $(followId).onclick = () => { ctl.view.follow = true; redraw(); };
  $(selId).onchange = e => { ctl.view.windowS = +e.target.value; ctl.view.follow = true; redraw(); };
}

registerChart("chart", RUN_CHART, drawChart);
registerChart("mfcChart", MFC_CHART, drawMfcChart);
registerChart("tempChart", TEMP_CHART, drawTempChart);
wireChartControls("followBtn", "windowSel", RUN_CHART, drawChart);
wireChartControls("followBtnMfc", "windowSelMfc", MFC_CHART, drawMfcChart);
wireChartControls("followBtnTemp", "windowSelTemp", TEMP_CHART, drawTempChart);

window.addEventListener("resize", drawAllCharts);


return {
  drawAllCharts, drawChart, smoothing,
  update(s) {
    MARKS = s.marks || [];
    RUN_PLASMA = s.run_valves?.plasma || "plasma_ground";
    MFC_IDS = (s.mfcs || []).map(m => m.id);
    MFC_LABELS = Object.fromEntries((s.mfcs || []).map(m => [m.id, m.label || m.id]));
  },
};
}
