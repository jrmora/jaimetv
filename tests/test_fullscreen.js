"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function setup(enabled = true, failure = false) {
  const elements = {};
  for (const id of ["fullscreen-toggle", "fullscreen-enter", "fullscreen-exit", "fullscreen-warning"]) {
    elements[`#${id}`] = {
      hidden: true, style: {}, attributes: {}, handlers: {},
      setAttribute(key, value) { this.attributes[key] = value; },
      removeAttribute(key) { delete this.attributes[key]; },
      addEventListener(event, handler) { this.handlers[event] = handler; },
    };
  }
  const handlers = {};
  const document = {
    fullscreenEnabled: enabled, fullscreenElement: null,
    querySelector: selector => elements[selector],
    addEventListener(event, handler) { handlers[event] = handler; },
    documentElement: {
      async requestFullscreen() {
        if (failure) throw new Error("Permission denied");
        document.fullscreenElement = this;
        handlers.fullscreenchange();
      },
    },
    async exitFullscreen() {
      document.fullscreenElement = null;
      handlers.fullscreenchange();
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/fullscreen.js"), "utf8"), { document });
  return { elements, document, handlers, button: elements["#fullscreen-toggle"] };
}

test("icon button enters, exits and follows Escape fullscreen changes", async () => {
  const page = setup();
  assert.equal(page.button.hidden, false);
  await page.button.handlers.click();
  assert.equal(page.button.attributes["aria-pressed"], "true");
  assert.equal(page.elements["#fullscreen-enter"].style.display, "none");
  await page.button.handlers.click();
  assert.equal(page.button.attributes["aria-pressed"], "false");
  await page.button.handlers.click();
  page.document.fullscreenElement = null;
  page.handlers.fullscreenchange();
  assert.equal(page.button.attributes["aria-label"], "Pantalla completa");
});

test("unsupported browsers keep the button hidden", () => {
  assert.equal(setup(false).button.hidden, true);
});

test("fullscreen rejection is visible and leaves the button usable", async () => {
  const page = setup(true, true);
  await page.button.handlers.click();
  assert.equal(page.elements["#fullscreen-warning"].hidden, false);
  assert.match(page.elements["#fullscreen-warning"].textContent, /Permission denied/);
  assert.equal(page.button.disabled, false);
  assert.equal(page.button.attributes["aria-pressed"], "false");
});
