"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/tmdb.js"), "utf8");

async function fixture(image = null, replacements = []) {
  const listeners = {};
  const originals = [{ hidden: false }, { hidden: false }];
  const fields = ["plot", "genre", "premiere", "episode_run_time", "rating", "category_name", "cast", "director"].map(name => ({
    dataset: { field: name },
    row: { hidden: false },
    closest() { return this.row; },
  }));
  let nextFields = replacements;
  const content = {
    textContent: "",
    image: null,
    querySelector(selector) { return selector === ".tmdb-backdrop" ? this.image : null; },
    querySelectorAll() { return nextFields.map(name => ({ dataset: { replacesField: name } })); },
    replaceChildren(...nodes) { this.image = nodes[0] || null; },
    addEventListener() {},
  };
  const refresh = { addEventListener(type, callback) { this.click = callback; } };
  const manualForm = {
    elements: { manual_tmdb: { value: "" } },
    addEventListener(type, callback) { this.submit = callback; },
    reset() { this.elements.manual_tmdb.value = ""; },
  };
  const manualControls = { disabled: false };
  const errorBox = { hidden: true };
  const elements = { "#tmdb-content": content, "#tmdb-error": errorBox, "#tmdb-refresh": refresh,
    "#tmdb-manual": manualForm, "#tmdb-manual-controls": manualControls };
  const section = {
    dataset: { endpoint: "/tmdb", csrf: "test" },
    querySelector(selector) { return elements[selector]; },
    addEventListener(type, callback) { listeners[type] = callback; },
    setAttribute() {},
  };
  let nextImage = image;
  let failure = false;
  const requests = [];
  runInNewContext(script, {
    Event,
    document: {
      dispatchEvent() {},
      querySelector(selector) { return selector === "#tmdb-section" ? section : null; },
      querySelectorAll(selector) { return selector === "[data-original-backdrop]" ? originals : fields; },
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (failure) throw new Error("Network error");
      return { ok: true, json: async () => ({ html: "" }) };
    },
    DOMParser: class {
      parseFromString() { return { body: { childNodes: nextImage ? [nextImage] : [] } }; }
    },
    URLSearchParams,
  });
  await new Promise(setImmediate);
  return {
    originals, content, errorBox, fields, requests, manualForm, manualControls,
    async manual(reference, fail = false) {
      failure = fail;
      manualForm.elements.manual_tmdb.value = reference;
      manualForm.submit({ preventDefault() {} });
      await new Promise(setImmediate);
    },
    emit(type, target) { listeners[type]({ target }); },
    async refresh(image, fail = false, replacements = []) {
      nextImage = image;
      failure = fail;
      nextFields = replacements;
      await refresh.click();
    },
  };
}

function backdrop(complete = false, naturalWidth = 0) {
  return {
    complete, naturalWidth, hidden: false,
    nextElementSibling: { hidden: true },
    matches(selector) { return selector === ".tmdb-backdrop"; },
  };
}

test("originals stay visible until TMDB loads; successful load hides all originals", async () => {
  const image = backdrop();
  const ui = await fixture(image);
  assert.ok(ui.originals.every(item => !item.hidden));
  image.complete = true;
  image.naturalWidth = 780;
  ui.emit("load", image);
  assert.ok(ui.originals.every(item => item.hidden));
  image.naturalWidth = 0;
  ui.emit("error", image);
  assert.ok(ui.originals.every(item => !item.hidden));
});

test("manual TMDB link posts reference and CSRF, preserving input on error", async () => {
  const ui = await fixture();
  const reference = "https://www.themoviedb.org/movie/999";
  await ui.manual(reference, true);
  assert.equal(ui.errorBox.hidden, false);
  assert.equal(ui.manualForm.elements.manual_tmdb.value, reference);
  assert.equal(ui.manualControls.disabled, false);
  await ui.manual(reference);
  const request = ui.requests.at(-1).options;
  assert.equal(request.method, "POST");
  assert.equal(request.body.get("manual_tmdb"), reference);
  assert.equal(request.body.get("csrf"), "test");
  assert.equal(request.body.has("selected"), false);
  assert.equal(ui.manualForm.elements.manual_tmdb.value, "");
  assert.equal(ui.manualControls.disabled, false);
});

test("missing, pending and failed replacements restore the originals", async () => {
  const ui = await fixture(backdrop(true, 780));
  assert.ok(ui.originals.every(item => item.hidden));
  await ui.refresh(null);
  assert.ok(ui.originals.every(item => !item.hidden));
  await ui.refresh(backdrop(true, 780));
  const failed = backdrop();
  await ui.refresh(failed);
  assert.ok(ui.originals.every(item => !item.hidden));
  failed.complete = true;
  ui.emit("error", failed);
  assert.ok(failed.hidden);
  assert.equal(failed.nextElementSibling.hidden, false);
  assert.ok(ui.originals.every(item => !item.hidden));
});

test("cached broken images show an explicit error and retain originals", async () => {
  const image = backdrop(true);
  const ui = await fixture(image);
  assert.ok(image.hidden);
  assert.equal(image.nextElementSibling.hidden, false);
  assert.ok(ui.originals.every(item => !item.hidden));
});

test("API errors preserve a loaded backdrop and surface the request error", async () => {
  const image = backdrop(true, 780);
  const ui = await fixture(image);
  await ui.refresh(null, true);
  assert.equal(ui.content.image, image);
  assert.ok(ui.originals.every(item => item.hidden));
  assert.equal(ui.errorBox.hidden, false);
});

test("only original fields replaced by rendered TMDB values are hidden", async () => {
  const ui = await fixture(null, ["plot", "genre", "premiere", "episode_run_time", "rating", "cast", "director"]);
  for (const field of ui.fields) {
    assert.equal(field.row.hidden, ["plot", "genre", "premiere", "episode_run_time", "rating", "cast", "director"].includes(field.dataset.field));
  }
  await ui.refresh(null, false, ["plot"]);
  assert.equal(ui.fields[0].row.hidden, true);
  assert.ok(ui.fields.slice(1).every(field => !field.row.hidden));
  await ui.refresh(null);
  assert.ok(ui.fields.every(field => !field.row.hidden));
});

test("request failure keeps original fields visible initially and preserves the last successful display", async () => {
  const ui = await fixture();
  await ui.refresh(null, true);
  assert.ok(ui.fields.every(field => !field.row.hidden));
  await ui.refresh(null, false, ["plot"]);
  await ui.refresh(null, true);
  assert.equal(ui.fields[0].row.hidden, true);
  assert.equal(ui.errorBox.hidden, false);
});
