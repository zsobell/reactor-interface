/** Visual HCPES parameter-space editor and linked-run monitor. */
"use strict";

const copy = value => JSON.parse(JSON.stringify(value));
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;",
})[c]);

const ESTABLISHMENT_FIELDS = [
  {key:"trend_window_s", label:"RoC rolling window", unit:"s", step:.1,
    help:"Recent sample-current interval used to calculate the displayed rate of change. The default 5 s window forgets older startup motion while resisting individual noisy readings."},
  {key:"stable_window_s", label:"Required stable time", unit:"s", step:.1,
    help:"After a complete rolling estimate first turns green, this timer starts at zero. The rolling rate must then remain below the drift limit for this entire time; any red reading resets the timer."},
  {key:"maximum_wait_s", label:"Maximum settle time", unit:"s", step:.1,
    help:"Longest time to wait for stable current after plasma is present. If it expires while plasma remains present, collection continues and the condition is marked Never settled."},
  {key:"max_drift_a_per_min", label:"Maximum current drift", unit:"mA/min", step:.01, scale:1000,
    help:"Largest allowed stage-current change per minute during plasma establishment. Lower values require a steadier plasma."},
];
const PARAMETER_FIELDS = [
  {key:"trend_window_s", label:"RoC rolling window", unit:"s", step:.1,
    help:"Recent sample-current interval used to calculate rate of change after sweep parameters change. This is independent of the required stable time."},
  {key:"stable_window_s", label:"Required stable time", unit:"s", step:.1,
    help:"After the complete rolling estimate first turns green, this timer starts at zero. It must remain green for this entire time before samples are collected; any red reading resets the timer."},
  {key:"maximum_wait_s", label:"Maximum settle time", unit:"s", step:.1,
    help:"Longest time to wait after a parameter change. If it expires while plasma remains present, collection continues and the condition is marked Never settled."},
  {key:"max_drift_a_per_min", label:"Maximum current drift", unit:"mA/min", step:.01, scale:1000,
    help:"Largest allowed stage-current change per minute after a parameter change. This can be looser than the plasma-establishment limit."},
];
const RECOVERY_FIELDS = [
  {key:"plasma_min_current_a", label:"Plasma-present minimum", unit:"mA", step:.01, scale:1000, min:0,
    help:"Stage current below this value is treated as plasma loss and starts the recovery sequence."},
  {key:"recovery_window_s", label:"Recovery retry budget", unit:"s", step:.1,
    help:"Cumulative time allowed for reignition pulses and post-pulse checks. Stability observation does not consume this budget. If exhausted, only this condition is skipped."},
  {key:"reignite_pulse_s", label:"Reignite relay pulse", unit:"s", step:.1,
    help:"How long the plasma-ground relay is held in its reignition position for each attempt."},
  {key:"reignite_settle_s", label:"Post-pulse check delay", unit:"s", step:.1, min:0,
    help:"How long to wait after restoring beam-on before checking whether plasma returned."},
];
const COLLECTION_FIELDS = [
  {key:"qualified_samples", label:"Qualified samples per condition", unit:"points", step:1, integer:true,
    help:"Number of fresh sample-current readings accepted after settling. There is no added interval or duration target; the next readings are collected at the instrument telemetry rate."},
];

export function moveAxis(axes, from, to){
  if(from < 0 || from >= axes.length || to < 0 || to >= axes.length) return axes;
  const [axis] = axes.splice(from, 1); axes.splice(to, 0, axis); return axes;
}

export function parseAxisList(text){
  const values = String(text).split(/[\s,]+/).filter(Boolean).map(Number);
  if(!values.length || values.some(value => !Number.isFinite(value) || value < 0))
    throw new Error("List values must be nonnegative numbers separated by commas.");
  if(new Set(values).size !== values.length) throw new Error("List values must be unique.");
  return values;
}

export function axisDisplayScale(capability){
  return 1;
}

export function axisDisplayUnit(capability){
  return capability?.unit || "";
}

export function formatDuration(seconds){
  const value = Number(seconds);
  if(!Number.isFinite(value)) return "—";
  if(value < 60) return `${value.toFixed(value < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(value / 60), remainder = Math.round(value % 60);
  return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}

export function launchReview(preview, sessionId, campaignId=""){
  const plan = preview.plan, sign = plan.stage_polarity > 0 ? "+" : "−";
  const settings=plan.settings, establishment=settings.establishment;
  const parameter=settings.parameter_change;
  const axes = preview.axes.map((axis, index) =>
    `${index + 1}. ${axis.label}: ${axis.mode} · ${axis.count} value(s)`).join("\n");
  const startup = preview.axes.filter(axis => axis.mode !== "locked_zero").map(axis => {
    const value = Number(plan.initial_setpoints[axis.target]);
    return `${axis.label}: ${value} ${axis.unit}`;
  }).join(" · ");
  return `Start HCPES characterization “${plan.name}” (revision ${plan.revision})?\n\n`
    + `Session: ${sessionId}\nStage wiring: ${sign} orientation`
    + (campaignId ? `\nLinked campaign: ${campaignId}` : "")
    + `\n\nOUTER → INNER LOOP ORDER\n${axes}`
    + `\n\n${preview.estimate.points} conditions · ${preview.estimate.qualified_samples} qualified samples`
    + `\nEach condition accepts its next ${settings.qualified_samples} fresh sample-current readings after settling; no added collection timer.`
    + `\nConfigured settle/delay estimate ${formatDuration(preview.estimate.best_case_s)}; establishment-timeout estimate ${formatDuration(preview.estimate.startup_timeout_case_s)}`
    + `\n\nINITIAL PLASMA CONDITION\n${startup}`
    + `\nPlasma is established and settled here before moving to sweep point 1. No qualified sweep samples are collected here.`
    + `\n\nPLASMA ESTABLISHMENT\nMeasure a ${establishment.trend_window_s}s rolling RoC, then keep it below ${(establishment.max_drift_a_per_min*1000).toFixed(3)} mA/min for ${establishment.stable_window_s}s; maximum ${establishment.maximum_wait_s}s`
    + `\nRecovery retry budget ${settings.recovery_window_s}s (settling does not consume it)`
    + `\n\nPARAMETER CHANGES\n${settings.condition_settle_mode === "time" ? `${settings.parameter_settle_s}s timed delay after each changed parameter` : `Measure a ${parameter.trend_window_s}s rolling RoC, then keep it below ${(parameter.max_drift_a_per_min*1000).toFixed(3)} mA/min for ${parameter.stable_window_s}s; maximum ${parameter.maximum_wait_s}s`}`
    + `\n\nCONFIRM BEFORE START\nThe physical stage leads are in the ${sign} orientation.`
    + `\nAll background MFCs are explicitly swept/fixed or locked at zero.`
    + `\n\nEVERY EXIT\nAll MFCs → 0; Ar isolation closes after Ar zero; HV off;`
    + `\nfour HCPES support supplies output off; plasma relay parked.`;
}

function defaultSessionId(){
  const date = new Date(), pad = value => String(value).padStart(2, "0");
  return `hcpes-${date.getFullYear()}${pad(date.getMonth()+1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}-${String(date.getMilliseconds()).padStart(3,"0")}`;
}

export function createHcpesEditor({$, windowObj=window, fetchImpl=fetch, toast=()=>{},
                                   confirmImpl=confirm, promptImpl=prompt}){
  let root = null, catalog = {axes:[]}, library = {plans:[], campaigns:[], selected_id:""};
  let plan = null, preview = null, dirty = false, mounted = false, disposed = false;
  let runtime = {running:false, phase:"idle"}, sessionId = defaultSessionId();
  const listeners = [];

  const capability = target => catalog.axes.find(item => item.target === target);
  const editable = () => !!plan && !plan.builtin;
  const pendingCampaign = () => (library.campaigns || []).find(campaign =>
    campaign.status === "awaiting_opposite" && campaign.opposite_plan_id === plan?.id);

  function listen(el, event, fn){
    el.addEventListener(event, fn); listeners.push(() => el.removeEventListener?.(event, fn));
  }
  async function request(url, options={}){
    const response = await fetchImpl(url, options);
    if(!response.ok){
      let message = response.statusText || `HTTP ${response.status}`;
      try{ message = (await response.json()).detail || message; }catch(_){}
      toast(message); const error = new Error(message); error.status = response.status; throw error;
    }
    return response.json();
  }
  const send = (url, method, body) => request(url, {method,
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});

  function choose(id){
    const selected = library.plans.find(item => item.id === id);
    plan = selected ? copy(selected) : null; dirty = false; preview = null;
    render(); if(plan) void refreshPreview(false);
  }
  async function load(preferred){
    const [nextCatalog, nextLibrary] = await Promise.all([
      request("/api/hcpes/capabilities"), request("/api/hcpes/plans"),
    ]);
    if(disposed) return;
    catalog = nextCatalog; library = nextLibrary;
    choose(preferred || library.selected_id);
  }
  function markDirty(){
    if(!editable()) return;
    dirty = true; preview = null;
    const status = $("hcpesEditStatus");
    if(status){ status.textContent = "unsaved changes — cannot start"; status.className = "hcpes-edit-status dirty"; }
    const save = $("hcpesSavePlan"); if(save) save.disabled = false;
    const start = $("hcpesStart"); if(start) start.disabled = true;
    const previewStatus = $("hcpesPreviewStatus");
    if(previewStatus) previewStatus.textContent = "Preview required after this change.";
  }
  function guardDirty(){
    return !dirty || confirmImpl("Discard unsaved changes to this HCPES plan?");
  }

  function axisFields(axis, index, locked, cap){
    const disabled = locked ? " disabled" : "";
    const scale = axisDisplayScale(cap);
    const shown = value => Number(value ?? 0) * scale;
    const scaleAttr = ` data-axis-scale="${scale}"`;
    if(axis.mode === "locked_zero") return `<div class="hcpes-axis-zero">Commanded to 0 sccm at startup and throughout acquisition.</div>`;
    if(axis.mode === "fixed") return `<label>Value<input data-axis-value="value"${scaleAttr} type="number" min="0" step="any" value="${esc(shown(axis.value))}"${disabled}></label>`;
    if(axis.mode === "list") return `<label class="wide">Values<input data-axis-list${scaleAttr} value="${esc((axis.values || []).map(shown).join(", "))}" placeholder="0, 25, 50"${disabled}></label>`;
    return `<label>Start<input data-axis-value="start"${scaleAttr} type="number" min="0" step="any" value="${esc(shown(axis.start))}"${disabled}></label>
      <label>Stop<input data-axis-value="stop"${scaleAttr} type="number" min="0" step="any" value="${esc(shown(axis.stop))}"${disabled}></label>
      <label>Step<input data-axis-value="step"${scaleAttr} type="number" min="0" step="any" value="${esc(shown(axis.step ?? 1 / scale))}"${disabled}></label>`;
  }
  function axisCard(axis, index, locked){
    const cap = capability(axis.target) || {label:axis.target, unit:"", role:""};
    const modes = ["fixed", "list", "linear", ...(cap.locked_zero_allowed ? ["locked_zero"] : [])];
    const names = {fixed:"Fixed", list:"Value list", linear:"Linear range", locked_zero:"Locked at zero"};
    return `<div class="hcpes-axis" data-axis="${index}">
      <div class="hcpes-axis-order"><span>${index+1}</span><small>${index ? "inner" : "outer"} loop</small></div>
      <div class="hcpes-axis-main"><div class="hcpes-axis-head">
        <div><strong>${esc(cap.label)}</strong><code>${esc(axis.target)}</code></div>
        <label>Mode<select data-axis-mode${locked ? " disabled" : ""}>${modes.map(mode =>
          `<option value="${mode}"${axis.mode === mode ? " selected" : ""}>${names[mode]}</option>`).join("")}</select></label>
        <span class="hcpes-axis-unit">${esc(axisDisplayUnit(cap))}</span>
        <div class="hcpes-axis-tools"><button data-axis-move="up" title="Move ${esc(cap.label)} one loop outward" aria-label="Move ${esc(cap.label)} outward"${locked || index === 0 ? " disabled" : ""}>↑ Outer</button><button data-axis-move="down" title="Move ${esc(cap.label)} one loop inward" aria-label="Move ${esc(cap.label)} inward"${locked || index === plan.axes.length-1 ? " disabled" : ""}>↓ Inner</button></div>
      </div><div class="hcpes-axis-fields">${axisFields(axis, index, locked, cap)}</div></div>
    </div>`;
  }
  function initialSetpointsHtml(locked){
    const fields = plan.axes.filter(axis => axis.mode !== "locked_zero").map(axis => {
      const cap = capability(axis.target) || {label:axis.target, unit:""};
      const scale = axisDisplayScale(cap);
      const fallback = axis.value ?? axis.start ?? axis.values?.[0] ?? 0;
      const value = Number(plan.initial_setpoints?.[axis.target] ?? fallback) * scale;
      const help = `Value used only to establish and stabilize the plasma before sweep point 1. ${cap.label} then changes to the first sweep value using the selected between-condition settling rule.`;
      return `<label class="hcpes-setting" title="${esc(help)}"><span>${esc(cap.label)}</span>
        <span class="hcpes-unit-input"><input data-initial-target="${esc(axis.target)}" data-axis-scale="${scale}" type="number" min="0" step="any" value="${esc(value)}" title="${esc(help)}"${locked ? " disabled" : ""}><span>${esc(axisDisplayUnit(cap))}</span></span>
        <small>${esc(help)}</small></label>`;
    }).join("");
    return `<section class="hcpes-setting-section hcpes-initial"><h4>Initial plasma condition</h4>
      <p>These values are used only to ignite and complete the plasma-establishment stability check. The controller then moves to sweep point 1 and settles that change before collecting qualified data.</p>
      <div class="hcpes-setting-grid">${fields}</div></section>`;
  }
  function settingsHtml(locked){
    const field = (definition, profile="") => {
      const source = profile ? plan.settings[profile] : plan.settings;
      const scale = definition.scale || 1;
      const value = Number(source[definition.key]) * scale;
      const min = definition.min ?? definition.step;
      return `<label class="hcpes-setting" title="${esc(definition.help)}"><span>${esc(definition.label)}</span>
        <span class="hcpes-unit-input"><input data-setting="${definition.key}"${profile ? ` data-setting-profile="${profile}"` : ""} data-setting-scale="${scale}" type="number" min="${min}" step="${definition.step}" value="${esc(value)}" title="${esc(definition.help)}"${locked ? " disabled" : ""}><span>${esc(definition.unit)}</span></span>
        <small>${esc(definition.help)}</small></label>`;
    };
    const timedHelp = "Wait this fixed time after each changed parameter when Timed delay mode is selected. It is not the plasma-establishment stability time.";
    const modeHelp = "Choose a simple fixed delay after each changed parameter, or one short stage-current stability check after all changes for the next condition are applied.";
    return `<section class="hcpes-setting-section"><h4>Plasma establishment and recovery</h4>
      <p>Used at initial beam establishment and after every successful reignition. Recovery and settling have independent timers.</p>
      <div class="hcpes-setting-grid">${ESTABLISHMENT_FIELDS.map(item => field(item,"establishment")).join("")}${RECOVERY_FIELDS.map(item => field(item)).join("")}</div></section>
      <section class="hcpes-setting-section"><h4>Between parameter changes</h4>
      <p>This shorter profile is separate from plasma establishment.</p>
      <div class="hcpes-setting-grid"><label class="hcpes-setting" title="${esc(modeHelp)}"><span>Between-condition method</span>
        <select data-setting-choice="condition_settle_mode" title="${esc(modeHelp)}"${locked ? " disabled" : ""}>
          <option value="time"${plan.settings.condition_settle_mode === "time" ? " selected" : ""}>Timed delay after each change</option>
          <option value="current"${plan.settings.condition_settle_mode === "current" ? " selected" : ""}>Stage-current stability after all changes</option>
        </select><small>${esc(modeHelp)}</small></label>
        <label class="hcpes-setting" title="${esc(timedHelp)}"><span>Timed delay per changed parameter</span>
        <span class="hcpes-unit-input"><input data-setting="parameter_settle_s" data-setting-scale="1" type="number" min="0" step="0.1" value="${esc(plan.settings.parameter_settle_s)}" title="${esc(timedHelp)}"${locked ? " disabled" : ""}><span>s</span></span><small>${esc(timedHelp)}</small></label>
        ${PARAMETER_FIELDS.map(item => field(item,"parameter_change")).join("")}</div></section>
      <section class="hcpes-setting-section"><h4>Qualified data collection</h4>
      <p>After settling, accept the next fresh sample-current readings at the instrument telemetry rate. No extra delay or collection-duration timer is applied.</p>
      <div class="hcpes-setting-grid">${COLLECTION_FIELDS.map(item => field(item)).join("")}</div></section>`;
  }
  function previewHtml(){
    if(!preview) return `<div class="hcpes-preview-empty" id="hcpesPreviewStatus">${dirty ? "Preview required after this change." : "Loading preview…"}</div>`;
    const e = preview.estimate;
    return `<div id="hcpesPreviewStatus" class="hcpes-metrics">
      <div><strong>${e.points}</strong><span>conditions</span></div>
      <div><strong>${e.parameter_changes}</strong><span>parameter writes</span></div>
      <div><strong>${e.qualified_samples}</strong><span>qualified readings</span></div>
      <div><strong>${formatDuration(e.best_case_s)}</strong><span>settle/delay best case</span></div>
      <div><strong>${formatDuration(e.startup_timeout_case_s)}</strong><span>settle/delay timeout case</span></div>
    </div><div class="hcpes-nesting"><b>Outer → inner:</b> ${preview.axes.map(axis => `${esc(axis.label)} (${axis.count})`).join(" → ")}</div>`;
  }
  function render(){
    if(!root || !plan) return;
    const locked = plan.builtin, sign = plan.stage_polarity > 0 ? "+" : "−";
    root.innerHTML = `<div class="hcpes-toolbar">
      <label>Plan<select id="hcpesPlanSelect">${library.plans.map(item => `<option value="${esc(item.id)}"${item.id === plan.id ? " selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label>
      <button id="hcpesNewPlan">New</button><button id="hcpesDuplicatePlan">Duplicate</button><button id="hcpesDeletePlan"${locked ? " disabled" : ""}>×</button>
      <span class="hcpes-spacer"></span><span id="hcpesEditStatus" class="hcpes-edit-status${dirty ? " dirty" : ""}">${dirty ? "unsaved changes — cannot start" : locked ? "protected safe template" : `revision ${plan.revision} · saved`}</span>
      <button id="hcpesSavePlan" class="primary"${locked || !dirty ? " disabled" : ""}>Save plan</button>
    </div>
    <div class="hcpes-meta"><label>Name<input data-meta="name" maxlength="100" value="${esc(plan.name)}"${locked ? " disabled" : ""}></label><label>Description<input data-meta="description" value="${esc(plan.description)}"${locked ? " disabled" : ""}></label></div>
    <div class="hcpes-polarity"><div><span>Stage wiring orientation</span><strong>${sign}</strong><small>Plan values are nonnegative magnitudes; files and plots apply this sign.</small></div>
      <div class="hcpes-segment"><button data-polarity="1" class="${plan.stage_polarity > 0 ? "active" : ""}"${locked ? " disabled" : ""}>+ Positive</button><button data-polarity="-1" class="${plan.stage_polarity < 0 ? "active" : ""}"${locked ? " disabled" : ""}>− Negative</button></div></div>
    <div class="hcpes-profile"><b>Restricted HCPES pre-start</b><span>all MFCs zero → Ar isolation open → initial plasma condition programmed → four supplies on → plasma relay to beam mode → establishment stability → sweep point 1. No precursor fill or precursor-valve action.</span></div>
    ${initialSetpointsHtml(locked)}
    <div class="hcpes-subhead"><span>Parameter-space blocks</span><small>Drag-free buttons set deterministic outer-to-inner nesting.</small></div>
    <div class="hcpes-axes">${plan.axes.map((axis, index) => axisCard(axis, index, locked)).join("")}</div>
    <details class="hcpes-settings" open><summary>Stability, recovery, and collection</summary>${settingsHtml(locked)}
      <div class="hint">If plasma disappears during a parameter-change check, that short check stops and recovery begins. Once plasma returns, the stricter full establishment profile replaces the interrupted parameter check. A settle timeout continues with <code>settled=false</code>; an exhausted retry budget rejects only that condition.</div></details>
    <div class="hcpes-preview"><div class="hcpes-subhead"><span>Review</span><button id="hcpesRefreshPreview" class="mini">Refresh preview</button></div>${previewHtml()}
      <div class="hcpes-cleanup"><b>Every exit commands:</b><span>all MFCs 0</span><span>Ar isolation closed after zero</span><span>HV off</span><span>four HCPES support supplies output off</span><span>plasma relay parked</span></div></div>
    <div class="hcpes-launch"><label>Session name<input id="hcpesSessionId" value="${esc(sessionId)}" maxlength="80"></label>
      <button id="hcpesStart" class="primary"${dirty || locked || runtime.running || !preview ? " disabled" : ""}>Review & start ${sign}</button><button id="hcpesStop" class="danger"${runtime.running ? "" : " disabled"}>■ Stop</button>
      <button id="hcpesOpposite"${runtime.running || runtime.phase !== "complete" || runtime.plan_id !== plan.id ? " hidden" : ""}>Create opposite-polarity follow-up</button></div>
    <div class="hcpes-live"><div><span>State / parameter</span><strong id="hcpesLivePhase">—</strong></div><div><span>Conditions finished</span><strong id="hcpesLivePoint">—</strong></div><div><span>Accepted / rejected</span><strong id="hcpesLiveAccepted">—</strong></div><div><span>Qualified samples</span><strong id="hcpesLiveSamples">—</strong></div><div><span>Estimated remaining</span><strong id="hcpesLiveEta">—</strong></div><div id="hcpesLiveRateCard" class="hcpes-live-rate"><span>Stage-current rate / limit</span><strong id="hcpesLiveRate">—</strong></div><div><span>Stage current</span><strong id="hcpesLiveCurrent">—</strong></div><div><span>Active stability window</span><strong id="hcpesLiveWindow">—</strong></div><div><span>Maximum settle timer</span><strong id="hcpesLiveSettle">—</strong></div><div><span>Recovery retry remaining</span><strong id="hcpesLiveRetry">—</strong></div><div><span>Recoveries / inaccessible</span><strong id="hcpesLiveRecovery">—</strong></div></div>
    <div class="hcpes-current-params"><b>Currently commanded</b><div id="hcpesLiveSetpoints">—</div></div>
    <div id="hcpesLiveNote" class="hcpes-live-note"></div>`;
    renderRuntime();
  }

  function renderRuntime(){
    const set = (id, value) => { const el=$(id); if(el) el.textContent=value; };
    const changing = runtime.changed_target
      ? ` · ${runtime.changed_target} → ${runtime.requested_value}` : "";
    const phaseText = (runtime.phase || "idle") + changing;
    set("hcpesLivePhase", phaseText);
    const phaseEl = $("hcpesLivePhase"); if(phaseEl) phaseEl.title = phaseText;
    const completed = runtime.points_completed || 0, total = runtime.point_total || 0;
    const percent = total ? Math.round(completed / total * 100) : 0;
    set("hcpesLivePoint", total ? `${completed} / ${total} · ${percent}%` : "—");
    set("hcpesLiveAccepted", total ? `${runtime.points_collected || 0} / ${runtime.points_rejected || 0}` : "—");
    set("hcpesLiveSamples", runtime.qualified_target ? `${runtime.qualified_samples || 0} / ${runtime.qualified_target}` : "—");
    set("hcpesLiveEta", runtime.estimated_remaining_s == null ? "—" : formatDuration(runtime.estimated_remaining_s));
    const rawRate = runtime.observed_drift_a_per_min;
    const rawThreshold = runtime.drift_threshold_a_per_min;
    const rate = Number(rawRate), threshold = Number(rawThreshold);
    // Number(null) is zero.  Treating an estimator that is still gathering its
    // window as 0.000 mA/min created a false stable indication followed by an
    // apparent spike when the first real estimate arrived.
    const finiteRate = rawRate !== null && rawRate !== undefined && Number.isFinite(rate);
    const finiteThreshold = rawThreshold !== null && rawThreshold !== undefined && Number.isFinite(threshold);
    const trendWindow = Number(runtime.drift_window_s);
    const trendSuffix = Number.isFinite(trendWindow) && trendWindow > 0
      ? ` · ${trendWindow.toFixed(1)} s window` : "";
    set("hcpesLiveRate", finiteRate ? `${(rate*1000).toFixed(3)}${finiteThreshold ? ` / ${(threshold*1000).toFixed(3)}` : ""} mA/min${trendSuffix}` : `measuring…${trendSuffix}`);
    const rateCard=$("hcpesLiveRateCard");
    if(rateCard) rateCard.className = "hcpes-live-rate" + (!finiteRate || !finiteThreshold ? "" : Math.abs(rate) < threshold ? " good" : " bad");
    set("hcpesLiveCurrent", runtime.current_a == null ? "—" : `${(Number(runtime.current_a)*1000).toFixed(4)} mA`);
    const profileNames={establishment:"plasma",parameter_change:"parameter",timed_parameter_change:"timed delay"};
    const profile=profileNames[runtime.active_stability_profile] || "";
    const windowElapsed=runtime.stability_window_elapsed_s, windowRequired=runtime.stability_window_required_s;
    set("hcpesLiveWindow", windowElapsed == null || windowRequired == null ? (profile || "—") : `${profile} ${Number(windowElapsed).toFixed(1)} / ${Number(windowRequired).toFixed(1)} s`);
    const waitElapsed=runtime.stability_wait_elapsed_s, waitMaximum=runtime.stability_max_wait_s;
    set("hcpesLiveSettle", waitElapsed == null || waitMaximum == null
      ? (runtime.settle_remaining_s == null ? "—" : `${Number(runtime.settle_remaining_s).toFixed(1)} s remaining`)
      : `${Number(waitElapsed).toFixed(1)} / ${Number(waitMaximum).toFixed(1)} s`);
    set("hcpesLiveRetry", runtime.recovery_remaining_s == null ? "—" : `${Number(runtime.recovery_remaining_s).toFixed(1)} s`);
    set("hcpesLiveRecovery", `${runtime.recovery_count || 0} / ${runtime.inaccessible_points || 0}`);
    const setpoints=$("hcpesLiveSetpoints");
    if(setpoints){
      const active=runtime.applied_setpoints || {};
      setpoints.innerHTML=Object.entries(active).length ? Object.entries(active).map(([target,value]) => {
        const cap=capability(target), shown=Number(value)*axisDisplayScale(cap);
        return `<span><i>${esc(cap?.label || target)}</i><strong>${esc(shown)} ${esc(axisDisplayUnit(cap))}</strong></span>`;
      }).join("") : "—";
    }
    set("hcpesLiveNote", runtime.error || (runtime.running ? `Session ${runtime.session_id || ""} owns all HCPES controls.` : ""));
    const stop=$("hcpesStop"); if(stop) stop.disabled=!runtime.running;
    const start=$("hcpesStart"); if(start) start.disabled=dirty || plan?.builtin || runtime.running || !preview;
    const opposite=$("hcpesOpposite"); if(opposite) opposite.hidden=runtime.running || runtime.phase !== "complete" || runtime.plan_id !== plan?.id;
  }

  async function refreshPreview(showError=true){
    try{
      preview = await send("/api/hcpes/preview", "POST", {plan});
      if(!disposed) render();
      return preview;
    }catch(error){
      preview = null;
      const status=$("hcpesPreviewStatus"); if(status) status.textContent=error.message;
      if(!showError) return null; throw error;
    }
  }
  function resetAxis(axis, mode){
    const target = axis.target;
    const scale = axisDisplayScale(capability(target));
    if(mode === "fixed") return {target, mode, value:0};
    if(mode === "list") return {target, mode, values:[0]};
    if(mode === "linear") return {target, mode, start:0, stop:1/scale, step:1/scale};
    return {target, mode:"locked_zero"};
  }

  async function onChange(event){
    const el=event.target;
    if(el.id === "hcpesPlanSelect"){
      if(!guardDirty()){ el.value=plan.id; return; }
      await send(`/api/hcpes/plans/${encodeURIComponent(el.value)}/select`, "POST", {});
      library.selected_id=el.value; choose(el.value); return;
    }
    if(el.id === "hcpesSessionId"){ sessionId=el.value.trim(); return; }
    if(el.dataset.meta){ plan[el.dataset.meta]=el.value; markDirty(); return; }
    if(el.dataset.initialTarget){
      plan.initial_setpoints ||= {};
      plan.initial_setpoints[el.dataset.initialTarget]=Number(el.value) / Number(el.dataset.axisScale || 1);
      markDirty(); return;
    }
    if(el.dataset.setting){
      const scale=Number(el.dataset.settingScale || 1);
      const value=el.dataset.setting === "qualified_samples"
        ? parseInt(el.value,10) : Number(el.value) / scale;
      const target=el.dataset.settingProfile
        ? plan.settings[el.dataset.settingProfile] : plan.settings;
      target[el.dataset.setting]=value;
      markDirty(); return;
    }
    if(el.dataset.settingChoice){ plan.settings[el.dataset.settingChoice]=el.value; markDirty(); return; }
    const row=el.closest?.("[data-axis]"); if(!row) return;
    const axis=plan.axes[Number(row.dataset.axis)];
    if(el.hasAttribute("data-axis-mode")){
      const replacement=resetAxis(axis, el.value);
      plan.axes[Number(row.dataset.axis)]=replacement;
      plan.initial_setpoints ||= {};
      if(replacement.mode === "locked_zero") delete plan.initial_setpoints[replacement.target];
      else if(!(replacement.target in plan.initial_setpoints)) plan.initial_setpoints[replacement.target]=0;
      markDirty(); render(); return;
    }
    if(el.dataset.axisValue){
      axis[el.dataset.axisValue]=Number(el.value) / Number(el.dataset.axisScale || 1);
      markDirty(); return;
    }
    if(el.hasAttribute("data-axis-list")){
      try{
        const scale=Number(el.dataset.axisScale || 1);
        axis.values=parseAxisList(el.value).map(value => value / scale);
        el.setCustomValidity("");
      }
      catch(error){ axis.values=[]; el.setCustomValidity(error.message); }
      markDirty();
    }
  }
  async function onClick(event){
    const button=event.target.closest?.("button"); if(!button) return;
    if(button.dataset.polarity){ plan.stage_polarity=Number(button.dataset.polarity); markDirty(); render(); return; }
    const row=button.closest?.("[data-axis]");
    if(row && button.dataset.axisMove){
      const from=Number(row.dataset.axis), to=from+(button.dataset.axisMove === "up" ? -1 : 1);
      moveAxis(plan.axes, from, to); markDirty(); render(); return;
    }
    try{
      if(button.id === "hcpesNewPlan" || button.id === "hcpesDuplicatePlan"){
        if(!guardDirty()) return;
        const name=promptImpl("Name for the HCPES plan:", button.id === "hcpesDuplicatePlan" ? `${plan.name} copy` : "New HCPES characterization");
        if(!name?.trim()) return;
        const created=await send("/api/hcpes/plans", "POST", {name:name.trim(), from_id:button.id === "hcpesDuplicatePlan" ? plan.id : "current-hcpes-plan"});
        await load(created.id); return;
      }
      if(button.id === "hcpesDeletePlan"){
        if(!confirmImpl(`Delete HCPES plan “${plan.name}”?`)) return;
        await send(`/api/hcpes/plans/${encodeURIComponent(plan.id)}`, "DELETE", {}); await load(); return;
      }
      if(button.id === "hcpesSavePlan"){
        const oldRevision=plan.revision;
        const saved=await send(`/api/hcpes/plans/${encodeURIComponent(plan.id)}`, "PUT", {expected_revision:oldRevision, plan});
        const index=library.plans.findIndex(item => item.id === saved.id); if(index >= 0) library.plans[index]=saved;
        plan=copy(saved); dirty=false; toast(`Saved ${saved.name} revision ${saved.revision}`, true); await refreshPreview(); return;
      }
      if(button.id === "hcpesRefreshPreview"){ await refreshPreview(); return; }
      if(button.id === "hcpesStart"){
        if(dirty) throw new Error("Save this plan before starting.");
        sessionId=$("hcpesSessionId").value.trim();
        if(!sessionId) throw new Error("Enter a session name.");
        const campaign=pendingCampaign();
        const exact=await send("/api/hcpes/preview", "POST", {plan_id:plan.id});
        if(!confirmImpl(launchReview(exact, sessionId, campaign?.id || ""))) return;
        const started=await send("/api/hcpes/start", "POST", {plan_id:plan.id, expected_revision:plan.revision,
          session_id:sessionId, polarity_confirmed:true, ...(campaign ? {campaign_id:campaign.id} : {})});
        updateStatus(started);
        return;
      }
      if(button.id === "hcpesStop"){ if(confirmImpl("Stop HCPES acquisition and run the full cleanup now?")) updateStatus(await send("/api/hcpes/stop", "POST", {})); return; }
      if(button.id === "hcpesOpposite"){
        if(!confirmImpl("The completed session has finished cleanup. Create an exact opposite-polarity follow-up?\n\nBefore starting it, switch off/verify the supplies and physically swap the stage leads. The follow-up performs a separate full startup.")) return;
        const result=await send(`/api/hcpes/plans/${encodeURIComponent(plan.id)}/opposite`, "POST", {expected_revision:plan.revision, source_session_id:runtime.session_id,
          name:`${plan.name} ${plan.stage_polarity > 0 ? "negative" : "positive"}`,
          campaign_name:`${plan.name} full polarity`});
        sessionId=defaultSessionId(); await load(result.plan.id);
      }
    }catch(error){
      if(button.id === "hcpesStart" && /session directory already exists|session id/i.test(error.message || "")){
        sessionId=defaultSessionId(); const input=$("hcpesSessionId"); if(input) input.value=sessionId;
      }
      toast(error.message || String(error));
    }
  }

  async function mount(){
    if(mounted || disposed) return; root=$("hcpesEditor"); if(!root) return;
    mounted=true; listen(root,"change",onChange); listen(root,"input",onChange); listen(root,"click",onClick);
    listen(windowObj,"beforeunload",event => { if(dirty){ event.preventDefault(); event.returnValue=""; } });
    await load();
  }
  function updateStatus(next){
    const wasRunning=!!runtime.running, priorSession=runtime.session_id;
    runtime=copy(next || {running:false,phase:"idle"});
    if(wasRunning && !runtime.running && priorSession && runtime.session_id === priorSession){
      sessionId=defaultSessionId(); const input=$("hcpesSessionId"); if(input) input.value=sessionId;
    }
    renderRuntime();
  }
  function dispose(){ disposed=true; while(listeners.length) listeners.pop()(); }
  return {mount, dispose, updateStatus, selected:()=>copy(plan), isDirty:()=>dirty,
    currentPreview:()=>copy(preview)};
}
