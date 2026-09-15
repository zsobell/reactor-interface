/** Device tile rendering and the delegated handlers that can command devices. */
export function createDevicePanels({$, document, cssEscape, esc, num, sci, put, cls,
                                    reconcile, post, toast, confirmImpl, promptImpl}) {
  let mounted = false;

  /* The Glassman card remains monitor-only. Keithley fields and output controls
     preserve the manually requested browser command paths. */
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
      const el = box.querySelector(`[data-id="${cssEscape(p.id)}"]`);
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
          ? "Pre-start ARMS this at the run's Sample bias level and leaves the "
            + "output off; each beam then switches it on a lead time early and "
            + "off a trail time late. Fields above override it by hand."
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



  // Kept separate from tile creation so telemetry updates never replace an input mid-edit.


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
      const el = box.querySelector(`[data-id="${cssEscape(m.id)}"]`);
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
      const el = box.querySelector(`[data-id="${cssEscape(r.id)}"]`);
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

  function readNumber(selector, what){
    const el = document.querySelector(selector);
    if(!el) return null;
    const raw = el.value.trim();
    if(raw === ""){ toast(`Enter a value for ${what} first.`); el.focus(); return null; }
    const value = parseFloat(raw);
    if(!Number.isFinite(value)){ toast(`"${raw}" is not a number.`); el.focus(); return null; }
    return value;
  }
  async function commandClick(event){
    const button = event.target.closest("button[data-act]");
    if(!button) return;
    const id = button.dataset.id;
    try{
      if(button.dataset.act === "valve"){
        await post(`/api/valve/${id}`, {state:button.dataset.state === "1"});
      }else if(button.dataset.act === "mfc"){
        const value = readNumber(`#mfc_${cssEscape(id)}`, `${id} flow`);
        if(value === null) return;
        const result = await post(`/api/mfc/${id}/setpoint`, {sccm:value});
        toast(`${id} setpoint set to ${result.setpoint_sccm} sccm`, true);
      }else if(button.dataset.act === "psuset"){
        const voltageEl = document.querySelector(`#psuv_${cssEscape(id)}`);
        const currentEl = document.querySelector(`#psui_${cssEscape(id)}`);
        const voltageRaw = (voltageEl && voltageEl.value.trim()) || "";
        const currentRaw = (currentEl && currentEl.value.trim()) || "";
        if(!voltageRaw && !currentRaw){ toast(`Enter a voltage or a current for ${id} first.`); return; }
        const done = [];
        if(voltageRaw){
          const value = readNumber(`#psuv_${cssEscape(id)}`, `${id} voltage`);
          if(value === null) return;
          await post(`/api/supply/${id}/voltage`, {volts:value}); done.push(`${value} V`);
        }
        if(currentRaw){
          const value = readNumber(`#psui_${cssEscape(id)}`, `${id} current`);
          if(value === null) return;
          await post(`/api/supply/${id}/current`, {amps:value}); done.push(`${value} A`);
        }
        toast(`${id} set to ${done.join(" · ")}`, true);
      }else if(button.dataset.act === "psuout"){
        const on = button.dataset.state !== "1";
        if(on && !confirmImpl(`Turn ON the ${id} output?\n\nIt will source at whatever this supply's setpoints currently are.`)) return;
        await post(`/api/supply/${id}/output`, {on});
        toast(`${id} output ${on ? "ON" : "off"}`, true);
      }
    }catch(_){}
  }
  function inputKeydown(event){
    if(event.key !== "Enter") return;
    const input = event.target.closest('input[data-f="input"]');
    if(!input) return;
    const button = input.parentElement.querySelector("button[data-act]");
    if(button && !button.disabled) button.click();
  }
  async function renameClick(event){
    const button = event.target.closest("button[data-rename]");
    if(!button) return;
    const kind = button.dataset.rename, id = button.dataset.id;
    const tile = button.closest(".valve, .tile");
    const current = (tile && tile.querySelector('[data-f="label"],[data-f="name"]')?.textContent || "").trim();
    const next = promptImpl(`Rename this ${kind} (blank = default name):`, current);
    if(next === null) return;
    try{
      const result = await post("/api/label", {kind, id, label:next});
      toast(`Renamed to "${result.label || "(default)"}"`, true);
    }catch(_){}
  }
  function mount(){
    if(mounted) return;
    mounted = true;
    document.addEventListener("click", commandClick);
    document.addEventListener("click", renameClick);
    document.addEventListener("keydown", inputKeydown);
  }
  function dispose(){
    if(!mounted) return;
    document.removeEventListener("click", commandClick);
    document.removeEventListener("click", renameClick);
    document.removeEventListener("keydown", inputKeydown);
    mounted = false;
  }
  return {dispose, mfcs, mount, supplies, valves};
}
