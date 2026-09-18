/** Capability-rendered editor for named pre-start and cleanup recipes. */
"use strict";

const clone = value => JSON.parse(JSON.stringify(value));
const html = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;",
})[c]);

export function actionDefaults(action){
  const args = {};
  for(const field of action.fields || []){
    if(field.default !== undefined) args[field.id] = clone(field.default);
    else if(field.type === "boolean") args[field.id] = false;
    else if(field.type === "number") args[field.id] = 0;
    else if(field.type === "choice" && field.choices?.length)
      args[field.id] = clone(field.choices[0].value);
    else args[field.id] = "";
  }
  return args;
}

export function moveItem(items, from, to){
  if(from < 0 || from >= items.length || to < 0 || to >= items.length) return items;
  const [item] = items.splice(from, 1);
  items.splice(to, 0, item);
  return items;
}

export function parameterValues(recipe, baseValues={}, overrides={}){
  const result = {};
  for(const parameter of recipe?.parameters || []){
    let value = overrides[parameter.id];
    if(value === undefined && parameter.source && baseValues[parameter.source] !== undefined)
      value = baseValues[parameter.source];
    if(value === undefined) value = parameter.default;
    result[parameter.id] = coerce(value, parameter.value_type);
  }
  return result;
}

function coerce(value, type){
  if(type === "number") return Number(value);
  if(type === "integer") return parseInt(value, 10);
  if(type === "boolean") return value === true || value === "true" || value === "1";
  return value;
}

export function launchReview(preview){
  const start = (preview.start_steps || []).map((s, i) => `${i+1}. ${s.summary}`).join("\n");
  const cleanup = (preview.abort_steps || []).map((s, i) => `${i+1}. ${s.summary}`).join("\n");
  return `Run pre-start recipe "${preview.name}" (revision ${preview.revision})?\n\n`
    + `START\n${start || "No start actions."}\n\n`
    + `ABORT / CLEANUP\n${cleanup || "No cleanup actions configured."}`;
}

export function createPrestartEditor({$, document, windowObj=window, fetchImpl=fetch,
                                      toast=()=>{}, confirmImpl=confirm,
                                      promptImpl=prompt, getBaseValues=()=>({})}){
  let root = null, catalog = {targets:[]}, library = {recipes:[], selected_id:""};
  let recipe = null, dirty = false, disposed = false, mounted = false;
  let section = "start_steps", launchOverrides = {}, sequence = 0;
  const listeners = [];

  const targets = () => new Map(catalog.targets.map(t => [t.id, t]));
  const target = id => targets().get(id);
  const targetDisplay = item => item ? `${item.label} · ${item.id}` : "";
  const action = step => (target(step.target)?.actions || []).find(a => a.id === step.action);
  const editable = () => !!recipe && !recipe.builtin;

  function listen(el, name, fn){
    el.addEventListener(name, fn);
    listeners.push(() => el.removeEventListener?.(name, fn));
  }

  async function request(url, options={}){
    const response = await fetchImpl(url, options);
    if(!response.ok){
      let message = response.statusText || `HTTP ${response.status}`;
      try{ message = (await response.json()).detail || message; }catch(_){}
      toast(message);
      const error = new Error(message); error.status = response.status; throw error;
    }
    return response.json();
  }

  async function send(url, method, body){
    return request(url, {method, headers:{"Content-Type":"application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
  }

  function selectLocal(id){
    const found = library.recipes.find(r => r.id === id);
    recipe = found ? clone(found) : null;
    launchOverrides = {};
    dirty = false;
    render();
  }

  async function load(preferredId){
    const [nextCatalog, nextLibrary] = await Promise.all([
      request("/api/prestart/capabilities"), request("/api/prestart/recipes"),
    ]);
    if(disposed) return;
    catalog = nextCatalog; library = nextLibrary;
    selectLocal(preferredId || library.selected_id);
  }

  function markDirty(){
    if(!editable()) return;
    dirty = true;
    const status = $("preEditStatus");
    if(status){ status.textContent = "unsaved changes"; status.className = "pre-edit-status dirty"; }
    const save = $("preSaveRecipe"); if(save) save.disabled = false;
  }

  function guardDirty(){
    return !dirty || confirmImpl("Discard unsaved changes to this pre-start recipe?");
  }

  function optionList(items, selected, label=value => value){
    return items.map(item => {
      const value = typeof item === "object" ? item.value : item;
      return `<option value="${html(value)}"${String(value) === String(selected) ? " selected" : ""}>`
        + `${html(label(item))}</option>`;
    }).join("");
  }

  function render(){
    if(!root || !recipe) return;
    const locked = recipe.builtin;
    const steps = recipe[section] || [];
    const base = getBaseValues() || {};
    const values = parameterValues(recipe, base, launchOverrides);
    root.innerHTML = `
      <div class="pre-toolbar">
        <label>Recipe<select id="preRecipeSelect">${library.recipes.map(r =>
          `<option value="${html(r.id)}"${r.id === recipe.id ? " selected" : ""}>${html(r.name)}</option>`
        ).join("")}</select></label>
        <button type="button" id="preNewRecipe">New</button>
        <button type="button" id="preDuplicateRecipe">Duplicate</button>
        <button type="button" id="preDeleteRecipe" title="Delete recipe"${locked ? " disabled" : ""}>×</button>
        <span class="pre-spacer"></span>
        <span id="preEditStatus" class="pre-edit-status">${locked ? "protected baseline" : `revision ${recipe.revision}`}</span>
        <button type="button" id="preSaveRecipe" class="primary"${locked || !dirty ? " disabled" : ""}>Save</button>
      </div>
      <div class="pre-meta">
        <label>Name<input id="preRecipeName" value="${html(recipe.name)}" maxlength="100"${locked ? " disabled" : ""}></label>
        <label>Description<input id="preRecipeDescription" value="${html(recipe.description)}"${locked ? " disabled" : ""}></label>
      </div>
      <div class="pre-launch">
        <div class="subhead">Launch values</div>
        <div class="params">${recipe.parameters.map(p => launchField(p, values[p.id])).join("") ||
          `<span class="hint">This recipe has no launch parameters.</span>`}</div>
      </div>
      <div class="pre-sectionbar">
        <div class="pre-segment" role="tablist">
          <button type="button" data-section="start_steps" class="${section === "start_steps" ? "active" : ""}">Start</button>
          <button type="button" data-section="abort_steps" class="${section === "abort_steps" ? "active" : ""}">Abort / cleanup</button>
        </div>
        <button type="button" id="preAddStep"${locked ? " disabled" : ""}>+ Add step</button>
      </div>
      <datalist id="preTargetChoices">${catalog.targets.map(t =>
        `<option value="${html(targetDisplay(t))}">${html(t.kind)}</option>`).join("")}</datalist>
      <div class="pre-step-list">${steps.map((step, index) => stepRow(step, index, locked)).join("") ||
        `<div class="pre-empty">No ${section === "start_steps" ? "start" : "cleanup"} steps.</div>`}</div>
      <details class="pre-parameters"><summary>Recipe parameters</summary>
        <div class="pre-param-list">${recipe.parameters.map((p, i) => parameterRow(p, i, locked)).join("")}</div>
        <button type="button" id="preAddParameter" class="mini"${locked ? " disabled" : ""}>+ Add parameter</button>
      </details>`;
  }

  function launchField(parameter, value){
    const id = `preLaunch_${parameter.id}`;
    const source = parameter.source ? `<span class="pre-source">${html(parameter.source)}</span>` : "";
    if(parameter.value_type === "boolean")
      return `<label>${html(parameter.label)} ${source}<input id="${id}" data-launch="${html(parameter.id)}" type="checkbox"${value ? " checked" : ""}></label>`;
    if(parameter.value_type === "choice")
      return `<label>${html(parameter.label)} ${source}<select id="${id}" data-launch="${html(parameter.id)}">${optionList(parameter.choices, value, c => c.label)}</select></label>`;
    const numeric = ["number","integer"].includes(parameter.value_type);
    return `<label>${html(parameter.label)} ${source}<span class="pre-unit-input"><input id="${id}" data-launch="${html(parameter.id)}" type="${numeric ? "number" : "text"}" value="${html(value)}"${parameter.minimum != null ? ` min="${parameter.minimum}"` : ""}${parameter.maximum != null ? ` max="${parameter.maximum}"` : ""}${parameter.value_type === "integer" ? ` step="1"` : ` step="any"`}><span>${html(parameter.unit)}</span></span></label>`;
  }

  function stepRow(step, index, locked){
    const selectedTarget = target(step.target);
    const selectedAction = action(step);
    const disabled = locked ? " disabled" : "";
    const invalid = !selectedTarget || !selectedAction;
    return `<div class="pre-step${invalid ? " invalid" : ""}${!step.enabled ? " disabled-step" : ""}" data-step="${index}">
      <div class="pre-step-order">${index+1}</div>
      <label class="pre-step-enabled" title="Enable step"><input type="checkbox" data-step-enabled${step.enabled ? " checked" : ""}${disabled}></label>
      <div class="pre-step-main">
        <div class="pre-step-head">
          <label>Device<input list="preTargetChoices" data-step-target value="${html(selectedTarget ? targetDisplay(selectedTarget) : step.target)}"${disabled}></label>
          <label>Action<select data-step-action${disabled}>${selectedTarget ? optionList(selectedTarget.actions, step.action, a => a.label) : `<option>${html(step.action)}</option>`}</select></label>
          <div class="pre-step-tools">
            <button type="button" data-move="up" title="Move up"${locked || index === 0 ? " disabled" : ""}>↑</button>
            <button type="button" data-move="down" title="Move down"${locked || index === recipe[section].length-1 ? " disabled" : ""}>↓</button>
            <button type="button" data-duplicate-step title="Duplicate step"${disabled}>⧉</button>
            <button type="button" data-delete-step title="Delete step"${disabled}>×</button>
          </div>
        </div>
        <div class="pre-step-fields">${selectedAction ? selectedAction.fields.map(field => fieldControl(step, field, locked)).join("") : `<span class="pre-field-error">Unknown target or action; choose a configured device to repair this step.</span>`}</div>
        <label class="pre-on-error"><input type="checkbox" data-continue${step.on_error === "continue" ? " checked" : ""}${disabled}> Continue if this step fails</label>
      </div>
    </div>`;
  }

  function compatibleParameters(field){
    const type = field.type === "choice" ? "choice" : field.type;
    return recipe.parameters.filter(p => p.value_type === type ||
      (field.type === "number" && p.value_type === "integer"));
  }

  function fieldControl(step, field, locked){
    const raw = step.args?.[field.id];
    const ref = raw && typeof raw === "object" ? raw.parameter : "";
    const fixed = ref ? field.default : raw;
    const disabled = locked ? " disabled" : "";
    const bindable = field.type !== "target";
    const binding = bindable ? `<select data-field-bind="${html(field.id)}"${disabled}>
      <option value="">Fixed value</option>${compatibleParameters(field).map(p =>
        `<option value="${html(p.id)}"${p.id === ref ? " selected" : ""}>Parameter: ${html(p.label)}</option>`).join("")}</select>` : "";
    let control;
    if(field.type === "target"){
      const choices = catalog.targets.filter(t => !field.target_kind || t.kind === field.target_kind);
      control = `<select data-field="${html(field.id)}"${disabled}>${choices.map(t =>
        `<option value="${html(t.id)}"${t.id === raw ? " selected" : ""}>${html(t.label)}</option>`).join("")}</select>`;
    }else if(field.type === "choice"){
      control = `<select data-field="${html(field.id)}"${disabled || ref ? " disabled" : ""}>${optionList(field.choices || [], fixed, c => c.label)}</select>`;
    }else if(field.type === "boolean"){
      control = `<input data-field="${html(field.id)}" type="checkbox"${fixed ? " checked" : ""}${disabled || ref ? " disabled" : ""}>`;
    }else{
      control = `<span class="pre-unit-input"><input data-field="${html(field.id)}" type="${field.type === "number" ? "number" : "text"}" value="${html(fixed ?? "")}"${field.minimum != null ? ` min="${field.minimum}"` : ""}${field.maximum != null ? ` max="${field.maximum}"` : ""}${field.type === "number" ? ` step="any"` : ""}${disabled || ref ? " disabled" : ""}><span>${html(field.unit || "")}</span></span>`;
    }
    return `<label class="pre-step-field">${html(field.label)}${binding}${control}</label>`;
  }

  function parameterRow(parameter, index, locked){
    const disabled = locked ? " disabled" : "";
    return `<div class="pre-param" data-parameter="${index}">
      <input data-param="label" value="${html(parameter.label)}" aria-label="Parameter label"${disabled}>
      <input data-param="id" value="${html(parameter.id)}" aria-label="Parameter id"${disabled}>
      <select data-param="value_type" aria-label="Parameter type"${disabled}>${optionList(["number","integer","boolean","string","choice"], parameter.value_type)}</select>
      <input data-param="default" value="${html(parameter.default)}" aria-label="Default value"${disabled}>
      <input data-param="unit" value="${html(parameter.unit)}" aria-label="Unit"${disabled}>
      <input data-param="source" value="${html(parameter.source || "")}" placeholder="run field (optional)" aria-label="Run field source"${disabled}>
      <button type="button" data-delete-parameter title="Delete parameter"${disabled}>×</button>
    </div>`;
  }

  function currentStep(element){
    const row = element.closest("[data-step]");
    return row ? recipe[section][Number(row.dataset.step)] : null;
  }

  function valueOf(element, type){
    if(type === "boolean" || element.type === "checkbox") return element.checked;
    if(type === "number") return Number(element.value);
    if(type === "integer") return parseInt(element.value, 10);
    const option = element.selectedOptions?.[0];
    if(type === "choice" && option){
      const match = option.value;
      const numeric = Number(match);
      return match !== "" && Number.isFinite(numeric) ? numeric : match;
    }
    return element.value;
  }

  async function onChange(event){
    const el = event.target;
    if(el.id === "preRecipeSelect"){
      if(!guardDirty()){ el.value = recipe.id; return; }
      await send(`/api/prestart/recipes/${encodeURIComponent(el.value)}/select`, "POST", {});
      library.selected_id = el.value; selectLocal(el.value); return;
    }
    if(el.dataset.launch){
      const parameter = recipe.parameters.find(p => p.id === el.dataset.launch);
      launchOverrides[parameter.id] = valueOf(el, parameter.value_type);
      const source = parameter.source ? $(`p_${parameter.source}`) : null;
      if(source){
        if(source.type === "checkbox") source.checked = !!launchOverrides[parameter.id];
        else source.value = launchOverrides[parameter.id];
        if(source.dispatchEvent && windowObj.Event)
          source.dispatchEvent(new windowObj.Event("input", {bubbles:true}));
      }
      return;
    }
    const step = currentStep(el);
    if(step && el.hasAttribute("data-step-target")){
      const next = target(el.value) || catalog.targets.find(t => targetDisplay(t) === el.value);
      if(next){ step.target = next.id; step.action = next.actions[0]?.id || "";
        step.args = actionDefaults(next.actions[0] || {fields:[]}); markDirty(); render(); }
      return;
    }
    if(step && el.hasAttribute("data-step-action")){
      step.action = el.value; step.args = actionDefaults(action(step) || {fields:[]});
      markDirty(); render(); return;
    }
    if(step && el.hasAttribute("data-step-enabled")){ step.enabled = el.checked; markDirty(); render(); return; }
    if(step && el.hasAttribute("data-continue")){ step.on_error = el.checked ? "continue" : "stop"; markDirty(); return; }
    if(step && el.dataset.fieldBind !== undefined){
      const field = action(step).fields.find(f => f.id === el.dataset.fieldBind);
      step.args[field.id] = el.value ? {parameter:el.value} : clone(field.default ?? (field.type === "boolean" ? false : field.type === "number" ? 0 : ""));
      markDirty(); render(); return;
    }
    if(step && el.dataset.field !== undefined){
      const field = action(step).fields.find(f => f.id === el.dataset.field);
      step.args[field.id] = valueOf(el, field.type); markDirty(); return;
    }
    const paramRow = el.closest?.("[data-parameter]");
    if(paramRow && el.dataset.param){
      const parameter = recipe.parameters[Number(paramRow.dataset.parameter)];
      const key = el.dataset.param;
      let value = el.value;
      if(key === "source") value = value || null;
      if(key === "default") value = coerce(value, parameter.value_type);
      parameter[key] = value; markDirty();
      if(key === "value_type") render();
    }
  }

  function onInput(event){
    const el = event.target;
    if(!editable()) return;
    if(el.id === "preRecipeName"){ recipe.name = el.value; markDirty(); }
    else if(el.id === "preRecipeDescription"){ recipe.description = el.value; markDirty(); }
  }

  async function onClick(event){
    const button = event.target.closest?.("button");
    if(!button) return;
    if(button.dataset.section){ section = button.dataset.section; render(); return; }
    if(button.id === "preSaveRecipe"){ await save(); return; }
    if(button.id === "preNewRecipe" || button.id === "preDuplicateRecipe"){
      if(!guardDirty()) return;
      const from = button.id === "preDuplicateRecipe" ? recipe.id : "current-prestart";
      const name = promptImpl("Name for the new pre-start recipe:",
        button.id === "preDuplicateRecipe" ? `${recipe.name} copy` : "New pre-start");
      if(!name?.trim()) return;
      const created = await send("/api/prestart/recipes", "POST", {name:name.trim(), from_id:from});
      await load(created.id); return;
    }
    if(button.id === "preDeleteRecipe"){
      if(!confirmImpl(`Delete pre-start recipe "${recipe.name}"?`)) return;
      await send(`/api/prestart/recipes/${encodeURIComponent(recipe.id)}`, "DELETE");
      await load(); return;
    }
    if(button.id === "preAddStep"){
      const first = catalog.targets[0], firstAction = first?.actions?.[0];
      recipe[section].push({id:uniqueStepId(), target:first.id, action:firstAction.id,
        args:actionDefaults(firstAction), enabled:true, on_error:"stop"});
      markDirty(); render(); return;
    }
    if(button.id === "preAddParameter"){
      const id = uniqueParameterId();
      recipe.parameters.push({id, label:"New parameter", value_type:"number",
        default:0, unit:"", minimum:null, maximum:null, choices:[], source:null});
      markDirty(); render(); return;
    }
    const step = currentStep(button);
    if(step){
      const index = recipe[section].indexOf(step);
      if(button.dataset.move){ moveItem(recipe[section], index, index + (button.dataset.move === "up" ? -1 : 1)); }
      else if(button.hasAttribute("data-duplicate-step")){
        const copy = clone(step); copy.id = uniqueStepId(); recipe[section].splice(index+1, 0, copy);
      }else if(button.hasAttribute("data-delete-step")) recipe[section].splice(index, 1);
      else return;
      markDirty(); render(); return;
    }
    const paramRow = button.closest?.("[data-parameter]");
    if(paramRow && button.hasAttribute("data-delete-parameter")){
      recipe.parameters.splice(Number(paramRow.dataset.parameter), 1); markDirty(); render();
    }
  }

  function uniqueStepId(){
    const used = new Set([...recipe.start_steps, ...recipe.abort_steps].map(s => s.id));
    let id; do{ id = `step-${Date.now()}-${++sequence}`; }while(used.has(id)); return id;
  }
  function uniqueParameterId(){
    const used = new Set(recipe.parameters.map(p => p.id));
    let n = 1, id = "parameter"; while(used.has(id)) id = `parameter${++n}`; return id;
  }

  async function save(){
    if(!dirty || !editable()) return recipe;
    const expected = recipe.revision;
    try{
      const saved = await send(`/api/prestart/recipes/${encodeURIComponent(recipe.id)}`,
        "PUT", {expected_revision:expected, recipe});
      const index = library.recipes.findIndex(r => r.id === saved.id);
      if(index >= 0) library.recipes[index] = clone(saved);
      recipe = clone(saved); dirty = false; render(); toast("Pre-start recipe saved", true);
      return recipe;
    }catch(error){
      const status = $("preEditStatus");
      if(status && error.status === 409){ status.textContent = "conflict: reload before saving"; status.className = "pre-edit-status conflict"; }
      throw error;
    }
  }

  async function prepareLaunch(baseValues=getBaseValues()){
    await save();
    const values = parameterValues(recipe, baseValues || {}, launchOverrides);
    const preview = await send("/api/prestart/preview", "POST",
      {recipe_id:recipe.id, values});
    return {recipe_id:recipe.id, recipe_revision:preview.revision, values, preview};
  }

  function updateStatus(pre={}){
    if(!root) return;
    root.querySelectorAll?.(".pre-step").forEach(row => row.classList.toggle(
      "active", pre.running && section === "start_steps"
        && recipe?.id === pre.recipe_id
        && recipe[section]?.[Number(row.dataset.step)]?.id === pre.step_id));
    const status = $("preSnapshotStatus");
    if(status) status.textContent = pre.running || pre.cleanup_available
      ? `${pre.recipe_name || "Recipe"} r${pre.revision || "?"} snapshot · ${pre.phase || pre.state}` : "";
  }

  function beforeUnload(event){
    if(!dirty) return;
    event.preventDefault(); event.returnValue = "";
  }

  async function mount(){
    if(mounted) return;
    mounted = true; disposed = false; root = $("preRecipeEditor");
    if(!root) return;
    listen(root, "click", event => { onClick(event).catch(error => toast(error.message)); });
    listen(root, "change", event => { onChange(event).catch(error => toast(error.message)); });
    listen(root, "input", onInput);
    listen(windowObj, "beforeunload", beforeUnload);
    root.innerHTML = `<div class="pre-empty">Loading pre-start recipes…</div>`;
    try{ await load(); }catch(error){ root.innerHTML = `<div class="pre-field-error">${html(error.message)}</div>`; }
  }

  function dispose(){
    disposed = true; mounted = false;
    while(listeners.length) listeners.pop()();
  }

  return {mount, dispose, load, save, prepareLaunch, updateStatus,
    selected:()=>recipe ? clone(recipe) : null, isDirty:()=>dirty,
    reviewText:launchReview};
}
