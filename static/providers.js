"use strict";

(() => {
  const panel = document.querySelector("#provider-filter");
  if (!panel) return;
  const options = [...panel.querySelectorAll(".provider-option")];
  const message = panel.querySelector("#provider-message");
  const services = JSON.parse(panel.dataset.subscriptions);
  const batchStatus = panel.querySelector("#tmdb-batch-status");
  const start = panel.querySelector("#tmdb-batch-start");
  const directory = panel.querySelector("#tmdb-directory");
  const cancel = panel.querySelector("#tmdb-batch-cancel");
  let seenRunning = false;
  let timer;

  function notify(element, text, error = false) {
    element.textContent = text;
    element.hidden = false;
    element.classList.toggle("text-danger", error);
  }

  async function request(endpoint, values = null) {
    const response = await fetch(endpoint, values ? {
      method: "POST", body: new URLSearchParams([["csrf", panel.dataset.csrf], ...values]),
    } : { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function updateServicesWarning() {
    panel.querySelector("#my-services-warning").hidden =
      panel.querySelector("#my-services-toggle").value !== "1" || services.length > 0;
  }
  panel.querySelector("#my-services-toggle").addEventListener("change", updateServicesWarning);
  panel.querySelector("#tmdb-batch-limit").form.addEventListener("submit", (event) => {
    event.preventDefault();
    begin("start");
  });
  panel.querySelector("#provider-search").addEventListener("input", (event) => {
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
  panel.addEventListener("error", (event) => {
    if (event.target.matches?.(".provider-choice img")) showLogoError(event.target);
  }, true);
  for (const image of panel.querySelectorAll(".provider-choice img")) {
    if (image.complete && image.naturalWidth === 0) showLogoError(image);
  }

  function renderBatch(state) {
    const running = state.status === "running";
    start.disabled = running;
    directory.disabled = running;
    cancel.hidden = !running;
    batchStatus.setAttribute("aria-busy", String(running));
    if (state.status === "idle") return;
    const progress = `${state.processed} de ${state.total} títulos; ${state.matched} asociados, ${state.candidates} por elegir, ${state.missing} sin coincidencia.`;
    if (running) {
      seenRunning = true;
      notify(batchStatus, state.phase === "platforms" ? "Cargando plataformas de Canadá…" : `Consultando ${state.kind === "movies" ? "películas" : "series"}: ${progress}`);
      timer = setTimeout(checkBatch, 2000);
    } else {
      const failed = state.status === "failed";
      notify(batchStatus, `${failed ? `Proceso detenido: ${state.error}` : state.status === "cancelled" ? "Proceso cancelado." : "Proceso completado."} ${progress} Los avances se conservan.`, failed);
      if (seenRunning && !failed) {
        start.disabled = true;
        directory.disabled = true;
        notify(batchStatus, `${batchStatus.textContent} Actualizando la página…`);
        setTimeout(() => location.reload(), 2000);
      }
    }
  }

  async function checkBatch() {
    clearTimeout(timer);
    try {
      renderBatch(await request(panel.dataset.batchEndpoint));
    } catch (error) {
      notify(batchStatus, `No se pudo consultar el estado. ${error.message} Recarga para volver a comprobarlo; el proceso no se cancela.`, true);
    }
  }
  async function begin(action) {
    if (action === "start" && !panel.querySelector("#tmdb-batch-limit").reportValidity()) return;
    clearTimeout(timer);
    start.disabled = true;
    directory.disabled = true;
    try {
      await request(panel.dataset.batchEndpoint, [
        ["action", action], ["kind", panel.dataset.kind],
        ["limit", action === "directory" ? "50" : panel.querySelector("#tmdb-batch-limit").value],
      ]);
      seenRunning = true;
    } catch (error) {
      notify(message, `No se pudo iniciar el proceso. ${error.message}`, true);
    } finally {
      start.disabled = false;
      directory.disabled = false;
      checkBatch();
    }
  }
  start.addEventListener("click", () => begin("start"));
  directory.addEventListener("click", () => begin("directory"));
  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await request(panel.dataset.batchEndpoint, [["action", "cancel"]]);
      notify(batchStatus, "Cancelación solicitada. Esperando a que termine la consulta actual…");
    } catch (error) {
      notify(batchStatus, `No se pudo solicitar la cancelación. ${error.message}`, true);
    } finally {
      cancel.disabled = false;
    }
  });
  checkBatch();
})();
