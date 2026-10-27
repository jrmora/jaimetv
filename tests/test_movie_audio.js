"use strict";

const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function setup(initial = {}, responses = []) {
  const calls = [];
  const timers = new Map();
  let nextTimer = 0;
  const select = {addEventListener(name, fn) { this.change = fn; }};
  const makeCopy = id => {
    const state = {metadata: null, running: false, error: null, stale: false, ...initial};
    const button = {
      addEventListener(name, fn) { this.click = fn; },
      setAttribute(name, value) { this[name] = value; },
    };
    const languages = {
      children: [], replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
    };
    const error = {};
    return {
      dataset: {audioCopy: id, endpoint: `/api/movies/${id}/audio`, state: JSON.stringify(state), unavailable: "false"},
      button, languages, error,
      querySelector(selector) {
        return {"[data-audio-check]": button, "[data-audio-languages]": languages, "[data-audio-error]": error}[selector];
      },
    };
  };
  const copies = [makeCopy("10"), makeCopy("11")];
  const root = {dataset: {csrf: "token"}, querySelectorAll: () => copies};
  const context = {
    document: {
      getElementById: id => id === "movie-audio" ? root : select,
      createElement: () => ({}),
    },
    URLSearchParams,
    AbortSignal: {timeout: () => "signal"},
    setTimeout: fn => { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      calls.push({url, options});
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return {
        ok: true, status: 200, headers: {get: () => "application/json"},
        ...response, json: async () => response.body,
      };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/movie-audio.js"), "utf8"), context);
  return {
    copies, calls, select, timers,
    async tick() {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id);
      await fn();
    },
  };
}

test("no inspection or polling on normal page load; version selection is local", () => {
  const page = setup();
  assert.equal(page.calls.length, 0);
  assert.equal(page.timers.size, 0);
  page.select.change({target: {value: "11"}});
  assert.equal(page.copies[0].hidden, true);
  assert.equal(page.copies[1].hidden, false);
  assert.equal(page.calls.length, 0);
});

test("manual click uses CSRF, polls and shows saved compact codes, preserving unknown tracks", async () => {
  const page = setup({}, [
    {status: 202, body: {running: true}},
    {body: {metadata: {languages: ["ES", "EN"], unknown: 1, tracks: 3}, running: false}},
  ]);
  const copy = page.copies[0];
  await copy.button.click();
  assert.equal(page.calls[0].options.method, "POST");
  assert.equal(page.calls[0].options.body.get("csrf"), "token");
  assert.equal(copy.button.disabled, true);
  await page.tick();
  assert.equal(page.calls[1].options.method, "GET");
  assert.deepEqual(copy.languages.children.map(child => child.textContent), ["ES", "EN", "?"]);
  assert.equal(copy.button.disabled, false);
  assert.equal(copy.button.textContent, "↻");
  assert.equal(page.timers.size, 0);
});

test("a failed refresh preserves previous languages and displays the error", async () => {
  const page = setup({metadata: {languages: ["ES"], unknown: 0, tracks: 1}}, [
    {ok: false, status: 409, body: {error: "Ya hay una comprobación en curso."}},
  ]);
  const copy = page.copies[0];
  await copy.button.click();
  assert.deepEqual(copy.languages.children.map(child => child.textContent), ["ES"]);
  assert.equal(copy.error.hidden, false);
  assert.match(copy.error.textContent, /en curso/);
  assert.equal(copy.button.disabled, false);
});

test("reload during a running job resumes GET polling without another POST", async () => {
  const page = setup({running: true}, [
    {body: {running: false, error: "Falta ffprobe.", metadata: null}},
    {body: {running: false, error: null, metadata: null}},
  ]);
  await page.tick();
  assert.equal(page.calls[0].options.method, "GET");
  assert.equal(page.copies[0].error.textContent, "Falta ffprobe.");
});

test("network or expired-session polling failures stop and allow recovery", async () => {
  for (const response of [new Error("private network error"), {ok: false, status: 401}]) {
    const page = setup({running: true}, [response]);
    await page.tick();
    assert.equal(page.copies[0].button.disabled, false);
    assert.equal(page.copies[0].error.hidden, false);
    assert.doesNotMatch(page.copies[0].error.textContent, /private network error/);
  }
});
