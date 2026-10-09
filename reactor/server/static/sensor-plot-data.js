/** Reduce a time series for canvas drawing while retaining each bucket's peaks. */
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
