/** Hardware-page presentation for the server-owned aperture lifetime record. */

export function formatApertureHours(value){
  const hours = Number(value);
  if(!Number.isFinite(hours)) return "—";
  return hours < 100 ? hours.toFixed(2) : hours.toFixed(1);
}

export function formatApertureDate(value){
  if(!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function createApertureCard({$, document, post, toast, confirmImpl}){
  let current = null;
  let pending = false;
  let lastState = null;

  function historyRows(rows){
    const body = $("apertureHistory");
    body.replaceChildren();
    if(!rows.length){
      const tr = document.createElement("tr"), td = document.createElement("td");
      td.colSpan = 3; td.className = "hint"; td.textContent = "No replacements recorded";
      tr.appendChild(td); body.appendChild(tr); return;
    }
    for(const row of [...rows].reverse()){
      const tr = document.createElement("tr");
      for(const text of [formatApertureDate(row.installed_at),
                         formatApertureDate(row.replaced_at),
                         `${formatApertureHours(row.runtime_h)} h`]){
        const td = document.createElement("td");
        td.textContent = text; tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  }

  function render(state){
    lastState = state;
    const card = $("apertureCard"), button = $("newApertureBtn");
    const available = !!(state && state.available && state.current);
    card.classList.toggle("active", available && state.active === true);
    card.classList.toggle("unknown", !available || state.active === null);
    if(!available){
      current = null;
      $("apertureStatus").textContent = "record unavailable";
      $("apertureHours").textContent = "—";
      $("apertureInstalled").textContent = "—";
      $("apertureObserved").textContent = "—";
      $("apertureNote").textContent = (state && state.error) || "Aperture record unavailable";
      historyRows([]);
      button.disabled = true;
      return;
    }
    current = state.current;
    $("apertureStatus").textContent = state.active === true ? "● timing beam-on"
      : state.active === false ? "○ not timing" : "△ observation gap";
    $("apertureHours").textContent = formatApertureHours(current.runtime_h);
    $("apertureInstalled").textContent = formatApertureDate(current.installed_at);
    $("apertureObserved").textContent = formatApertureDate(current.last_observed_at);
    const warning = state.persistence_error
      ? `SAVE ERROR: ${state.persistence_error}`
      : (state.active === null ? (current.observation_gap_reason || "Beam state is unknown") : "");
    $("apertureNote").textContent = warning;
    historyRows(state.history || []);
    button.disabled = pending || state.active === true || !!state.persistence_error;
    button.title = state.active === true
      ? "Turn the beam off before recording an aperture replacement" : "";
  }

  async function replace(){
    if(pending || !current) return;
    const expected = current.id;
    const summary = `${formatApertureHours(current.runtime_h)} hours since `
      + `${formatApertureDate(current.installed_at)}`;
    if(!confirmImpl(`New Aperture Installed?\n\nThe current aperture (${summary}) `
      + `will be archived and the new aperture will start at 0 hours.\n\n`
      + `This changes the maintenance record only; it does not command hardware.`)) return;
    pending = true;
    $("newApertureBtn").disabled = true;
    try{
      const response = await post("/api/aperture/replaced", {
        expected_aperture_id: expected, confirm: true,
      });
      // A WebSocket frame may already have delivered the new aperture while
      // this request was in flight.  Never overwrite that newer live state
      // with a late response for the aperture that was confirmed.
      if(current && current.id === expected) render(response.aperture_lifetime);
      toast("Prior aperture archived; new aperture timing started", true);
    }finally{
      pending = false;
      if(lastState) render(lastState);
    }
  }

  function mount(){ $("newApertureBtn").onclick = replace; }
  function dispose(){ $("newApertureBtn").onclick = null; }
  return {dispose, mount, render};
}
