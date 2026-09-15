import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const html = await readFile(new URL('../../reactor/server/static/index.html', import.meta.url), 'utf8');
const elements = new Map();
function element(tag, attrs = '') {
  return {
    tagName: tag.toUpperCase(), value: /\bvalue="([^"]*)"/.exec(attrs)?.[1] || '',
    type: /\btype="([^"]*)"/.exec(attrs)?.[1] || '', checked: /\bchecked\b/.test(attrs),
    textContent: '', innerHTML: '', children: [], dataset: {}, events: {},
    clientWidth: 0, clientHeight: 0, classList: {toggle() {}, add() {}, remove() {}},
    style: {setProperty() {}},
    addEventListener(name, fn) {this.events[name] = fn;},
    removeEventListener(name) {delete this.events[name];},
    querySelectorAll() {return [];}, querySelector() {return null;},
    appendChild() {}, setAttribute() {}, getBoundingClientRect() {return {left: 0};},
  };
}
for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  elements.set(match[3], element(match[1], match[2]));
}
globalThis.document = {
  getElementById: id => elements.get(id) || null,
  querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  createElement: tag => element(tag), documentElement: element('html'),
};
const windowEvents = {};
globalThis.window = {addEventListener(name, fn) {windowEvents[name] = fn;}, devicePixelRatio: 1};
globalThis.location = {protocol: 'https:', host: 'reactor.test'};
const stored = new Map();
globalThis.localStorage = {getItem: k => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v)};
const requested = [];
globalThis.fetch = async url => {
  requested.push(url);
  return {ok: true, json: async () => ({params: {}, samples: [],
    events: url === '/api/events' ? [{t: 1700000000, kind: 'startup', message: 'seeded startup event'}] : [],
    total_s: 2505})};
};
const sockets = [];
globalThis.WebSocket = class {
  constructor(url) {this.url = url; sockets.push(this);}
  close() {this.closed = true;}
};
await import('../../reactor/server/static/control.js');
await new Promise(resolve => setTimeout(resolve, 20));
assert.equal(sockets[0].url, 'wss://reactor.test/ws', 'bootstrap reaches secure telemetry connection');
assert.ok(requested.includes('/api/run_params'), 'server settings loaded');
assert.ok(requested.includes('/api/events'), 'event history loaded');
assert.match(elements.get('events').innerHTML, /seeded startup event/,
  'seeded event history renders before a new telemetry event arrives');
assert.match(elements.get('smoothHint').textContent, /off/, 'page reads extracted chart settings during bootstrap');
const formHandler = elements.get('aldParams').events.input;
windowEvents.pagehide({persisted:true});
assert.equal(sockets[0].closed, true, 'cached navigation suspends telemetry');
windowEvents.pageshow({persisted:true});
assert.equal(sockets.length, 2, 'back-forward cache restore reconnects telemetry');
assert.equal(elements.get('aldParams').events.input, formHandler,
  'cached navigation preserves the mounted form handlers');
windowEvents.pagehide({persisted:false});
windowEvents.pageshow({persisted:true});
assert.equal(sockets.length, 2, 'permanently disposed page cannot reconnect');
console.log('PASS control bootstrap, settings, chart integration and cached-page reconnect');
