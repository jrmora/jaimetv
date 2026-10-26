"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/catalog.js"), "utf8");

function fixture(kids = false) {
  const listeners = {};
  const attributes = {};
  const scrolls = [];
  const genreUpdates = [];
  const urls = [];
  const kidsTab = { href: "http://local/kids/series" };
  const cardResults = {
    setAttribute(name, value) { attributes[name] = value; },
    replaceChildren() {},
    addEventListener(type, callback) { listeners[type] = callback; },
  };
  const spinner = { hidden: true };
  const status = { textContent: "" };
  const search = { addEventListener(type, callback) { listeners.input = callback; } };
  const genres = {
    addEventListener(type, callback) { listeners.genreChange = callback; },
    replaceChildren(...children) { genreUpdates.push(children); },
  };
  const form = {
    dataset: { endpoint: "/api/catalog/movies" },
    elements: { page: { value: 1 } },
    addEventListener(type, callback) { listeners[type] = callback; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const elements = {
    "#filters": form, "#error": { hidden: true }, "#card-results": cardResults,
    "#search-spinner": spinner, "#search-status": status,
    "#clear-filters": { href: "http://local/movies" },
    "#catalog-genres": genres,
    "#kids-categories": kids ? {
      addEventListener(type, callback) { listeners.kidsChange = callback; },
    } : null,
  };
  let respond;
  const context = {
    URL, URLSearchParams,
    FormData: class {
      *[Symbol.iterator]() {
        yield ["q", "harry"];
        yield ["genre", "Old"];
        if (kids) {
          yield ["children", "only"];
          yield ["kids_category", "anime"];
        }
      }
    },
    AbortController,
    DOMParser: class { parseFromString(html) { return { body: { childNodes: [html] } }; } },
    setTimeout: callback => { listeners.timer = callback; return 1; },
    clearTimeout() {},
    location: { pathname: "/movies" },
    history: { replaceState(state, title, url) { urls.push(url); } },
    window: { scrollTo(options) { scrolls.push(options); } },
    fetch: () => new Promise(resolve => { respond = resolve; }),
    document: {
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => {
        if (selector.includes('type="search"')) return [search];
        return kids && selector === "#kids-tabs a" ? [kidsTab] : [];
      },
      addEventListener() {},
    },
  };
  runInNewContext(script, context);
  return {
    attributes, spinner, status, listeners, scrolls, genreUpdates, urls, kidsTab,
    respond(total, extra = {}) { respond({ ok: true, json: async () => ({ html: "", page: 1, total, ...extra }) }); },
    fail() { respond({ ok: false, json: async () => ({ error: "Error de prueba" }) }); },
  };
}

test("typing shows the search spinner until results arrive, then announces the count", async () => {
  const ui = fixture();
  ui.listeners.input();
  assert.equal(ui.spinner.hidden, false);
  assert.equal(ui.attributes["aria-busy"], "true");
  assert.equal(ui.status.textContent, "Buscando…");
  const pending = ui.listeners.timer();
  assert.equal(ui.spinner.hidden, false);
  ui.respond(27);
  await pending;
  assert.equal(ui.spinner.hidden, true);
  assert.equal(ui.attributes["aria-busy"], "false");
  assert.equal(ui.status.textContent, "27 resultados");
  assert.equal(ui.scrolls.length, 0);
});

test("pagination scrolls to the top after the new results arrive", async () => {
  const ui = fixture();
  let prevented = false;
  const pending = ui.listeners.click({
    target: { closest: () => ({ href: "http://local/movies?page=2" }) },
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.equal(ui.scrolls.length, 0);
  ui.respond(100);
  await pending;
  assert.equal(ui.scrolls.length, 1);
  assert.equal(ui.scrolls[0].top, 0);
});

test("failed pagination leaves the scroll position unchanged", async () => {
  const ui = fixture();
  const pending = ui.listeners.click({
    target: { closest: () => ({ href: "http://local/movies?page=2" }) },
    preventDefault() {},
  });
  ui.fail();
  await pending;
  assert.equal(ui.scrolls.length, 0);
});

test("genre controls outside the filters form refresh results without scrolling", async () => {
  const ui = fixture();
  const pending = ui.listeners.genreChange({ target: { matches: () => true } });
  assert.equal(ui.spinner.hidden, false);
  ui.respond(5);
  await pending;
  assert.equal(ui.status.textContent, "5 resultados");
  assert.equal(ui.scrolls.length, 0);
});

test("updated genre choices replace old buttons and clear removed selections from the URL", async () => {
  const ui = fixture();
  for (const selected of [["Drama"], []]) {
    const pending = ui.listeners.genreChange({ target: { matches: () => true } });
    ui.respond(5, { genres_html: "<fieldset>Updated</fieldset>", selected_genres: selected });
    await pending;
    assert.equal(ui.genreUpdates.at(-1)[0], "<fieldset>Updated</fieldset>");
    const url = new URL(ui.urls.at(-1), "http://local");
    assert.deepEqual(url.searchParams.getAll("genre"), selected);
  }
  assert.equal(ui.genreUpdates.length, 2, "delegated genre events work after replacing the controls");
});

test("kids category chips refresh results and preserve their selection with pagination", async () => {
  const ui = fixture(true);
  const pending = ui.listeners.kidsChange({ target: { matches: () => true } });
  assert.equal(ui.spinner.hidden, false);
  ui.respond(60);
  await pending;
  let url = new URL(ui.urls.at(-1), "http://local");
  assert.equal(url.searchParams.get("kids_category"), "anime");
  assert.equal(url.searchParams.get("children"), "only");
  assert.equal(new URL(ui.kidsTab.href).searchParams.get("kids_category"), "anime");
  assert.equal(ui.scrolls.length, 0);
  const next = ui.listeners.click({
    target: { closest: () => ({ href: "http://local/kids/movies?page=2" }) },
    preventDefault() {},
  });
  ui.respond(60, { page: 2 });
  await next;
  url = new URL(ui.urls.at(-1), "http://local");
  assert.equal(url.searchParams.get("kids_category"), "anime");
  assert.equal(url.searchParams.get("page"), "2");
  assert.equal(ui.scrolls.length, 1);
});
