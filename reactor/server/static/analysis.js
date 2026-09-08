"use strict";

/* ===========================================================================
   State
   =========================================================================== */

const $ = id => document.getElementById(id);
const LS_KEY = "reactorAnalysis.v1";

/** The loaded file: {name, n, header[], columns{name->Float64Array},
    numeric[], binary:Set, text[]}  (text[] = non-numeric column names) */
let DATA = null;

/** Dropped Auger spectra, key -> dataset of the SAME shape as DATA, so every
    drawing and export path below works on one without knowing where it came
    from. A plot names its source in `p.auger` (a key here); no `p.auger` means
    it draws from the loaded CSV. */
const AUGER = new Map();

/** The dataset one plot draws from, or null when it is not loaded. */
function dsFor(p){
  return p.auger ? (AUGER.get(p.auger) || null) : DATA;
}

/** Persisted layout. `plots` entries are pure settings - no data, no DOM. */
let LAYOUT = {cols: 2, plotH: 260, settings: true, plots: []};

let nextId = 1;

/* ===========================================================================
   CSV
   =========================================================================== */

/** Full-quote-aware CSV split. The run/merge exports quote any field holding a
    comma (recipe_step does), so a naive split on "," corrupts every later
    column in those rows. */
function parseCsv(text){
  const out = [];
  let row = [], field = "", inQ = false;
  for(let i = 0; i < text.length; i++){
    const c = text[i];
    if(inQ){
      if(c === '"'){
        if(text[i+1] === '"'){ field += '"'; i++; } else inQ = false;
      } else field += c;
    } else if(c === '"'){ inQ = true; }
    else if(c === ','){ row.push(field); field = ""; }
    else if(c === '\n'){ row.push(field); out.push(row); row = []; field = ""; }
    else if(c !== '\r'){ field += c; }
  }
  if(field !== "" || row.length){ row.push(field); out.push(row); }
  return out;
}

/** Header + typed columns. A column counts as numeric if most of the cells that
    HAVE a value parse as numbers; blanks become NaN, and mean "not sampled
    here", never zero (a dropped reading is not a reading of 0).

    Judging against the ROW COUNT instead - the old rule - silently threw away
    every sparse column in a merged file, because in a merged file almost every
    column is sparse by construction. The reactor's slow channels are logged
    only on the ticks they were actually resampled (~50% of rows), and the
    ellipsometry columns only exist on ellipsometer rows (~17%), so thickness,
    rho, bubbler and stage temperature all fell under the 50%-of-all-rows bar
    and never reached the plot dropdown at all. Sparsity is not a reason to
    treat a column as text; unparseable content is. */
function buildDataset(name, text){
  const rows = parseCsv(text).filter(r => r.length > 1);
  if(rows.length < 2) throw new Error("no data rows — is this a CSV with a header?");
  const header = rows[0].map(h => h.trim()).filter(h => h !== "");
  const n = rows.length - 1;
  const columns = {}, numeric = [], textCols = [], binary = new Set();

  header.forEach((h, ci) => {
    const arr = new Float64Array(n);
    let good = 0, filled = 0, onlyBinary = true;
    for(let r = 0; r < n; r++){
      const cell = rows[r+1][ci];
      const blank = (cell === undefined || cell === "");
      const v = blank ? NaN : Number(cell);
      arr[r] = v;
      if(!blank) filled++;
      if(Number.isFinite(v)){ good++; if(v !== 0 && v !== 1) onlyBinary = false; }
    }
    if(good >= 2 && good > filled * 0.5){
      columns[h] = arr;
      numeric.push(h);
      if(onlyBinary) binary.add(h);
    } else {
      textCols.push(h);
    }
  });
  if(!numeric.length) throw new Error("no numeric columns found");
  return {name, n, header, columns, numeric, text: textCols, binary};
}

/* ===========================================================================
   Column naming
   =========================================================================== */

const PRETTY = {
  cycle_number: "Cycle number",
  recipe_cycle: "Cycle (whole)",
  elapsed_s: "Elapsed time (s)",
  reactor_elapsed_s: "Elapsed time (s)",
  pressure: "Chamber pressure (Torr)",
  inst_ammeter: "Sample current (A)",
  stage_temp: "Stage temperature (°C)",
  aux_bubbler: "Precursor bubbler (°C)",
  aux_tc_a: "Thermocouple A (°C)",
  aux_tc_b: "Thermocouple B (°C)",
  gauge_ar_baratron: "Ar Baratron (Torr)",
  gauge_prec1_dose: "Precursor 1 dose pressure (Torr)",
  gauge_prec2_dose: "Precursor 2 dose pressure (Torr)",
  mfc_ar: "Ar flow (sccm)",
  mfc_h2: "H2 flow (sccm)",
  mfc_n2: "N2 flow (sccm)",
  beam_on: "Beam on (0/1)",
  dosing: "Dosing (0/1)",
  paused: "Paused (0/1)",          // only in run exports written before 2026-08-21
  recipe_step: "Recipe step",
  energy_ev: "Kinetic energy (eV)",
  intensity: "Intensity (a.u.)",
  hv_hv_voltage: "HV supply voltage (V)",
  hv_hv_current: "HV supply current (mA)",
  hv_hv_arcs: "HV arc count",
  fit_diff: "Fit difference",
};
function pretty(col){
  if(!col) return "";
  if(PRETTY[col]) return PRETTY[col];
  const m = /^Thick\(([^)]*)\)/.exec(col);        // FS-1: "Thick(A).1"
  if(m) return `Thickness (${m[1] === "A" ? "Å" : m[1]})`;
  // Whatever else the refit carried. A model with a Drude layer adds
  // "rho(uOhm*cm)"; others add n/k or per-layer thicknesses. They need no
  // special handling beyond a readable label - being numeric columns they
  // already reach the y-axis dropdown like any other.
  const r = /^rho\(([^)]*)\)/.exec(col);
  if(r) return `Resistivity (${r[1].replace("uOhm", "µΩ").replace("*", "·")})`;
  return col;
}

/** Columns worth defaulting a fresh plot to, best first. */
const SEED_Y = ["Thick(A).1", "Thick(nm).1", "pressure", "inst_ammeter",
                "gauge_prec1_dose", "stage_temp", "aux_bubbler"];
function seedY(used){
  if(!DATA) return "";
  const thick = DATA.numeric.find(c => c.startsWith("Thick"));
  const pref = [thick, ...SEED_Y].filter(Boolean);
  return pref.find(c => DATA.columns[c] && !used.has(c))
      || DATA.numeric.find(c => !used.has(c) && !isXish(c))
      || DATA.numeric[0] || "";
}
function isXish(c){
  return ["cycle_number","recipe_cycle","elapsed_s","reactor_elapsed_s"].includes(c);
}
/** Preferred x column: cycles if the file has them, else elapsed time. */
function seedX(){
  if(!DATA) return "cycle_number";
  return ["cycle_number","reactor_elapsed_s","elapsed_s"]
           .find(c => DATA.columns[c]) || DATA.numeric[0];
}

/* ===========================================================================
   Persistence
   =========================================================================== */

function saveLayout(){
  LAYOUT.cols = +$("cols").value;
  LAYOUT.plotH = +$("plotH").value;
  LAYOUT.settings = $("showSettings").checked;
  try{ localStorage.setItem(LS_KEY, JSON.stringify(LAYOUT)); }catch(_){}
}
function loadLayout(){
  try{
    const raw = localStorage.getItem(LS_KEY);
    if(raw){
      const l = JSON.parse(raw);
      if(l && Array.isArray(l.plots)) LAYOUT = l;
    }
  }catch(_){}
  LAYOUT.cols = LAYOUT.cols || 2;
  LAYOUT.plotH = LAYOUT.plotH || 260;
  LAYOUT.settings = LAYOUT.settings !== false;
  for(const p of LAYOUT.plots) p.id = nextId++;
  $("cols").value = LAYOUT.cols;
  $("plotH").value = LAYOUT.plotH;
  $("showSettings").checked = LAYOUT.settings;
  applyChrome();
}
function applyChrome(){
  $("grid").style.setProperty("--cols", $("cols").value);
  $("grid").style.setProperty("--ph", $("plotH").value + "px");
  document.querySelectorAll(".settings")
    .forEach(el => el.classList.toggle("hidden", !$("showSettings").checked));
}

function newPlot(){
  const used = new Set(LAYOUT.plots.filter(p => !p.auger).map(p => p.y1).filter(Boolean));
  return {
    id: nextId++, title: "",
    x: seedX(), xmin: "", xmax: "",
    y1: seedY(used), y1min: "", y1max: "", y1log: false,
    y2: "", y2min: "", y2max: "", y2log: false,
  };
}

/* ===========================================================================
   Building one plot card
   =========================================================================== */

function esc(s){
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function options(ds, selected, blank){
  const cols = ds ? ds.numeric : [];
  let html = blank ? `<option value="">${esc(blank)}</option>` : "";
  for(const c of cols)
    html += `<option value="${esc(c)}"${c === selected ? " selected" : ""}>${esc(pretty(c))}</option>`;
  // A column the current file lacks stays selected and visible, so a layout
  // built on a richer file says what is missing instead of silently resetting.
  if(selected && !cols.includes(selected))
    html += `<option value="${esc(selected)}" selected>${esc(pretty(selected))} — not in this file</option>`;
  return html;
}

function axisRow(p, key, label, colour, withLog){
  const sel = p[key];
  return `
    <div class="axis">
      <label>${key === "x" ? "X" : `<span class="sw" style="background:${colour}"></span> ${label}`}</label>
      <select data-f="${key}">${options(dsFor(p), sel, key === "y2" ? "— none —" : null)}</select>
      <input data-f="${key}min" placeholder="min" value="${esc(p[key + "min"])}">
      <input data-f="${key}max" placeholder="max" value="${esc(p[key + "max"])}">
      ${withLog
        ? `<label class="lg"><input type="checkbox" data-f="${key}log" ${p[key + "log"] ? "checked" : ""}>log</label>`
        : `<span></span>`}
    </div>`;
}

function plotCard(p){
  const el = document.createElement("section");
  el.className = "plot";
  el.dataset.id = p.id;
  el.innerHTML = `
    <div class="phead">
      <input class="title" data-f="title" placeholder="${esc(autoTitle(p))}"
             value="${esc(p.title)}">
      <button class="mini" data-act="png" title="Save this plot as a PNG">PNG</button>
      <button class="mini" data-act="csv" title="Save this plot's data as a CSV">CSV</button>
      <button class="mini" data-act="dup" title="Duplicate">⧉</button>
      <button class="mini" data-act="del" title="Remove">✕</button>
    </div>
    <div class="canvasbox"><canvas></canvas></div>
    <div class="warn"></div>
    <div class="settings">
      ${axisRow(p, "y1", p.auger ? "Y" : "Y1", "var(--y1)", true)}
      ${p.auger ? "" : axisRow(p, "y2", "Y2", "var(--y2)", true)}
      ${axisRow(p, "x", "X", "", false)}
    </div>`;
  el.querySelector(".settings").classList.toggle("hidden", !$("showSettings").checked);

  el.addEventListener("input", ev => {
    const f = ev.target.dataset.f;
    if(!f) return;
    p[f] = ev.target.type === "checkbox" ? ev.target.checked : ev.target.value;
    if(f === "y1" || f === "y2" || f === "x")
      el.querySelector(".title").placeholder = autoTitle(p);
    saveLayout();
    drawPlot(p, el);
  });
  el.addEventListener("click", ev => {
    const act = ev.target.closest("button[data-act]")?.dataset.act;
    if(act === "del"){
      LAYOUT.plots = LAYOUT.plots.filter(q => q.id !== p.id);
      // Losing its last plot drops the spectrum too, so a removed Auger file
      // does not linger invisibly in storage.
      if(p.auger && !LAYOUT.plots.some(q => q.auger === p.auger)){
        AUGER.delete(p.auger); saveAuger(); updateAugerInfo();
      }
      saveLayout(); renderGrid();
    } else if(act === "dup"){
      const copy = {...p, id: nextId++};
      LAYOUT.plots.splice(LAYOUT.plots.indexOf(p) + 1, 0, copy);
      saveLayout(); renderGrid();
    } else if(act === "png"){
      exportPng(p, el);
    } else if(act === "csv"){
      exportCsv(p);
    }
  });

  const cv = el.querySelector("canvas");
  cv.addEventListener("mousemove", ev => {
    const r = cv.getBoundingClientRect();
    cv._hover = {x: ev.clientX - r.left, y: ev.clientY - r.top};
    drawPlot(p, el);
  });
  cv.addEventListener("mouseleave", () => { cv._hover = null; drawPlot(p, el); });
  return el;
}

function autoTitle(p){
  // An Auger plot is named after its file: which spectrum it is matters more
  // than which columns it draws (there are only ever the two).
  if(p.auger) return `Auger · ${(AUGER.get(p.auger) || {}).name || p.auger}`;
  const ys = [pretty(p.y1), p.y2 ? pretty(p.y2) : ""].filter(Boolean).join(" · ");
  return ys ? `${ys}  vs  ${pretty(p.x)}` : "(pick a Y column)";
}

function renderGrid(){
  const grid = $("grid");
  grid.innerHTML = "";
  for(const p of LAYOUT.plots) grid.appendChild(plotCard(p));
  $("emptyMsg").classList.toggle("hidden", LAYOUT.plots.length > 0);
  applyChrome();
  drawAll();
}
function drawAll(){
  for(const p of LAYOUT.plots){
    const el = $("grid").querySelector(`.plot[data-id="${p.id}"]`);
    if(el) drawPlot(p, el);
  }
}

/* ===========================================================================
   Drawing
   =========================================================================== */

const num = v => (v === "" || v === null || v === undefined) ? null : (Number.isFinite(+v) ? +v : null);

/** Points for one series, filtered to the x window. Returns parallel arrays so
    a gap (NaN) stays a gap. */
function collect(ds, p, ykey){
  const xs = ds.columns[p.x], ys = ds.columns[ykey];
  if(!xs || !ys) return null;
  const lo = num(p.xmin), hi = num(p.xmax);
  const X = [], Y = [];
  for(let i = 0; i < ds.n; i++){
    const x = xs[i];
    if(!Number.isFinite(x)) continue;
    if(lo !== null && x < lo) continue;
    if(hi !== null && x > hi) continue;
    X.push(x); Y.push(ys[i]);
  }
  return {X, Y};
}

function extent(vals, log){
  let lo = Infinity, hi = -Infinity;
  for(const v of vals){
    if(!Number.isFinite(v)) continue;
    if(log && v <= 0) continue;
    if(v < lo) lo = v;
    if(v > hi) hi = v;
  }
  return lo === Infinity ? null : [lo, hi];
}

/** Resolve an axis range: explicit min/max win, blanks auto-fit with padding. */
function axisRange(p, key, vals, log){
  const explicit = [num(p[key + "min"]), num(p[key + "max"])];
  let [lo, hi] = extent(vals, log) || (log ? [1e-9, 1] : [0, 1]);
  if(log){ lo = Math.log10(lo); hi = Math.log10(hi); }
  if(hi - lo < 1e-12){ hi = lo + (Math.abs(lo) || 1) * 0.05; lo -= (Math.abs(lo) || 1) * 0.05; }
  const pad = (hi - lo) * 0.06;
  lo -= pad; hi += pad;
  if(explicit[0] !== null) lo = log ? Math.log10(Math.max(explicit[0], 1e-300)) : explicit[0];
  if(explicit[1] !== null) hi = log ? Math.log10(Math.max(explicit[1], 1e-300)) : explicit[1];
  if(!(hi > lo)) hi = lo + 1;
  return [lo, hi];
}

function fmtTick(v, log){
  const x = log ? Math.pow(10, v) : v;
  const a = Math.abs(x);
  if(x === 0) return "0";
  if(a < 1e-3 || a >= 1e5) return x.toExponential(1);
  return String(+x.toPrecision(4));
}

function drawPlot(p, el){
  const cv = el.querySelector("canvas");
  const warnEl = el.querySelector(".warn");
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if(w < 2 || h < 2) return;
  cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#0d1117"; g.fillRect(0, 0, w, h);   // opaque, so PNG export works

  const D = dsFor(p);
  const warns = [];
  if(!D){
    centre(g, w, h, p.auger ? "spectrum not loaded — drop the file again"
                            : "load a data file");
    warnEl.textContent = p.auger ? `Auger spectrum "${p.auger}" is not loaded` : "";
    el.classList.toggle("missing", !!p.auger);
    return;
  }
  for(const [k, label] of [["x","X"],["y1","Y1"],["y2","Y2"]]){
    if(p[k] && !D.columns[p[k]]) warns.push(`${label} column "${p[k]}" is not in this file`);
  }
  el.classList.toggle("missing", warns.length > 0);
  warnEl.textContent = warns.join(" · ");
  if(!p.y1 || !D.columns[p.y1] || !D.columns[p.x]){
    centre(g, w, h, warns.length ? "column missing — pick another" : "pick a Y column");
    return;
  }

  const s1 = collect(D, p, p.y1);
  const s2 = (p.y2 && D.columns[p.y2]) ? collect(D, p, p.y2) : null;
  if(!s1.X.length){ centre(g, w, h, "no points in this X range"); return; }

  const padL = 62, padR = s2 ? 62 : 16, padT = 12, padB = 30;
  const W = w - padL - padR, H = h - padT - padB;
  if(W < 30 || H < 30) return;

  const [x0, x1] = axisRange(p, "x", s1.X, false);
  const [a0, a1] = axisRange(p, "y1", s1.Y, p.y1log);
  const [b0, b1] = s2 ? axisRange(p, "y2", s2.Y, p.y2log) : [0, 1];

  const PX = v => padL + W * (v - x0) / (x1 - x0);
  const mk = (lo, hi, log) => v => {
    const q = log ? (v > 0 ? Math.log10(v) : NaN) : v;
    return padT + H * (1 - (q - lo) / (hi - lo));
  };
  const PY1 = mk(a0, a1, p.y1log), PY2 = mk(b0, b1, p.y2log);

  // frame + horizontal grid, labelled left (Y1) and right (Y2)
  g.strokeStyle = "#2a323d"; g.lineWidth = 1;
  g.strokeRect(padL, padT, W, H);
  g.font = "10px system-ui"; g.textBaseline = "middle";
  for(let i = 0; i <= 4; i++){
    const y = padT + H * i / 4;
    g.strokeStyle = "#1b2230";
    g.beginPath(); g.moveTo(padL, y); g.lineTo(padL + W, y); g.stroke();
    g.textAlign = "right"; g.fillStyle = "#7d8894";
    g.fillText(fmtTick(a1 - (a1 - a0) * i / 4, p.y1log), padL - 6, y);
    if(s2){
      g.textAlign = "left"; g.fillStyle = "#a8724a";
      g.fillText(fmtTick(b1 - (b1 - b0) * i / 4, p.y2log), padL + W + 6, y);
    }
  }
  g.textAlign = "center"; g.textBaseline = "top"; g.fillStyle = "#7d8894";
  for(let i = 0; i <= 5; i++){
    const v = x0 + (x1 - x0) * i / 5, x = PX(v);
    if(i > 0 && i < 5){
      g.strokeStyle = "#161d29";
      g.beginPath(); g.moveTo(x, padT); g.lineTo(x, padT + H); g.stroke();
    }
    g.fillText(fmtTick(v, false), x, padT + H + 6);
  }
  g.fillStyle = "#7d8894"; g.textAlign = "center";
  g.fillText(pretty(p.x), padL + W / 2, h - 12);

  // series. A 0/1 column (beam_on, dosing) is a state, not a measurement, so
  // it is drawn as a step - a straight interpolation between samples would
  // imply the valve was half open.
  //
  // Every datum joins to the one before and after it: a row with no value for
  // THIS column is skipped, not treated as a break in the trace. Breaking on it
  // is what made merged files render as dashes - the union-of-instants layout
  // means one column's samples are separated by rows belonging to other
  // columns, so a channel sampled perfectly steadily came out as isolated stubs.
  const clip = () => { g.save(); g.beginPath(); g.rect(padL, padT, W, H); g.clip(); };
  const line = (s, PY, colour, step) => {
    clip();
    g.strokeStyle = colour; g.lineWidth = 1.6; g.beginPath();
    let drawing = false, px = 0, py = 0;
    for(let i = 0; i < s.X.length; i++){
      const v = s.Y[i];
      if(!Number.isFinite(v)) continue;         // not sampled here - skip, don't break
      const X = PX(s.X[i]), Y = PY(v);
      if(!Number.isFinite(Y)) continue;         // off a log axis (v <= 0)
      if(!drawing){ g.moveTo(X, Y); }
      else if(step){ g.lineTo(X, py); g.lineTo(X, Y); }
      else { g.lineTo(X, Y); }
      drawing = true; px = X; py = Y;
    }
    g.stroke(); g.restore();
  };
  line(s1, PY1, "#58a6ff", D.binary.has(p.y1));
  if(s2) line(s2, PY2, "#f0883e", D.binary.has(p.y2));

  // legend
  g.font = "11px system-ui"; g.textAlign = "left"; g.textBaseline = "top";
  let lx = padL + 6;
  for(const [col, colour] of [[p.y1, "#58a6ff"], [s2 ? p.y2 : "", "#f0883e"]]){
    if(!col) continue;
    g.fillStyle = colour; g.fillRect(lx, padT + 8, 9, 3);
    g.fillStyle = "#c9d3de";
    const label = pretty(col) + (isLogFor(p, col) ? " [log]" : "");
    g.fillText(label, lx + 13, padT + 3);
    lx += 13 + g.measureText(label).width + 14;
  }

  hover(g, cv, p, {padL, padT, W, H, PX, PY1, PY2, s1, s2, x0, x1, w, h});
}
function isLogFor(p, col){
  return (col === p.y1 && p.y1log) || (col === p.y2 && p.y2log);
}
function centre(g, w, h, msg){
  g.fillStyle = "#6b7683"; g.font = "12px system-ui";
  g.textAlign = "center"; g.textBaseline = "middle";
  g.fillText(msg, w / 2, h / 2);
}

function hover(g, cv, p, G){
  const hv = cv._hover;
  if(!hv || hv.x < G.padL || hv.x > G.padL + G.W) return;
  const xv = G.x0 + (hv.x - G.padL) / G.W * (G.x1 - G.x0);
  let best = -1, bd = Infinity;
  for(let i = 0; i < G.s1.X.length; i++){
    const d = Math.abs(G.s1.X[i] - xv);
    if(d < bd){ bd = d; best = i; }
  }
  if(best < 0) return;
  const x = G.PX(G.s1.X[best]);
  g.save();
  g.strokeStyle = "#8b949e"; g.globalAlpha = .55; g.setLineDash([4, 3]);
  g.beginPath(); g.moveTo(x, G.padT); g.lineTo(x, G.padT + G.H); g.stroke();
  g.restore();

  const lines = [`${pretty(p.x)} ${fmtTick(G.s1.X[best], false)}`];
  const dot = (val, PY, colour) => {
    if(!Number.isFinite(val)) return;
    const y = PY(val);
    if(!Number.isFinite(y)) return;
    g.fillStyle = colour; g.beginPath(); g.arc(x, y, 3.4, 0, Math.PI * 2); g.fill();
  };
  dot(G.s1.Y[best], G.PY1, "#58a6ff");
  lines.push(`${pretty(p.y1)} ${fmtTick(G.s1.Y[best], false)}`);
  if(G.s2){
    // s2 is filtered by the same x window, so indices line up
    const v = G.s2.Y[best];
    dot(v, G.PY2, "#f0883e");
    lines.push(`${pretty(p.y2)} ${fmtTick(v, false)}`);
  }

  g.font = "11px system-ui"; g.textAlign = "left"; g.textBaseline = "top";
  const bw = Math.max(...lines.map(l => g.measureText(l).width)) + 12;
  const bh = lines.length * 14 + 10;
  let bx = x + 10; if(bx + bw > G.w - 4) bx = x - 10 - bw;
  const by = G.padT + 4;
  g.fillStyle = "rgba(13,17,23,0.93)"; g.strokeStyle = "#2a323d";
  g.fillRect(bx, by, bw, bh); g.strokeRect(bx, by, bw, bh);
  g.fillStyle = "#e6edf3";
  lines.forEach((l, i) => g.fillText(l, bx + 6, by + 5 + i * 14));
}

/* ===========================================================================
   Export
   =========================================================================== */

function download(blob, name){
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
/* Both exports name the file after the plot itself (its title, or the axis
   pairing autoTitle builds), so a folder of exports says what each one is
   without opening it. Same stem for the PNG and the CSV of a given plot, so the
   picture and its numbers sit together in a directory listing. */
function plotStem(p){
  const base = (p.title || autoTitle(p)).replace(/[^\w.-]+/g, "_")
                 .replace(/^_+|_+$/g, "").slice(0, 60);
  // An Auger plot's title already carries its file name, so only a CSV plot
  // gets the source file prepended.
  const ds = p.auger ? null : dsFor(p);
  const src = (ds && ds.name ? ds.name : "").replace(/\.csv$/i, "")
                 .replace(/[^\w.-]+/g, "_").slice(0, 60);
  return [src, base].filter(Boolean).join("__") || "plot";
}
function exportPng(p, el){
  const cv = el.querySelector("canvas");
  cv._hover = null; drawPlot(p, el);            // no crosshair in the saved image
  cv.toBlob(b => { if(b) download(b, `${plotStem(p)}.png`); });
}
/* The rows behind one plot: its X and whichever Y axes are set, in axis order
   and de-duplicated (an X plotted against itself would otherwise appear twice).
   Rows where every Y is blank are dropped - they are gaps in the trace, not
   zeros, and exporting them as empty rows makes the CSV harder to use. */
function exportCsv(p){
  const D = dsFor(p);
  if(!D){ toast(p.auger ? "That spectrum is not loaded." : "No file loaded."); return; }
  const cols = [];
  for(const c of [p.x, p.y1, p.y2])
    if(c && D.columns[c] && !cols.includes(c)) cols.push(c);
  if(!cols.length){ toast("This plot has no columns from the loaded file."); return; }
  const ys = cols.filter(c => c !== p.x);
  const cell = v => (v == null || !isFinite(v)) ? "" : String(v);
  const lines = [cols.join(",")];
  for(let i = 0; i < D.n; i++){
    const vals = cols.map(c => D.columns[c][i]);
    if(ys.length && ys.every(c => !isFinite(D.columns[c][i]))) continue;
    lines.push(vals.map(cell).join(","));
  }
  download(new Blob([lines.join("\n")], {type: "text/csv"}), `${plotStem(p)}.csv`);
  toast(`Exported ${lines.length - 1} rows`, true);
}

/* ===========================================================================
   Loading
   =========================================================================== */

let toastTimer = null;
function toast(msg, ok){
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "toast" + (ok ? " ok" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), ok ? 2600 : 7000);
}

function adopt(name, text){
  DATA = buildDataset(name, text);
  const xcol = DATA.columns[seedX()];
  let range = "";
  if(xcol){
    const e = extent(xcol, false);
    if(e) range = `  ·  ${pretty(seedX())} ${fmtTick(e[0])} → ${fmtTick(e[1])}`;
  }
  $("fileInfo").innerHTML =
    `<b>${esc(DATA.name)}</b>  ·  ${DATA.n} rows  ·  ${DATA.numeric.length} numeric columns${esc(range)}`;
  // Existing plots keep their column names and re-resolve against the new
  // file; only a plot with nothing set gets seeded.
  for(const p of LAYOUT.plots){
    if(p.auger) continue;                    // draws its own dataset, not this one
    if(!p.x) p.x = seedX();
    if(!p.y1) p.y1 = seedY(new Set());
  }
  // First ever visit: put up a starting grid rather than an empty page.
  // newPlot() picks a different Y each time, so these land on three different
  // channels instead of three copies of the same one.
  if(!LAYOUT.plots.some(p => !p.auger))
    for(let i = 0; i < 3; i++) LAYOUT.plots.push(newPlot());
  saveLayout();
  renderGrid();
}

async function loadServerFile(name){
  const r = await fetch("/api/data/file?name=" + encodeURIComponent(name));
  if(!r.ok) throw new Error(`could not read ${name} (${r.status})`);
  adopt(name, await r.text());
}

async function rescan(){
  try{
    const j = await (await fetch("/api/data/files")).json();
    const sel = $("srcSel");
    const prev = sel.value;
    sel.innerHTML = j.files.length
      ? j.files.map(f =>
          `<option value="${esc(f.name)}">${esc(f.name)}  —  ${esc(f.kind)}, ${(f.size/1024).toFixed(0)} kB</option>`
        ).join("")
      : `<option value="">— no CSVs in ${esc(j.dir)} —</option>`;
    // Default to the newest plot-ready file. Files are newest-first, but a
    // plain name sort puts "_run.csv" above "_bycycle.csv" for the same run,
    // and the by-cycle one is the one you actually want against cycle number.
    const best = j.files.find(f => f.name.endsWith("_reactor_synced.csv"))
              || j.files.find(f => f.name.endsWith("_bycycle.csv"));
    if(best) sel.value = best.name;
    if(prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  }catch(_){
    $("srcSel").innerHTML =
      `<option value="">— data folder unavailable (is the server running?) —</option>`;
  }
}

/* ===========================================================================
   Wiring
   =========================================================================== */

$("loadBtn").onclick = async () => {
  const name = $("srcSel").value;
  if(!name){ toast("No file selected."); return; }
  try{ await loadServerFile(name); toast(`Loaded ${name}`, true); }
  catch(e){ toast(String(e.message || e)); }
};
$("rescanBtn").onclick = rescan;

$("drop").onclick = () => $("fileInput").click();
$("fileInput").onchange = async ev => {
  const f = ev.target.files && ev.target.files[0];
  if(f) await readLocal(f);
  ev.target.value = "";
};
async function readLocal(f){
  try{ adopt(f.name, await f.text()); toast(`Loaded ${f.name}`, true); }
  catch(e){ toast(`${f.name}: ${e.message || e}`); }
}
/** Drag-and-drop plumbing, shared by the CSV and the Auger drop zone. */
function wireDrop(el, handler){
  for(const evt of ["dragenter", "dragover"])
    el.addEventListener(evt, e => { e.preventDefault(); el.classList.add("over"); });
  for(const evt of ["dragleave", "drop"])
    el.addEventListener(evt, e => { e.preventDefault(); el.classList.remove("over"); });
  el.addEventListener("drop", async e => {
    const files = [...(e.dataTransfer.files || [])];
    if(files.length) await handler(files);
  });
}
wireDrop($("drop"), files => readLocal(files[0]));

/* ---- Ellipsometer sync (moved here from the control page's Diagnostics tab) --
   The merge response already carries the finished CSV, so it is adopted straight
   into the plots instead of being downloaded and dragged back in. A copy is
   still saved server-side into the data folder, which is what makes it show up
   in the source list and become the file this page auto-opens next visit. */
async function loadEllSidecars(){
  try{
    const j = await (await fetch("/api/ellipsometer/sidecars")).json();
    const side = $("ellSidecar"), rlog = $("ellReactorLog");
    side.innerHTML = j.sidecars.length
      ? j.sidecars.map(f => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join("")
      : `<option value="">— no sidecars in data folder —</option>`;
    rlog.innerHTML = (j.reactor_runs && j.reactor_runs.length)
      ? j.reactor_runs.map(f => `<option value="${esc(f.name)}">${esc(f.name)}</option>`).join("")
      : `<option value="">— none (ellipsometry only) —</option>`;
  }catch(_){ /* keep whatever is there */ }
}
$("ellRefresh").onclick = () => { loadEllSidecars(); rescan(); };
$("ellMerge").onclick = async () => {
  const f = $("ellFile").files && $("ellFile").files[0];
  if(!f){ toast("Choose the refit file you downloaded from the FS-1 first."); return; }
  const sidecar = $("ellSidecar").value;
  if(!sidecar){ toast("No sidecar to align to — capture a run first, then ↻."); return; }
  const qs = new URLSearchParams({
    sidecar, reactor_run: $("ellReactorLog").value || "", filename: f.name });
  let r;
  try{
    r = await fetch("/api/ellipsometer/merge?" + qs.toString(),
      { method:"POST", headers:{"Content-Type":"text/plain"}, body: await f.text() });
  }catch(_){ toast("Merge request failed."); return; }
  if(!r.ok){
    let msg = r.statusText; try{ msg = (await r.json()).detail || msg; }catch(_){}
    toast(msg); return;
  }
  const j = await r.json();
  const warn = (j.warnings || []);
  // Nothing to plot (e.g. an old run export with no cycle_number column):
  // say why rather than adopting an empty dataset over a good one.
  if(!j.n_points){
    $("ellResult").textContent = "no rows produced — nothing loaded";
    $("ellWarn").textContent = warn.join("  •  ")
      || "the selected sidecar / reactor run produced no matching rows";
    $("ellWarn").style.color = "var(--bad)";
    toast(warn[0] || "Merge produced no rows.", false);
    return;
  }
  const tm = j.time_map;
  const nchan = (j.reactor_channels || []).length;
  $("ellResult").textContent =
    (j.mode === "combined"
      ? `${j.n_points} rows · ${nchan} channels + ${(j.ellipsometry_columns||[]).join("/")}`
      : `${j.n_points} pts · ellipsometry only`)
    + ` · clock ${tm.b.toFixed(4)} · jitter ${(tm.max_residual_s*1000).toFixed(0)} ms`;
  $("ellWarn").textContent =
    [...warn, j.save_error ? `could not save a copy to the data folder: ${j.save_error}` : ""]
      .filter(Boolean).join("  •  ");
  $("ellWarn").style.color = (warn.length || j.save_error) ? "var(--bad)" : "var(--dim)";
  adopt(j.saved_as || j.filename, j.csv);      // straight into the plots
  await rescan();
  if(j.saved_as) $("srcSel").value = j.saved_as;
  loadEllSidecars();
  toast(j.saved_as ? `Merged and plotted · saved as ${j.saved_as}`
                   : "Merged and plotted", true);
};

/* ---- Auger spectra ---------------------------------------------------------
   A dropped AES export is parsed here and kept as its own dataset. Spectra are
   persisted next to the layout because the reactor CSV re-opens itself on every
   visit: a plot that went blank on reload would be the odd one out. */

const LS_AUGER = "reactorAnalysis.auger.v1";

/** One region's points, wrapped in the same shape as a loaded CSV. */
function augerDataset(key, meta, X, Y){
  return {
    name: key, key, meta, n: X.length,
    header: ["energy_ev", "intensity"],
    columns: {energy_ev: Float64Array.from(X), intensity: Float64Array.from(Y)},
    numeric: ["energy_ev", "intensity"], text: [], binary: new Set(),
  };
}

/** Split an AES text export into one spectrum per region.

    The export is a header line naming the region
    (`Element ; Region 1 of 1; Time Per Step 50; Sweeps 20; AES;`) over
    whitespace- or comma-separated `energy  intensity` rows. A file covering
    several element windows repeats that pair, so ANY non-numeric line starts a
    new spectrum - which also means a bare two-column file with no header at
    all still parses. */
function parseAuger(name, text){
  const stem = name.replace(/\.[^.]*$/, "");
  const blocks = [];
  let cur = null;
  for(const raw of text.split(/\r?\n/)){
    const line = raw.trim();
    if(!line) continue;
    const parts = line.split(/[\s,]+/);
    const x = Number(parts[0]), y = Number(parts[1]);
    if(parts.length >= 2 && Number.isFinite(x) && Number.isFinite(y)){
      if(!cur){ cur = {meta: "", X: [], Y: []}; blocks.push(cur); }
      cur.X.push(x); cur.Y.push(y);
    } else {
      cur = {meta: line, X: [], Y: []};
      blocks.push(cur);
    }
  }
  const kept = blocks.filter(b => b.X.length > 1);
  if(!kept.length)
    throw new Error("no 'energy  intensity' rows found — is this an AES export?");
  return kept.map((b, i) => {
    const fields = b.meta.split(";").map(s => s.trim()).filter(Boolean);
    // Field 1 is the element on a per-element export and the literal word
    // "Element" on a survey; only the former is worth putting in the name.
    const el = (fields[0] && !/^element$/i.test(fields[0])) ? fields[0] : "";
    const region = kept.length > 1 ? (el || `region ${i + 1}`) : "";
    return augerDataset([stem, region].filter(Boolean).join(" · "),
                        b.meta, b.X, b.Y);
  });
}

function saveAuger(){
  const arr = [...AUGER.values()].map(s => ({
    key: s.key, meta: s.meta,
    x: Array.from(s.columns.energy_ev), y: Array.from(s.columns.intensity),
  }));
  // Oldest first, so a full quota sheds the stalest spectrum rather than the
  // one just dropped. If not even one fits, the spectra just live for this
  // session - never at the cost of the layout failing to save.
  while(arr.length){
    try{ localStorage.setItem(LS_AUGER, JSON.stringify(arr)); return; }
    catch(_){ arr.shift(); }
  }
  try{ localStorage.removeItem(LS_AUGER); }catch(_){}
}
function loadAuger(){
  try{
    const arr = JSON.parse(localStorage.getItem(LS_AUGER) || "[]");
    for(const s of (Array.isArray(arr) ? arr : []))
      if(s && s.key && s.x && s.y)
        AUGER.set(s.key, augerDataset(s.key, s.meta || "", s.x, s.y));
  }catch(_){}
  updateAugerInfo();
}
function updateAugerInfo(){
  const n = AUGER.size;
  $("augerInfo").textContent = n
    ? `${n} spectr${n === 1 ? "um" : "a"} · ${[...AUGER.keys()].join(", ")}`
    : "—";
}

/** Adopt one dropped file: every region becomes a spectrum, and every spectrum
    gets a plot - reusing the plot it already had if the same file comes back,
    so a re-export refreshes in place instead of stacking up. */
async function readAuger(f){
  let specs;
  try{ specs = parseAuger(f.name, await f.text()); }
  catch(e){ toast(`${f.name}: ${e.message || e}`); return 0; }
  const fresh = [];
  let first = null;
  for(const ds of specs){
    AUGER.set(ds.key, ds);
    let p = LAYOUT.plots.find(q => q.auger === ds.key);
    if(!p){
      p = {id: nextId++, title: "", auger: ds.key,
           x: "energy_ev", xmin: "", xmax: "",
           y1: "intensity", y1min: "", y1max: "", y1log: false,
           y2: "", y2min: "", y2max: "", y2log: false};
      fresh.push(p);
    }
    first = first || p;
  }
  LAYOUT.plots.unshift(...fresh);              // straight below the drop box
  saveAuger(); saveLayout(); renderGrid(); updateAugerInfo();
  if(first){
    const el = $("grid").querySelector(`.plot[data-id="${first.id}"]`);
    if(el) el.scrollIntoView({behavior: "smooth", block: "nearest"});
  }
  return specs.length;
}
async function readAugerFiles(files){
  let n = 0;
  for(const f of files) n += await readAuger(f);
  if(n) toast(`Plotted ${n} Auger spectr${n === 1 ? "um" : "a"}`, true);
}
wireDrop($("augerDrop"), readAugerFiles);
$("augerDrop").onclick = () => $("augerInput").click();
$("augerInput").onchange = async ev => {
  const files = [...(ev.target.files || [])];
  ev.target.value = "";
  await readAugerFiles(files);
};

$("addPlot").onclick = () => { LAYOUT.plots.push(newPlot()); saveLayout(); renderGrid(); };
$("resetLayout").onclick = () => {
  if(!confirm("Discard all plots and their settings?")) return;
  LAYOUT.plots = []; saveLayout(); renderGrid();
};
$("dupLayout").onclick = () => {
  const {cols, plotH, plots} = LAYOUT;
  const clean = plots.map(({id, ...rest}) => rest);
  download(new Blob([JSON.stringify({cols, plotH, plots: clean}, null, 2)],
                    {type: "application/json"}), "reactor_analysis_layout.json");
};
$("impLayout").onclick = () => $("layoutInput").click();
$("layoutInput").onchange = async ev => {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if(!f) return;
  try{
    const l = JSON.parse(await f.text());
    if(!l || !Array.isArray(l.plots)) throw new Error("not a layout file");
    LAYOUT.plots = l.plots.map(p => ({...p, id: nextId++}));
    if(l.cols) $("cols").value = l.cols;
    if(l.plotH) $("plotH").value = l.plotH;
    saveLayout(); renderGrid();
    toast(`Layout loaded (${LAYOUT.plots.length} plots)`, true);
  }catch(e){ toast(`${f.name}: ${e.message || e}`); }
};

for(const id of ["cols", "plotH", "showSettings"])
  $(id).addEventListener("change", () => { saveLayout(); applyChrome(); drawAll(); });

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawAll, 80);
});

/* The newest merged (reactor-synced) file in the data folder, or "" if there is
   none. That file is the end product of a run - reactor channels plus
   ellipsometry keyed by cycle - so it is what this page should be showing. */
let NEWEST_SYNCED = "";
async function newestSynced(){
  try{
    const j = await (await fetch("/api/data/files")).json();
    const f = (j.files || []).find(x => x.name.endsWith("_reactor_synced.csv"));
    return f ? f.name : "";
  }catch(_){ return ""; }
}

/* Coming back to an already-open tab after finishing a run elsewhere should not
   require a manual rescan: if a newer merged file has appeared since, adopt it.
   Only ever replaces the view when the file is genuinely different from what is
   loaded, so it cannot stomp on a file opened by hand mid-session. */
window.addEventListener("focus", async () => {
  const latest = await newestSynced();
  if(!latest || latest === NEWEST_SYNCED || (DATA && DATA.name === latest)) return;
  NEWEST_SYNCED = latest;
  try{
    await loadServerFile(latest);
    await rescan();
    $("srcSel").value = latest;
    toast(`Loaded newly merged ${latest}`, true);
  }catch(_){}
});

(async () => {
  loadLayout();
  loadAuger();
  renderGrid();
  await rescan();
  await loadEllSidecars();
  // Open the newest plot-ready file straight away - that is the "compile a new
  // file and everything repopulates" path, and on a first visit it is what
  // seeds the starting grid. rescan() has already defaulted the selector to the
  // newest merged file when one exists.
  NEWEST_SYNCED = await newestSynced();
  const first = $("srcSel").value;
  if(first){
    try{ await loadServerFile(first); }catch(_){}
  }
})();
