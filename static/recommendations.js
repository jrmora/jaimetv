"use strict";

(() => {
  const section = document.querySelector("#recommendations");
  if (!section) return;
  const content = section.querySelector("#recommendations-content");
  const errorBox = section.querySelector("#recommendations-error");
  const retry = section.querySelector("#recommendations-retry");
  let controller;

  async function load() {
    controller?.abort();
    const request = new AbortController();
    controller = request;
    section.setAttribute("aria-busy", "true");
    errorBox.hidden = true;
    retry.hidden = true;
    content.replaceChildren();
    section.hidden = true;
    try {
      const response = await fetch(section.dataset.endpoint, { signal: request.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      if (request.signal.aborted) return;
      const html = new DOMParser().parseFromString(result.html, "text/html");
      content.replaceChildren(...html.body.childNodes);
      section.hidden = !content.querySelector("article");
      for (const image of content.querySelectorAll("img")) {
        const failed = () => {
          image.hidden = true;
          image.nextElementSibling.hidden = false;
        };
        image.addEventListener("error", failed);
        if (image.complete && image.naturalWidth === 0) failed();
      }
    } catch (error) {
      if (request.signal.aborted) return;
      section.hidden = false;
      errorBox.textContent = `No se pudieron cargar las recomendaciones. ${error.message}`;
      errorBox.hidden = false;
      retry.hidden = false;
    } finally {
      if (controller === request) section.setAttribute("aria-busy", "false");
    }
  }

  document.addEventListener("tmdb-updated", load);
  document.addEventListener("movie-state-changed", load);
  retry.addEventListener("click", load);
})();
