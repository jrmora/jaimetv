"use strict";

(() => {
  const filters = document.querySelector("#catalog-filters");
  if (!filters) return;
  const toggle = document.querySelector("#filters-toggle");
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
  const update = () => {
    filters.open = !compact.matches;
    if (toggle) {
      toggle.hidden = !compact.matches;
      toggle.setAttribute("aria-expanded", String(filters.open));
    }
  };
  compact.addEventListener("change", update);
  update();
})();
