import assert from 'node:assert/strict';
import {createRunSetupLoader} from '../../reactor/server/static/control-run-setup.js';
import {formValuesFromRun} from '../../reactor/server/static/control-run-setup-values.js';

assert.deepEqual(formValuesFromRun({min_current_a:0.003, tolerance_frac:0.2,
  run_name:'old', cycles:40}), {min_current_ua:3000, tolerance_pct:20, cycles:40});

function element(){
  return {value:'', textContent:'', disabled:false, dataset:{}, children:[],
    replaceChildren(){this.children=[]; this.value='';},
    append(child){this.children.push(child);}};
}
const ids = ['runSetupStatus','runSetupSelect','runSetupLoad','runSetupApply',
  'runSetupRefresh','runSetupPreview'];
const elements = new Map(ids.map(id => [id, element()]));
const file = 'Mo-042/Mo-042_261027_120000_run_setup.json';
const setup = {file, digest:'abc', mode:'ald', source:'final recorded settings', schema_version:1,
  params:{cycles:42, mfc1_gas_enable:true, mfc1_gas_flow_sccm:2.4},
  current_gas_names:{mfc1:'H2'},
  gas_conflicts:[{mfc:'mfc1', source_gas:'NH3', current_gas:'H2', used:true}]};
const calls = [], applied = [];
let allow = false;
const loader = createRunSetupLoader({$:id => elements.get(id),
  documentObj:{createElement:() => element()}, confirmImpl:() => allow,
  runForms:{runParams:() => ({cycles:10, mfc1_gas_enable:false}),
    applyLoadedSetup:(params,mode) => applied.push({params,mode})},
  fetchImpl:async (url, options) => {
    calls.push({url, options});
    const body = url === '/api/run-setups'
      ? {runs:[{file, display_name:'Mo-042 · 2026-10-27', mode:'ald'}]}
      : url.startsWith('/api/run-setups/load?') ? setup
      : {params:setup.params, mode:'ald'};
    return {ok:true, json:async () => body};
  }});
loader.mount();
await loader.refresh();
assert.equal(elements.get('runSetupSelect').children.length, 2);
elements.get('runSetupSelect').value = file;
await elements.get('runSetupLoad').onclick();
assert.match(elements.get('runSetupPreview').textContent, /NH3/);
assert.equal(elements.get('runSetupApply').disabled, false);
await elements.get('runSetupApply').onclick();
assert.equal(applied.length, 0, 'gas conflict needs acknowledgement');
allow = true;
await elements.get('runSetupApply').onclick();
assert.equal(applied.length, 1);
assert.equal(applied[0].params.cycles, 42);
assert.deepEqual(JSON.parse(calls.at(-1).options.body).acknowledge_gases, ['mfc1']);
assert.equal(JSON.parse(calls.at(-1).options.body).digest, 'abc');
assert.deepEqual(JSON.parse(calls.at(-1).options.body).current_gas_names, {mfc1:'H2'});
loader.setBusy(true);
assert.equal(elements.get('runSetupApply').disabled, true);
assert.equal(elements.get('runSetupSelect').disabled, true);
console.log('PASS saved run review, gas acknowledgement, apply and busy guard');
