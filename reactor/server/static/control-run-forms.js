/** Run-parameter form state, persistence, mode selection and derived hints. */
const PARAM_KEYS = ["cycles","dose_pressure_torr","dose_s","pump_a_s","beam_s",
  "pump_b_s","min_current_ua","sample_bias_v","sample_bias_polarity",
  "fill_pulse_on_s","fill_pulse_off_s","tolerance_pct",
  "reignite_pulse_s","reignite_settle_s","ar_close_delay_s",
  "pre_ar_sccm","pre_valve_delay_s","pre_hold_s",
  "gas_overlap_s","smooth_on","smooth_n",
  "h2_gas_enable","h2_gas_order","h2_gas_pct","h2_gas_flow_sccm",
  "n2_gas_enable","n2_gas_order","n2_gas_pct","n2_gas_flow_sccm"];

export function createRunForms({$, document, storage, fetchImpl, cls, smoothing,
                                drawChart, setTimeoutImpl=setTimeout,
                                clearTimeoutImpl=clearTimeout}) {
  let saveTimer = null;
  let suggestedRunName = "";
  let mounted = false;
  const listeners = [];

  function listen(el, name, fn){
    el.addEventListener(name, fn);
    listeners.push(() => el.removeEventListener(name, fn));
  }
  function runMode(){ return $("modeSel").value === "cvd" ? "cvd" : "ald"; }
  function applyMode(){
    const mode = runMode();
    for(const el of document.querySelectorAll("[data-mode]"))
      cls(el, "hidden", el.dataset.mode !== mode);
    $("runTitle").textContent = mode === "cvd" ? "EE-CVD run" : "EE-ALD run";
    $("gasWindowWord").textContent = mode === "cvd" ? "cycle" : "exposure";
    storage.setItem("runMode", mode);
    updateGasSchedHint();
  }
  function updateSmoothHint(){
    const value = smoothing();
    const el = $("smoothHint");
    el.textContent = value.on
      ? `${value.n}-point centred average on chamber pressure (~${(value.n/5).toFixed(1)} s)`
      : "off — chamber pressure drawn raw";
    el.style.color = value.on ? "var(--accent)" : "var(--dim)";
  }
  function paramGet(key){
    const el = $("p_" + key);
    return el ? (el.type === "checkbox" ? el.checked : el.value) : undefined;
  }
  function paramSet(key, value){
    const el = $("p_" + key);
    if(!el) return;
    if(el.type === "checkbox") el.checked = !!value;
    else el.value = value;
  }
  async function loadParams(){
    try{
      const params = JSON.parse(storage.getItem("aldParams") || "{}");
      for(const key of PARAM_KEYS) if(params[key] != null) paramSet(key, params[key]);
    }catch(_){}
    try{
      const response = await fetchImpl("/api/run_params");
      if(response.ok){
        const params = (await response.json()).params || {};
        for(const key of PARAM_KEYS) if(params[key] != null) paramSet(key, params[key]);
        if(Object.keys(params).length)
          storage.setItem("aldParams", JSON.stringify(params));
      }
    }catch(_){}
    updateReigniteHint();
    updateGasSchedHint();
    updateSmoothHint();
  }
  function saveParams(){
    const params = {};
    for(const key of PARAM_KEYS) params[key] = paramGet(key);
    storage.setItem("aldParams", JSON.stringify(params));
    if(saveTimer !== null) clearTimeoutImpl(saveTimer);
    saveTimer = setTimeoutImpl(() => {
      saveTimer = null;
      fetchImpl("/api/run_params", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify(params)
      }).catch(() => {});
    }, 800);
  }
  async function refreshRunName(force){
    try{
      const response = await fetchImpl("/api/run/next-name");
      if(!response.ok) return;
      const data = await response.json();
      const el = $("p_run_name");
      const untouched = !el.value.trim() || el.value.trim() === suggestedRunName;
      suggestedRunName = data.suggested || "";
      if(force || untouched) el.value = suggestedRunName;
    }catch(_){}
  }
  function updateReigniteHint(){
    const pulse = parseFloat($("p_reignite_pulse_s").value) || 0;
    const settle = parseFloat($("p_reignite_settle_s").value) || 0;
    const total = 0.2 + pulse + settle;
    const hz = total > 0 ? 1/total : Infinity;
    const el = $("reigniteHint");
    el.textContent = `reignite ${total.toFixed(2)}s/attempt = ${hz.toFixed(1)}/s`
      + (hz < 2 ? "  (slower than 2/s)" : "");
    el.style.color = hz < 2 ? "var(--warn)" : "var(--dim)";
  }
  function readGasField(id){
    return {id, enable:$("p_"+id+"_gas_enable").checked,
      order:$("p_"+id+"_gas_order").value,
      pct:parseFloat($("p_"+id+"_gas_pct").value) || 0,
      flow:parseFloat($("p_"+id+"_gas_flow_sccm").value) || 0};
  }
  function updateGasSchedHint(){
    const cvd = runMode() === "cvd";
    const span = cvd
      ? (parseFloat($("p_dose_s").value) || 0) + (parseFloat($("p_pump_a_s").value) || 0)
      : (parseFloat($("p_beam_s").value) || 0);
    const anchor = cvd ? "cycle" : "beam";
    const overlap = parseFloat($("p_gas_overlap_s").value) || 0;
    const gases = ["h2", "n2"].map(readGasField).filter(g => g.enable);
    const first = gases.find(g => g.order === "first");
    const second = gases.find(g => g.order === "second");
    const collision = gases.filter(g => g.order === "first").length > 1
      || gases.filter(g => g.order === "second").length > 1;
    const handoff = first ? first.pct/100*span : 0;
    const secondOff = Math.min(span, handoff + (second ? second.pct/100*span : 0));
    const lines = [];
    if(first){
      const on = cvd
        ? `on ${anchor}+0s (re-arms +${Math.max(0, secondOff-overlap).toFixed(2)}s)`
        : `on ${anchor}−${overlap.toFixed(2)}s`;
      lines.push(`${first.id.toUpperCase()} ${on} · off ${anchor}+${handoff.toFixed(2)}s `
        + `(${first.pct}% @ ${first.flow}sccm)`);
    }
    if(second){
      const onAt = Math.max(0, handoff - overlap);
      lines.push(`${second.id.toUpperCase()} on ${anchor}+${onAt.toFixed(2)}s `
        + `· off ${anchor}+${secondOff.toFixed(2)}s (${second.pct}% @ ${second.flow}sccm)`);
    }
    const el = $("gasSchedHint");
    el.textContent = collision
      ? "Both gases set to the same order — pick First for one, Second for the other."
      : (gases.length ? lines.join("   ") : "no gas scheduled — H2/N2 stay off all run");
    el.style.color = collision ? "var(--warn)" : "var(--dim)";
  }
  function runParams(){
    const value = key => parseFloat($("p_" + key).value);
    const cvd = runMode() === "cvd";
    const params = {
      cycles:parseInt($("p_cycles").value, 10) || 1,
      dose_pressure_torr:value("dose_pressure_torr"),
      dose_s:value("dose_s"), pump_a_s:value("pump_a_s"),
      min_current_a:(value("min_current_ua") || 500)*1e-6,
      sample_bias_v:Math.abs(value("sample_bias_v") || 0),
      sample_bias_polarity:parseInt($("p_sample_bias_polarity").value, 10) || 1,
      fill_pulse_on_s:value("fill_pulse_on_s"),
      fill_pulse_off_s:value("fill_pulse_off_s"),
      tolerance_frac:(value("tolerance_pct") || 20)/100,
      reignite_pulse_s:value("reignite_pulse_s"),
      reignite_settle_s:value("reignite_settle_s"),
      ar_close_delay_s:value("ar_close_delay_s"),
      run_name:($("p_run_name").value || "").trim(),
    };
    if(!cvd){ params.beam_s = value("beam_s"); params.pump_b_s = value("pump_b_s"); }
    params.gas_overlap_s = value("gas_overlap_s") || 0;
    for(const gas of ["h2", "n2"]){
      const item = readGasField(gas);
      params[gas+"_gas_enable"] = item.enable;
      params[gas+"_gas_order"] = item.order;
      params[gas+"_gas_pct"] = item.pct;
      params[gas+"_gas_flow_sccm"] = item.flow;
    }
    return params;
  }
  function mount(){
    if(mounted) return;
    mounted = true;
    $("modeSel").value = storage.getItem("runMode") || "ald";
    listen($("aldParams"), "input", () => {
      saveParams(); updateReigniteHint(); updateGasSchedHint();
    });
    listen($("gasSchedSection"), "input", () => {saveParams(); updateGasSchedHint();});
    listen($("gasSchedSection"), "change", () => {saveParams(); updateGasSchedHint();});
    listen($("advSection"), "input", () => {saveParams(); updateReigniteHint();});
    listen($("preSection"), "input", saveParams);
    listen($("modeSel"), "change", applyMode);
    listen($("p_smooth_on"), "click", event => event.stopPropagation());
    listen($("smoothSection"), "input", () => {
      saveParams(); updateSmoothHint(); drawChart();
    });
  }
  function dispose(){
    for(const remove of listeners.splice(0)) remove();
    if(saveTimer !== null) clearTimeoutImpl(saveTimer);
    saveTimer = null;
    mounted = false;
  }
  return {applyMode, dispose, loadParams, mount, refreshRunName, runMode,
          runParams, saveParams, updateGasSchedHint};
}
