"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("tablet filters start collapsed and desktop filters reopen on resize", () => {
  const filters = { open: true };
  let change;
  const media = {
    matches: true,
    addEventListener(event, handler) { change = handler; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/responsive.js"), "utf8"), {
    document: { querySelector: selector => selector === "#catalog-filters" ? filters : null },
    matchMedia: () => media,
  });

  assert.equal(filters.open, false);
  filters.open = true;
  assert.equal(filters.open, true, "user can expand the filters");
  media.matches = false;
  change();
  assert.equal(filters.open, true);
  media.matches = true;
  change();
  assert.equal(filters.open, false);
});

test("compact filter button opens and closes the panel with an accessible state", () => {
    const listeners = {};
    const attributes = {};
    const filters = {
      open: true,
      classList: { add() {} },
      addEventListener(event, handler) { listeners[event] = handler; },
    };
    const toggle = {
      hidden: true,
      addEventListener(event, handler) { listeners[event] = handler; },
      setAttribute(name, value) { attributes[name] = value; },
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/responsive.js"), "utf8"), {
      document: { querySelector: selector => selector === "#catalog-filters" ? filters : toggle },
      matchMedia: () => ({ matches: true, addEventListener() {} }),
    });
    assert.equal(toggle.hidden, false);
    assert.equal(filters.open, false);
    assert.equal(attributes["aria-expanded"], "false");
    listeners.click();
    assert.equal(filters.open, true);
    assert.equal(attributes["aria-expanded"], "true");
    listeners.click();
    assert.equal(filters.open, false);
    assert.equal(attributes["aria-expanded"], "false");
  });
