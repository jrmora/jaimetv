"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("admin submits selected catalog, limit, directory and cancellation with CSRF", async () => {
  const elements = {};
  for (const id of ["tmdb-batch-status", "tmdb-batch-controls", "tmdb-batch-start",
    "tmdb-directory", "tmdb-batch-cancel", "tmdb-kind", "tmdb-batch-limit"]) {
    elements[`#${id}`] = {
      handlers: {}, classList: { toggle() {} }, setAttribute() {},
      addEventListener(event, handler) { this.handlers[event] = handler; },
      reportValidity() { return true; },
    };
  }
  elements["#tmdb-kind"].value = "series";
  elements["#tmdb-batch-limit"].value = "25";
  const posts = [];
  let state = { status: "idle" };
  const timers = [];
  let reloads = 0;
  const panel = {
    dataset: { batchEndpoint: "/api/tmdb/batch", csrf: "csrf-token" },
    querySelector: selector => elements[selector],
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/admin.js"), "utf8"), {
    document: { querySelector: () => panel }, URLSearchParams,
    fetch: async (endpoint, options) => {
      if (options.method === "POST") posts.push(options.body);
      return { ok: true, json: async () => state };
    },
    clearTimeout() {}, setTimeout(callback) { timers.push(callback); },
    location: { reload() { reloads++; } },
  });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  elements["#tmdb-batch-controls"].handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(posts[0].get("kind"), "series");
  assert.equal(posts[0].get("limit"), "25");
  assert.equal(posts[0].get("csrf"), "csrf-token");
  await elements["#tmdb-directory"].handlers.click();
  await flush();
  assert.equal(posts[1].get("action"), "directory");
  await elements["#tmdb-batch-cancel"].handlers.click();
  assert.equal(posts[2].get("action"), "cancel");
  state = { status: "running", phase: "platforms" };
  await elements["#tmdb-directory"].handlers.click();
  await flush();
  state = { status: "completed" };
  await timers.shift()();
  timers.shift()();
  assert.equal(reloads, 1, "completion refreshes the persisted update dates");
});
