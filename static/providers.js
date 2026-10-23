"use strict";

(() => {
  const panel = document.querySelector("#provider-filter");
  if (!panel) return;
  const options = [...panel.querySelectorAll(".provider-option")];
  const services = JSON.parse(panel.dataset.subscriptions);

  panel.querySelector("#my-services-toggle").addEventListener("change", () => {
    panel.querySelector("#my-services-warning").hidden =
      panel.querySelector("#my-services-toggle").value !== "1" || services.length > 0;
  });
  panel.querySelector("#provider-search").addEventListener("input", event => {
    const query = event.target.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const option of options) {
      option.hidden = !option.dataset.providerName.toLocaleLowerCase().includes(query);
      if (!option.hidden) visible += 1;
    }
    panel.querySelector("#provider-search-empty").hidden = visible > 0;
  });
  function showLogoError(image) {
    image.hidden = true;
    const fallback = image.nextElementSibling;
    fallback.textContent = `${image.alt} (icono no disponible)`;
    fallback.hidden = false;
  }
  panel.addEventListener("error", event => {
    if (event.target.matches?.(".provider-choice img")) showLogoError(event.target);
  }, true);
  for (const image of panel.querySelectorAll(".provider-choice img")) {
    if (image.complete && image.naturalWidth === 0) showLogoError(image);
  }
})();
