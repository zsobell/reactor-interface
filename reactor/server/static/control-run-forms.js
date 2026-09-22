/** Run-parameter form state, persistence, mode selection and derived hints. */
const PARAM_KEYS = ["cycles","dose_pressure_torr","dose_s","pump_a_s","beam_s",
  "pump_b_s","min_current_ua","sample_bias_v","sample_bias_polarity",
  "sample_bias_lead_s","sample_bias_trail_s",
  "fill_pulse_on_s","fill_pulse_off_s","tolerance_pct",
  "reignite_pulse_s","reignite_settle_s","ar_close_delay_s",
  "ar_soft_open_pulses","ar_soft_open_on_s","ar_soft_open_gap_s",
  "pre_ar_sccm","pre_valve_delay_s","pre_hold_s",
  "gas_overlap_s","smooth_on","smooth_n",
  "mfc1_gas_enable","mfc1_gas_order","mfc1_gas_pct","mfc1_gas_flow_sccm",
  "mfc2_gas_enable","mfc2_gas_order","mfc2_gas_pct","mfc2_gas_flow_sccm"];

export function createRunForms({$, document, storage, fetchImpl, cls, smoothing,
                                drawChart, setTimeoutImpl=setTimeout,
                                clearTimeoutImpl=clearTimeout,
                                onLiveEditStatus=null}) {
  let saveTimer = null;
  let suggestedRunName = "";
  let mounted = false;
  const listeners = [];
  const showLiveStatus = onLiveEditStatus || ((state, message) => {
    const el = $("liveEditStatus");
    if(!el) return;
    el.dataset.state = state;
    el.textContent = message || "";
  });

  function listen(el, name, fn){
    el.addEventListener(name, fn);
    listeners.push(() => el.removeEventListener(name, fn));
  }
  function runMode(){ return $("modeSel").value === "cvd" ? "cvd" : "ald"; }
  function applyMode(){
    const m = runMode();
    for(const el of document.querySelectorAll("[data-mode]"))
      cls(el, "hidden", el.dataset.mode !== m);
    $("runTitle").textContent = m === "cvd" ? "EE-CVD run" : "EE-ALD run";
    $("gasWindowWord").textContent = m === "cvd" ? "cycle" : "exposure";
    storage.setItem("runMode", m);
    updateGasSchedHint();
    refreshEstimate();          // EE-CVD has no beam or pump B, so it is shorter
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
      const p = JSON.parse(storage.getItem("aldParams")||"{}");
      for(const k of PARAM_KEYS) if(p[k]!=null) paramSet(k, p[k]);
    }catch(_){}
    try{
      const r = await fetchImpl("/api/run_params");
      if(r.ok){
        const p = (await r.json()).params || {};
        for(const k of PARAM_KEYS) if(p[k]!=null) paramSet(k, p[k]);
        // Mirror the server's copy locally so the next load starts from it.
        if(Object.keys(p).length) storage.setItem("aldParams", JSON.stringify(p));
      }
    }catch(_){ /* server copy unavailable - the cache above already applied */ }
    updateReigniteHint();
    updateGasSchedHint();
    updateSmoothHint();
    refreshEstimate();
  }
  function fieldKey(target){
    const id = target && target.id || "";
    if(!id.startsWith("p_")) return null;
    const key = id.slice(2);
    if(key === "min_current_ua") return "min_current_a";
    if(key === "tolerance_pct") return "tolerance_frac";
    if(key === "run_name" || key.startsWith("pre_") || key.startsWith("smooth_")) return null;
    return key;
  }
  function saveParams(event){
    const p={}; for(const k of PARAM_KEYS) p[k]=paramGet(k);
    storage.setItem("aldParams", JSON.stringify(p));
    const key = fieldKey(event && event.target);
    if(key) markLiveDirty(key);
    /* Debounced: saveParams() fires on every keystroke in the params grid, and
       one POST per character would be silly. Fire-and-forget - a failed save
       leaves the local cache correct and is not worth interrupting the operator
       over. */
    clearTimeoutImpl(saveTimer);
    saveTimer = setTimeoutImpl(() => {
      fetchImpl("/api/run_params", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(p)
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
    const gases = ["mfc1","mfc2"].map(readGasField).filter(g => g.enable);
    const first = gases.find(g => g.order === "first");
    const second = gases.find(g => g.order === "second");
    // Simultaneous is the one order two gases may share, so it is excluded from
    // the collision check — and needs its own, since exactly one is refused.
    const simul = gases.filter(g => g.order === "simultaneous");
    const collision = gases.filter(g=>g.order==="first").length > 1
                    || gases.filter(g=>g.order==="second").length > 1;
    // % is meaningless for a gas that covers the whole window.
    for(const g of ["mfc1","mfc2"]){
      const pct = $("p_"+g+"_gas_pct");
      pct.disabled = $("p_"+g+"_gas_order").value === "simultaneous";
      pct.style.opacity = pct.disabled ? 0.4 : "";
    }
    const handoff = first ? first.pct/100*span : 0;
    const secondOff = Math.min(span, handoff + (second ? second.pct/100*span : 0));
    const lines = [];
    if(first){
      // EE-CVD has no run-up before a cycle, so "first" re-arms by the overlap
      // before the cycle ends instead — the same handoff, wrapped.
      const on = cvd
        ? `on ${anchor}+0s (re-arms +${Math.max(0, secondOff-ov).toFixed(2)}s)`
        : `on ${anchor}−${ov.toFixed(2)}s`;
      lines.push(`${gasName(first.id)} ${on}`
        + ` · off ${anchor}+${handoff.toFixed(2)}s (${first.pct}% @ ${first.flow}sccm)`);
    }
    if(second){
      const onAt = Math.max(0, handoff - ov);
      lines.push(`${gasName(second.id)} on ${anchor}+${onAt.toFixed(2)}s`
        + ` · off ${anchor}+${secondOff.toFixed(2)}s (${second.pct}% @ ${second.flow}sccm)`);
    }
    if(simul.length){
      // No windows to report: every simultaneous gas is on for the whole thing.
      lines.length = 0;
      const on = cvd ? `on ${anchor}+0s` : `on ${anchor}−${ov.toFixed(2)}s`;
      for(const g of simul)
        lines.push(`${gasName(g.id)} ${on} · off ${anchor}+${span.toFixed(2)}s`
          + ` (whole ${anchor} @ ${g.flow}sccm)`);
    }
    const el = $("gasSchedHint");
    const problem = collision
      ? "Both gases set to the same order — pick First for one, Second for the other."
      : (simul.length === 1
          ? `${gasName(simul[0].id)} is Simultaneous on its own — set the `
            + `other gas to Simultaneous too, or put this one back to First or `
            + `Second. The run will not start like this.`
          : "");
    el.textContent = problem
      || (gases.length ? lines.join("   ")
          : `no gas scheduled — ${gasName("mfc1")}/${gasName("mfc2")} stay off all run`);
    el.style.color = problem ? "var(--warn)" : "var(--dim)";
  }
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
      // How far the bias brackets the beam. Scheduled inside pump A / pump B, so
      // these never lengthen a cycle - see RecipeRunner._schedule_bias.
      sample_bias_lead_s: P("sample_bias_lead_s") || 0,
      sample_bias_trail_s: P("sample_bias_trail_s") || 0,
      // Not used by the recipe builders - the server applies these as they are
      // saved, because a manual open carries no run params. They ride along so
      // the run's parameters file records the soft open it actually ran with.
      ar_soft_open_pulses: parseInt($("p_ar_soft_open_pulses").value, 10) || 0,
      ar_soft_open_on_s: P("ar_soft_open_on_s") || 0,
      ar_soft_open_gap_s: P("ar_soft_open_gap_s") || 0,
      fill_pulse_on_s: P("fill_pulse_on_s"), fill_pulse_off_s: P("fill_pulse_off_s"),
      tolerance_frac: (P("tolerance_pct")||20)/100,
      reignite_pulse_s: P("reignite_pulse_s"), reignite_settle_s: P("reignite_settle_s"),
      ar_close_delay_s: P("ar_close_delay_s"),
      run_name: ($("p_run_name").value || "").trim(),
    };
    if(!cvd){ params.beam_s = P("beam_s"); params.pump_b_s = P("pump_b_s"); }
    params.gas_overlap_s = P("gas_overlap_s") || 0;
    for(const gas of ["mfc1","mfc2"]){
      const g = readGasField(gas);
      params[gas+"_gas_enable"] = g.enable;
      params[gas+"_gas_order"] = g.order;
      params[gas+"_gas_pct"] = g.pct;
      params[gas+"_gas_flow_sccm"] = g.flow;
    }
    return params;
  }
  let MFC_GAS = {}, MFC_LABELS = {};
  let RUN_ACTIVE = false;
  let liveParamsTimer = null, estimateTimer = null;
  let runIdentity = null, runRevision = null, runGeneration = 0;
  let liveInFlight = false, liveRefreshInFlight = false, livePaused = false;
  let acknowledged = {}, dirtyKeys = new Set();
  let plannedRunS = null, etaLive = false;
  function gasName(id){
    if(MFC_GAS[id]) return MFC_GAS[id];
    const full = MFC_LABELS[id] || String(id).toUpperCase();
    return full.split(" - ")[0].trim() || full;
  }

  function applyGasNames(){
    for(const el of document.querySelectorAll("[data-gas-name]")){
      const name = gasName(el.dataset.gasName);
      if(el.textContent !== name) el.textContent = name;
    }
  }

  function gasScheduleError(){
    const simul = ["mfc1","mfc2"].map(readGasField)
      .filter(g => g.enable && g.order === "simultaneous");
    if(simul.length !== 1) return "";
    const other = gasName(simul[0].id === "mfc1" ? "mfc2" : "mfc1");
    return `${gasName(simul[0].id)} is set to Simultaneous on its own.\n\n`
      + `Simultaneous means two or more gases flow together for the whole `
      + `window, so set ${other} to Simultaneous as well (and switch it on), or `
      + `put ${simul[0].id.toUpperCase()} back to First or Second.`;
  }

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

  function clockAt(s){
    return new Date(Date.now() + s*1000)
      .toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
  }

  function renderIdleEta(){
    $("rEtaLbl").textContent = "est. duration";
    $("rFinishLbl").textContent = "finish if started now";
    $("rElapsed").textContent = "—";
    $("rEta").textContent    = plannedRunS == null ? "—" : fmtDur(plannedRunS);
    $("rFinish").textContent = plannedRunS == null ? "—" : clockAt(plannedRunS);
  }

  function refreshEstimate(){
    // Debounced like saveParams, just tighter - this one is what the operator is
    // watching while they type, so it should land as soon as they stop.
    clearTimeoutImpl(estimateTimer);
    estimateTimer = setTimeoutImpl(async () => {
      try{
        const r = await fetchImpl("/api/run/estimate", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({...runParams(), mode: runMode()}),
        });
        plannedRunS = r.ok ? ((await r.json()).total_s ?? null) : null;
      }catch(_){ plannedRunS = null; }    // server unreachable - show "—"
      if(!etaLive) renderIdleEta();
    }, 250);
  }

  function updateEta(r, running){
    const showing = running || r.state === "paused";
    etaLive = showing;
    if(!showing){ renderIdleEta(); return; }

    $("rEtaLbl").textContent = "est. remaining";
    $("rFinishLbl").textContent = "finish";
    const elapsed = r.started_at ? (Date.now()/1000 - r.started_at) : null;
    $("rElapsed").textContent = elapsed == null ? "—" : fmtDur(elapsed);

    const eta = r.run_remaining_s ?? null;
    const held = r.paused;
    $("rEta").textContent = eta == null ? "—"
      : fmtDur(eta) + (held ? " (held)" : "");
    $("rFinish").textContent = eta == null ? "—" : clockAt(eta);
  }

  function canonicalParams(){
    const params = runParams();
    delete params.run_name;
    return params;
  }
  function setCanonicalField(key, value){
    if(key === "min_current_a") return paramSet("min_current_ua", Number(value) * 1e6);
    if(key === "tolerance_frac") return paramSet("tolerance_pct", Number(value) * 100);
    paramSet(key, value);
  }
  function applyActiveSnapshot(snapshot, discard=false){
    if(!snapshot || !snapshot.editable || snapshot.run_started_at !== runIdentity) return false;
    acknowledged = {...snapshot.params};
    runRevision = snapshot.revision;
    if(snapshot.mode) $("modeSel").value = snapshot.mode;
    for(const [key,value] of Object.entries(acknowledged))
      if(discard || !dirtyKeys.has(key)) setCanonicalField(key, value);
    if(discard) dirtyKeys.clear();
    applyMode(); updateReigniteHint(); updateGasSchedHint();
    return true;
  }
  async function refreshActiveSnapshot(discard=false){
    if(liveRefreshInFlight) return false;
    const generation = runGeneration;
    liveRefreshInFlight = true;
    try{
      const response = await fetchImpl("/api/run/params", {cache:"no-store"});
      const snapshot = response.ok ? await response.json() : null;
      if(generation !== runGeneration || !RUN_ACTIVE) return false;
      if(!applyActiveSnapshot(snapshot, discard)) throw new Error("active run changed");
      const current = canonicalParams();
      for(const key of [...dirtyKeys])
        if(acknowledged[key] === current[key]) dirtyKeys.delete(key);
      livePaused = false;
      showLiveStatus(dirtyKeys.size ? "pending" : "idle",
        dirtyKeys.size ? "Draft changes are waiting to be applied." : "Active values are current.");
      return true;
    }catch(error){
      if(generation === runGeneration)
        showLiveStatus("unconfirmed", `Could not refresh active values: ${error.message}`);
      return false;
    }finally{
      if(generation === runGeneration) liveRefreshInFlight = false;
    }
  }
  function markLiveDirty(key){
    if(!RUN_ACTIVE) return;
    if(/^mfc[12]_gas_/.test(key)){
      const prefix = key.split("_gas_")[0] + "_gas_";
      for(const suffix of ["enable","order","pct","flow_sccm"])
        dirtyKeys.add(prefix + suffix);
    }else dirtyKeys.add(key);
    livePaused = false;
    showLiveStatus("pending", "Changes pending…");
    pushLiveParams();
  }
  function pushLiveParams(){
    if(!RUN_ACTIVE || livePaused || liveInFlight || !dirtyKeys.size || runRevision == null) return;
    clearTimeoutImpl(liveParamsTimer);
    const identity = runIdentity;
    const generation = runGeneration;
    liveParamsTimer = setTimeoutImpl(async () => {
      liveParamsTimer = null;
      if(!RUN_ACTIVE || !mounted || identity !== runIdentity || generation !== runGeneration) return;
      const current = canonicalParams();
      const sentKeys = [...dirtyKeys];
      const draft = Object.fromEntries(sentKeys.map(key => [key, current[key]]));
      liveInFlight = true;
      showLiveStatus("pending", `Applying ${sentKeys.join(", ")}…`);
      try{
        const response = await fetchImpl("/api/run/params", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({...draft, _run_started_at:identity,
            _run_revision:runRevision}),
        });
        let data = {};
        try{ data = await response.json(); }catch(_){}
        if(generation !== runGeneration || identity !== runIdentity) return;
        if(!response.ok) throw Object.assign(new Error(data.detail || response.statusText || "edit rejected"),
          {confirmed:true});
        applyActiveSnapshot(data, false);
        const after = canonicalParams();
        for(const key of sentKeys) if(after[key] === draft[key]) dirtyKeys.delete(key);
        showLiveStatus(dirtyKeys.size ? "pending" : "applied",
          dirtyKeys.size ? "Newer draft changes are pending." : `Run accepted ${sentKeys.join(", ")}.`);
      }catch(error){
        if(generation !== runGeneration) return;
        livePaused = true;
        showLiveStatus(error.confirmed ? "rejected" : "unconfirmed",
          error.confirmed ? `Run rejected the edit: ${error.message}`
            : `Edit outcome is unconfirmed: ${error.message || "connection lost"}`);
      }finally{
        if(generation === runGeneration){
          liveInFlight = false;
          if(!livePaused && dirtyKeys.size) pushLiveParams();
        }
      }
    }, 800);
  }
  function setRunActive(active, identity, revision=null){
    if(!active || identity !== runIdentity){
      if(liveParamsTimer !== null) clearTimeoutImpl(liveParamsTimer);
      liveParamsTimer = null;
      runGeneration++;
      dirtyKeys.clear(); acknowledged = {}; runRevision = null;
      liveInFlight = false; liveRefreshInFlight = false; livePaused = false;
    }
    runIdentity = identity;
    RUN_ACTIVE = active;
    $("modeSel").disabled = active;
    if(!active){ showLiveStatus("idle", ""); return; }
    if(runRevision == null) refreshActiveSnapshot(false);
    else if(revision != null && revision > runRevision && !liveInFlight)
      refreshActiveSnapshot(false);
  }
  function updateGasNames(mfcs){
    const labels = Object.fromEntries(mfcs.map(m => [m.id, m.label || m.id]));
    const names = Object.fromEntries(mfcs.map(m => [m.id, m.gas_name || ""]));
    const changed = JSON.stringify([labels,names]) !== JSON.stringify([MFC_LABELS,MFC_GAS]);
    MFC_LABELS = labels; MFC_GAS = names;
    if(changed){ applyGasNames(); updateGasSchedHint(); }
  }

  function mount(){
    if(mounted) return;
    mounted = true;
    $("modeSel").value = storage.getItem("runMode") || "ald";
    listen($("aldParams"), "input", event => {
      saveParams(event); updateReigniteHint(); updateGasSchedHint(); refreshEstimate();
    });
    listen($("gasSchedSection"), "input", event => {saveParams(event); updateGasSchedHint();});
    listen($("gasSchedSection"), "change", event => {saveParams(event); updateGasSchedHint();});
    listen($("advSection"), "input", event => {saveParams(event); updateReigniteHint(); refreshEstimate();});
    listen($("preSection"), "input", saveParams);
    listen($("modeSel"), "change", applyMode);
    listen($("p_smooth_on"), "click", event => event.stopPropagation());
    listen($("smoothSection"), "input", () => {
      saveParams(); updateSmoothHint(); drawChart();
    });
    const refresh = $("liveEditRefresh"), reapply = $("liveEditReapply");
    if(refresh) listen(refresh, "click", () => refreshActiveSnapshot(true));
    if(reapply) listen(reapply, "click", async () => {
      if(await refreshActiveSnapshot(false)){ livePaused = false; pushLiveParams(); }
    });
  }
  function dispose(){
    for(const remove of listeners.splice(0)) remove();
    if(saveTimer !== null) clearTimeoutImpl(saveTimer);
    for(const timer of [liveParamsTimer, estimateTimer])
      if(timer !== null) clearTimeoutImpl(timer);
    saveTimer = liveParamsTimer = estimateTimer = null;
    RUN_ACTIVE = false;
    runGeneration++;
    mounted = false;
  }
  return {applyMode, dispose, loadParams, mount, refreshRunName, runMode,
          gasScheduleError, setRunActive, updateGasNames, updateEta,
          refreshActiveSnapshot, runParams, saveParams, updateGasSchedHint};
}
