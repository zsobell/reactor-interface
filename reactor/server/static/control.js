import {createLiveCharts} from "./live-charts.js";
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

// ---------------------------------------------------------------- transport
const SHUTDOWN_HINT = "Stops this server and any other reactor server still "
  + "running, then start it again from the shortcut. Aborts a running recipe, "
  + "and gas stops either way — read the prompt.";

function connect(){
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
  ws.onopen  = () => {
    setLink("live", "live");
    // If we are talking to a server again, no shutdown is in progress - so
    // whatever disabled the button (a shutdown that failed, or a server that
    // came back some other way) should not leave the control dead.
    const sb = $("shutdownBtn"), sh = $("shutdownHint");
    if(sb && sb.disabled){
      sb.disabled = false;
      if(sh) sh.textContent = SHUTDOWN_HINT;
    }
  };
  ws.onclose = () => { setLink("disconnected", "bad"); setTimeout(connect, 1500); };
  ws.onerror = () => setLink("error", "bad");
  ws.onmessage = ev => render(JSON.parse(ev.data));
}
function setLink(text, kind){
  const b = $("linkBadge"); b.textContent = text; b.className = "badge " + (kind||"");
}

async function post(url, body){
  const r = await fetch(url, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: body === undefined ? "{}" : JSON.stringify(body)
  });
  if(!r.ok){
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch(_){}
    toast(msg); throw new Error(msg);
  }
  return r.json();
}

let toastTimer = null;
function toast(msg, ok){
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "toast" + (ok ? " ok" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), ok ? 2500 : 7000);
}

/** Read a numeric input, complaining rather than silently doing nothing. */
function readNum(sel, what){
  const el = document.querySelector(sel);
  if(!el) return null;
  const raw = el.value.trim();
  if(raw === ""){ toast(`Enter a value for ${what} first.`); el.focus(); return null; }
  const v = parseFloat(raw);
  if(!Number.isFinite(v)){ toast(`"${raw}" is not a number.`); el.focus(); return null; }
  return v;
}

// ---------------------------------------------------------------- render
function render(s){
  $("siteName").textContent = s.site;
  banners(s); pressure(s); stage(s); hero(s); gauges(s); instruments(s); aux(s);
  supplies(s);
  mfcs(s); valves(s); valveId(s); recipe(s); logging(s); conns(s);
  ellipsometer(s); events(s);

  charts.update(s);
  const rv = s.run_valves || {dose:"prec1", plasma:"plasma_ground"};
  const vopen = {};
  for(const v of s.valves) vopen[v.id] = v.open;
  const sample = {
    t: s.t,
    pressure: s.snapshot.pressure,                        // chamber (cold cathode)
    current: s.snapshot["inst.ammeter"],                  // sample current
    prec_pressure: s.snapshot["gauge.prec1_dose"],        // precursor dose pressure
    stage_temp: s.snapshot["stage.temp"],
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
  $("pressureChan").textContent = cf.channel || "—";
  $("pressureCurve").textContent = sc.preset || `${sc.type||"?"} g=${sc.gain} o=${sc.offset}`;
  $("pressureWarn").textContent = sc.type === "log10"
    ? "Log gauge: a 0.1 V offset is roughly a 30% pressure error. Confirm the "
      + "curve against the gauge controller before trusting these numbers."
    : "";
}

function stage(s){
  const st = s.stage_temp;
  const rd = s.readings["stage.temp"];
  const el = $("stageTemp");
  el.textContent = st.enabled ? num(st.value, 1) : "off";
  cls(el, "stale", !!(rd && !rd.ok));
  const cf = CFG.stage_temp || {};
  $("stageChan").textContent = cf.channel || "—";
  $("stageType").textContent = cf.tc_type || "—";
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

/* Power supplies (the Glassman HV plasma supply).
   MONITOR ONLY — no setpoint inputs, no HV button, nothing that writes. Zach
   drives this supply from its front panel; the program reads and logs it.
   Adding controls here is a change to reactor behaviour (docs/CONTROL_MODEL.md)
   and needs his explicit go-ahead, not just a code change. */
function supplies(s){
  const box = $("supplies");
  const list = s.power_supplies || [];
  const isK = p => p.driver === "keithley_2260b";
  /* Two shapes on one card grid. The Glassman is monitor-only and reports arc
     count / HV status / fault flags; the Keithley DC supplies report measured
     V and I and an output state this program switches. Neither has a setpoint
     input: the Glassman is set by hand, and the Keithleys take only their
     output enable plus (bias unit only) a voltage from the run parameters. */
  reconcile(box, list, p => p.id, p => isK(p) ? `
    <div class="tile psu" data-id="${esc(p.id)}">
      <div class="top">
        <span class="name">${esc(p.label || p.id)}</span>
        <span class="mode" data-f="mode" title="constant-voltage / constant-current"></span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="v">—</span><span class="unit">V</span></span>
      </div>
      <div class="kv"><span>Current</span><span><span data-f="i">—</span> A</span></div>
      <div class="row" style="margin-top:6px">
        <input type="number" step="0.1" min="0" placeholder="V"
               id="psuv_${esc(p.id)}" data-f="vin" style="flex:1;min-width:64px">
        <input type="number" step="0.01" min="0" placeholder="A"
               id="psui_${esc(p.id)}" data-f="iin" style="flex:1;min-width:64px">
        <button data-act="psuset" data-id="${esc(p.id)}">Set</button>
      </div>
      <div class="row" style="margin-top:6px">
        <button data-act="psuout" data-id="${esc(p.id)}" data-f="outbtn"
                style="flex:1">Output</button>
      </div>
      <div class="kv" style="margin-top:6px"><span>Setpoint (device)</span>
        <span data-f="sp">—</span></div>
      <div class="chips">
        <span class="chip" data-f="chipOut">output</span>
        <span class="chip" data-f="chipRole"></span>
        <span class="chip" data-f="chipQues"></span>
      </div>
      <div class="kv"><span>Model</span><span data-f="model">—</span></div>
      <div class="kv"><span>Rated</span><span data-f="rated">—</span></div>
      <div class="kv"><span>Link</span><span data-f="link">—</span></div>
      <div class="note" data-f="err"></div>
      <div class="hint" data-f="hint"></div>
    </div>` : `
    <div class="tile" data-id="${esc(p.id)}">
      <div class="top">
        <span class="name">${esc(p.label || p.id)}</span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="v">—</span
          ><span class="unit">${esc(p.unit_v || "V")}</span></span>
      </div>
      <div class="kv"><span>Current</span>
        <span><span data-f="i">—</span> ${esc(p.unit_i || "mA")}</span></div>
      <div class="kv"><span>Arc count</span><span data-f="arcs">—</span></div>
      <div class="chips">
        <span class="chip" data-f="chipHv">HV</span>
        <span class="chip" data-f="chipMode">mode</span>
        <span class="chip" data-f="chipRem">local</span>
        <span class="chip" data-f="chipTrip">I-trip</span>
      </div>
      <div class="kv"><span>Model</span><span data-f="model">—</span></div>
      <div class="kv"><span>Link</span><span data-f="link">—</span></div>
      <div class="note" data-f="faults"></div>
      <div class="note" data-f="err"></div>
      <div class="hint">Monitor only — set voltage and current on the supply's
        front panel. HV is commanded off at the end of a run.</div>
    </div>`);

  for(const p of list){
    const el = box.querySelector(`[data-id="${CSS.escape(p.id)}"]`);
    if(!el) continue;
    const pre = isK(p) ? "psu." : "hv.";
    const rd = s.readings[pre+p.id+".voltage"];
    const bad = !!(rd && !rd.ok);
    const chip = (field, text, on, hot) => {
      const c = put(el, field, text);
      if(c){ cls(c, "on", !!on && !hot); cls(c, "hot", !!hot); }
    };

    const v = put(el, "v", sci(s.snapshot[pre+p.id+".voltage"], 4));
    if(v) cls(v, "stale", bad);
    put(el, "i", sci(s.snapshot[pre+p.id+".current"], 4));
    put(el, "model", p.model || p.driver || "—");
    put(el, "err", bad ? rd.detail : (p.error || ""));

    if(isK(p)){
      /* Red border for a dead link only. An energised output is normal here —
         these run for the whole deposition — so it is flagged green, not red,
         and the questionable-status chip carries the alarm. */
      cls(el, "trip", !p.connected);
      chip("chipOut", p.output_on === null || p.output_on === undefined
                        ? "output —"
                        : (p.output_on ? "OUTPUT ON" : "output off"),
                      p.output_on, false);
      const sign = p.polarity < 0 ? "−" : "+";
      chip("chipRole", p.is_sample_bias ? `sample bias  ${sign}` : "", false, false);
      /* Non-zero questionable-status register: the bit map is not documented
         in anything we have, so show the raw value rather than inventing a
         meaning for it. */
      chip("chipQues", p.questionable ? `status 0x${(p.questionable).toString(16)}` : "",
                       false, !!p.questionable);
      put(el, "rated", (p.max_voltage != null && p.max_current != null)
        ? `${(+p.max_voltage).toFixed(1)} V · ${(+p.max_current).toFixed(3)} A max`
        : "—");
      /* Port is REPORTED, not configured — resolved from the USB serial. */
      put(el, "link", `${p.port || "?"} · USB serial ${p.usb_serial || "?"}`);
      put(el, "sp", (p.voltage_setpoint != null && p.current_setpoint != null)
        ? `${(+p.voltage_setpoint).toFixed(3)} V · ${(+p.current_setpoint).toFixed(3)} A`
        : "—");

      /* CV/CC light. Blank while the output is off - the mode has no meaning
         then, and the raw-value decoding is provisional (see OUTPUT_MODES in
         the driver), so not claiming anything is the honest default. */
      const md = put(el, "mode", p.mode || "");
      if(md){ cls(md, "cv", p.mode === "CV"); cls(md, "cc", p.mode === "CC"); }

      const ob = el.querySelector('[data-f="outbtn"]');
      if(ob){
        ob.textContent = p.output_on ? "Output ON — click to turn off"
                                     : "Output off — click to turn on";
        cls(ob, "danger", !!p.output_on);
        ob.dataset.state = p.output_on ? "1" : "0";
        ob.disabled = !p.connected;
      }
      for(const f of ["vin", "iin"]){
        const inp = el.querySelector(`[data-f="${f}"]`);
        if(inp) inp.disabled = !p.connected;
      }
      const sb = el.querySelector('[data-act="psuset"]');
      if(sb) sb.disabled = !p.connected;

      put(el, "hint", p.is_sample_bias
        ? "Pre-start sets this from the run's Sample bias field and switches it "
          + "on only when that is non-zero. Fields above override it by hand."
        : "Output switched on at pre-start, off at run end. Fields above set it "
          + "by hand.");
    } else {
      cls(el, "trip", !p.connected || !!p.faulted);
      const arcs = s.snapshot["hv."+p.id+".arc_count"];
      put(el, "arcs", (arcs === null || arcs === undefined) ? "—" : String(arcs));
      chip("chipHv",   p.hv_on ? "HV ON" : "HV off", p.hv_on, p.hv_on);
      chip("chipMode", p.voltage_mode === null || p.voltage_mode === undefined
                         ? "mode —"
                         : (p.voltage_mode ? "V mode" : "I mode"), false, false);
      chip("chipRem",  p.remote ? "remote" : "local", false, false);
      chip("chipTrip", p.current_trip_enabled ? "I-trip on" : "I-trip off",
                       p.current_trip_enabled, false);
      put(el, "link", `${p.port || "?"} · ${p.baud || "?"} 8N1 · addr ${p.address}`
                      + (p.firmware ? ` · fw ${p.firmware}` : ""));
      put(el, "faults", (p.faults && p.faults.length)
        ? "FAULT: " + p.faults.join(", ") : "");
    }
  }
}

function gauges(s){
  const list = s.gauges || [];
  cls($("gaugeCard"), "hidden", !list.length);
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
  cls($("auxCard"), "hidden", !s.aux.length);
  if(!s.aux.length) return;
  const box = $("aux");
  reconcile(box, s.aux, a => a.id, a => `
    <div class="tile" data-id="${esc(a.id)}">
      <div class="top">
        <span class="name">${esc(a.label)}</span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="val">—</span
          ><span class="unit">${esc(a.unit||"")}</span></span>
      </div>
      <div class="kv"><span>Raw</span><span data-f="volts">—</span></div>
    </div>`);
  for(const a of s.aux){
    const el = box.querySelector(`[data-id="${CSS.escape(a.id)}"]`);
    if(!el) continue;
    put(el, "val", sci(a.value, 4));
    put(el, "volts", a.volts === undefined || a.volts === null
      ? "—" : num(a.volts,4) + " V");
  }
}

function mfcs(s){
  const box = $("mfcs");
  reconcile(box, s.mfcs, m => m.id, m => `
    <div class="tile" data-id="${esc(m.id)}">
      <div class="top">
        <span class="name" data-f="name">${esc(m.label || m.id)}</span>
        <button class="rename" data-rename="mfc" data-id="${esc(m.id)}"
                title="Rename MFC">✎</button>
        <span class="src" data-f="gas"></span>
        <div style="flex:1"></div>
        <span class="read"><span data-f="flow">—</span
          ><span class="unit">sccm</span></span>
      </div>
      <div class="bar"><i data-f="bar" style="width:0"></i></div>
      <div class="row" style="margin-top:0">
        <input type="number" step="0.01" min="0" placeholder="sccm"
               id="mfc_${esc(m.id)}" data-f="input" style="flex:1;min-width:80px">
        <button data-act="mfc" data-id="${esc(m.id)}">Set flow</button>
      </div>
      <div class="kv" style="margin-top:6px"><span>Setpoint (device)</span><span data-f="sp">—</span></div>
      <div class="kv"><span>Full scale</span><span data-f="fs">—</span></div>
      <div class="kv"><span>Body temp</span><span data-f="temp">—</span></div>
      <div class="kv"><span>Device mode</span><span data-f="mode">—</span></div>
      <div class="note" data-f="health"></div>
      <div class="note" data-f="err"></div>
      <div class="note" data-f="iso"></div>
    </div>`);

  const valveById = {}; for(const v of s.valves) valveById[v.id] = v;

  for(const m of s.mfcs){
    const el = box.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
    if(!el) continue;
    cls(el, "trip", !m.connected);
    const flow = s.snapshot[`mfc.${m.id}.flow`];
    const pct  = s.snapshot[`mfc.${m.id}.flow_pct`];
    put(el, "name", m.label || m.id);
    put(el, "gas", m.gas || "");
    put(el, "flow", (flow===null||flow===undefined) ? "—" : Number(flow).toFixed(3));
    put(el, "sp", num(s.snapshot[`mfc.${m.id}.setpoint`],3) + " sccm");
    // Full scale is read from the instrument, not configured - it moves with gas.
    put(el, "fs", m.full_scale_sccm === null || m.full_scale_sccm === undefined
      ? "—" : `${num(m.full_scale_sccm,1)} sccm`
        + (typeof pct === "number" ? `  (${pct.toFixed(1)}%)` : ""));
    put(el, "temp", num(s.snapshot[`mfc.${m.id}.temp`],1) + " °C");
    put(el, "mode", m.device_mode || "—");
    const bad = m.health
      ? Object.entries(m.health).filter(([, v]) => v && v !== "No")
      : [];
    put(el, "health", bad.length
      ? "device reports: " + bad.map(([k, v]) => `${k}=${v}`).join(", ") : "");
    put(el, "err", m.error || "");
    const bar = el.querySelector('[data-f="bar"]');
    if(bar) bar.style.width =
      Math.max(0, Math.min(100, typeof pct === "number" ? pct : 0)) + "%";

    // Requested by operator: gate the setpoint on the MFC's isolation valve.
    const iso = m.isolation_valve ? valveById[m.isolation_valve] : null;
    const isoClosed = !!(iso && !iso.open);
    put(el, "iso", isoClosed
      ? `${iso.label} is closed — open it to set flow above 0`
      : "");
    el.querySelector('[data-act="mfc"]').disabled = !m.connected || isoClosed;
    el.querySelector('[data-f="input"]').disabled = !m.connected || isoClosed;
  }
}

function valves(s){
  const box = $("valves");
  // One header row per control box, then that box's valves. Grouping is by the
  // `bank` field; anything without a bank falls into an "ungrouped" section.
  // The headers span the full grid width so each bank reads as its own block.
  const banks = (s.valve_banks || []).slice();
  if(s.valves.some(v => !v.bank)) banks.push({id:"", label:"Ungrouped", note:""});

  const rows = [];
  for(const b of banks){
    const members = s.valves.filter(v => (v.bank || "") === b.id);
    if(!members.length) continue;
    rows.push({kind:"bank", id:"bank:"+b.id, bank:b});
    for(const v of members) rows.push({kind:"valve", id:"valve:"+v.id, valve:v});
  }

  reconcile(box, rows, r => r.id, r => r.kind === "bank" ? `
    <div class="bankhdr" data-id="${esc(r.id)}">
      <div class="bankname">${esc(r.bank.label)}</div>
      <div class="banknote">${esc(r.bank.note || "")}</div>
    </div>` : `
    <div class="valve" data-id="${esc(r.id)}">
      <div class="lbl">
        <strong data-f="label">${esc(r.valve.label)}</strong>
        <span class="sub" data-f="sub"></span>
      </div>
      <button class="rename" data-rename="valve" data-id="${esc(r.valve.id)}"
              title="Rename valve">✎</button>
      <button data-act="valve" data-id="${esc(r.valve.id)}" data-f="btn">—</button>
    </div>`);

  for(const r of rows){
    if(r.kind !== "valve") continue;
    const v = r.valve;
    const el = box.querySelector(`[data-id="${CSS.escape(r.id)}"]`);
    if(!el) continue;
    cls(el, "open", v.open);
    put(el, "label", v.label);
    put(el, "sub", `${v.line || "no line"} · ${v.open ? "OPEN" : "closed"}`);
    const btn = el.querySelector('[data-f="btn"]');
    btn.textContent = v.open ? "Close" : "Open";
    btn.dataset.state = v.open ? "0" : "1";
    btn.disabled = !v.line;   // only a valve with no DAQ line can't be driven
  }
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
  const pre = s.prestart || {};
  // Pre-start and a run both drive the plasma-ground relay, so only one of them
  // can be armed at a time (the server refuses the overlap with a 409 too).
  $("aldStart").disabled  = running || !!pre.running;
  $("pauseBtn").disabled  = r.state !== "running";
  $("resumeBtn").disabled = r.state !== "paused";
  $("abortBtn").disabled  = !running;
  $("preStartBtn").disabled = running || !!pre.running;
  $("preStopBtn").disabled  = !pre.running;
  // Stop only ends the SEQUENCE, so it greys out the moment the plasma has
  // struck and held - exactly when the tool is primed and most likely to need
  // backing out of. Abort stays live for that whole primed state.
  $("preAbortBtn").disabled = running || !(pre.running || pre.done);

  const pEl = $("preStatus");
  pEl.textContent = pre.running
    ? (pre.phase || "running") + (pre.strikes ? ` · ${pre.strikes} strikes` : "")
    : (pre.done ? "complete — beam grounded, Ar and fill running" : "idle");
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
  $("recipeBar").style.width =
    ((r.cycles_total ? r.cycle / r.cycles_total : 0)*100).toFixed(1) + "%";
  updateEta(r, running);
  $("rCycle").textContent  = r.cycles_total ? `${r.cycle} / ${r.cycles_total}` : "—";
  $("rStep").textContent   = r.step_desc || "—";
  $("rRemain").textContent = (r.step_remaining_s ?? null) === null
    ? "—" : num(r.step_remaining_s, 2) + " s";

  // --- phase strip: which cycle step is active ---
  // step_index is 1-based over the mode's own step list, so walking the
  // *visible* phases in order works for EE-ALD (4) and EE-CVD (2) alike.
  const cycling = running && r.phase === "cycling";
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
function updateEta(r, running){
  const showing = running || r.state === "paused";
  const elapsed = (showing && r.started_at) ? (Date.now()/1000 - r.started_at) : null;
  $("rElapsed").textContent = elapsed == null ? "—" : fmtDur(elapsed);

  const eta = showing ? (r.run_remaining_s ?? null) : null;
  const held = showing && r.paused;
  $("rEta").textContent = eta == null ? "—"
    : fmtDur(eta) + (held ? " (held)" : "");
  $("rFinish").textContent = eta == null ? "—"
    : new Date(Date.now() + eta*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
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
  const cols = ["elapsed_s","stage_temp_c","sample_current_a","precursor_dosing",
                "precursor_pressure_torr","chamber_pressure_torr"];
  const fmt = v => (v==null||Number.isNaN(v)) ? "" : v;
  const lines = [cols.join(",")];
  for(const r of rd.rows){
    lines.push([ (r.t - rd.startT).toFixed(3), fmt(r.stage_temp), fmt(r.current),
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
      i.connected ? "OK" : "FAIL", i.error || i.identity || ""));
  for(const p of (s.power_supplies || [])){
    if(p.driver === "keithley_2260b"){
      // Address column shows the USB serial, not just the port: the port is
      // resolved FROM the serial and can move, the serial cannot.
      rows.push(tr(p.label || p.id, "Keithley 2260B · SCPI over USB serial",
        `${p.port || "?"} · serial ${p.usb_serial || "?"}`,
        p.connected ? "OK" : "FAIL",
        p.error
          || (p.questionable
                ? `questionable status 0x${(p.questionable).toString(16)}`
                : (p.output_on ? "output ON" : "output off"))));
    } else {
      rows.push(tr(p.label || p.id, "Glassman ASCII · serial · monitor + HV off",
        `${p.port || "?"} ${p.baud || "?"} addr ${p.address}`,
        p.connected ? "OK" : "FAIL",
        p.error || ((p.faults && p.faults.length) ? "FAULT: " + p.faults.join(", ")
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
  const color = state === "OK" ? "var(--ok)" : "var(--bad)";
  return `<tr><td>${esc(a)}</td><td>${esc(b)}</td>
    <td style="font-family:var(--mono)">${esc(c||"—")}</td>
    <td style="color:${color};font-weight:600">${esc(state)}</td>
    <td style="color:var(--dim)">${esc(detail||"")}</td></tr>`;
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
function liveAlerts(s){
  const out = [];                            // {msg, bad} - bad = red, else amber
  const r = s.recipe || {};
  for(const [stream, error] of Object.entries(s.logging?.errors || {}))
    out.push({msg: `Recording failure (${stream}): ${error}`, bad: true});

  if(r.state === "error" && r.error) out.push({msg: r.error, bad: true});

  // The operator's example: precursor fill pressure off its setpoint. This is
  // the same flag the fill readout shows, read from the same field.
  const g = s.regulator || {};
  if(g.running && g.in_bounds === false)
    out.push({msg: `precursor fill pressure out of bounds (target ${num(g.target_torr,3)} Torr)`,
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
let EVENTS = [];
let EVENT_SEEN = new Set();

function pushEvents(list){
  let added = 0;
  for(const e of list || []){
    const key = `${e.t}|${e.message}`;
    if(EVENT_SEEN.has(key)) continue;
    EVENT_SEEN.add(key);
    EVENTS.push(e);
    added++;
  }
  if(EVENTS.length > 20000){
    EVENTS = EVENTS.slice(-20000);
    EVENT_SEEN = new Set(EVENTS.map(e => `${e.t}|${e.message}`));
  }
  return added;
}

async function seedEvents(){
  try{
    const r = await fetch("/api/events");
    if(!r.ok) return;
    pushEvents((await r.json()).events || []);
  }catch(_){ /* the live tail alone still works */ }
}

function events(s){
  // Only re-render when something actually arrived - this runs at 5 Hz and the
  // log can be twenty thousand rows.
  if(pushEvents(s.events)){
    setHtml($("events"), EVENTS.slice().reverse().map(e =>
      `<div><span class="t">${clock(e.t)}</span>
       <span class="k-${esc(e.kind)}">${esc(e.message)}</span></div>`).join(""));
  }

  const live = liveAlerts(s);
  const chip = $("alertChip");
  cls(chip, "hidden", !live.length);
  cls($("diagDot"), "on", !!live.length);
  if(live.length){
    const more = live.length > 1 ? `  (+${live.length - 1} more)` : "";
    chip.textContent = `⚠ ${live[0].msg}${more}`;
    cls(chip, "bad", live[0].bad);
  }
}

const charts = createLiveCharts({$, trend, num, clock, sci, fmtCurrent,
                                  currentUnit, esc, setHtml});
const {drawAllCharts, drawChart, smoothing} = charts;

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
  $("alertChip").onclick = () => show("diag");
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

document.addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]");
  if(!b) return;
  const id = b.dataset.id;
  try{
    if(b.dataset.act === "valve"){
      await post(`/api/valve/${id}`, {state: b.dataset.state === "1"});
    } else if(b.dataset.act === "mfc"){
      const v = readNum(`#mfc_${CSS.escape(id)}`, `${id} flow`);
      if(v === null) return;
      const res = await post(`/api/mfc/${id}/setpoint`, {sccm: v});
      toast(`${id} setpoint set to ${res.setpoint_sccm} sccm`, true);
    } else if(b.dataset.act === "psuset"){
      /* Either field on its own is a valid edit - blank means "leave it".
         Voltage first, so raising both never briefly runs at the old (lower)
         current limit against the new voltage. */
      const vEl = document.querySelector(`#psuv_${CSS.escape(id)}`);
      const iEl = document.querySelector(`#psui_${CSS.escape(id)}`);
      const vRaw = (vEl && vEl.value.trim()) || "";
      const iRaw = (iEl && iEl.value.trim()) || "";
      if(!vRaw && !iRaw){ toast(`Enter a voltage or a current for ${id} first.`); return; }
      const done = [];
      if(vRaw){
        const v = readNum(`#psuv_${CSS.escape(id)}`, `${id} voltage`);
        if(v === null) return;
        await post(`/api/supply/${id}/voltage`, {volts: v});
        done.push(`${v} V`);
      }
      if(iRaw){
        const a = readNum(`#psui_${CSS.escape(id)}`, `${id} current`);
        if(a === null) return;
        await post(`/api/supply/${id}/current`, {amps: a});
        done.push(`${a} A`);
      }
      toast(`${id} set to ${done.join(" · ")}`, true);
    } else if(b.dataset.act === "psuout"){
      /* Turning an output ON is the one click here that energises something,
         so it asks first. Turning off never does. */
      const on = b.dataset.state !== "1";
      if(on && !confirm(`Turn ON the ${id} output?

`
          + `It will source at whatever this supply's setpoints currently are.`))
        return;
      await post(`/api/supply/${id}/output`, {on});
      toast(`${id} output ${on ? "ON" : "off"}`, true);
    }
  }catch(_){ /* toast already shown */ }
});

document.addEventListener("keydown", ev => {
  if(ev.key !== "Enter") return;
  const input = ev.target.closest('input[data-f="input"]');
  if(!input) return;
  const btn = input.parentElement.querySelector("button[data-act]");
  if(btn && !btn.disabled) btn.click();
});

// Rename a valve / MFC / gauge (e.g. when swapping precursors). Display-only;
// persisted server-side. Blank reverts to the reactor.yaml name.
document.addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-rename]");
  if(!b) return;
  const kind = b.dataset.rename, id = b.dataset.id;
  const tile = b.closest(".valve, .tile");
  const cur = (tile && tile.querySelector('[data-f="label"],[data-f="name"]')
                ?.textContent || "").trim();
  const next = prompt(`Rename this ${kind} (blank = default name):`, cur);
  if(next === null) return;                       // cancelled
  try{
    const r = await post("/api/label", {kind, id, label: next});
    toast(`Renamed to "${r.label || "(default)"}"`, true);
  }catch(_){ /* toast already shown */ }
});

// ALD run params: persist to localStorage, launch via /api/run/ald
// Every p_* field persisted between sessions - run parameters plus the display
// preferences that sit alongside them. What actually gets sent to the server is
// runParams(), not this list.
const ALD_KEYS = ["cycles","dose_pressure_torr","dose_s","pump_a_s","beam_s",
  "pump_b_s","min_current_ua","sample_bias_v","sample_bias_polarity",
  "fill_pulse_on_s","fill_pulse_off_s","tolerance_pct",
  "reignite_pulse_s","reignite_settle_s","ar_close_delay_s",
  "pre_ar_sccm","pre_valve_delay_s","pre_hold_s",
  "gas_overlap_s","smooth_on","smooth_n",
  "h2_gas_enable","h2_gas_order","h2_gas_pct","h2_gas_flow_sccm",
  "n2_gas_enable","n2_gas_order","n2_gas_pct","n2_gas_flow_sccm"];

/* EE-ALD vs EE-CVD. EE-CVD holds the beam on for the whole run, so it has no
   beam-exposure or pump-B parameter and only two cycle phases. Elements tagged
   data-mode="<mode>" are shown only in that mode. */
function runMode(){ return $("modeSel").value === "cvd" ? "cvd" : "ald"; }
function applyMode(){
  const m = runMode();
  for(const el of document.querySelectorAll("[data-mode]"))
    cls(el, "hidden", el.dataset.mode !== m);
  $("runTitle").textContent = m === "cvd" ? "EE-CVD run" : "EE-ALD run";
  $("gasWindowWord").textContent = m === "cvd" ? "cycle" : "exposure";
  localStorage.setItem("runMode", m);
  updateGasSchedHint();
}

function updateSmoothHint(){
  const sm = smoothing();
  const el = $("smoothHint");
  el.textContent = sm.on
    ? `${sm.n}-point centred average on chamber pressure (~${(sm.n/5).toFixed(1)} s)`
    : "off — chamber pressure drawn raw";
  el.style.color = sm.on ? "var(--accent)" : "var(--dim)";
}
function paramGet(k){
  const el = $("p_"+k);
  return el ? (el.type === "checkbox" ? el.checked : el.value) : undefined;
}
function paramSet(k, v){
  const el = $("p_"+k);
  if(!el) return;
  if(el.type === "checkbox") el.checked = !!v; else el.value = v;
}
/* Run parameters are owned by the SERVER, with localStorage as a per-browser
   cache. They used to be localStorage-only, so every machine had its own set
   and a Tailscale view showed defaults instead of what the reactor PC had
   configured. One reactor, one set of parameters.

   Load order: local cache first (instant, and works if the server is
   unreachable), then the server's copy on top of it. */
async function loadParams(){
  try{
    const p = JSON.parse(localStorage.getItem("aldParams")||"{}");
    for(const k of ALD_KEYS) if(p[k]!=null) paramSet(k, p[k]);
  }catch(_){}
  try{
    const r = await fetch("/api/run_params");
    if(r.ok){
      const p = (await r.json()).params || {};
      for(const k of ALD_KEYS) if(p[k]!=null) paramSet(k, p[k]);
      // Mirror the server's copy locally so the next load starts from it.
      if(Object.keys(p).length) localStorage.setItem("aldParams", JSON.stringify(p));
    }
  }catch(_){ /* server copy unavailable - the cache above already applied */ }
  updateReigniteHint();
  updateGasSchedHint();
  updateSmoothHint();
}

let saveParamsTimer = null;
function saveParams(){
  const p={}; for(const k of ALD_KEYS) p[k]=paramGet(k);
  localStorage.setItem("aldParams", JSON.stringify(p));
  /* Debounced: saveParams() fires on every keystroke in the params grid, and
     one POST per character would be silly. Fire-and-forget - a failed save
     leaves the local cache correct and is not worth interrupting the operator
     over. */
  clearTimeout(saveParamsTimer);
  saveParamsTimer = setTimeout(() => {
    fetch("/api/run_params", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(p)
    }).catch(() => {});
  }, 800);
}

/* Run name. Deliberately NOT persisted in localStorage with the other params:
   it is owned by the server, which records the name of each run as it actually
   starts, so the sequence follows real runs and is the same on every browser
   that connects. `force` overwrites whatever is in the box (used after a run
   starts, when the old name has just been consumed); otherwise a name the
   operator has typed is left alone. */
let suggestedRunName = "";
async function refreshRunName(force){
  try{
    const r = await fetch("/api/run/next-name");
    if(!r.ok) return;
    const d = await r.json();
    const el = $("p_run_name");
    const untouched = !el.value.trim() || el.value.trim() === suggestedRunName;
    suggestedRunName = d.suggested || "";
    if(force || untouched) el.value = suggestedRunName;
  }catch(_){ /* naming is a convenience; a failed fetch must not block a run */ }
}
// Shows the achieved retry rate so it's obvious whether the two fields above
// meet "at least twice a second" without doing the arithmetic by hand. A full
// attempt is the 0.2s current-check poll plus these two, not just the two.
// It sits in the collapsed section's summary so it stays readable when the
// advanced fields are folded away.
const REIGNITE_POLL_S = 0.2;
function updateReigniteHint(){
  const pulse = parseFloat($("p_reignite_pulse_s").value)||0;
  const settle = parseFloat($("p_reignite_settle_s").value)||0;
  const total = REIGNITE_POLL_S + pulse + settle;
  const hz = total > 0 ? 1/total : Infinity;
  const el = $("reigniteHint");
  el.textContent = `reignite ${total.toFixed(2)}s/attempt = ${hz.toFixed(1)}/s`
    + (hz < 2 ? "  (slower than 2/s)" : "");
  el.style.color = hz < 2 ? "var(--warn)" : "var(--dim)";
}
// Live plain-English timeline for the gas schedule, mirroring exactly what
// RecipeRunner._electron_beam computes at run time (see recipe.py) - and
// flags the same order collision the server will otherwise reject with a 409.
function readGasField(id){
  return {
    id, enable: $("p_"+id+"_gas_enable").checked,
    order: $("p_"+id+"_gas_order").value,
    pct: parseFloat($("p_"+id+"_gas_pct").value) || 0,
    flow: parseFloat($("p_"+id+"_gas_flow_sccm").value) || 0,
  };
}
function updateGasSchedHint(){
  // EE-ALD measures against the beam exposure and reports offsets from beam
  // start; EE-CVD measures against the whole cycle (dose + pump A) and reports
  // offsets from cycle start. Mirrors _electron_beam and _beam_watch in
  // recipe.py respectively.
  const cvd = runMode() === "cvd";
  const span = cvd
    ? (parseFloat($("p_dose_s").value) || 0) + (parseFloat($("p_pump_a_s").value) || 0)
    : (parseFloat($("p_beam_s").value) || 0);
  const anchor = cvd ? "cycle" : "beam";
  const ov = parseFloat($("p_gas_overlap_s").value) || 0;
  const gases = ["h2","n2"].map(readGasField).filter(g => g.enable);
  const first = gases.find(g => g.order === "first");
  const second = gases.find(g => g.order === "second");
  const collision = gases.filter(g=>g.order==="first").length > 1
                  || gases.filter(g=>g.order==="second").length > 1;
  const handoff = first ? first.pct/100*span : 0;
  const secondOff = Math.min(span, handoff + (second ? second.pct/100*span : 0));
  const lines = [];
  if(first){
    // EE-CVD has no run-up before a cycle, so "first" re-arms by the overlap
    // before the cycle ends instead — the same handoff, wrapped.
    const on = cvd
      ? `on ${anchor}+0s (re-arms +${Math.max(0, secondOff-ov).toFixed(2)}s)`
      : `on ${anchor}−${ov.toFixed(2)}s`;
    lines.push(`${first.id.toUpperCase()} ${on}`
      + ` · off ${anchor}+${handoff.toFixed(2)}s (${first.pct}% @ ${first.flow}sccm)`);
  }
  if(second){
    const onAt = Math.max(0, handoff - ov);
    lines.push(`${second.id.toUpperCase()} on ${anchor}+${onAt.toFixed(2)}s`
      + ` · off ${anchor}+${secondOff.toFixed(2)}s (${second.pct}% @ ${second.flow}sccm)`);
  }
  const el = $("gasSchedHint");
  el.textContent = collision
    ? "Both gases set to the same order — pick First for one, Second for the other."
    : (gases.length ? lines.join("   ") : "no gas scheduled — H2/N2 stay off all run");
  el.style.color = collision ? "var(--warn)" : "var(--dim)";
}
$("aldParams").addEventListener("input", () => { saveParams(); updateReigniteHint(); updateGasSchedHint(); });
$("gasSchedSection").addEventListener("input", () => { saveParams(); updateGasSchedHint(); });
$("gasSchedSection").addEventListener("change", () => { saveParams(); updateGasSchedHint(); });
$("advSection").addEventListener("input", () => { saveParams(); updateReigniteHint(); });
$("preSection").addEventListener("input", saveParams);
$("modeSel").onchange = applyMode;
// The toggle lives in the <summary> so it stays reachable while collapsed, so
// its clicks must not also open/close the panel.
$("p_smooth_on").addEventListener("click", e => e.stopPropagation());
$("smoothSection").addEventListener("input", () => {
  saveParams(); updateSmoothHint(); drawChart();
});

/** Everything the run builders accept, for whichever mode is selected. */
function runParams(){
  const P = k => parseFloat($("p_"+k).value);
  const cvd = runMode() === "cvd";
  const params = {
    cycles: parseInt($("p_cycles").value,10)||1,
    dose_pressure_torr: P("dose_pressure_torr"),
    dose_s: P("dose_s"), pump_a_s: P("pump_a_s"),
    min_current_a: (P("min_current_ua")||500)*1e-6,
    // Magnitude and lead orientation. The supply is single-quadrant, so the
    // sign only ever reaches the LOG, never the instrument - see
    // Supervisor.supplies_output_on.
    sample_bias_v: Math.abs(P("sample_bias_v") || 0),
    sample_bias_polarity: parseInt($("p_sample_bias_polarity").value, 10) || 1,
    fill_pulse_on_s: P("fill_pulse_on_s"), fill_pulse_off_s: P("fill_pulse_off_s"),
    tolerance_frac: (P("tolerance_pct")||20)/100,
    reignite_pulse_s: P("reignite_pulse_s"), reignite_settle_s: P("reignite_settle_s"),
    ar_close_delay_s: P("ar_close_delay_s"),
    run_name: ($("p_run_name").value || "").trim(),
  };
  if(!cvd){ params.beam_s = P("beam_s"); params.pump_b_s = P("pump_b_s"); }
  params.gas_overlap_s = P("gas_overlap_s") || 0;
  for(const gas of ["h2","n2"]){
    const g = readGasField(gas);
    params[gas+"_gas_enable"] = g.enable;
    params[gas+"_gas_order"] = g.order;
    params[gas+"_gas_pct"] = g.pct;
    params[gas+"_gas_flow_sccm"] = g.flow;
  }
  return params;
}

$("aldStart").onclick = async () => {
  saveParams();
  const cvd = runMode() === "cvd";
  const params = runParams();
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
    await refreshRunName(true);   // this name is now used; offer the next one
  }catch(_){}
};

// Pre-start: the dialog is the point of the button as much as the sequence is -
// the three valves have to be in REMOTE and the supplies on, or the sequence
// commands hardware that cannot respond.
$("preStartBtn").onclick = async () => {
  saveParams();
  // The bias is the one output here that energises the sample, so the dialog
  // states it explicitly rather than leaving the operator to remember what is
  // in the field.
  const biasV = Math.abs(parseFloat($("p_sample_bias_v").value) || 0);
  const biasSign = parseInt($("p_sample_bias_polarity").value, 10) < 0 ? "−" : "+";
  const biasLine = biasV > 0
    ? `Sample bias: ${biasSign}${biasV} V — the stage WILL be energised.`
    : "Sample bias: 0 V — stage bias stays off.";
  if(!confirm("Set Ar Pneumatic, Plasma Ground, and Precursor Fill to Remote.\n"
    + "Turn on HV at the Glassman front panel if you want plasma.\n\n"
    + "Pre-start will switch ON the steering, grid and collimating supply "
    + "outputs — they stay on for the whole run — then open the Ar pneumatic, "
    + "flow Ar, start the precursor fill pulse, and strike the plasma and hold "
    + "it. It retries the strike until you press Stop pre-start.\n\n"
    + biasLine)) return;
  const P = k => parseFloat($("p_"+k).value);
  const p = runParams();
  try{
    await post("/api/prestart/start", {
      ar_sccm: P("pre_ar_sccm"),
      valve_delay_s: P("pre_valve_delay_s"),
      hold_s: P("pre_hold_s"),
      dose_pressure_torr: p.dose_pressure_torr,
      fill_pulse_on_s: p.fill_pulse_on_s, fill_pulse_off_s: p.fill_pulse_off_s,
      tolerance_frac: p.tolerance_frac,
      min_current_a: p.min_current_a,
      reignite_pulse_s: p.reignite_pulse_s, reignite_settle_s: p.reignite_settle_s,
      sample_bias_v: p.sample_bias_v,
      sample_bias_polarity: p.sample_bias_polarity,
    });
    $("preSection").open = true;
  }catch(_){}
};
/* Restart the server so it picks up changed code (requested 2026-08-25, after
   a pre-start silently did nothing against a four-day-old process).

   The dialog is most of the feature: a restart is not a neutral act on this
   tool. The WebSocket already retries every 1.5 s, so the page reconnects on
   its own once the new process is listening - all this has to do is say so and
   stop the button being pressed twice. */
$("shutdownBtn").onclick = async () => {
  // lastRunState is maintained by recordRun() on every frame.
  const running = ["running", "paused", "aborting"].includes(lastRunState);
  const lines = [
    "Shut down the reactor server?",
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
    "The interface will go offline. Start it again from the Reactor Interface "
      + "shortcut.",
  ].filter(l => l !== null);
  if(!confirm(lines.join("\n"))) return;

  const btn = $("shutdownBtn");
  btn.disabled = true;
  $("shutdownHint").textContent = "Shutting down… this page will go offline. "
    + "Start the server again from the Reactor Interface shortcut.";
  try{
    const r = await post("/api/server/shutdown");
    setLink("shutting down", "warn");
    if(r && r.also_killed && r.also_killed.length)
      toast(`Also killed ${r.also_killed.length} other server instance(s)`, true);
  }catch(_){
    // A 501 means this server cannot stop itself; post() already toasted.
    btn.disabled = false;
    $("shutdownHint").textContent = "Shutdown is not available for this server.";
  }
};

$("preStopBtn").onclick = () => post("/api/prestart/stop");
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
(async () => {
  try{ CFG = await (await fetch("/api/config")).json(); }catch(_){ CFG = {}; }
  $("modeSel").value = localStorage.getItem("runMode") || "ald";
  await loadParams();
  applyMode();
  refreshRunName(false);
  $("shutdownHint").textContent = SHUTDOWN_HINT;
  await seedTrend();
  await seedEvents();
  connect();
})();
