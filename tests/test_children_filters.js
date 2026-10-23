"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/catalog.js"), "utf8");
const setup = script.slice(0, script.indexOf("function parameters()"));

test("toggle hides, disables and clears children's categories, then restores availability", () => {
  const toggle = { checked: false };
  const input = { checked: true, disabled: false, labels: [{ hidden: false }] };
  const form = {
    querySelector() { return toggle; },
    querySelectorAll() { return [input]; },
  };
  const context = { document: { querySelector() { return form; } } };
  runInNewContext(setup + "\nupdateChildrenCategories();", context);
  assert.equal(input.checked, false);
  assert.equal(input.disabled, true);
  assert.equal(input.labels[0].hidden, true);
  toggle.checked = true;
  runInNewContext("updateChildrenCategories();", context);
  assert.equal(input.disabled, false);
  assert.equal(input.labels[0].hidden, false);
  assert.equal(input.checked, false);
});

test("dedicated kids listing retains its category selection without a toggle", () => {
  const input = { checked: true, disabled: false, labels: [{ hidden: false }] };
  const form = {
    querySelector() { return null; },
    querySelectorAll() { return [input]; },
  };
  runInNewContext(setup + "\nupdateChildrenCategories();", {
    document: { querySelector() { return form; } },
  });
  assert.equal(input.checked, true);
  assert.equal(input.disabled, false);
  assert.equal(input.labels[0].hidden, false);
});
