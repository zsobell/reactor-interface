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
    querySelectorAll() {return [];}, querySelector() {return null;},
    appendChild() {}, setAttribute() {}, getBoundingClientRect() {return {left: 0};},
  };
}
for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  elements.set(match[3], element(match[1], match[2]));
}
globalThis.document = {
  getElementById: id => elements.get(id) || null,
  querySelectorAll: () => [], addEventListener() {},
  createElement: tag => element(tag), documentElement: element('html'),
};
globalThis.window = {addEventListener() {}, devicePixelRatio: 1};
globalThis.location = {protocol: 'https:', host: 'reactor.test'};
const stored = new Map();
globalThis.localStorage = {getItem: k => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v)};
const requested = [];
globalThis.fetch = async url => {
  requested.push(url);
  return {ok: true, json: async () => ({params: {}, samples: [], events: []})};
};
let connected = null;
globalThis.WebSocket = class {constructor(url) {connected = url;}};
await import('../../reactor/server/static/control.js');
await new Promise(resolve => setTimeout(resolve, 20));
assert.equal(connected, 'wss://reactor.test/ws', 'bootstrap reaches secure telemetry connection');
assert.ok(requested.includes('/api/run_params'), 'server settings loaded');
assert.ok(requested.includes('/api/events'), 'event history loaded');
assert.match(elements.get('smoothHint').textContent, /off/, 'page reads extracted chart settings during bootstrap');
console.log('PASS control-page module bootstrap, shared settings, chart integration and HTTPS WebSocket');
