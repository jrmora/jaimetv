"use strict";

(() => {
  const button = document.querySelector("#catalog-sync");
  const status = document.querySelector("#sync-status");
  if (!button || !status) return;
  let reloadOnCompletion = false;

  function message(text, error = false) {
    status.hidden = false;
    status.className = `container mt-3 text-${error ? "danger" : "body"}`;
    status.textContent = text;
  }

  function render(result) {
    const running = result.status === "running";
    button.disabled = running;
    button.textContent = running ? "Sincronizando…" : "Sincronizar catálogo";
    status.setAttribute("aria-busy", String(running));
    if (running) {
      reloadOnCompletion = true;
      message("Descargando y actualizando el catálogo. Puedes seguir navegando; puede tardar varios minutos.");
      setTimeout(check, 2000);
    } else if (result.status === "completed") {
      const counts = { new: 0, updated: 0, unchanged: 0, excluded: 0, removed_excluded: 0 };
      for (const changes of Object.values(result.changes)) {
        for (const key of Object.keys(counts)) counts[key] += changes[key];
      }
      const duplicates = Object.values(result.duplicates).reduce((sum, count) => sum + count, 0);
      message(`Sincronización completada: ${counts.new} nuevos, ${counts.updated} actualizados, ` +
        `${counts.unchanged} sin cambios, ${counts.excluded} excluidos y ${counts.removed_excluded} retirados por exclusión. ` +
        (duplicates ? `Aviso: ${duplicates} IDs repetidos; se conservó la última versión. ` : "") +
        (reloadOnCompletion ? "Actualizando esta página…" : ""));
      if (reloadOnCompletion) {
        button.disabled = true;
        setTimeout(() => location.reload(), 2000);
      }
    } else if (result.status === "failed") {
      message(`No se pudo sincronizar. ${result.error}`, true);
    }
  }

  async function check() {
    try {
      const response = await fetch(button.dataset.endpoint, { cache: "no-store" });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      render(result);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Sincronizar catálogo";
      status.setAttribute("aria-busy", "false");
      message(`No se pudo comprobar la sincronización. ${error.message} Recarga la página para consultar su estado; esto no cancela el trabajo.`, true);
    }
  }

  button.hidden = false;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const response = await fetch(button.dataset.endpoint, {
        method: "POST", body: new URLSearchParams({ csrf: button.dataset.csrf }),
      });
      const result = await response.json();
      if (response.status === 409) {
        render({ status: "running" });
      } else {
        if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
        render(result);
      }
    } catch (error) {
      button.disabled = false;
      message(`No se pudo iniciar la sincronización. ${error.message}`, true);
    }
  });
  check();
})();
