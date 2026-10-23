"use strict";

(() => {
  const panel = document.querySelector("#tmdb-admin");
  if (!panel) return;
  const status = panel.querySelector("#tmdb-batch-status");
  const form = panel.querySelector("#tmdb-batch-controls");
  const start = panel.querySelector("#tmdb-batch-start");
  const directory = panel.querySelector("#tmdb-directory");
  const cancel = panel.querySelector("#tmdb-batch-cancel");
  const kind = panel.querySelector("#tmdb-kind");
  const limit = panel.querySelector("#tmdb-batch-limit");
  let timer;
  let wasRunning = false;

  function message(text, error = false) {
    status.hidden = false;
    status.textContent = text;
    status.classList.toggle("text-danger", error);
  }

  async function request(values) {
    const response = await fetch(panel.dataset.batchEndpoint, values ? {
      method: "POST", body: new URLSearchParams({ csrf: panel.dataset.csrf, ...values }),
    } : { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  async function check() {
    clearTimeout(timer);
    try {
      const state = await request();
      const running = state.status === "running";
      for (const control of [start, directory, kind, limit]) control.disabled = running;
      cancel.hidden = !running;
      status.setAttribute("aria-busy", String(running));
      if (state.status === "idle") return;
      const progress = `${state.processed || 0} de ${state.total || 0} títulos; ${state.matched || 0} asociados, ${state.candidates || 0} por elegir, ${state.missing || 0} sin coincidencia.`;
      if (running) {
        wasRunning = true;
        message(state.phase === "platforms" ? "Cargando plataformas de Canadá…" :
          `Consultando ${state.kind === "movies" ? "películas" : "series"}: ${progress}`);
        timer = setTimeout(check, 2000);
      } else {
        message(`${state.status === "failed" ? `Proceso detenido: ${state.error}` :
          state.status === "cancelled" ? "Proceso cancelado." : "Proceso completado."} ${progress} Los avances se conservan.`,
        state.status === "failed");
        if (wasRunning) {
          wasRunning = false;
          setTimeout(() => location.reload(), 2000);
        }
      }
    } catch (error) {
      message(`No se pudo consultar el estado. ${error.message} Recarga para comprobarlo; el proceso no se cancela.`, true);
    }
  }

  async function begin(action) {
    if (action === "start" && !form.reportValidity()) return;
    clearTimeout(timer);
    start.disabled = directory.disabled = true;
    try {
      await request({ action, kind: kind.value, limit: action === "directory" ? "50" : limit.value });
    } catch (error) {
      message(`No se pudo iniciar el proceso. ${error.message}`, true);
      start.disabled = directory.disabled = false;
      return;
    }
    check();
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    begin("start");
  });
  directory.addEventListener("click", () => begin("directory"));
  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      await request({ action: "cancel" });
      message("Cancelación solicitada. Esperando a que termine la consulta actual…");
    } catch (error) {
      message(`No se pudo cancelar. ${error.message}`, true);
    } finally {
      cancel.disabled = false;
    }
  });
  check();
})();
