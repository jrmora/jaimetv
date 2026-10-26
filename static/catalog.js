"use strict";

const form = document.querySelector("#filters");
const errorBox = document.querySelector("#error");
const cardResults = document.querySelector("#card-results");
const searchSpinner = document.querySelector("#search-spinner");
const searchStatus = document.querySelector("#search-status");
const genreControls = document.querySelector("#catalog-genres");
let controller;
let timer;
let version = 0;

function updateChildrenCategories() {
  const toggle = form.querySelector("#children-toggle");
  if (!toggle) return;
  for (const input of form.querySelectorAll("[data-children-category]")) {
    input.disabled = !toggle.checked;
    if (input.disabled) input.checked = false;
    input.labels.forEach(label => { label.hidden = input.disabled; });
  }
}

function setBusy(busy, message = "") {
  cardResults.setAttribute("aria-busy", String(busy));
  if (searchSpinner) searchSpinner.hidden = !busy;
  if (searchStatus) searchStatus.textContent = busy ? "Buscando…" : message;
}

function parameters() {
  updateChildrenCategories();
  const params = new URLSearchParams(new FormData(form));
  const children = params.getAll("children");
  if (children.length) params.set("children", children.at(-1));
  const services = params.getAll("my_services");
  if (services.length) params.set("my_services", services.at(-1));
  return params;
}

async function load(page = 1, scrollToTop = false) {
  clearTimeout(timer);
  controller?.abort();
  controller = new AbortController();
  const current = ++version;
  form.elements.page.value = page;
  const params = parameters();
  setBusy(true);
  errorBox.hidden = true;
  let message = "";
  try {
    const response = await fetch(`${form.dataset.endpoint}?${params}`, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Error HTTP ${response.status}`);
    if (current !== version) return;
    const content = new DOMParser().parseFromString(data.html, "text/html");
    cardResults.replaceChildren(...content.body.childNodes);
    if (genreControls && typeof data.genres_html === "string") {
      const genres = new DOMParser().parseFromString(data.genres_html, "text/html");
      genreControls.replaceChildren(...genres.body.childNodes);
      params.delete("genre");
      for (const genre of data.selected_genres) params.append("genre", genre);
    }
    form.elements.page.value = data.page;
    params.set("page", data.page);
    const clear = document.querySelector("#clear-filters");
    const clearUrl = new URL(clear.href);
    clearUrl.searchParams.set("children", params.get("children"));
    clear.href = clearUrl;
    history.replaceState(null, "", `${location.pathname}?${params}`);
    message = data.total === 1 ? "1 resultado" : `${data.total} resultados`;
    if (scrollToTop) window.scrollTo({ top: 0, behavior: "instant" });
  } catch (error) {
    if (error.name !== "AbortError" && current === version) {
      errorBox.textContent = `No se pudo actualizar el catálogo. ${error.message}. Vuelve a intentarlo.`;
      errorBox.hidden = false;
    }
  } finally {
    if (current === version) setBusy(false, message);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  load();
});
for (const input of document.querySelectorAll('input[type="search"][name][form="filters"], #filters input[type="search"][name]')) {
  input.addEventListener("input", () => {
    clearTimeout(timer);
    controller?.abort();
    ++version;
    setBusy(true);
    timer = setTimeout(() => load(), 250);
  });
}
for (const select of form.querySelectorAll("select")) {
  select.addEventListener("change", () => load());
}
form.querySelector("#children-toggle")?.addEventListener("change", () => load());
for (const input of form.querySelectorAll('.filter-chips input, .provider-options input, input[name="watched"], input[name="my_services"]')) {
  input.addEventListener("change", () => load());
}
genreControls?.addEventListener("change", (event) => {
  if (event.target.matches('input[name="genre"]')) return load();
});
cardResults.addEventListener("click", (event) => {
  const link = event.target.closest(".card-pagination a");
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  return load(Number(new URL(link.href).searchParams.get("page")), true);
});
document.addEventListener("movie-state-changed", () => load(Number(form.elements.page.value)));
updateChildrenCategories();
