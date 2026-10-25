"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/catalog.js"), "utf8");

function fixture() {
  const listeners = {};
  const attributes = {};
  const cardResults = {
    setAttribute(name, value) { attributes[name] = value; },
    replaceChildren() {},
    addEventListener() {},
  };
  const spinner = { hidden: true };
  const status = { textContent: "" };
  const search = { addEventListener(type, callback) { listeners.input = callback; } };
  const form = {
    dataset: { endpoint: "/api/catalog/movies" },
    elements: { page: { value: 1 } },
    addEventListener(type, callback) { listeners[type] = callback; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const elements = {
    "#filters": form, "#error": { hidden: true }, "#card-results": cardResults,
    "#search-spinner": spinner, "#search-status": status,
    "#clear-filters": { href: "http://local/movies" },
  };
  let respond;
  const context = {
    URL, URLSearchParams,
    FormData: class { *[Symbol.iterator]() { yield ["q", "harry"]; } },
    AbortController,
    DOMParser: class { parseFromString() { return { body: { childNodes: [] } }; } },
    setTimeout: callback => { listeners.timer = callback; return 1; },
    clearTimeout() {},
    location: { pathname: "/movies" },
    history: { replaceState() {} },
    fetch: () => new Promise(resolve => { respond = resolve; }),
    document: {
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => (selector.includes('type="search"') ? [search] : []),
      addEventListener() {},
    },
  };
  runInNewContext(script, context);
  return {
    attributes, spinner, status, listeners,
    respond(total) { respond({ ok: true, json: async () => ({ html: "", page: 1, total }) }); },
  };
}

test("typing shows the search spinner until results arrive, then announces the count", async () => {
  const ui = fixture();
  ui.listeners.input();
  assert.equal(ui.spinner.hidden, false);
  assert.equal(ui.attributes["aria-busy"], "true");
  assert.equal(ui.status.textContent, "Buscando…");
  const pending = ui.listeners.timer();
  assert.equal(ui.spinner.hidden, false);
  ui.respond(27);
  await pending;
  assert.equal(ui.spinner.hidden, true);
  assert.equal(ui.attributes["aria-busy"], "false");
  assert.equal(ui.status.textContent, "27 resultados");
});
