"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");
const { withSpa } = require("./spa-stub");

const script = readFileSync(join(__dirname, "../static/catalog.js"), "utf8");

function runCatalog(form) {
  const cardResults = { addEventListener() {}, setAttribute() {}, hasAttribute() { return false; } };
  form.addEventListener = () => {};
  const root = {
    querySelector: selector => (selector === "#filters" ? form : selector === "#card-results" ? cardResults : null),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  runInNewContext(script, withSpa({ document: root }));
}

test("toggle hides, disables and clears children's categories, then restores availability", () => {
  const toggle = { checked: false, addEventListener() {} };
  const input = { checked: true, disabled: false, labels: [{ hidden: false }] };
  const form = {
    querySelector(selector) { return selector === "#children-toggle" ? toggle : null; },
    querySelectorAll(selector) { return selector === "[data-children-category]" ? [input] : []; },
  };
  runCatalog(form);
  assert.equal(input.checked, false);
  assert.equal(input.disabled, true);
  assert.equal(input.labels[0].hidden, true);
  toggle.checked = true;
  runCatalog(form);
  assert.equal(input.disabled, false);
  assert.equal(input.labels[0].hidden, false);
  assert.equal(input.checked, false);
});

test("dedicated kids listing retains its category selection without a toggle", () => {
  const input = { checked: true, disabled: false, labels: [{ hidden: false }] };
  const form = {
    querySelector() { return null; },
    querySelectorAll(selector) { return selector === "[data-children-category]" ? [input] : []; },
  };
  runCatalog(form);
  assert.equal(input.checked, true);
  assert.equal(input.disabled, false);
  assert.equal(input.labels[0].hidden, false);
});
