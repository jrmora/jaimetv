"use strict";

(() => {
  const panel = document.querySelector("#provider-filter");
  if (!panel) return;
  const services = JSON.parse(panel.dataset.subscriptions);

  for (const radio of panel.querySelectorAll('input[name="my_services"]')) {
    radio.addEventListener("change", () => {
      panel.querySelector("#my-services-warning").hidden =
        radio.value !== "1" || services.length > 0;
    });
  }
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
