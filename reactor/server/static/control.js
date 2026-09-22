import {createLiveCharts} from "./live-charts.js";
import {createControlTransport, SHUTDOWN_HINT} from "./control-transport.js";
import {createRunForms} from "./control-run-forms.js";
import {createDevicePanels} from "./control-device-panels.js";
import {createPrestartEditor} from "./prestart-editor.js";
import {createHcpesEditor} from "./hcpes-editor.js";
import {createApertureCard} from "./aperture-card.js";
"use strict";

/* ---------------------------------------------------------------------------
   Telemetry arrives at the loop rate, so tiles are built ONCE and then updated
   field-by-field. Replacing innerHTML every frame would destroy an <input>
   mid-typing, which silently discarded setpoints.
   --------------------------------------------------------------------------- */

const $ = id => document.getElementById(id);
let CFG = {};
const trend = [];

const num = (v,d=3) =>
  (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
const clock = t => new Date(t*1000).toLocaleTimeString();

/** Pressure spans decades, so fixed notation is useless. */
function sci(v, digits=2){
  if(v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if(a !== 0 && (a < 1e-3 || a >= 1e5)) return Number(v).toExponential(digits);
  return Number(v).toPrecision(4);
}
/* Sample current runs from a few mA down to well under a µA, so "4.656e-5 A"
   is the wrong way to show it. Pick mA or µA from the magnitude - the readout
   from the live value, the plot axis from the largest value in view so the
   whole trace shares one unit. */
function currentUnit(v){
  const a = Math.abs(Number(v));
  return (Number.isFinite(a) && a >= 1e-3)
    ? {mul:1e3, unit:"mA", dp:3}
    : {mul:1e6, unit:"µA", dp:2};
}
function fmtCurrent(v){
  if(v === null || v === undefined || Number.isNaN(v)) return {text:"—", unit:"µA"};
  const u = currentUnit(v);
  return {text:(Number(v)*u.mul).toFixed(u.dp), unit:u.unit};
}
function esc(x){
  return String(x ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function put(root, field, text){
  const el = root.querySelector(`[data-f="${field}"]`);
  if(el && el.textContent !== String(text)) el.textContent = text;
  return el;
}
function cls(el, name, on){ el.classList.toggle(name, !!on); }
function setHtml(el, html){ if(el.innerHTML !== html) el.innerHTML = html; }

/** Create missing tiles, drop stale ones, keep existing DOM (and input state). */
function reconcile(container, items, key, buildHtml){
  if(!items.length){
    setHtml(container, `<div class="hint">none configured</div>`);
    container.dataset.empty = "1";
    return;
  }
  if(container.dataset.empty){ container.innerHTML = ""; delete container.dataset.empty; }
  const seen = new Set();
  for(const item of items){
    const id = key(item);
    seen.add(id);
    if(!container.querySelector(`[data-id="${CSS.escape(id)}"]`)){
      const wrap = document.createElement("div");
      wrap.innerHTML = buildHtml(item);
      container.appendChild(wrap.firstElementChild);
    }
  }
  for(const el of [...container.children]){
    if(el.dataset.id && !seen.has(el.dataset.id)) el.remove();
  }
}

/* Fill the charts from the server's own history before the first frame arrives.
   Without this, a browser that connects mid-session starts with empty plots and
   has to accumulate an hour of samples to catch up - which is what every
   Tailscale client saw. The server already keeps ~1 h of trend; ask for it.

   The two sides name some channels differently: the server's history rows are
   the run-export shape (inst_ammeter, gauge_prec1_dose, aux_bubbler) while the
   chart wants the render() shape (current, prec_pressure, bubbler_temp). Map
   them rather than renaming either - the server names are baked into saved
   analysis-page layouts, and the client names are used throughout drawChart. */
async function seedTrend(){
  try{
    const r = await fetch("/api/trend?limit=18000");
    if(!r.ok) return;
    const rows = (await r.json()).samples || [];
    const seeded = [];
    for(const s of rows){
      const o = {
        t: s.t,
        pressure: s.pressure,
        current: s.inst_ammeter,
        prec_pressure: s.gauge_prec1_dose,
        stage_temp: s.stage_temp,
        bubbler_temp: s.aux_bubbler,
        dosing: s.dosing ? 1 : 0,
        beam_on: s.beam_on ? 1 : 0,
      };
      // mfc_* already share a name on both sides.
      for(const k in s) if(k.startsWith("mfc_")) o[k] = s[k];
      seeded.push(o);
    }
    if(seeded.length){
      trend.splice(0, trend.length, ...seeded);
      drawAllCharts();
    }
  }catch(_){ /* no history is survivable - the charts just start empty */ }
}

// ---------------------------------------------------------------- render
function render(s){
  $("siteName").textContent = s.site;
  banners(s); pressure(s); stage(s); hero(s); gauges(s); instruments(s); aux(s);
  devicePanels.supplies(s);
  devicePanels.mfcs(s); devicePanels.valves(s); valveId(s); recipe(s); logging(s); conns(s);
  ellipsometer(s); events(s);
  hcpesEditor.updateStatus(s.hcpes || {});
  apertureCard.render(s.aperture_lifetime || {});

  charts.update(s);
  runForms.updateGasNames(s.mfcs || []);
  const rv = s.run_valves || {dose:"prec1", plasma:"plasma_ground"};
  const vopen = {};
  for(const v of s.valves) vopen[v.id] = v.open;
  const sample = {
    t: s.t,
    pressure: s.snapshot.pressure,                        // chamber (cold cathode)
    current: s.snapshot["inst.ammeter"],                  // sample current
    prec_pressure: s.snapshot["gauge.prec1_dose"],        // precursor dose pressure
    stage_temp: s.snapshot["stage.temp"],
    aperture_lifetime_s: s.snapshot.aperture_lifetime_s,
    bubbler_temp: s.snapshot["aux.bubbler"],
    dosing: vopen[rv.dose] ? 1 : 0,
    // beam ON = plasma-ground relay OFF (valve closed)
    beam_on: vopen[rv.plasma] === false ? 1 : 0,
  };
  for(const m of s.mfcs) sample["mfc_"+m.id] = s.snapshot[`mfc.${m.id}.flow`];  // actual flow, not setpoint
  trend.push(sample);
  if(trend.length > 18000) trend.shift();   // ~1 h at the 5 Hz publish rate
  recordRun(s, sample);
  drawAllCharts();
}

function banners(s){
  const out = [];
  if(!s.daq.configured){
    out.push(`<div class="banner">No DAQ inputs are running — pressure and stage
      temperature are unavailable. ${esc(s.daq.error || "")}</div>`);
  }
  setHtml($("banners"), out.join(""));
}

function pressure(s){
  const p = s.snapshot.pressure, r = s.readings["pressure"];
  const el = $("pressure");
  el.textContent = sci(p);
  cls(el, "stale", !!(r && !r.ok));
  const v = s.snapshot["pressure.volts"];
  $("pressureV").textContent = v === undefined ? "—" : num(v,4) + " V";
  const cf = CFG.pressure || {}, sc = cf.scaling || {};
  // Same numbers as the Run tab's hero readout, on the card that describes the
  // sensor - a channel and a curve with no reading next to them is a spec
  // sheet, not an instrument panel (operator, 2026-08-27).
  const hp = $("hwPressure");
  if(hp){
    hp.textContent = sci(p);
    cls(hp, "stale", !!(r && !r.ok));
    $("hwPressureV").textContent = v === undefined ? "—" : num(v,4) + " V";
  }
  $("pressureChan").textContent = cf.channel || "—";
  $("pressureCurve").textContent = sc.preset || `${sc.type||"?"} g=${sc.gain} o=${sc.offset}`;
  /* There used to be a warning here that a log gauge's 0.1 V offset is ~30%
     of pressure and the curve should be confirmed against the controller.
     It has been - the curve reads correctly - so the warning is gone
     (2026-08-26). It was advice for bring-up, not a live condition. */
}

function stage(s){
  const st = s.stage_temp;
  const rd = s.readings["stage.temp"];
  const el = $("stageTemp");
  el.textContent = st.enabled ? num(st.value, 1) : "off";
  cls(el, "stale", !!(rd && !rd.ok));
  const cf = CFG.stage_temp || {};
  $("stageChan").textContent = cf.channel || "—";
  $("stageType").textContent = cf.tc_type ? `type ${cf.tc_type}` : "—";
  const hs = $("hwStageTemp");
  if(hs){
    hs.textContent = st.enabled ? num(st.value, 1) : "off";
    cls(hs, "stale", !!(rd && !rd.ok));
    // Only ever a number if the channel is declared kind:"voltage"; a DAQmx
    // thermocouple channel returns °C and no millivolts.
    const mv = s.snapshot["stage.temp.volts"];
    $("stageVolts").textContent = typeof mv === "number"
      ? num(mv, 5) + " V" : "not measured";
  }
}

/* Hero readouts that aren't already covered by pressure()/stage(): the
   precursor fill pressure with its regulation status, the bubbler thermocouple
   sitting next to it, and the sample current with the beam's lit state. */
const DEFAULT_FILL_GAUGE = "gauge.prec1_dose";
function hero(s){
  const g = s.regulator || {};
  // While the regulator runs, follow whatever gauge it was pointed at, so the
  // readout always shows the pressure actually being held.
  const key = (g.running && g.gauge) ? g.gauge : DEFAULT_FILL_GAUGE;
  const fp = s.snapshot[key];
  const fEl = $("fillPress");
  fEl.textContent = num(fp, 3);
  const frd = s.readings[key];
  cls(fEl, "stale", !!(frd && !frd.ok));
  const outOfBounds = !!(g.running && g.in_bounds === false);
  cls($("fillRO"), "flag", outOfBounds);
  const gid = key.startsWith("gauge.") ? key.slice(6) : key;
  const gauge = (s.gauges || []).find(x => x.id === gid);
  $("fillSub").textContent = g.running
    ? `target ${num(g.target_torr,3)}` + (g.duty ? " · filling" : "")
      + (outOfBounds ? " · OUT OF BOUNDS" : "")
    : (gauge ? gauge.label : gid);

  const b = (s.aux || []).find(a => a.id === "bubbler");
  const bEl = $("bubblerTemp");
  bEl.textContent = b ? num(b.value, 1) : "—";
  $("bubblerSub").textContent = b ? b.label : "no bubbler TC configured";
  const brd = b ? s.readings["aux." + b.id] : null;
  cls(bEl, "stale", !!(brd && !brd.ok));

  const cur = s.snapshot["inst.ammeter"];
  const cEl = $("sampleCur"), f = fmtCurrent(cur);
  cEl.textContent = f.text;
  $("curUnit").textContent = f.unit;
  const crd = s.readings["inst.ammeter"];
  cls(cEl, "stale", !!(crd && !crd.ok));
  // Beam state comes from a run's beam watcher, or from the pre-start sequence
  // while that is the thing driving the plasma.
  const pre = s.prestart || {};
  const beam = (s.recipe && s.recipe.beam) || (pre.running ? pre : null);
  cls($("curRO"), "beam", !!beam);
  $("curSub").textContent = beam ? (beam.lit ? "● beam lit" : "○ no plasma") : "";
}

function instruments(s){
  const box = $("instruments");
  reconcile(box, s.instruments, i => i.id, i => `
    <div class="tile" data-id="${esc(i.id)}">
      <div class="top">
        <span class="name">${esc(i.label || i.id)}</span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="val">—</span
          ><span class="unit">${esc(i.unit||"")}</span></span>
      </div>
      <div class="kv"><span>Identity</span>
        <span data-f="idn" style="font-size:11px">—</span></div>
      <div class="kv"><span>Query</span><span data-f="q">—</span></div>
      <div class="note" data-f="setup"></div>
      <div class="note" data-f="err"></div>
    </div>`);

  for(const i of s.instruments){
    const el = box.querySelector(`[data-id="${CSS.escape(i.id)}"]`);
    if(!el) continue;
    const rd = s.readings["inst."+i.id];
    const bad = !!(rd && !rd.ok);
    cls(el, "trip", !i.connected);
    const v = put(el, "val", sci(s.snapshot["inst."+i.id], 4));
    if(v) cls(v, "stale", bad);
    put(el, "idn", i.identity || "—");
    put(el, "q", i.query || "—");
    put(el, "setup", (i.setup_sent && i.setup_sent.length)
      ? "setup sent: " + i.setup_sent.join(" · ") : "");
    put(el, "err", bad ? rd.detail : (i.error || ""));
  }
}

function gauges(s){
  const list = s.gauges || [];
  // The CARD always shows now - it carries the chamber gauge's channel and
  // curve as well as these tiles - so only the tile section is hidden.
  cls($("gaugeSub"), "hidden", !list.length);
  if(!list.length) return;
  const box = $("gauges");
  reconcile(box, list, g => g.id, g => `
    <div class="tile" data-id="${esc(g.id)}">
      <div class="top">
        <span class="name" data-f="name">${esc(g.label)}</span>
        <button class="rename" data-rename="gauge" data-id="${esc(g.id)}"
                title="Rename gauge">✎</button>
        <div style="flex:1"></div>
        <span class="read"><span data-f="val">—</span
          ><span class="unit">${esc(g.unit||"")}</span></span>
      </div>
      <div class="kv"><span>Gauge output</span><span data-f="volts">—</span></div>
      <div class="kv"><span>Channel</span><span data-f="chan">—</span></div>
    </div>`);
  for(const g of list){
    const el = box.querySelector(`[data-id="${CSS.escape(g.id)}"]`);
    if(!el) continue;
    put(el, "name", g.label);
    // Baratrons are linear 10 Torr heads — show fixed 2 decimals, never sci.
    put(el, "val", num(g.value, 2));
    put(el, "volts", g.volts === null || g.volts === undefined
      ? "—" : num(g.volts,4) + " V");
    put(el, "chan", g.channel || "—");
  }
}

function aux(s){
  // The CARD always shows - it carries the stage TC's channel and type as well
  // as these tiles - so only the "Other" block is hidden when there are none.
  cls($("auxSub"), "hidden", !s.aux.length);
  if(!s.aux.length) return;
  const box = $("aux");
  /* Same shape as the Stage block above it: name, reading, channel, type,
     voltage. Asked for 2026-08-27 - "Stage temp is the same as the other
     thermocouples, so make the others have the same fields". */
  reconcile(box, s.aux, a => a.id, a => `
    <div class="tile" data-id="${esc(a.id)}">
      <div class="top">
        <span class="name">${esc(a.label)}</span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="val">—</span
          ><span class="unit">${esc(a.unit||"")}</span></span>
      </div>
      <div class="kv"><span>Channel</span><span data-f="chan">—</span></div>
      <div class="kv"><span>Type</span><span data-f="kind">—</span></div>
      <div class="kv"><span>Voltage</span><span data-f="volts">—</span></div>
    </div>`);
  let anyTc = false;
  for(const a of s.aux){
    const el = box.querySelector(`[data-id="${CSS.escape(a.id)}"]`);
    if(!el) continue;
    // A thermocouple reads in degrees, not decades - fixed 1 dp, like the
    // stage TC, rather than the sci() the generic aux channel used to get.
    const tc = a.kind === "thermocouple";
    anyTc = anyTc || tc;
    put(el, "val", tc ? num(a.value, 1) : sci(a.value, 4));
    put(el, "chan", a.channel || "—");
    put(el, "kind", tc ? `thermocouple, type ${a.tc_type || "?"}` : (a.kind || "—"));
    put(el, "volts", a.volts === undefined || a.volts === null
      ? "not measured" : num(a.volts,5) + " V");
  }
  // Said once for the card, not once per tile.
  const hint = $("tcVoltHint");
  if(hint) hint.textContent = anyTc
    ? "No millivolts for a thermocouple channel: DAQmx does the cold-junction "
      + "compensation and linearisation on the module and returns °C, so the "
      + "raw signal never reaches this program. A channel declared "
      + "kind: \"voltage\" in reactor.yaml does report volts."
    : "";
}

let idGroupsLoaded = false;
function valveId(s){
  const vid = s.valve_id || {};
  const running = !!vid.running;

  if(!idGroupsLoaded && vid.groups && Object.keys(vid.groups).length){
    $("idGroup").innerHTML = Object.keys(vid.groups).map(name =>
      `<option value="${esc(name)}">${esc(name)}</option>`).join("");
    idGroupsLoaded = true;
  }

  // Sticky STOP bar - only while a sweep is running.
  cls($("sweepBar"), "hidden", !running);
  if(running){
    $("sweepLine").textContent = vid.current_line || "starting…";
    cls($("sweepDot"), "on", !!vid.line_state);
    $("sweepSub").textContent =
      `line ${(vid.index ?? 0) + 1} of ${vid.total}  ·  rep ${vid.rep||0}/${vid.reps||0}`
      + `  ·  ${vid.line_state ? "ON" : "off"}  ·  ${vid.phase||""}`;
  }

  // Setup and live controls are BOTH always visible - the STOP and the mark
  // buttons must be findable before a sweep is ever started. Start is disabled
  // while running; the mark buttons and card STOP are disabled while idle.
  $("idStartBtn").disabled = running;
  $("idStopCard").disabled = !running;
  setHtml($("idMarkBtns"),
    (vid.unidentified_valves||[]).map(v =>
      `<button data-mark="${esc(v.id)}" ${running?"":"disabled"}>${esc(v.label)} moved</button>`).join("")
    + `<button data-mark="__other" ${running?"":"disabled"}>Other…</button>`);
  $("idNow").textContent = running
    ? (vid.current_line||"—") + (vid.line_state ? "   ● ON" : "   ○ off")
    : "— not running —";
  $("idProgress").textContent = running
    ? `line ${(vid.index??0)+1}/${vid.total} · rep ${vid.rep||0}/${vid.reps||0} · ${vid.phase||""}`
    : "—";

  const marks = vid.marks || [];
  setHtml($("idMarks"), marks.length
    ? "<strong>Bound so far:</strong>" + marks.map(m =>
        `<div>${esc(m.line)} &rarr; ${esc(m.valve||m.note||"?")}</div>`).join("")
    : (vid.phase === "done"
        ? "<span class='hint'>sweep complete — no valves marked</span>" : ""));
}

function recipe(s){
  const r = s.recipe;
  const running = ["running","paused","aborting"].includes(r.state);
  const editable = ["running","paused"].includes(r.state);
  runForms.setRunActive(editable, r.started_at, r.params_revision);
  cls($("liveEditBadge"), "hidden", !editable);
  cls($("liveEditPanel"), "hidden", !editable);
  const pre = s.prestart || {};
  prestartEditor.updateStatus(pre);
  // Pre-start and a run both drive the plasma-ground relay, so only one of them
  // can be armed at a time (the server refuses the overlap with a 409 too).
  $("aldStart").disabled  = running || !!pre.running
    || (!!pre.cleanup_available && !pre.primed);
  $("pauseBtn").disabled  = r.state !== "running";
  $("resumeBtn").disabled = r.state !== "paused";
  $("abortBtn").disabled  = !running;
  $("preStartBtn").disabled = running || !!pre.cleanup_available;
  // Abort is the only way out of a pre-start (2026-08-28). There used to be a
  // Stop button beside it that ended the SEQUENCE and left the tool primed -
  // Ar flowing, fill pulsing - so it greyed out at the moment the operator was
  // most likely to want out, and Abort had to be pressed anyway. Abort stays
  // live across the whole primed state, running or struck-and-held.
  $("preAbortBtn").disabled = running || !pre.cleanup_available;

  const pEl = $("preStatus");
  pEl.textContent = pre.running
    ? (pre.phase || "running") + (pre.strikes ? ` · ${pre.strikes} strikes` : "")
    : (pre.primed ? `primed · ${pre.recipe_name || "recipe complete"}`
      : (pre.state === "error" ? (pre.phase || "error") : (pre.state || "idle")));
  pEl.style.color = pre.running ? "var(--hot)" : "var(--dim)";

  // Standalone fill (pre-run precursor charge): available only when no run is
  // active. Stop is live whenever the regulator is running.
  const regRunning = !!(s.regulator && s.regulator.running);
  $("fillStart").disabled = running || regRunning;
  $("fillStop").disabled  = !regRunning;

  $("recipeInfo").textContent = (running || r.state === "done" || r.state === "error")
    ? `${r.recipe} — ${r.state}${r.phase ? " · " + r.phase : ""}`
      + (r.error ? ` · ${r.error}` : (r.message ? ` · ${r.message}` : ""))
    : "No run in progress.";
  // A run that ENDED leaves nothing behind here except a finished one's final
  // tally: an abort used to leave the bar part-filled and the cycle/step/
  // remaining frozen at whatever they were when it was pressed, which reads
  // like a run still going (2026-09-09). "done" keeps its numbers - they are
  // the result; anything else (aborted, error, idle) clears.
  const live = running || r.state === "done";
  $("recipeBar").style.width = live
    ? ((r.cycles_total ? r.cycle / r.cycles_total : 0)*100).toFixed(1) + "%" : "0%";
  runForms.updateEta(r, running);
  $("rCycle").textContent  = live && r.cycles_total
    ? `${r.cycle} / ${r.cycles_total}` : "—";
  $("rStep").textContent   = live ? (r.step_desc || "—") : "—";
  $("rRemain").textContent = live && (r.step_remaining_s ?? null) !== null
    ? num(r.step_remaining_s, 2) + " s" : "—";

  // --- phase strip: which cycle step is active ---
  // step_index is 1-based over the mode's own step list, so walking the
  // *visible* phases in order works for EE-ALD (4) and EE-CVD (2) alike.
  const cycling = running && r.phase === "cycling";   // cleared when not
  const activeIdx = cycling ? r.step_index : 0;
  const phases = [...document.querySelectorAll("#phaseStrip .phase")]
                   .filter(el => !el.classList.contains("hidden"));
  phases.forEach((el, i) => {
    const active = (i + 1) === activeIdx;
    const isBeam = el.dataset.op === "electron_beam";
    cls(el, "active", active);
    cls(el, "beam", isBeam);
    const tEl = el.querySelector(".pt");
    let fill = el.querySelector(".fill");
    if(!fill){ fill = document.createElement("div"); fill.className="fill"; el.appendChild(fill); }
    if(active){
      const dur = r.step_duration_s, rem = r.step_remaining_s;
      // beam shows exposure remaining (accounts for reignite pauses)
      const showRem = (isBeam && r.beam) ? r.beam.remaining : rem;
      tEl.textContent = (showRem == null) ? "…" : num(showRem,1)+"s";
      fill.style.width = (dur && showRem!=null ? (1-showRem/dur)*100 : 0) + "%";
    } else {
      tEl.textContent = "—";
      fill.style.width = "0";
    }
  });

  // EE-ALD counts down an exposure budget; EE-CVD has none, so it reports the
  // lit time banked so far instead. Live current and fill pressure are hero
  // readouts at the top of the tab, not repeated here.
  const b = r.beam;
  $("rBeam").textContent =
    !b ? "—"
    : (b.remaining != null) ? num(b.remaining,1)+" s left"
    : (b.lit_s != null) ? num(b.lit_s,1)+" s lit"
    : (b.lit ? "beam lit" : "no plasma");
}

/* Estimated time remaining. The server computes it (recipe.run_remaining_s):
   cycle length x cycles, ticking down in real time, frozen while a reignite or
   an operator pause has the cycle held still. See RecipeRunner.run_remaining_s.

   It used to be extrapolated here from the run's own measured pace, which is
   why it wandered: the divisor changed every time a cycle completed, so the
   number moved for reasons invisible from the Run tab and never matched the
   arithmetic the operator had already done from the parameters they typed. */
function fmtDur(s){
  if(!isFinite(s) || s < 0) return "—";
  // Under a minute, show tenths - the run clock is good to 0.1 s and the last
  // seconds of a run are the ones anyone is actually watching.
  if(s < 60) return `${s.toFixed(1)}s`;
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), sec = s%60;
  return h ? `${h}h ${String(m).padStart(2,"0")}m`
           : `${m}m ${String(sec).padStart(2,"0")}s`;
}


// ------------------------------------------------ run recording + auto-download
let runData = null;     // {startT, rows:[...]} while a run is active
let lastRunState = "idle";
/* Set when THIS browser presses Start (or Start CVD). Only that browser saves
   the client-side CSV at run end - see recordRun. Remote viewers over Tailscale
   were all getting a save dialog for runs they had nothing to do with. */
let iStartedThisRun = false;
function recordRun(s, sample){
  const r = s.recipe;
  const running = ["running","paused","aborting"].includes(r.state);
  if(running && r.started_at && (!runData || runData.startT !== r.started_at)){
    runData = {startT: r.started_at, name: r.recipe, rows: []};   // new run began
  }
  if(runData && running){
    runData.rows.push({
      t: sample.t, stage_temp: sample.stage_temp, current: sample.current,
      dosing: sample.dosing, prec_pressure: sample.prec_pressure,
      pressure: sample.pressure,
      aperture_lifetime_s: sample.aperture_lifetime_s,
    });
  }
  // on transition out of a running state into done/idle/error, export
  if(runData && !running && ["done","idle","error"].includes(r.state)
     && lastRunState !== r.state){
    /* Only the browser that STARTED the run saves a copy.
       Every connected client used to fire this, so every laptop tailscaled
       into the server got a save dialog when a run ended - including ones
       just watching. The server-side export (data/<run>/..._run.csv) is the
       authoritative copy and is richer than this one anyway; this is a
       convenience for whoever pressed Start. */
    if(runData.rows.length && iStartedThisRun) downloadRun(runData);
    iStartedThisRun = false;
    runData = null;
  }
  lastRunState = r.state;
}
function downloadRun(rd){
  const cols = ["elapsed_s","aperture_lifetime_s","stage_temp_c","sample_current_a","precursor_dosing",
                "precursor_pressure_torr","chamber_pressure_torr"];
  const fmt = v => (v==null||Number.isNaN(v)) ? "" : v;
  const lines = [cols.join(",")];
  for(const r of rd.rows){
    lines.push([ (r.t - rd.startT).toFixed(3), fmt(r.aperture_lifetime_s),
                 fmt(r.stage_temp), fmt(r.current),
                 fmt(r.dosing), fmt(r.prec_pressure), fmt(r.pressure) ].join(","));
  }
  const stamp = new Date(rd.startT*1000).toISOString().slice(0,19).replace(/[:T]/g,"-");
  const blob = new Blob([lines.join("\n")], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `run_${stamp}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 5000);
  toast(`Run data downloaded (${rd.rows.length} rows)`, true);
}

function logging(s){
  const L = s.logging;
  $("logStart").disabled = L.active;
  $("logStop").disabled  = !L.active;
  $("logFile").textContent = L.path ? L.path.split(/[\\/]/).pop() : "—";
  $("logRows").textContent = L.active ? L.rows : "—";
  if(CFG.logging && !$("logCols").textContent)
    $("logCols").textContent =
      "columns: " + Object.keys(CFG.logging.columns).join(", ");
}

function conns(s){
  const rows = [];
  rows.push(tr("cDAQ / DAQmx", "analog in + digital out",
    `${s.daq.inputs} input(s)`,
    s.daq.configured ? "OK" : "FAIL", s.daq.error || ""));
  for(const m of s.mfcs)
    rows.push(tr(m.label || m.id, "MKS · Modbus TCP", m.host,
      m.connected ? "OK" : "FAIL", m.error || m.device_info || ""));
  for(const i of s.instruments)
    rows.push(tr(i.label || i.id, "SCPI · VISA", i.resource,
      ...connState(i, i.identity || "")));
  for(const p of (s.power_supplies || [])){
    if(p.driver === "keithley_2260b"){
      // Address column shows the USB serial, not just the port: the port is
      // resolved FROM the serial and can move, the serial cannot.
      rows.push(tr(p.label || p.id, "Keithley 2260B · SCPI over USB serial",
        `${p.port || "?"} · serial ${p.usb_serial || "?"}`,
        ...connState(p, p.questionable
                        ? `questionable status 0x${(p.questionable).toString(16)}`
                        : (p.output_on ? "output ON" : "output off"))));
    } else {
      rows.push(tr(p.label || p.id, "Glassman ASCII · serial · monitor + HV off",
        `${p.port || "?"} ${p.baud || "?"} addr ${p.address}`,
        ...connState(p, (p.faults && p.faults.length)
                        ? "FAULT: " + p.faults.join(", ")
                        : (p.firmware ? "firmware " + p.firmware : ""))));
    }
  }
  const e = s.ellipsometer;
  if(e && e.enabled)
    rows.push(tr(e.label || "FS-1 Ellipsometer", "TCP stream · read-only",
      `${e.host}:${e.port}`, e.connected ? "OK" : "FAIL",
      e.error || `${e.points_seen||0} pts seen`));
  setHtml($("conns"), rows.join(""));
}

function ellipsometer(s){
  const e = s.ellipsometer;
  if(!e) return;
  const stream = $("ellStream"), live = $("ellLive"), pts = $("ellPoints");
  if(!e.enabled){
    if(stream){ stream.textContent = "disabled in config"; stream.style.color = "var(--dim)"; }
    if(pts) pts.textContent = "—";
    if(live) live.textContent = "—";
    return;
  }
  const fresh = e.last_recv && (Date.now()/1000 - e.last_recv) < 5;
  if(stream){
    stream.textContent = e.connected
      ? `connected · ${e.host}:${e.port}`
      : `offline · ${e.error || "no connection"}`;
    stream.style.color = e.connected ? "var(--ok)" : "var(--bad)";
  }
  // Two different counts, and the distinction matters: capture_rows is what is
  // in the sidecar for the acquisition running now (what gets refit and
  // merged); points_seen is everything streamed since the program started.
  if(pts){
    pts.textContent = e.capture_active
      ? `${e.capture_rows||0}` + (e.capture_file ? ` · ${e.capture_file}` : "")
      : (e.points_seen ? `none · ${e.points_seen} seen since start` : "—");
    pts.style.color = e.capture_active ? "var(--fg)" : "var(--dim)";
  }
  if(live){
    if(fresh && e.last_index){
      const th = (e.last_thickness==null) ? "—"
        : Number(e.last_thickness).toFixed(2) + " " + (e.last_thickness_unit||"");
      const fd = (e.last_fit_diff==null) ? "" : ` · fit diff ${sci(e.last_fit_diff, 3)}`;
      live.textContent = `#${e.last_index} · t=${num(e.last_time_s,1)}s · ${th}${fd}`;
      live.style.color = "var(--fg)";
    } else {
      live.textContent = e.points_seen ? "— idle (no acquisition running) —" : "— no data yet —";
      live.style.color = "var(--dim)";
    }
  }
}
function tr(a,b,c,state,detail){
  const color = state === "OK" ? "var(--ok)"
              : state === "RETRY" ? "var(--warn)" : "var(--bad)";
  return `<tr><td title="${esc(a)}">${esc(a)}</td><td>${esc(b)}</td>
    <td style="font-family:var(--mono)" title="${esc(c||"")}">${esc(c||"—")}</td>
    <td style="color:${color};font-weight:600">${esc(state)}</td>
    <td style="color:var(--dim)" title="${esc(detail||"")}">${esc(detail||"")}</td></tr>`;
}

// The event log lives on the Diagnostics tab, so a condition that needs seeing
// from the Run tab is also shown on a chip in the header.
//
// The chip reports what is wrong RIGHT NOW. It used to pin the newest error or
// flag EVENT for two minutes, which meant a fill-pressure excursion that had
// already corrected itself kept shouting long after the pressure was back, and
// the operator had no way to tell a live problem from a stale one. Events are
// history and the log keeps them; this is the live condition, and it clears the
// moment the condition does. Display only - it never changes reactor behaviour.
export function liveAlerts(s){
  const out = [];                            // {msg, bad} - bad = red, else amber
  const r = s.recipe || {};
  const logging = s.logging || {};
  // DataLogger intentionally latches errors for the Diagnostics history. A
  // latched failure is not necessarily wrong RIGHT NOW, so only promote it to
  // the header while the owner of that exact stream is active; once that
  // stream closes, the chip clears and the error remains in the log.
  const activeRecordingChannels = new Set();
  if(logging.active) for(const channel of ["manual", "extended"])
    activeRecordingChannels.add(channel);
  if(logging.run_export?.active) for(const channel of ["run", "bycycle", "events", "parameters"])
    activeRecordingChannels.add(channel);
  if(logging.ellipsometer?.active) for(const channel of ["ellipsometer", "ellipsometer move"])
    activeRecordingChannels.add(channel);
  if(logging.hcpes?.active) for(const channel of ["worker", "queue"])
    activeRecordingChannels.add(channel);
  for(const [stream, error] of Object.entries(logging.errors || {}))
    if(activeRecordingChannels.has(stream))
      out.push({msg:`Recording failure (${stream}): ${error}`, bad:true});

  if(r.state === "error" && r.error) out.push({msg: r.error, bad: true});

  // The operator's example: precursor fill pressure off its setpoint. This is
  // the same flag the fill readout shows, read from the same field.
  const g = s.regulator || {};
  if(g.running && g.in_bounds === false)
    out.push({msg: `precursor fill pressure out of bounds (target ${num(g.target_torr,3)} Torr)`,
              bad: false});

  /* Every other commanded value its own measurement disagrees with, on the
     same rule (Supervisor.setpoint_flags). Added 2026-09-01 after the N2 line
     on Mo-017 sat at a setpoint it never reached with nothing to say so: "All
     params should be monitored like the precursor pressure." Warn-only - the
     setpoint is still written exactly as typed. */
  for(const f of (s.setpoint_flags || []))
    out.push({msg: `${f.label} ${num(f.measured,2)} ${f.unit} vs setpoint `
                   + `${num(f.commanded,2)} ${f.unit} (${(f.off_frac*100).toFixed(0)}% off)`,
              bad: false});

  // Plasma out mid-beam, while it is out.
  if(r.beam && r.beam.lit === false)
    out.push({msg: "plasma out — reigniting", bad: false});

  // Hardware that is faulted or unreachable, while it is.
  for(const p of (s.power_supplies || [])){
    if(p.faults && p.faults.length)
      out.push({msg: `${p.label || p.id}: ${p.faults.join(", ")}`, bad: true});
    else if(p.connected === false)
      out.push({msg: `${p.label || p.id} disconnected`, bad: true});
  }
  for(const m of (s.mfcs || []))
    if(m.connected === false) out.push({msg: `${m.label || m.id} disconnected`, bad: true});
  for(const i of (s.instruments || []))
    if(i.connected === false) out.push({msg: `${i.label || i.id} disconnected`, bad: true});
  if(s.daq && s.daq.configured === false && s.daq.error)
    out.push({msg: `DAQ: ${s.daq.error}`, bad: true});

  out.sort((a, b) => (b.bad ? 1 : 0) - (a.bad ? 1 : 0));   // errors first
  return out;
}
/* The event log the panel renders. Seeded once from /api/events (the server
   keeps 20000) and then appended from each telemetry frame, which carries only
   the last 200 - shipping the whole buffer 5x a second would be about a MB/s of
   repetition, which is precisely the wrong thing to do over Tailscale.

   Keyed by timestamp+message to drop the overlap between the seed and the first
   few frames. Two genuinely identical events in the same millisecond would
   collapse into one; that is a fair trade for not double-printing every reload. */
const MAX_CHIPS = 4;

/* Repaints only when the set of conditions actually changes - this runs at
   5 Hz, and rebuilding the header every frame made the chips flicker and stole
   any text selection in them. */
let LAST_CHIP_SIG = "";
function renderAlertChips(live){
  const sig = live.map(a => `${a.bad ? "!" : "."}${a.msg}`).join("|");
  if(sig === LAST_CHIP_SIG) return;
  LAST_CHIP_SIG = sig;
  const shown = live.slice(0, MAX_CHIPS);
  const rest = live.length - shown.length;
  setHtml($("alertChips"),
    shown.map(a => `<div class="alertchip${a.bad ? " bad" : ""}" title="${esc(a.msg)}"
                     >⚠ ${esc(a.msg)}</div>`).join("")
    + (rest > 0 ? `<div class="alertchip more" title="click for the event log"
                    >+${rest} more</div>` : ""));
}

/* The event log the panel renders. Seeded once from /api/events (the server
   keeps 20000) and then appended from each telemetry frame, which carries only
   the last 200 - shipping the whole buffer 5x a second would be about a MB/s of
   repetition, which is precisely the wrong thing to do over Tailscale.

   Keyed by timestamp+message to drop the overlap between the seed and the first
   few frames. Two genuinely identical events in the same millisecond would
   collapse into one; that is a fair trade for not double-printing every reload. */
const LOG_CAP = 200000;          // matches the server's own buffer

/* Two panels, one mechanism: an event list and the errors-only subset. Both
   are seeded once and tailed from every frame, and both dedupe on
   timestamp+message so the seed and the first frames do not double-print. */
function makeLog(){ return {rows: [], seen: new Set()}; }
const EVENT_LOG = makeLog();
const ERROR_LOG = makeLog();

function pushLog(log, list){
  let added = 0;
  for(const e of list || []){
    const key = `${e.t}|${e.message}`;
    if(log.seen.has(key)) continue;
    log.seen.add(key);
    log.rows.push(e);
    added++;
  }
  if(log.rows.length > LOG_CAP){
    log.rows = log.rows.slice(-LOG_CAP);
    log.seen = new Set(log.rows.map(e => `${e.t}|${e.message}`));
  }
  return added;
}

function renderLog(el, rows){
  setHtml(el, rows.slice().reverse().map(e =>
    `<div><span class="t">${clock(e.t)}</span>
     <span class="k-${esc(e.kind)}">${esc(e.message)}</span></div>`).join(""));
}

async function seedEvents(){
  for(const [url, key, log] of [["/api/events", "events", EVENT_LOG],
                                ["/api/errors", "errors", ERROR_LOG]]){
    try{
      const r = await fetch(url);
      if(!r.ok) continue;
      pushLog(log, (await r.json())[key] || []);
    }catch(_){ /* the live tail alone still works */ }
  }
  renderLog($("events"), EVENT_LOG.rows);
  renderLog($("errors"), ERROR_LOG.rows);
  updateErrCount();
}

function updateErrCount(){
  const n = ERROR_LOG.rows.length;
  $("errCount").textContent = n ? `${n} logged this session` : "";
  if(!n) setHtml($("errors"),
    `<div class="quiet">Nothing yet. Errors and flags land here as they
     happen, and in the run's own <code>_errors.log</code>.</div>`);
}

function events(s){
  // Only re-render when something actually arrived - this runs at 5 Hz and the
  // log can be two hundred thousand rows.
  if(pushLog(EVENT_LOG, s.events)) renderLog($("events"), EVENT_LOG.rows);
  if(pushLog(ERROR_LOG, s.errors)){
    renderLog($("errors"), ERROR_LOG.rows);
    updateErrCount();
  }

  const live = liveAlerts(s);
  cls($("diagDot"), "on", !!live.length);
  renderAlertChips(live);
}

const charts = createLiveCharts({$, trend, num, clock, sci, fmtCurrent,
                                  currentUnit, esc, setHtml});
const {drawAllCharts, drawChart, smoothing} = charts;
const transport = createControlTransport({$, document, fetchImpl:fetch,
  WebSocketCtor:WebSocket, location, render, sessionStorage,
  reloadPage:()=>window.location.reload(), onLifecycle:appendLocalLog});
const {dispose:disposeTransport, post, resume:resumeTransport,
  setLink, suspend:suspendTransport, toast} = transport;
const devicePanels = createDevicePanels({$, document, cssEscape:value => CSS.escape(value),
  esc, num, sci, put, cls, reconcile, post, toast,
  confirmImpl:(...args) => confirm(...args), promptImpl:(...args) => prompt(...args)});
const runForms = createRunForms({$, document, storage:localStorage, fetchImpl:fetch,
  cls, smoothing, drawChart});
function prestartBaseValues(){
  const P = key => parseFloat($("p_" + key).value);
  const run = runForms.runParams();
  return {
    ar_sccm:P("pre_ar_sccm"), valve_delay_s:P("pre_valve_delay_s"),
    hold_s:P("pre_hold_s"), dose_pressure_torr:run.dose_pressure_torr,
    fill_pulse_on_s:run.fill_pulse_on_s, fill_pulse_off_s:run.fill_pulse_off_s,
    tolerance_frac:run.tolerance_frac, min_current_a:run.min_current_a,
    reignite_pulse_s:run.reignite_pulse_s,
    reignite_settle_s:run.reignite_settle_s,
    sample_bias_v:run.sample_bias_v,
    sample_bias_polarity:run.sample_bias_polarity,
  };
}

function appendLocalLog(kind, message){
  const entry = {t:Date.now() / 1000, kind, message};
  pushLog(EVENT_LOG, [entry]);
  renderLog($("events"), EVENT_LOG.rows);
  if(kind === "error" || kind === "flag"){
    pushLog(ERROR_LOG, [entry]);
    renderLog($("errors"), ERROR_LOG.rows);
    updateErrCount();
  }
}
const prestartEditor = createPrestartEditor({$, document, windowObj:window,
  fetchImpl:fetch, toast, confirmImpl:(...args)=>confirm(...args),
  promptImpl:(...args)=>prompt(...args), getBaseValues:prestartBaseValues});
const hcpesEditor = createHcpesEditor({$, windowObj:window, fetchImpl:fetch, toast,
  confirmImpl:(...args)=>confirm(...args), promptImpl:(...args)=>prompt(...args)});
const apertureCard = createApertureCard({$, document, post, toast,
  confirmImpl:(...args)=>confirm(...args)});
apertureCard.mount();

// ---------------------------------------------------------------- tabs
// Panes are display:none when inactive, which zeroes the canvases' size - so
// entering a pane always forces a redraw. The sweep STOP bar is fixed to the
// viewport and lives outside the panes, so it stays reachable from any tab.
(function tabs(){
  const btns = [...document.querySelectorAll(".tabs button")];
  const panes = [...document.querySelectorAll("[data-pane]")];
  function show(name){
    if(!panes.some(p => p.dataset.pane === name)) name = "run";
    for(const b of btns) cls(b, "active", b.dataset.tab === name);
    for(const p of panes) cls(p, "hidden", p.dataset.pane !== name);
    localStorage.setItem("activeTab", name);
    drawAllCharts();
  }
  for(const b of btns) b.onclick = () => show(b.dataset.tab);
  $("alertChips").onclick = () => show("diag");
  show(localStorage.getItem("activeTab") || "run");
})();

// ---------------------------------------------------------------- actions
// Valve identification. STOP is the safety-critical control - keep it trivial.
// Two STOP controls, same action: the fixed top bar and the one in the card.
$("sweepStop").onclick = () => post("/api/valve_id/stop");
$("idStopCard").onclick = () => post("/api/valve_id/stop");
$("idStartBtn").onclick = async () => {
  await post("/api/valve_id/start", {
    group: $("idGroup").value,
    reps: parseInt($("idReps").value, 10) || 3,
    on_s: parseFloat($("idOn").value) || 1,
    off_s: parseFloat($("idOff").value) || 1,
    gap_s: parseFloat($("idGap").value) || 3,
    start_index: parseInt($("idStart").value, 10) || 0,
  });
};

// ---- Ellipsometer refit merge ----
// Lives on the Analysis page (/analysis) now: it both merges the refit file
// and plots the result, so the file never has to be saved and dragged back.
// This tab keeps only the live FS-1 stream status.
document.addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-mark]");
  if(!b) return;
  let valve = b.dataset.mark, note = "";
  if(valve === "__other"){
    note = (prompt("Which valve moved on this line?") || "").trim();
    valve = "";
    if(!note) return;
  }
  try { await post("/api/valve_id/mark", {valve, note}); } catch(_){}
});


$("aldStart").onclick = async () => {
  runForms.saveParams();
  const gasErr = runForms.gasScheduleError();
  if(gasErr){ alert(gasErr); $("gasSchedSection").open = true; return; }
  const cvd = runForms.runMode() === "cvd";
  const params = runForms.runParams();
  const what = cvd
    ? `This doses precursor every cycle with the electron beam held on for the `
      + `whole run.`
    : `This doses precursor and fires the electron beam each cycle.`;
  if(!confirm(`Start ${cvd ? "EE-CVD" : "EE-ALD"} run — ${params.cycles} cycles?`
    + `\n\nRun name: ${params.run_name || "(unnamed)"}`
    + `\n\n${what}`)) return;
  try{
    await post(cvd ? "/api/run/cvd" : "/api/run/ald", params);
    iStartedThisRun = true;       // so only this browser saves the CSV at the end
    await runForms.refreshRunName(true);   // this name is now used; offer the next one
  }catch(_){}
};

// Pre-start: the dialog is the point of the button as much as the sequence is -
// the three valves have to be in REMOTE and the supplies on, or the sequence
// commands hardware that cannot respond.
$("preStartBtn").onclick = async () => {
  runForms.saveParams();
  try{
    const launch = await prestartEditor.prepareLaunch(prestartBaseValues());
    const readiness = launch.recipe_id === "current-prestart"
      ? "Set Ar Pneumatic, Plasma Ground, and Precursor Fill to Remote.\n"
        + "Turn on HV at the Glassman front panel if you want plasma.\n\n"
      : "Verify every listed device is ready for remote control.\n"
        + "The Glassman remains front-panel controlled; recipes can only command HV off.\n\n";
    if(!confirm(readiness + prestartEditor.reviewText(launch.preview))) return;
    await post("/api/prestart/start", {
      ...launch.values, recipe_id:launch.recipe_id,
      recipe_revision:launch.recipe_revision,
    });
    $("preSection").open = true;
  }catch(_){}
};
function confirmServerLifecycle(restarting){
  // lastRunState is maintained by recordRun() on every frame.
  const running = ["running", "paused", "aborting"].includes(lastRunState);
  const lines = [
    restarting ? "Restart the reactor server?" : "Shut down the reactor server?",
    "",
    "This stops THIS server and kills any other reactor server still running, "
      + "so nothing is left holding the DAQ or the serial ports.",
    "",
    running
      ? "*** A RUN IS ACTIVE AND WILL BE ABORTED. *** Its end-of-run teardown "
        + "runs: MFCs zeroed, fill valve closed, HV commanded off, DC supply "
        + "outputs switched off."
      : "No run is active.",
    "",
    "Either way:",
    "  • Gas stops — the MFCs zero their own setpoints when this "
      + "program disconnects.",
    "  • Valves are not commanded; last-commanded state is restored on "
      + "startup.",
    // null = omit this line. NOT "" — the empty strings above are deliberate
    // blank separators, so filtering on falsiness would flatten the dialog.
    running ? null : "  • HV and the DC supply outputs are NOT switched off "
      + "— neither driver commands anything on disconnect. They stay as "
      + "they are.",
    "",
    restarting
      ? "The same browser tab will reconnect after the replacement server is ready."
      : "The interface will go offline. Start it again from the Reactor Interface shortcut.",
  ].filter(l => l !== null);
  return confirm(lines.join("\n"));
}

async function requestServerLifecycle(restarting){
  if(!confirmServerLifecycle(restarting)) return;

  appendLocalLog("command", `server ${restarting ? "restart" : "shutdown"} requested from this browser`);

  const shutdownBtn = $("shutdownBtn"), restartBtn = $("restartBtn");
  shutdownBtn.disabled = true;
  restartBtn.disabled = true;
  $("shutdownHint").style.color = "";
  $("shutdownHint").textContent = restarting
    ? "Releasing hardware, then starting the replacement server…"
    : "Releasing the DAQ and the serial ports…";
  try{
    // This response now arrives AFTER the teardown has run, and carries a
    // receipt of what was let go - so there is something real to show rather
    // than "the page went offline, probably fine".
    const r = await post(restarting ? "/api/server/restart" : "/api/server/shutdown");
    setLink(restarting ? "restarting" : "shutting down", "warn");
    if(r && r.also_killed && r.also_killed.length)
      toast(`Also killed ${r.also_killed.length} other server instance(s)`, true);
    if(restarting){
      appendLocalLog("restart", `restart accepted; hardware teardown completed and replacement `
        + `process ${(r.restart && r.restart.pid) || "unknown"} is taking over`);
      $("shutdownHint").textContent = "Hardware released. Waiting for the replacement server…";
      transport.watchRestart(r);
    }else transport.watchShutdown(r);
  }catch(error){
    // A 501 means this server cannot manage its own lifecycle; post() toasted it.
    shutdownBtn.disabled = false;
    restartBtn.disabled = false;
    $("shutdownHint").textContent = `${restarting ? "Restart" : "Shutdown"} is not available for this server.`;
    appendLocalLog("error", `server ${restarting ? "restart" : "shutdown"} request failed: `
      + `${error && error.message ? error.message : "unknown error"}`);
  }
}

$("shutdownBtn").onclick = () => requestServerLifecycle(false);
$("restartBtn").onclick = () => requestServerLifecycle(true);

/* Did it actually stop, and is the tool ready to start again?

   Reported 2026-08-27: the button greyed out, the server stayed up holding the
   DAQ and the COM ports, and the page said nothing. The first version of this
   watcher answered that by polling until the port went quiet - but the port
   going quiet was never the question. uvicorn releases the listening socket
   BEFORE the lifespan teardown, so "not responding" arrives while the DAQ and
   COM8-COM12 may still be held, and the page was reporting success on the one
   fact that was never in doubt.

   Zach, 2026-09-10: "there is no way for me to know if it worked or not. I
   need some confirmation things are shut down and ready to be booted again,
   and it needs to be a lot faster."

   So the server now tears down INSIDE the request and answers with a receipt
   (see /api/server/shutdown). `receipt` here is that answer, and it is the
   real confirmation - by the time it arrives every port has already been let
   go. All this loop adds is the second half of "ready to be booted again": the
   process is gone, so the shortcut will not hit "port already in use".

   250 ms, not 1500: the whole stop is now ~1 s, and a 1.5 s poll turned that
   into a wait that LOOKED like a hang. */



$("preAbortBtn").onclick = () => post("/api/prestart/abort");

$("pauseBtn").onclick  = () => post("/api/recipe/pause");
$("resumeBtn").onclick = () => post("/api/recipe/resume");
$("abortBtn").onclick  = () => post("/api/recipe/abort");

// Standalone fill: reuse the ALD dose-pressure + fill-pulse params and the
// default fill valve / precursor Baratron so it matches the run's own regulation.
$("fillStart").onclick = async () => {
  const P = k => parseFloat($("p_"+k).value);
  try{
    await post("/api/fill/start", {
      valve: "rpm_top", gauge: DEFAULT_FILL_GAUGE,
      target_torr: P("dose_pressure_torr"),
      pulse_on_s: P("fill_pulse_on_s"), pulse_off_s: P("fill_pulse_off_s"),
      tolerance_frac: (P("tolerance_pct")||20)/100,
    });
    toast(`Filling to ${P("dose_pressure_torr")} Torr`, true);
  }catch(_){}
};
$("fillStop").onclick = () => post("/api/fill/stop");
$("logStart").onclick  = () => post("/api/log/start", {label: $("logLabel").value || null});
$("logStop").onclick   = () => post("/api/log/stop");

// ---------------------------------------------------------------- bootstrap
let pageDisposed = false;
let pageVisible = true;
let bootstrapComplete = false;
(async () => {
  try{ CFG = await (await fetch("/api/config")).json(); }catch(_){ CFG = {}; }
  if(pageDisposed) return;
  devicePanels.mount();
  runForms.mount();
  await runForms.loadParams();
  if(pageDisposed) return;
  await prestartEditor.mount();
  if(pageDisposed) return;
  await hcpesEditor.mount();
  if(pageDisposed) return;
  runForms.applyMode();
  runForms.refreshRunName(false);
  $("shutdownHint").textContent = SHUTDOWN_HINT;
  await seedTrend();
  if(pageDisposed) return;
  await seedEvents();
  if(pageDisposed) return;
  bootstrapComplete = true;
  const restartedVersion = sessionStorage.getItem("reactor.restart.success");
  if(restartedVersion){
    sessionStorage.removeItem("reactor.restart.success");
    toast(`Restart successful - running version ${restartedVersion}`, true);
  }
  if(pageVisible) resumeTransport();
})();

window.addEventListener("pagehide", event => {
  pageVisible = false;
  if(event.persisted){
    suspendTransport();
    return;
  }
  pageDisposed = true;
  devicePanels.dispose();
  runForms.dispose();
  prestartEditor.dispose();
  hcpesEditor.dispose();
  apertureCard.dispose();
  disposeTransport();
});
window.addEventListener("pageshow", event => {
  if(!event.persisted || pageDisposed) return;
  pageVisible = true;
  resumeTransport(bootstrapComplete);
});

function connState(dev, whenOk){
  if(dev.connected) return ["OK", whenOk];
  const r = dev.retry;
  if(!r) return ["FAIL", dev.error || ""];
  const ago = Math.max(0, Math.round(Date.now()/1000 - (r.last || 0)));
  const down = Math.max(0, Math.round(Date.now()/1000 - (r.first || 0)));
  return ["RETRY",
    `try ${r.attempts}, ${ago}s ago · down ${fmtSpan(down)} · ${r.error || ""}`];
}
function fmtSpan(sec){
  if(sec < 60) return `${sec}s`;
  if(sec < 3600) return `${Math.floor(sec/60)}m${String(sec%60).padStart(2,"0")}s`;
  return `${Math.floor(sec/3600)}h${String(Math.floor(sec%3600/60)).padStart(2,"0")}m`;
}
