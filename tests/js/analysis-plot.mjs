import assert from 'node:assert/strict';
import fs from 'node:fs';
import {finiteExtent, nearestFinitePoint, resolveAxisRange}
  from '../../reactor/server/static/analysis-plot.js';

assert.deepEqual(finiteExtent([NaN, 10, 20, Infinity, 15]), [10, 20],
  'loaded-file summaries ignore missing and non-finite samples');
assert.deepEqual(finiteExtent([-1, 0, 0.01, 100], true), [0.01, 100],
  'log extents include only finite positive samples');
assert.equal(finiteExtent([NaN, Infinity]), null,
  'a loaded column with no finite samples has no summary extent');

const loader = fs.readFileSync(
  new URL('../../reactor/server/static/analysis.js', import.meta.url), 'utf8');
assert.match(loader, /finiteExtent\(xcol,\s*false\)/,
  'the loaded-file adoption path uses the exported extent helper');

assert.deepEqual(resolveAxisRange([null, null], [10, 20, 15], false), [10, 20],
  'linear auto-fit uses exact data extent without padding');
assert.deepEqual(resolveAxisRange([null, null], [0.01, 100], true), [-2, 2],
  'log auto-fit uses exact positive extent without padding');
assert.deepEqual(resolveAxisRange([5, 25], [10, 20], false), [5, 25],
  'explicit bounds still win');
const constant = resolveAxisRange([null, null], [7, 7], false);
assert.ok(constant[0] < 7 && constant[1] > 7, 'constant series retains a drawable span');
assert.deepEqual(resolveAxisRange([null, null], [NaN, -1, 0], true), [-9, 0],
  'empty positive log extent retains deterministic fallback');

const sparse = {X:[0, 1, 4, 10], Y:[NaN, 11, NaN, 99]};
assert.deepEqual(nearestFinitePoint(sparse, 4.1),
  {index:1, x:1, y:11, distance:3.0999999999999996},
  'nearest row with NaN is skipped in favor of an actual sample');
assert.equal(nearestFinitePoint({X:[9, 2, 6], Y:[90, 20, 60]}, 5).x, 6,
  'unsorted samples are searched independently');
assert.equal(nearestFinitePoint({X:[1, 1, 2], Y:[4, 5, 6]}, 1).index, 0,
  'duplicate-x ties are deterministic');
assert.equal(nearestFinitePoint({X:[0, .2, 1], Y:[0, 2, 3]}, .1).x, 0,
  'binary and sub-second samples use the same nearest-point rule');
assert.equal(nearestFinitePoint({X:[1, 2, 3], Y:[-5, 0, 7]}, 1.1, true).x, 3,
  'log-invalid values are never selected');
assert.equal(nearestFinitePoint({X:[1, 2], Y:[NaN, NaN]}, 1.5), null,
  'a series with no finite visible point is explicitly unavailable');

const y1 = nearestFinitePoint({X:[0, 5], Y:[10, 50]}, 3.6);
const y2 = nearestFinitePoint({X:[1, 4.1], Y:[100, 410]}, 3.6);
assert.equal(y1.x, 5);
assert.equal(y2.x, 4.1, 'asynchronous Y axes choose their own actual timestamps');
console.log('PASS exact analysis axes and nearest finite per-series hover samples');
