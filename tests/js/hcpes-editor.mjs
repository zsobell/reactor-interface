import assert from "node:assert/strict";
import {
  axisDisplayScale, axisDisplayUnit, createHcpesEditor, formatDuration,
  launchReview, moveAxis, parseAxisList,
} from "../../reactor/server/static/hcpes-editor.js";

assert.deepEqual(parseAxisList("0, 10  20"), [0, 10, 20]);
assert.throws(() => parseAxisList("1, 1"), /unique/);
assert.throws(() => parseAxisList("-1, 2"), /nonnegative/);
const axes = [{target:"a"},{target:"b"},{target:"c"}];
moveAxis(axes, 2, 0);
assert.deepEqual(axes.map(axis => axis.target), ["c","a","b"]);
assert.equal(formatDuration(16200), "270m 00s");
assert.equal(axisDisplayScale({quantity:"current",unit:"A"}), 1000);
assert.equal(axisDisplayUnit({quantity:"current",unit:"A"}), "mA");

const plan = {
  schema_version:1, id:"map", name:"Cathode map", description:"", revision:2,
  builtin:false, prestart_recipe_id:"current-hcpes-prestart", stage_polarity:1,
  settings:{establishment:{stable_window_s:20,maximum_wait_s:60,max_drift_a_per_min:.0001},
    parameter_change:{stable_window_s:3,maximum_wait_s:10,max_drift_a_per_min:.0003},
    parameter_settle_s:3, condition_settle_mode:"time",
    plasma_min_current_a:.0001, recovery_window_s:30, reignite_pulse_s:1,
    reignite_settle_s:1, qualified_samples:5},
  axes:[
    {target:"mfc:ar", mode:"linear", start:1, stop:2, step:1},
    {target:"supply:stage_bias", mode:"fixed", value:10},
    {target:"supply:collimating", mode:"fixed", value:.0015},
    {target:"mfc:bg", mode:"locked_zero"},
  ],
};
const preview = {plan, estimate:{points:2, parameter_changes:4, qualified_samples:10,
  best_case_s:23, startup_timeout_case_s:63}, axes:[
  {label:"Ar", mode:"linear", count:2}, {label:"Stage", mode:"fixed", count:1},
  {label:"Collimating", mode:"fixed", count:1},
  {label:"Background", mode:"locked_zero", count:1},
]};
assert.match(launchReview(preview, "positive-001"),
  /Stage wiring: \+ orientation[\s\S]*no added collection timer[\s\S]*PLASMA ESTABLISHMENT[\s\S]*settling does not consume it[\s\S]*four HCPES support supplies output off/);

function eventTarget(){
  const events = new Map();
  return {events, addEventListener(name, fn){ events.set(name, fn); },
    removeEventListener(name, fn){ if(events.get(name) === fn) events.delete(name); }};
}
function element(){
  return {textContent:"", className:"", disabled:false, hidden:false, value:"",
    innerHTML:"", style:{}, setCustomValidity(message){ this.validationMessage=message; }};
}
const root = {...eventTarget(), innerHTML:""};
const windowObj = {...eventTarget()};
const elements = new Map([["hcpesEditor", root]]);
const $ = id => {
  if(!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
const catalog = {schema_version:1, axes:[
  {target:"mfc:ar", label:"Ar", unit:"sccm", role:"ar_flow", locked_zero_allowed:false},
  {target:"supply:stage_bias", label:"Stage", unit:"V", role:"stage_bias", locked_zero_allowed:false},
  {target:"supply:collimating", label:"Collimating", unit:"A", quantity:"current", role:"collimating_current", locked_zero_allowed:false},
  {target:"mfc:bg", label:"Background", unit:"sccm", role:"background_flow", locked_zero_allowed:true},
]};
const library = {schema_version:1, selected_id:"map", plans:[plan], campaigns:[]};
const fetchImpl = async (url, options={}) => {
  let body = {};
  if(options.body) body = JSON.parse(options.body);
  const data = url.endsWith("capabilities") ? catalog
    : url.endsWith("plans") ? library
    : url.endsWith("preview") ? {...preview, plan:body.plan || plan}
    : {};
  return {ok:true, status:200, statusText:"OK", json:async () => data};
};
const editor = createHcpesEditor({$, windowObj, fetchImpl,
  confirmImpl:()=>true, promptImpl:()=>null});
await editor.mount();
await new Promise(resolve => setTimeout(resolve, 0));
await editor.mount();
assert.equal(root.events.size, 3, "mount installs one delegated handler per event");
assert.equal(windowObj.events.size, 1, "unsaved-change guard is installed once");
assert.match(root.innerHTML, /Parameter-space blocks/);
assert.match(root.innerHTML, /four HCPES support supplies output off/);
assert.match(root.innerHTML, /next fresh sample-current readings/);
assert.doesNotMatch(root.innerHTML, /Qualified sample interval/);
assert.match(root.innerHTML, /Plasma establishment and recovery/);
assert.match(root.innerHTML, /Stability observation does not consume this budget/);
assert.match(root.innerHTML, /Between parameter changes/);
assert.match(root.innerHTML, /Move Ar inward/);
assert.match(root.innerHTML, /Collimating[\s\S]*1\.5[\s\S]*mA/,
  "current sweep blocks display stored amperes as operator-facing milliamperes");
assert.equal(editor.currentPreview().estimate.points, 2);

const nameInput = {dataset:{meta:"name"}, value:"Changed map", closest:()=>null};
await root.events.get("input")({target:nameInput});
assert.equal(editor.isDirty(), true);
assert.equal(elements.get("hcpesStart").disabled, true,
  "an unsaved plan cannot launch");
const establishmentDrift = {dataset:{setting:"max_drift_a_per_min",
  settingProfile:"establishment", settingScale:"1000"}, value:"0.2", closest:()=>null};
await root.events.get("input")({target:establishmentDrift});
assert.equal(editor.selected().settings.establishment.max_drift_a_per_min, .0002,
  "mA/min display value is converted to the stored A/min setting");
const currentRow={dataset:{axis:"2"}};
const currentAxis={dataset:{axisValue:"value",axisScale:"1000"},value:"2.5",
  closest:()=>currentRow,hasAttribute:()=>false};
await root.events.get("input")({target:currentAxis});
assert.equal(editor.selected().axes[2].value,.0025,
  "mA sweep input is converted back to stored amperes");

$("hcpesSessionId").value = "positive-001";
editor.updateStatus({running:true, phase:"timed settle after mfc:ar",
  session_id:"positive-001", point_total:10, points_completed:1,
  points_collected:1, points_rejected:0, qualified_target:5, qualified_samples:2,
  observed_drift_a_per_min:.0002, drift_threshold_a_per_min:.001,
  current_a:.0012, settle_remaining_s:1.4, recovery_remaining_s:18.2,
  active_stability_profile:"parameter_change", stability_window_elapsed_s:1.7,
  stability_window_required_s:3, stability_wait_elapsed_s:2.1,
  stability_max_wait_s:10, recovery_count:0,
  inaccessible_points:0, applied_setpoints:{"mfc:ar":2}});
assert.match(elements.get("hcpesLivePoint").textContent, /1 \/ 10 · 10%/);
assert.match(elements.get("hcpesLiveRateCard").className, /good/);
assert.match(elements.get("hcpesLiveWindow").textContent, /parameter 1.7 \/ 3.0 s/);
assert.equal(elements.get("hcpesLiveRetry").textContent, "18.2 s");
assert.match(elements.get("hcpesLiveSetpoints").innerHTML, /Ar/);
const oldSession = elements.get("hcpesSessionId").value;
editor.updateStatus({running:false, phase:"aborted", session_id:"positive-001"});
assert.notEqual(elements.get("hcpesSessionId").value, oldSession,
  "terminal runs receive a fresh retry name");

editor.dispose();
assert.equal(root.events.size, 0);
assert.equal(windowObj.events.size, 0);

console.log("PASS HCPES editor model, progress, retry naming, and lifecycle");
