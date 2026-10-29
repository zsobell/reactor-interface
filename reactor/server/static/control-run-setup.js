/** Review a saved run before copying its values into the next-run form. */
import {formValuesFromRun} from './control-run-setup-values.js';

const LABELS = {
  cycles:'Cycles', dose_pressure_torr:'Dose pressure (Torr)', dose_s:'Dose time (s)',
  dose_pulses:'Pulses per cycle', dose_pulse_gap_s:'Wait between pulses (s)',
  pump_a_s:'Pump A (s)', beam_s:'Beam exposure (s)', pump_b_s:'Pump B (s)',
  min_current_ua:'Min beam current (µA)', sample_bias_v:'Sample bias (V)',
  sample_bias_polarity:'Bias polarity', plasma_current_ma:'Plasma current limit (mA)',
  grid_voltage_v:'Grid voltage (V)', steering_current_a:'Steering current limit (A)',
  collimating_current_a:'Collimating current limit (A)',
  sample_bias_lead_s:'Bias lead (s)', sample_bias_trail_s:'Bias trail (s)',
  fill_pulse_on_s:'Fill pulse on (s)', fill_pulse_off_s:'Fill pulse off (s)',
  tolerance_pct:'Fill flag tolerance (%)', reignite_pulse_s:'Reignite pulse (s)',
  reignite_settle_s:'Reignite settle (s)', ar_close_delay_s:'Ar close delay (s)',
  ar_soft_open_pulses:'Ar soft-open pulses', ar_soft_open_on_s:'Ar pulse width (s)',
  ar_soft_open_gap_s:'Ar pulse gap (s)', pre_ar_sccm:'Pre-start Ar flow (sccm)',
  pre_valve_delay_s:'Pre-start valve delay (s)', pre_hold_s:'Pre-start hold (s)',
  gas_overlap_s:'Gas overlap (s)',
};
function label(key){
  if(key === 'min_current_a') key = 'min_current_ua';
  if(key === 'tolerance_frac') key = 'tolerance_pct';
  const gas = /^(mfc[12])_gas_(enable|order|pct|flow_sccm)$/.exec(key);
  if(gas){
    const field = {enable:'on', order:'order', pct:'window (%)',
      flow_sccm:'flow (sccm)'}[gas[2]];
    return `${gas[1].toUpperCase().replace('MFC','MFC ')} ${field}`;
  }
  return LABELS[key] || key.replaceAll('_',' ');
}
function shown(value){
  if(value === true) return 'On';
  if(value === false) return 'Off';
  if(value === undefined || value === null) return 'not set';
  return String(value);
}

export function createRunSetupLoader({$, fetchImpl, runForms, documentObj=document,
                                      confirmImpl=(...args) => globalThis.confirm(...args)}) {
  let selected = null;
  let busy = false;
  let mounted = false;
  let selectionGeneration = 0;

  async function request(url, options){
    const response = await fetchImpl(url, options);
    const body = await response.json();
    if(!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
    return body;
  }

  function state(message, error=false){
    const el = $("runSetupStatus");
    el.textContent = message;
    el.dataset.state = error ? "error" : "ready";
  }

  function setBusy(value){
    const wasBusy = busy;
    if(value && !busy){
      selectionGeneration++;
      selected = null;
      $("runSetupPreview").textContent = "";
    }
    busy = !!value;
    $("runSetupSelect").disabled = busy;
    $("runSetupLoad").disabled = busy;
    $("runSetupApply").disabled = busy || !selected;
    if(busy) state("Wait until the current run or pre-start finishes.");
    else if(wasBusy) state("Select a run to review its settings.");
  }

  async function refresh(){
    try{
      const data = await request("/api/run-setups");
      const select = $("runSetupSelect");
      const before = select.value;
      select.replaceChildren();
      const blank = documentObj.createElement("option");
      blank.value = ""; blank.textContent = "Choose a previous run";
      select.append(blank);
      for(const entry of data.runs){
        const option = documentObj.createElement("option");
        option.value = entry.file;
        option.textContent = `${entry.display_name} · ${entry.mode.toUpperCase()}`;
        select.append(option);
      }
      if(before && data.runs.some(entry => entry.file === before)) select.value = before;
      state(data.runs.length ? "Select a run to review its settings."
        : "No saved ALD/CVD settings found yet.");
    }catch(error){ state(`Could not list previous runs: ${error.message}`, true); }
  }

  function review(data){
    const current = runForms.runParams();
    const currentForm = formValuesFromRun(current);
    const savedForm = formValuesFromRun(data.params);
    const changes = Object.entries(savedForm).filter(([key, value]) =>
      JSON.stringify(currentForm[key]) !== JSON.stringify(value));
    const conflicts = data.gas_conflicts || [];
    const lines = [
      `${data.mode.toUpperCase()} · ${data.source}`,
      `${changes.length} settings differ from the current form.`,
    ];
    for(const [key, value] of changes)
      lines.push(`${label(key)}: ${shown(currentForm[key])} → ${shown(value)}`);
    if(conflicts.length){
      lines.push("Gas identity needs review:");
      for(const item of conflicts)
        lines.push(`${item.mfc.toUpperCase()}: saved ${item.source_gas || "unknown"}; current ${item.current_gas || "unknown"}${item.used ? " (flow requested)" : ""}`);
    }
    if(data.missing_fields?.length)
      lines.push(`Unavailable in this dataset (kept as currently set): ${data.missing_fields.map(label).join(", ")}`);
    if(data.prestart_recipe)
      lines.push(`Source pre-start: ${data.prestart_recipe.name || data.prestart_recipe.id} r${data.prestart_recipe.revision || "?"}. The current pre-start selection will stay as it is.`);
    $("runSetupPreview").textContent = lines.join("\n");
  }

  async function load(){
    if(busy) return;
    const file = $("runSetupSelect").value;
    const generation = ++selectionGeneration;
    if(!file){ state("Choose a previous run first.", true); return; }
    try{
      const loaded = await request(`/api/run-setups/load?file=${encodeURIComponent(file)}`);
      if(generation !== selectionGeneration || busy || $("runSetupSelect").value !== file) return;
      selected = loaded;
      review(selected);
      $("runSetupApply").disabled = false;
      state("Review the values, then apply them to the next run.");
    }catch(error){
      if(generation !== selectionGeneration) return;
      selected = null;
      $("runSetupApply").disabled = true;
      state(`Could not load settings: ${error.message}`, true);
    }
  }

  async function apply(){
    if(busy || !selected || selected.file !== $("runSetupSelect").value) return;
    const generation = selectionGeneration;
    const conflicts = (selected.gas_conflicts || []).filter(item => item.used);
    if(conflicts.length){
      const text = conflicts.map(item => `${item.mfc.toUpperCase()}: saved ${item.source_gas || "unknown"}, current ${item.current_gas || "unknown"}`).join("\n");
      if(!confirmImpl(`Gas selection differs or is unknown:\n${text}\n\nUse the saved flow settings for these channels anyway?`)) return;
    }
    try{
      const checked = await request("/api/run-setups/apply", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({file:selected.file,
          digest:selected.digest,
          current_gas_names:selected.current_gas_names,
          acknowledge_gases:conflicts.map(item => item.mfc)}),
      });
      if(generation !== selectionGeneration || busy) return;
      await runForms.applyLoadedSetup(checked.params, checked.mode);
      if(generation !== selectionGeneration) return;
      selected = null;
      $("runSetupApply").disabled = true;
      state("Settings copied to the next-run form. Review them before starting.");
    }catch(error){
      if(generation === selectionGeneration)
        state(`Settings were not applied: ${error.message}`, true);
    }
  }

  function mount(){
    if(mounted) return;
    mounted = true;
    $("runSetupRefresh").onclick = refresh;
    $("runSetupLoad").onclick = load;
    $("runSetupApply").onclick = apply;
    $("runSetupSelect").onchange = () => {
      selectionGeneration++;
      selected = null;
      $("runSetupApply").disabled = true;
      $("runSetupPreview").textContent = "";
    };
    refresh();
  }
  return {mount, refresh, setBusy};
}
