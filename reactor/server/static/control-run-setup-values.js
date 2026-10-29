/** Convert the recorded start payload back to the editable form's units. */
export function formValuesFromRun(params){
  const values = {};
  for(const [key, value] of Object.entries(params || {})){
    if(key === 'run_name') continue;
    if(key === 'min_current_a') values.min_current_ua = Number(value) * 1e6;
    else if(key === 'tolerance_frac') values.tolerance_pct = Number(value) * 100;
    else values[key] = value;
  }
  return values;
}
