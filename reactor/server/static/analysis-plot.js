/** Pure plot geometry helpers, kept DOM-free for regression coverage. */

export function finiteExtent(values, log=false){
  let lo = Infinity, hi = -Infinity;
  for(const value of values){
    if(!Number.isFinite(value) || (log && value <= 0)) continue;
    if(value < lo) lo = value;
    if(value > hi) hi = value;
  }
  return lo === Infinity ? null : [lo, hi];
}

export function resolveAxisRange(explicit, values, log=false){
  let [lo, hi] = finiteExtent(values, log) || (log ? [1e-9, 1] : [0, 1]);
  if(log){ lo = Math.log10(lo); hi = Math.log10(hi); }
  // Exact finite extents for real spans.  A constant series is the one case
  // that still needs a non-zero range or every coordinate divides by zero.
  if(hi - lo < 1e-12){
    const centre = lo;
    const half = (Math.abs(centre) || 1) * 0.05;
    lo = centre - half; hi = centre + half;
  }
  if(explicit[0] !== null){
    lo = log ? Math.log10(Math.max(explicit[0], 1e-300)) : explicit[0];
  }
  if(explicit[1] !== null){
    hi = log ? Math.log10(Math.max(explicit[1], 1e-300)) : explicit[1];
  }
  if(!(hi > lo)) hi = lo + 1;
  return [lo, hi];
}

export function nearestFinitePoint(series, targetX, log=false){
  if(!series || !Number.isFinite(targetX)) return null;
  let best = null;
  const count = Math.min(series.X.length, series.Y.length);
  for(let index = 0; index < count; index++){
    const x = series.X[index], y = series.Y[index];
    if(!Number.isFinite(x) || !Number.isFinite(y) || (log && y <= 0)) continue;
    const distance = Math.abs(x - targetX);
    if(best === null || distance < best.distance){
      best = {index, x, y, distance};
    }
  }
  return best;
}
