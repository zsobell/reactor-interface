import assert from "node:assert/strict";
import {
  axisKeys, continuousOrder, displayScale, displayValue, filterPoints, heatCells,
  heatColour, label, metricKeys, metricValue, pointConditionLines, rawCurrent, sliceGroups,
} from "../../reactor/server/static/hcpes-analysis.js";

const source = {
  kind:"campaign", compatible:true,
  fields:["source_session","source_polarity","source_point","point_index",
    "signed_stage_bias_v","setpoint:mfc:ar","setpoint:supply:grid_bias","setpoint:supply:collimating",
    "stage_current_mean_a","aperture_lifetime_mean_s","settled","accessibility"],
  points:[
    {source_session:"neg",source_point:2,signed_order:1,signed_stage_bias_v:-10,
      "setpoint:mfc:ar":1,"setpoint:supply:grid_bias":100,"setpoint:supply:collimating":1.5,
      stage_current_mean_a:.001,aperture_lifetime_mean_s:120,
      settled:true,accessibility:"accessible",
      channel_stats:{pressure:{mean:1e-5},aperture_lifetime_s:{mean:120}}},
    {source_session:"neg",source_point:1,signed_order:2,signed_stage_bias_v:0,
      "setpoint:mfc:ar":1,"setpoint:supply:grid_bias":100,"setpoint:supply:collimating":1.5,
      stage_current_mean_a:.002,aperture_lifetime_mean_s:121,
      settled:false,accessibility:"recovered",
      channel_stats:{pressure:{mean:2e-5},aperture_lifetime_s:{mean:121}}},
    {source_session:"pos",source_point:1,signed_order:3,signed_stage_bias_v:0,
      "setpoint:mfc:ar":1,"setpoint:supply:grid_bias":100,"setpoint:supply:collimating":1.5,
      stage_current_mean_a:.003,aperture_lifetime_mean_s:122,
      settled:true,accessibility:"accessible",
      channel_stats:{pressure:{mean:3e-5},aperture_lifetime_s:{mean:122}}},
    {source_session:"pos",source_point:2,signed_order:4,signed_stage_bias_v:10,
      "setpoint:mfc:ar":2,"setpoint:supply:grid_bias":100,"setpoint:supply:collimating":1.5,
      stage_current_mean_a:.004,aperture_lifetime_mean_s:123,
      settled:true,accessibility:"accessible",
      channel_stats:{pressure:{mean:4e-5},aperture_lifetime_s:{mean:123}}},
  ],
};

assert.deepEqual(axisKeys(source), ["signed_stage_bias_v","setpoint:mfc:ar",
  "setpoint:supply:grid_bias","setpoint:supply:collimating"]);
assert.ok(metricKeys(source).includes("stage_current_mean_a"));
assert.ok(metricKeys(source).includes("aperture_lifetime_mean_s"));
assert.ok(metricKeys(source).includes("channel:pressure"));
assert.equal(label("aperture_lifetime_mean_s"), "Aperture lifetime mean (s)");
assert.equal(label("stage_current_mean_a"), "Stage current mean (mA)");
assert.equal(label("setpoint:supply:collimating"), "collimating (A)");
assert.equal(displayScale("channel:inst.ammeter"),1000);
assert.equal(displayValue("stage_current_mean_a",.0012),1.2);
assert.equal(displayValue("stage_current_mean_a",null),null,
  "missing measurements stay missing rather than becoming zero");
assert.ok(pointConditionLines(source,source.points[0]).includes("collimating (A): 1.5"));
assert.notEqual(heatColour(0),heatColour(.5));
assert.notEqual(heatColour(.5),heatColour(1));
assert.equal(metricValue(source.points[1], "channel:aperture_lifetime_s"), 121);
assert.equal(metricValue(source.points[1], "channel:pressure"), 2e-5);
assert.deepEqual(filterPoints(source.points,{"setpoint:mfc:ar":"1"}).map(p=>p.source_point),[2,1,1]);

const ordered=continuousOrder([...source.points].reverse(),"signed_stage_bias_v");
assert.deepEqual(ordered.map(point=>[point.signed_stage_bias_v,point.source_session]),
  [[-10,"neg"],[0,"neg"],[0,"pos"],[10,"pos"]],
  "duplicate zero keeps negative then positive provenance");

const groups=sliceGroups(source,source.points,"signed_stage_bias_v",{
  "setpoint:supply:grid_bias":"100",
});
assert.equal(groups.length,2,"unfiltered Ar values become separate slices");

const mismatched={...source,compatible:false};
const mismatchGroups=sliceGroups(mismatched,source.points,"signed_stage_bias_v",{
  "setpoint:mfc:ar":"1","setpoint:supply:grid_bias":"100",
});
assert.equal(mismatchGroups.length,2,"incompatible polarity sessions never stitch");

const heat=heatCells(source.points,"signed_stage_bias_v","setpoint:mfc:ar",
  "stage_current_mean_a");
const zero=heat.find(cell=>cell.x===0&&cell.y===1);
assert.equal(zero.value,.0025,"duplicate cells average qualified point summaries");
assert.equal(zero.points.length,2,"heat cell retains both source points");
assert.equal(rawCurrent({measurements:{"inst.ammeter":.001}}),.001);
assert.equal(rawCurrent({measurements:{"inst.ammeter":"bad"}}),null);

console.log("PASS HCPES analysis filters, signed ordering, heat cells, and provenance");
