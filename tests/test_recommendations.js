"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { withSpa } = require("./spa-stub");

function setup() {
  const events = {};
  const content = {
    nodes: [],
    replaceChildren(...nodes) { this.nodes = nodes; },
    querySelector() { return this.nodes.length ? {} : null; },
    querySelectorAll() { return []; },
  };
  const error = { hidden: true };
  const retry = { addEventListener(name, callback) { this.click = callback; } };
  const elements = {
    "#recommendations-content": content,
    "#recommendations-error": error,
    "#recommendations-retry": retry,
  };
  const section = {
    hidden: true, dataset: { endpoint: "/recommendations" },
    querySelector: selector => elements[selector],
    setAttribute(name, value) { this[name] = value; },
  };
  let result = { html: "cards" };
  let ok = true;
  let requests = 0;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/recommendations.js"), "utf8"), withSpa({
    document: {
      querySelector: () => section,
      addEventListener(name, callback) { events[name] = callback; },
    },
    AbortController,
    DOMParser: class {
      parseFromString(html) { return { body: { childNodes: html.trim() ? ["card"] : [] } }; }
    },
    fetch: async () => {
      requests++;
      return { ok, json: async () => result };
    },
  }));
  return {
    section, content, error, retry,
    load: () => events["tmdb-updated"](),
    respond(value, success = true) { result = value; ok = success; },
    requests: () => requests,
  };
}

test("recommendations wait for metadata, display cards and hide empty results", async () => {
  const ui = setup();
  assert.equal(ui.requests(), 0);
  await ui.load();
  assert.equal(ui.section.hidden, false);
  assert.equal(ui.content.nodes.length, 1);
  assert.equal(ui.section["aria-busy"], "false");
  ui.respond({ html: " " });
  await ui.load();
  assert.equal(ui.section.hidden, true);
  assert.equal(ui.content.nodes.length, 0);
});

test("recommendation errors are visible and can be retried independently", async () => {
  const ui = setup();
  ui.respond({ error: "TMDB no disponible" }, false);
  await ui.load();
  assert.equal(ui.error.hidden, false);
  assert.equal(ui.retry.hidden, false);
  assert.match(ui.error.textContent, /TMDB no disponible/);
  ui.respond({ html: "cards" });
  await ui.retry.click();
  assert.equal(ui.error.hidden, true);
  assert.equal(ui.retry.hidden, true);
  assert.equal(ui.section.hidden, false);
});
