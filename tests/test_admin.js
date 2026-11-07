"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { withSpa } = require("./spa-stub");

test("admin submits a single batch action and cancellation with CSRF", async () => {
  const elements = {};
  for (const id of ["tmdb-batch-status", "tmdb-batch-controls", "tmdb-batch-start",
    "tmdb-batch-cancel", "tmdb-kind", "tmdb-batch-limit"]) {
    elements[`#${id}`] = {
      handlers: {}, classList: { toggle() {} }, setAttribute() {},
      addEventListener(event, handler) { this.handlers[event] = handler; },
      reportValidity() { return true; },
    };
  }
  elements["#tmdb-kind"].querySelector = () => ({ value: "series" });
  elements["#tmdb-batch-limit"].value = "25";
  const posts = [];
  let state = { status: "idle" };
  const timers = [];
  let reloads = 0;
  const panel = {
    dataset: { batchEndpoint: "/api/tmdb/batch", csrf: "csrf-token" },
    querySelector: selector => elements[selector],
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/admin.js"), "utf8"), withSpa({
    document: { querySelector: () => panel }, URLSearchParams,
    fetch: async (endpoint, options) => {
      if (options.method === "POST") posts.push(options.body);
      return { ok: true, json: async () => state };
    },
    clearTimeout() {}, setTimeout(callback) { timers.push(callback); },
    location: { reload() { reloads++; } },
  }));
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  assert.equal(elements["#tmdb-kind"].disabled, false);
  elements["#tmdb-batch-controls"].handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(posts[0].get("kind"), "series");
  assert.equal(posts[0].get("limit"), "25");
  assert.equal(posts[0].get("csrf"), "csrf-token");
  assert.equal(posts[0].get("action"), "start");
  await elements["#tmdb-batch-cancel"].handlers.click();
  assert.equal(posts[1].get("action"), "cancel");
  state = { status: "running", phase: "platforms" };
  elements["#tmdb-batch-controls"].handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(elements["#tmdb-kind"].disabled, true);
  state = { status: "completed" };
  await timers.shift()();
  assert.equal(elements["#tmdb-kind"].disabled, false);
  timers.shift()();
  assert.equal(reloads, 1, "completion refreshes the persisted update dates");
});
