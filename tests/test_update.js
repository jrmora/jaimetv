"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function setup(responses) {
  const calls = [];
  const timers = [];
  let click;
  const button = {
    dataset: { endpoint: "/api/application-update", csrf: "token" },
    addEventListener(name, callback) { click = callback; },
  };
  const status = { hidden: true };
  const context = {
    document: { querySelector: selector => selector === "#application-update" ? button : status },
    URLSearchParams,
    fetch: async (url, options) => {
      calls.push({ url, options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return { ok: true, status: 200, ...response, json: async () => response.body };
    },
    setTimeout: callback => timers.push(callback),
    location: { reload() { context.reloaded = true; } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/update.js"), "utf8"), context);
  return { calls, timers, button, status, context, click: () => click() };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

test("one click submits immediately with CSRF and reloads on completion", async () => {
  const page = setup([
    { body: { status: "idle" } },
    { body: { status: "running" } },
    { body: { status: "running", message: "Installing" } },
    { body: { status: "completed", message: "Updated" } },
  ]);
  await flush();
  await page.click();
  await flush();
  assert.equal(page.calls[1].options.method, "POST");
  assert.equal(page.calls[1].options.body.get("csrf"), "token");
  assert.equal(page.button.disabled, true);
  await page.timers.shift()();
  await flush();
  page.timers.shift()();
  assert.equal(page.context.reloaded, true);
});

test("failure is shown without a success reload", async () => {
  const page = setup([{ body: { status: "failed", message: "Rollback failed" } }]);
  await flush();
  assert.equal(page.status.textContent, "Rollback failed");
  assert.equal(page.button.disabled, false);
  assert.equal(page.context.reloaded, undefined);
});

test("expired session stops polling with an explicit message", async () => {
  const page = setup([{ status: 401, ok: false }]);
  await flush();
  assert.match(page.status.textContent, /sesión caducó/);
  assert.equal(page.timers.length, 0);
});
