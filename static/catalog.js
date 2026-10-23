"use strict";

const form = document.querySelector("#filters");
const errorBox = document.querySelector("#error");
const cardResults = document.querySelector("#card-results");
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

function parameters() {
  updateChildrenCategories();
  const params = new URLSearchParams(new FormData(form));
  const children = params.getAll("children");
  if (children.length) params.set("children", children.at(-1));
  const services = params.getAll("my_services");
  if (services.length) params.set("my_services", services.at(-1));
  return params;
}

async function load(page = 1) {
  clearTimeout(timer);
  controller?.abort();
  controller = new AbortController();
  const current = ++version;
  form.elements.page.value = page;
  const params = parameters();
  cardResults.setAttribute("aria-busy", "true");
  errorBox.hidden = true;
  try {
    const response = await fetch(`${form.dataset.endpoint}?${params}`, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Error HTTP ${response.status}`);
    if (current !== version) return;
    const content = new DOMParser().parseFromString(data.html, "text/html");
    cardResults.replaceChildren(...content.body.childNodes);
    form.elements.page.value = data.page;
    params.set("page", data.page);
    const clear = document.querySelector("#clear-filters");
    const clearUrl = new URL(clear.href);
    clearUrl.searchParams.set("children", params.get("children"));
    clear.href = clearUrl;
    history.replaceState(null, "", `${location.pathname}?${params}`);
  } catch (error) {
    if (error.name !== "AbortError" && current === version) {
      errorBox.textContent = `No se pudo actualizar el catálogo. ${error.message}. Vuelve a intentarlo.`;
      errorBox.hidden = false;
    }
  } finally {
    if (current === version) cardResults.setAttribute("aria-busy", "false");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  load();
});
for (const input of form.querySelectorAll('input[type="search"][name]')) {
  input.addEventListener("input", () => {
    clearTimeout(timer);
    controller?.abort();
    ++version;
    cardResults.setAttribute("aria-busy", "false");
    timer = setTimeout(() => load(), 250);
  });
}
for (const select of form.querySelectorAll("select")) {
  select.addEventListener("change", () => load());
}
form.querySelector("#children-toggle")?.addEventListener("change", () => load());
for (const input of form.querySelectorAll('.filter-chips input, .provider-options input, input[name="watched"]')) {
  input.addEventListener("change", () => load());
}
cardResults.addEventListener("click", (event) => {
  const link = event.target.closest(".card-pagination a");
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  load(Number(new URL(link.href).searchParams.get("page")));
});
document.addEventListener("movie-state-changed", () => load(Number(form.elements.page.value)));
updateChildrenCategories();
