import assert from "node:assert/strict";
import {
  actionDefaults, createPrestartEditor, launchReview, moveItem, parameterValues,
} from "../../reactor/server/static/prestart-editor.js";

const action = {fields:[
  {id:"seconds", type:"number", default:1.5},
  {id:"enabled", type:"boolean", default:true},
  {id:"mode", type:"choice", choices:[{value:"above", label:"Above"}]},
]};
assert.deepEqual(actionDefaults(action), {seconds:1.5, enabled:true, mode:"above"});

const order = [{id:"a"},{id:"b"},{id:"c"}];
moveItem(order, 2, 0);
assert.deepEqual(order.map(x => x.id), ["c","a","b"]);

const recipe = {parameters:[
  {id:"flow", value_type:"number", default:2, source:"ar_sccm"},
  {id:"count", value_type:"integer", default:3},
  {id:"armed", value_type:"boolean", default:false},
]};
assert.deepEqual(parameterValues(recipe, {ar_sccm:"4.5"}, {armed:"true"}),
  {flow:4.5, count:3, armed:true});
assert.match(launchReview({name:"Setup", revision:4,
  start_steps:[{summary:"Open Ar"}], abort_steps:[{summary:"Close Ar"}]}),
  /Setup[\s\S]*START[\s\S]*Open Ar[\s\S]*ABORT \/ CLEANUP[\s\S]*Close Ar/);

function eventTarget(){
  const events = new Map();
  return {
    events,
    addEventListener(name, fn){ events.set(name, fn); },
    removeEventListener(name, fn){ if(events.get(name) === fn) events.delete(name); },
  };
}
const root = {...eventTarget(), innerHTML:"", querySelectorAll:()=>[]};
const windowObj = {...eventTarget(), Event:class {constructor(type){this.type=type;}}};
const selected = {
  schema_version:1, id:"current-prestart", name:"Current pre-start", description:"",
  revision:1, builtin:true, parameters:[], start_steps:[], abort_steps:[],
};
const fetchImpl = async url => ({ok:true, status:200, statusText:"OK",
  json:async () => url.endsWith("capabilities")
    ? {schema_version:1, targets:[]}
    : {schema_version:1, selected_id:selected.id, recipes:[selected]}});
const editor = createPrestartEditor({$:id => id === "preRecipeEditor" ? root : null,
  document:{}, windowObj, fetchImpl, confirmImpl:()=>true, promptImpl:()=>null});
await editor.mount();
await editor.mount();
assert.equal(root.events.size, 3, "mount is idempotent");
assert.equal(windowObj.events.size, 1, "unsaved-change guard is mounted once");
assert.equal(editor.selected().id, "current-prestart");
assert.match(root.innerHTML, /protected baseline/);
editor.dispose();
assert.equal(root.events.size, 0, "dispose removes root listeners");
assert.equal(windowObj.events.size, 0, "dispose removes window listener");

console.log("PASS pre-start editor model, review, and lifecycle");
