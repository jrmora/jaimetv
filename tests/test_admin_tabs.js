"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { withSpa } = require("./spa-stub");

function fakeElement(extra = {}) {
  return {
    classes: new Set(), attributes: {}, handlers: {}, hidden: false, tabIndex: 0,
    classList: {
      toggle(name, force) {
        if (force) this.owner.classes.add(name); else this.owner.classes.delete(name);
      },
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name]; },
    addEventListener(event, handler) { this.handlers[event] = handler; },
    focus() { this.focused = true; },
    ...extra,
  };
}

test("admin tabs switch panels, keep the hash in sync and support arrow keys", () => {
  const names = ["application", "catalog", "tmdb"];
  const tabs = names.map(name => {
    const tab = fakeElement({ dataset: { tab: name }, attributes: { "aria-controls": `admin-panel-${name}`, "role": "tab" } });
    tab.classList.owner = tab;
    tab.closest = () => tab;
    return tab;
  });
  const panels = Object.fromEntries(names.map(name => {
    const panel = fakeElement();
    panel.classList.owner = panel;
    panel.hidden = name !== "application";
    return [`admin-panel-${name}`, panel];
  }));
  const tablist = fakeElement();
  tablist.querySelectorAll = () => tabs;
  tablist.contains = tab => tabs.includes(tab);
  tabs[0].classes.add("active");
  const replaced = [];
  const context = withSpa({
    document: {},
    location: { hash: "#tab-tmdb" },
    history: { state: null, replaceState(state, title, url) { replaced.push(url); } },
    root: null,
  });
  context.document = {
    querySelector: selector => (selector === "#admin-tabs" ? tablist : null),
  };
  context.window.jaimetvSpa.register = init => init({
    root: { querySelector: selector => (selector === "#admin-tabs" ? tablist : null),
      getElementById: id => panels[id] },
    signal: new AbortController().signal,
  });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/admin-tabs.js"), "utf8"), context);

  assert.equal(panels["admin-panel-tmdb"].hidden, false, "hash selects the matching tab");
  assert.equal(panels["admin-panel-application"].hidden, true);
  assert.equal(tabs[2].attributes["aria-selected"], "true");
  assert.equal(replaced.length, 0, "initial selection does not rewrite the hash");

  tablist.handlers.keydown({ key: "ArrowRight", target: tabs[2], preventDefault() {} });
  assert.equal(panels["admin-panel-application"].hidden, false, "arrow wraps to the first tab");
  assert.equal(tabs[0].focused, true);
  assert.equal(replaced.at(-1), "#tab-application");

  tablist.handlers.click({ target: tabs[1] });
  assert.equal(panels["admin-panel-catalog"].hidden, false);
  assert.equal(panels["admin-panel-application"].hidden, true);
});
