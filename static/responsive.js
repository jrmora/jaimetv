"use strict";

(() => {
  const filters = document.querySelector("#catalog-filters");
  if (!filters) return;
  const compact = matchMedia("(max-width: 1199.98px)");
  const update = () => { filters.open = !compact.matches; };
  compact.addEventListener("change", update);
  update();
})();
