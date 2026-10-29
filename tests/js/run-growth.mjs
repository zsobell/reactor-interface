import assert from 'node:assert/strict';
import {createRunGrowth} from '../../reactor/server/static/run-growth.js';

const update = createRunGrowth();
const frame = (t, cycle, thickness, opts = {}) => ({
  t, recipe: {state: 'running', started_at: 1000, cycle},
  ellipsometer: {enabled: true, connected: true, last_recv: t,
    last_index: t, last_thickness: thickness, last_thickness_unit: 'nm', ...opts},
});

assert.deepEqual(update({t: 1000, recipe: {state: 'idle'}, ellipsometer: null}),
  {thickness: '—', growth: '—'});
assert.deepEqual(update(frame(1001, 1, 2)), {thickness: '2.00 nm', growth: '—'});
update(frame(1002, 1, 2.2));
assert.equal(update(frame(1003, 2, 2.5)).growth, '~+0.40 nm/cycle');
assert.equal(update({...frame(1010, 2, 2.5), ellipsometer: {
  ...frame(1003, 2, 2.5).ellipsometer}}).growth, '—', 'stale point hides estimate');
assert.equal(update(frame(1605, 3, 3)).growth, '—', 'old points leave the ten minute window');
assert.equal(update({...frame(1606, 3, 3), recipe: {state: 'running',
  started_at: 1500, cycle: 3}}).growth, '—', 'new run clears prior points');
assert.equal(update({...frame(1607, 4, 3.2, {last_thickness_unit: 'A'}),
  recipe: {state: 'running', started_at: 1500, cycle: 4}}).growth,
  '—', 'unit change clears prior points');
assert.equal(update({t: 1608, recipe: {state: 'idle'}, ellipsometer: null}).growth,
  '—', 'idle clears estimate');
console.log('PASS run thickness and recent growth guide');
