import assert from 'node:assert/strict';
import {createApertureCard, formatApertureHours}
  from '../../reactor/server/static/aperture-card.js';

class ClassList {
  constructor(){ this.names = new Set(); }
  toggle(name, on){ if(on) this.names.add(name); else this.names.delete(name); }
  contains(name){ return this.names.has(name); }
}
class Element {
  constructor(tag='div'){
    this.tagName = tag; this.textContent = ''; this.children = []; this.disabled = false;
    this.title = ''; this.className = ''; this.classList = new ClassList(); this.onclick = null;
  }
  appendChild(child){ this.children.push(child); return child; }
  replaceChildren(...children){ this.children = [...children]; }
}
const elements = new Map();
const $ = id => {
  if(!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const document = {createElement: tag => new Element(tag)};
let confirmAnswer = false, posts = [], toastText = '';
const now = '2026-09-19T12:00:00Z';
const initial = {
  available:true, active:false, persistence_error:'',
  current:{id:'ap-1', installed_at:now, last_observed_at:now,
           runtime_h:12.345, observation_gap_reason:''},
  history:[{id:'old-<script>', installed_at:'2026-01-01T00:00:00Z',
            replaced_at:'2026-02-01T00:00:00Z', runtime_h:44.5}],
};
const replacement = {
  available:true, active:null, persistence_error:'',
  current:{id:'ap-2', installed_at:now, last_observed_at:null,
           runtime_h:0, observation_gap_reason:'waiting for observation'},
  history:initial.history.concat([{id:'ap-1', installed_at:now,
                                   replaced_at:now, runtime_h:12.345}]),
};
const card = createApertureCard({$, document,
  post:async (url, body) => { posts.push({url, body}); return {aperture_lifetime:replacement}; },
  toast:text => { toastText = text; }, confirmImpl:() => confirmAnswer});
card.mount(); card.render(initial);
assert.equal($('apertureHours').textContent, '12.35', 'hours are compact and readable');
assert.equal($('apertureHistory').children.length, 1, 'history renders one record');
assert.match($('apertureHistory').children[0].children[2].textContent, /44\.50 h/);
assert.doesNotMatch($('apertureHistory').children[0].textContent, /script/,
  'identities are never interpolated as markup');

await $('newApertureBtn').onclick();
assert.equal(posts.length, 0, 'cancel changes nothing');
confirmAnswer = true;
await $('newApertureBtn').onclick();
assert.deepEqual(posts[0], {url:'/api/aperture/replaced',
  body:{expected_aperture_id:'ap-1', confirm:true}}, 'confirmation sends identity guard');
assert.equal($('apertureHours').textContent, '0.00', 'successful reset renders new record');
assert.match(toastText, /archived/);

card.render({...replacement, active:true});
assert.equal($('newApertureBtn').disabled, true, 'active timing disables replacement');
assert.ok($('apertureCard').classList.contains('active'));
card.render({available:false, error:'damaged file', history:[]});
assert.equal($('newApertureBtn').disabled, true, 'unavailable state cannot mutate');
assert.equal($('apertureNote').textContent, 'damaged file');
assert.equal(formatApertureHours(NaN), '—');
card.dispose();
assert.equal($('newApertureBtn').onclick, null, 'dispose removes action handler');
console.log('PASS aperture card display, history, confirmation, and guarded replacement');
