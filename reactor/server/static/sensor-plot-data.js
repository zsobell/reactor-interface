/** Reduce a time series for canvas drawing while retaining each bucket's peaks. */
export function sensorTimeRange({mode, windowSeconds, start, end, earliest, now}){
  if(mode === "manual") return [start ?? earliest ?? now-windowSeconds, end ?? now];
  return [now-windowSeconds, now];
}

export function navigateSensorRange([from,to], key){
  const span=to-from;
  if(!(span > 0)) return null;
  if(key === "ArrowLeft") return [from-span*0.2,to-span*0.2];
  if(key === "ArrowRight") return [from+span*0.2,to+span*0.2];
  if(key === "ArrowUp") return [to-Math.max(1,span*0.8),to];
  if(key === "ArrowDown") return [to-span*1.25,to];
  return null;
}

export function reduceSensorPoints(points, from, to, buckets=700){
  const visible = points.filter(([t]) => Number.isFinite(t) && t >= from && t <= to);
  if(visible.length <= buckets * 2) return visible;
  const size = Math.ceil(visible.length / buckets);
  const out = [];
  for(let start=0; start<visible.length; start+=size){
    const chunk = visible.slice(start, start+size);
    let min=-1, max=-1, gap=-1;
    for(let i=0; i<chunk.length; i++){
      const value = chunk[i][1];
      if(!Number.isFinite(value)){
        if(gap < 0) gap = i;
      } else {
        if(min < 0 || value < chunk[min][1]) min = i;
        if(max < 0 || value > chunk[max][1]) max = i;
      }
    }
    for(const i of [...new Set([min,max,gap].filter(i => i >= 0))].sort((a,b)=>a-b))
      out.push(chunk[i]);
  }
  const last = visible.at(-1);
  if(last && out.at(-1) !== last) out.push(last);
  return out;
}
