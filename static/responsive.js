"use strict";

  window.jaimetvSpa.register(({ root, signal }) => {
  const filters = root.querySelector("#catalog-filters");
  if (!filters) return;
  const toggle = root.querySelector("#filters-toggle");
  const compact = matchMedia("(max-width: 1199.98px)");
  if (toggle) {
    filters.classList.add("has-filter-toggle");
    toggle.addEventListener("click", () => {
      filters.open = !filters.open;
      toggle.setAttribute("aria-expanded", String(filters.open));
    });
    filters.addEventListener("toggle", () => {
      toggle.setAttribute("aria-expanded", String(filters.open));
    });
  }
  const collapse = () => {
    if (!compact.matches || !filters.open) return;
    filters.open = false;
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  };
  root.addEventListener("change", (event) => {
    if (event.target.closest?.("#filters, #catalog-genres, #kids-categories")) collapse();
  }, { signal });
  root.addEventListener("click", (event) => {
    if (event.target.closest?.("#filters [data-ordering]")) collapse();
  }, { signal });
  const update = () => {
    filters.open = !compact.matches;
    if (toggle) {
      toggle.hidden = !compact.matches;
      toggle.setAttribute("aria-expanded", String(filters.open));
    }
  };
  compact.addEventListener("change", update, { signal });
  update();
  });
