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
    document: { querySelector: () => filters },
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
