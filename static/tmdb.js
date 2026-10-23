"use strict";

(() => {
  const section = document.querySelector("#tmdb-section");
  if (!section) return;
  const content = section.querySelector("#tmdb-content");
  const errorBox = section.querySelector("#tmdb-error");
  const refresh = section.querySelector("#tmdb-refresh");
  const originalBackdrops = document.querySelectorAll("[data-original-backdrop]");
  let busy = false;

  function updateOriginalFields() {
    const replacements = new Set(
      Array.from(content.querySelectorAll("[data-replaces-field]"), row => row.dataset.replacesField)
    );
    for (const field of document.querySelectorAll("[data-field]")) {
      field.closest("tr").hidden = replacements.has(field.dataset.field);
    }
  }

  function updateBackdrop() {
    const image = content.querySelector(".tmdb-backdrop");
    if (image && image.complete && image.naturalWidth === 0) {
      image.hidden = true;
      image.nextElementSibling.hidden = false;
    }
    const loaded = Boolean(image && image.complete && image.naturalWidth > 0 && !image.hidden);
    for (const backdrop of originalBackdrops) backdrop.hidden = loaded;
  }

  section.addEventListener("load", (event) => {
    if (event.target.matches?.(".tmdb-backdrop")) updateBackdrop();
  }, true);
  section.addEventListener("error", (event) => {
    if (event.target.matches?.(".provider-logo")) {
      event.target.hidden = true;
      event.target.nextElementSibling.hidden = false;
    }
    if (event.target.matches?.(".tmdb-backdrop")) {
      event.target.hidden = true;
      event.target.nextElementSibling.hidden = false;
      updateBackdrop();
    }
  }, true);

  async function load(post = false, selected = null) {
    if (busy) return;
    busy = true;
    section.setAttribute("aria-busy", "true");
    refresh.disabled = true;
    errorBox.hidden = true;
    const options = {};
    if (post) {
      options.method = "POST";
      options.body = new URLSearchParams({ csrf: section.dataset.csrf });
      if (selected) options.body.set("selected", selected);
    }
    try {
      const response = await fetch(section.dataset.endpoint, options);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      const html = new DOMParser().parseFromString(result.html, "text/html");
      content.replaceChildren(...html.body.childNodes);
      const labels = document.querySelector("#content-labels");
      if (labels && typeof result.content_labels === "string") {
        const labelHtml = new DOMParser().parseFromString(result.content_labels, "text/html");
        labels.replaceChildren(...labelHtml.body.childNodes);
      }
      updateBackdrop();
      updateOriginalFields();
    } catch (error) {
      errorBox.textContent = `No se pudo consultar TMDB. ${error.message}`;
      errorBox.hidden = false;
      if (content.textContent.includes("Consultando TMDB")) content.replaceChildren();
    } finally {
      busy = false;
      refresh.disabled = false;
      section.setAttribute("aria-busy", "false");
    }
  }
  refresh.addEventListener("click", () => load(true));
  content.addEventListener("click", (event) => {
    const candidate = event.target.closest(".tmdb-candidate");
    if (candidate) load(true, candidate.dataset.id);
  });
  load();
})();
