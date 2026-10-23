"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/watched.js"), "utf8");

function setup(response, banned = false, confirmed = true) {
  let submit;
  const events = [];
  const classes = new Set(["btn-outline-secondary"]);
  const attributes = {};
  const confirmations = [];
  const button = {
    disabled: false,
    classList: { toggle(name, active) { active ? classes.add(name) : classes.delete(name); } },
    setAttribute(name, value) { attributes[name] = value; },
  };
  const errorBox = { hidden: true, textContent: "" };
  const form = {
    action: `/catalog/movies/10/${banned ? "banned" : "watched"}`,
    elements: { watched: { value: "1" }, banned: { value: "1" } },
    matches: selector => selector === (banned ? ".banned-form" : ".watched-form"),
    querySelector: selector => selector === "button" ? button : errorBox,
  };
  runInNewContext(script, {
    document: {
      addEventListener(name, handler) { if (name === "submit") submit = handler; },
      dispatchEvent(event) { events.push(event.type); },
      querySelector() { return {}; },
    },
    FormData: class { constructor(value) { this.form = value; } },
    CustomEvent: class { constructor(type) { this.type = type; } },
    fetch: response,
    window: { confirm(message) { confirmations.push(message); return confirmed; } },
    DOMParser: class { parseFromString() { return { querySelector() { return null; } }; } },
  });
  return { form, button, errorBox, events, classes, attributes, confirmations,
    submit: () => submit({ target: form, preventDefault() {} }) };
}

test("mark and unmark updates the button and notifies the live catalog", async () => {
  let watched = true;
  const state = setup(async (url, options) => {
    assert.equal(url, "/catalog/movies/10/watched");
    assert.equal(options.method, "POST");
    assert.equal(options.headers.Accept, "application/json");
    return { ok: true, json: async () => ({ watched }) };
  });
  await state.submit();
  assert.equal(state.form.elements.watched.value, "0");
  assert.equal(state.button.textContent, "Vista");
  assert.equal(state.attributes["aria-pressed"], "true");
  assert.equal(state.classes.has("btn-success"), true);
  assert.deepEqual(state.events, ["movie-state-changed"]);
  watched = false;
  await state.submit();
  assert.equal(state.form.elements.watched.value, "1");
  assert.equal(state.button.textContent, "Ya la vi");
  assert.equal(state.attributes["aria-pressed"], "false");
  assert.equal(state.classes.has("btn-outline-secondary"), true);
  assert.equal(state.button.disabled, false);
});

test("network and HTTP errors remain visible without falsely changing watched state", async () => {
  for (const response of [
    async () => { throw new Error("Sin conexión"); },
    async () => ({ ok: false, status: 400, text: async () => "Sesión caducada" }),
  ]) {
    const state = setup(response);
    await state.submit();
    assert.equal(state.errorBox.hidden, false);
    assert.match(state.errorBox.textContent, /No se pudo guardar el estado/);
    assert.equal(state.form.elements.watched.value, "1");
    assert.equal(state.events.length, 0);
    assert.equal(state.button.disabled, false);
  }
});

test("a second submit cannot write while the first request is pending", async () => {
  let finish;
  let requests = 0;
  const state = setup(() => {
    requests++;
    return new Promise(resolve => { finish = resolve; });
  });

  const pending = state.submit();
  assert.equal(state.button.disabled, true);
  await state.submit();
  assert.equal(requests, 1);
  finish({ ok: true, json: async () => ({ watched: true }) });
  await pending;
  assert.equal(state.button.disabled, false);
});

test("ban and restore buttons update and notify the catalog without a false watched mark", async () => {
  let banned = true;
  const state = setup(async () => ({ ok: true, json: async () => ({ banned }) }), true);
  await state.submit();
  assert.equal(state.button.textContent, "Desbloquear");
  assert.equal(state.form.elements.banned.value, "0");
  assert.equal(state.classes.has("btn-danger"), true);
  assert.equal(state.form.elements.watched.value, "1");
  assert.deepEqual(state.events, ["movie-state-changed"]);
  banned = false;
  await state.submit();
  assert.equal(state.button.textContent, "No me interesa");
  assert.equal(state.form.elements.banned.value, "1");
  assert.equal(state.classes.has("btn-danger"), false);
  assert.equal(state.confirmations.length, 1);
});

test("cancelling a ban confirmation sends no request and changes no state", async () => {
  let requests = 0;
  const state = setup(async () => { requests++; }, true, false);
  await state.submit();
  assert.equal(state.confirmations.length, 1);
  assert.match(state.confirmations[0], /ocultar esta película/);
  assert.equal(requests, 0);
  assert.equal(state.form.elements.banned.value, "1");
  assert.equal(state.button.disabled, false);
  assert.equal(state.events.length, 0);
});

test("watched changes do not request a ban confirmation", async () => {
  const state = setup(async () => ({ ok: true, json: async () => ({ watched: true }) }));
  await state.submit();
  assert.equal(state.confirmations.length, 0);
});
