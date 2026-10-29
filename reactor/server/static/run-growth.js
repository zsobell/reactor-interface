/** A rough, browser-local growth guide from streamed FS-1 points. */
export function createRunGrowth() {
  let runStarted = null;
  let lastRecv = null;
  let lastIndex = null;
  let unit = null;
  let points = [];

  return function update(state) {
    const run = state.recipe || {};
    const ell = state.ellipsometer || {};
    const active = ["running", "paused", "aborting"].includes(run.state);
    const identity = active ? run.started_at : null;
    if (identity !== runStarted) {
      runStarted = identity;
      lastRecv = null;
      lastIndex = null;
      unit = null;
      points = [];
    }

    const now = Number(state.t);
    const recv = Number(ell.last_recv);
    const thickness = Number(ell.last_thickness);
    const fresh = ell.enabled && ell.connected && ell.last_recv != null
      && Number.isFinite(now) && Number.isFinite(recv)
      && now - recv >= -2 && now - recv <= 5
      && ell.last_thickness != null && Number.isFinite(thickness);
    if (!fresh) return {thickness: "—", growth: "—"};

    const displayUnit = ell.last_thickness_unit || "";
    const display = `${thickness.toFixed(2)}${displayUnit ? ` ${displayUnit}` : ""}`;
    const cycle = Number(run.cycle);
    if (active && identity != null && Number.isInteger(cycle) && cycle >= 1
        && recv !== lastRecv) {
      if (lastRecv != null && (recv < lastRecv || ell.last_index <= lastIndex
          || displayUnit !== unit)) points = [];
      points.push({t: recv, cycle, thickness});
      lastRecv = recv;
      lastIndex = ell.last_index;
      unit = displayUnit;
    }
    points = points.filter(p => p.t >= now - 600);

    // Average each cycle's observations so a burst of samples in one cycle
    // cannot dominate the estimate. A line through those means is only a guide.
    const bins = new Map();
    for (const p of points) {
      const b = bins.get(p.cycle) || {sum: 0, count: 0};
      b.sum += p.thickness;
      b.count++;
      bins.set(p.cycle, b);
    }
    if (bins.size < 2) return {thickness: display, growth: "—"};
    const means = [...bins].map(([x, b]) => [x, b.sum / b.count]);
    const xbar = means.reduce((sum, p) => sum + p[0], 0) / means.length;
    const ybar = means.reduce((sum, p) => sum + p[1], 0) / means.length;
    const denom = means.reduce((sum, p) => sum + (p[0] - xbar) ** 2, 0);
    const slope = means.reduce((sum, p) => sum + (p[0] - xbar) * (p[1] - ybar), 0) / denom;
    return {thickness: display,
      growth: `~${slope >= 0 ? "+" : ""}${slope.toFixed(2)}${displayUnit ? ` ${displayUnit}` : ""}/cycle`};
  };
}
