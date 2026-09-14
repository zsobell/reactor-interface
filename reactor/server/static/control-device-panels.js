/** Device tile rendering and the delegated handlers that can command devices. */
export function createDevicePanels({$, document, cssEscape, esc, num, sci, put, cls,
                                    reconcile, post, toast, confirmImpl, promptImpl}) {
  let mounted = false;

  /* The Glassman card remains monitor-only. Keithley fields and output controls
     preserve the manually requested browser command paths. */
  function supplies(state){
    const box = $("supplies");
    const list = state.power_supplies || [];
    const isKeithley = supply => supply.driver === "keithley_2260b";
    reconcile(box, list, supply => supply.id, supply => isKeithley(supply) ? `
      <div class="tile psu" data-id="${esc(supply.id)}">
        <div class="top"><span class="name">${esc(supply.label || supply.id)}</span>
          <span class="mode" data-f="mode" title="constant-voltage / constant-current"></span>
          <div style="flex:1"></div><span class="read"><span data-f="v">—</span><span class="unit">V</span></span></div>
        <div class="kv"><span>Current</span><span><span data-f="i">—</span> A</span></div>
        <div class="row" style="margin-top:6px">
          <input type="number" step="0.1" min="0" placeholder="V" id="psuv_${esc(supply.id)}" data-f="vin" style="flex:1;min-width:64px">
          <input type="number" step="0.01" min="0" placeholder="A" id="psui_${esc(supply.id)}" data-f="iin" style="flex:1;min-width:64px">
          <button data-act="psuset" data-id="${esc(supply.id)}">Set</button></div>
        <div class="row" style="margin-top:6px"><button data-act="psuout" data-id="${esc(supply.id)}" data-f="outbtn" style="flex:1">Output</button></div>
        <div class="kv" style="margin-top:6px"><span>Setpoint (device)</span><span data-f="sp">—</span></div>
        <div class="chips"><span class="chip" data-f="chipOut">output</span><span class="chip" data-f="chipRole"></span><span class="chip" data-f="chipQues"></span></div>
        <div class="kv"><span>Model</span><span data-f="model">—</span></div>
        <div class="kv"><span>Rated</span><span data-f="rated">—</span></div>
        <div class="kv"><span>Link</span><span data-f="link">—</span></div>
        <div class="note" data-f="err"></div><div class="hint" data-f="hint"></div>
      </div>` : `
      <div class="tile" data-id="${esc(supply.id)}">
        <div class="top"><span class="name">${esc(supply.label || supply.id)}</span><div style="flex:1"></div>
          <span class="read"><span data-f="v">—</span><span class="unit">${esc(supply.unit_v || "V")}</span></span></div>
        <div class="kv"><span>Current</span><span><span data-f="i">—</span> ${esc(supply.unit_i || "mA")}</span></div>
        <div class="kv"><span>Arc count</span><span data-f="arcs">—</span></div>
        <div class="chips"><span class="chip" data-f="chipHv">HV</span><span class="chip" data-f="chipMode">mode</span><span class="chip" data-f="chipRem">local</span><span class="chip" data-f="chipTrip">I-trip</span></div>
        <div class="kv"><span>Model</span><span data-f="model">—</span></div>
        <div class="kv"><span>Link</span><span data-f="link">—</span></div>
        <div class="note" data-f="faults"></div><div class="note" data-f="err"></div>
        <div class="hint">Monitor only — set voltage and current on the supply's front panel. HV is commanded off at the end of a run.</div>
      </div>`);

  }

  function renderSupplyState(state, supply, el, isKeithley){
    const prefix = isKeithley ? "psu." : "hv.";
    const reading = state.readings[prefix+supply.id+".voltage"];
    const bad = !!(reading && !reading.ok);
    const chip = (field, text, on, hot) => {
      const target = put(el, field, text);
      if(target){ cls(target, "on", !!on && !hot); cls(target, "hot", !!hot); }
    };
    const voltage = put(el, "v", sci(state.snapshot[prefix+supply.id+".voltage"], 4));
    if(voltage) cls(voltage, "stale", bad);
    put(el, "i", sci(state.snapshot[prefix+supply.id+".current"], 4));
    put(el, "model", supply.model || supply.driver || "—");
    put(el, "err", bad ? reading.detail : (supply.error || ""));
    if(isKeithley){
      cls(el, "trip", !supply.connected);
      chip("chipOut", supply.output_on == null ? "output —"
        : (supply.output_on ? "OUTPUT ON" : "output off"), supply.output_on, false);
      const sign = supply.polarity < 0 ? "−" : "+";
      chip("chipRole", supply.is_sample_bias ? `sample bias  ${sign}` : "", false, false);
      chip("chipQues", supply.questionable ? `status 0x${supply.questionable.toString(16)}` : "",
        false, !!supply.questionable);
      put(el, "rated", supply.max_voltage != null && supply.max_current != null
        ? `${(+supply.max_voltage).toFixed(1)} V · ${(+supply.max_current).toFixed(3)} A max` : "—");
      put(el, "link", `${supply.port || "?"} · USB serial ${supply.usb_serial || "?"}`);
      put(el, "sp", supply.voltage_setpoint != null && supply.current_setpoint != null
        ? `${(+supply.voltage_setpoint).toFixed(3)} V · ${(+supply.current_setpoint).toFixed(3)} A` : "—");
      const mode = put(el, "mode", supply.mode || "");
      if(mode){ cls(mode, "cv", supply.mode === "CV"); cls(mode, "cc", supply.mode === "CC"); }
      const output = el.querySelector('[data-f="outbtn"]');
      if(output){
        output.textContent = supply.output_on ? "Output ON — click to turn off" : "Output off — click to turn on";
        cls(output, "danger", !!supply.output_on);
        output.dataset.state = supply.output_on ? "1" : "0";
        output.disabled = !supply.connected;
      }
      for(const field of ["vin", "iin"]){
        const input = el.querySelector(`[data-f="${field}"]`);
        if(input) input.disabled = !supply.connected;
      }
      const setButton = el.querySelector('[data-act="psuset"]');
      if(setButton) setButton.disabled = !supply.connected;
      put(el, "hint", supply.is_sample_bias
        ? "Pre-start sets this from the run's Sample bias field and switches it on only when that is non-zero. Fields above override it by hand."
        : "Output switched on at pre-start, off at run end. Fields above set it by hand.");
    } else {
      cls(el, "trip", !supply.connected || !!supply.faulted);
      const arcs = state.snapshot["hv."+supply.id+".arc_count"];
      put(el, "arcs", arcs == null ? "—" : String(arcs));
      chip("chipHv", supply.hv_on ? "HV ON" : "HV off", supply.hv_on, supply.hv_on);
      chip("chipMode", supply.voltage_mode == null ? "mode —" : (supply.voltage_mode ? "V mode" : "I mode"), false, false);
      chip("chipRem", supply.remote ? "remote" : "local", false, false);
      chip("chipTrip", supply.current_trip_enabled ? "I-trip on" : "I-trip off", supply.current_trip_enabled, false);
      put(el, "link", `${supply.port || "?"} · ${supply.baud || "?"} 8N1 · addr ${supply.address}`
        + (supply.firmware ? ` · fw ${supply.firmware}` : ""));
      put(el, "faults", supply.faults && supply.faults.length ? "FAULT: " + supply.faults.join(", ") : "");
    }
  }

  // Kept separate from tile creation so telemetry updates never replace an input mid-edit.
  function updateSupplies(state){
    supplies(state);
    const box = $("supplies");
    for(const supply of state.power_supplies || []){
      const el = box.querySelector(`[data-id="${cssEscape(supply.id)}"]`);
      if(el) renderSupplyState(state, supply, el, supply.driver === "keithley_2260b");
    }
  }

  function mfcs(state){
    const box = $("mfcs");
    reconcile(box, state.mfcs, mfc => mfc.id, mfc => `
      <div class="tile" data-id="${esc(mfc.id)}">
        <div class="top"><span class="name" data-f="name">${esc(mfc.label || mfc.id)}</span>
          <button class="rename" data-rename="mfc" data-id="${esc(mfc.id)}" title="Rename MFC">✎</button>
          <span class="src" data-f="gas"></span><div style="flex:1"></div>
          <span class="read"><span data-f="flow">—</span><span class="unit">sccm</span></span></div>
        <div class="bar"><i data-f="bar" style="width:0"></i></div>
        <div class="row" style="margin-top:0"><input type="number" step="0.01" min="0" placeholder="sccm" id="mfc_${esc(mfc.id)}" data-f="input" style="flex:1;min-width:80px"><button data-act="mfc" data-id="${esc(mfc.id)}">Set flow</button></div>
        <div class="kv" style="margin-top:6px"><span>Setpoint (device)</span><span data-f="sp">—</span></div>
        <div class="kv"><span>Full scale</span><span data-f="fs">—</span></div>
        <div class="kv"><span>Body temp</span><span data-f="temp">—</span></div>
        <div class="kv"><span>Device mode</span><span data-f="mode">—</span></div>
        <div class="note" data-f="health"></div><div class="note" data-f="err"></div><div class="note" data-f="iso"></div>
      </div>`);
    const valvesById = Object.fromEntries(state.valves.map(valve => [valve.id, valve]));
    for(const mfc of state.mfcs){
      const el = box.querySelector(`[data-id="${cssEscape(mfc.id)}"]`);
      if(!el) continue;
      cls(el, "trip", !mfc.connected);
      const flow = state.snapshot[`mfc.${mfc.id}.flow`];
      const percent = state.snapshot[`mfc.${mfc.id}.flow_pct`];
      put(el, "name", mfc.label || mfc.id); put(el, "gas", mfc.gas || "");
      put(el, "flow", flow == null ? "—" : Number(flow).toFixed(3));
      put(el, "sp", num(state.snapshot[`mfc.${mfc.id}.setpoint`], 3) + " sccm");
      put(el, "fs", mfc.full_scale_sccm == null ? "—" : `${num(mfc.full_scale_sccm,1)} sccm`
        + (typeof percent === "number" ? `  (${percent.toFixed(1)}%)` : ""));
      put(el, "temp", num(state.snapshot[`mfc.${mfc.id}.temp`],1) + " °C");
      put(el, "mode", mfc.device_mode || "—");
      const bad = mfc.health ? Object.entries(mfc.health).filter(([, value]) => value && value !== "No") : [];
      put(el, "health", bad.length ? "device reports: " + bad.map(([key,value]) => `${key}=${value}`).join(", ") : "");
      put(el, "err", mfc.error || "");
      const bar = el.querySelector('[data-f="bar"]');
      if(bar) bar.style.width = Math.max(0, Math.min(100, typeof percent === "number" ? percent : 0)) + "%";
      const isolation = mfc.isolation_valve ? valvesById[mfc.isolation_valve] : null;
      const closed = !!(isolation && !isolation.open);
      put(el, "iso", closed ? `${isolation.label} is closed — open it to set flow above 0` : "");
      el.querySelector('[data-act="mfc"]').disabled = !mfc.connected || closed;
      el.querySelector('[data-f="input"]').disabled = !mfc.connected || closed;
    }
  }

  function valves(state){
    const box = $("valves");
    const banks = (state.valve_banks || []).slice();
    if(state.valves.some(valve => !valve.bank)) banks.push({id:"", label:"Ungrouped", note:""});
    const rows = [];
    for(const bank of banks){
      const members = state.valves.filter(valve => (valve.bank || "") === bank.id);
      if(!members.length) continue;
      rows.push({kind:"bank", id:"bank:"+bank.id, bank});
      for(const valve of members) rows.push({kind:"valve", id:"valve:"+valve.id, valve});
    }
    reconcile(box, rows, row => row.id, row => row.kind === "bank" ? `
      <div class="bankhdr" data-id="${esc(row.id)}"><div class="bankname">${esc(row.bank.label)}</div><div class="banknote">${esc(row.bank.note || "")}</div></div>` : `
      <div class="valve" data-id="${esc(row.id)}"><div class="lbl"><strong data-f="label">${esc(row.valve.label)}</strong><span class="sub" data-f="sub"></span></div>
        <button class="rename" data-rename="valve" data-id="${esc(row.valve.id)}" title="Rename valve">✎</button>
        <button data-act="valve" data-id="${esc(row.valve.id)}" data-f="btn">—</button></div>`);
    for(const row of rows){
      if(row.kind !== "valve") continue;
      const valve = row.valve;
      const el = box.querySelector(`[data-id="${cssEscape(row.id)}"]`);
      if(!el) continue;
      cls(el, "open", valve.open); put(el, "label", valve.label);
      put(el, "sub", `${valve.line || "no line"} · ${valve.open ? "OPEN" : "closed"}`);
      const button = el.querySelector('[data-f="btn"]');
      button.textContent = valve.open ? "Close" : "Open";
      button.dataset.state = valve.open ? "0" : "1";
      button.disabled = !valve.line;
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
  return {dispose, mfcs, mount, supplies:updateSupplies, valves};
}
