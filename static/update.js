"use strict";

window.jaimetvSpa.register(() => {
  const button = document.querySelector("#application-update");
  const status = document.querySelector("#update-status");
  if (!button || !status) return;
  let started = false;
  let retries = 0;

  function show(text) {
    status.hidden = false;
    status.textContent = text;
  }

  async function poll() {
    try {
      const response = await fetch(button.dataset.endpoint, { cache: "no-store" });
      if (response.status === 401) {
        show("La sesión caducó. Recarga la página e inicia sesión para consultar la actualización.");
        button.disabled = false;
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      retries = 0;
      button.disabled = result.status === "running";
      button.textContent = button.disabled ? "Actualizando…" : "Actualizar aplicación";
      if (result.status !== "idle") show(result.message);
      if (result.status === "running") {
        started = true;
        setTimeout(poll, 3000);
      } else if (started && result.status === "completed") {
        show(`${result.message} Recargando…`);
        setTimeout(() => location.reload(), 1500);
      }
    } catch (error) {
      if (started && retries++ < 60) {
        show("Esperando al servidor durante la actualización…");
        setTimeout(poll, 3000);
      } else {
        button.disabled = false;
        show(`No se pudo consultar la actualización: ${error.message}. Recarga para consultar su estado.`);
      }
    }
  }

  button.addEventListener("click", async () => {
    button.disabled = true;
    show("Solicitando actualización…");
    try {
      const response = await fetch(button.dataset.endpoint, {
        method: "POST", body: new URLSearchParams({ csrf: button.dataset.csrf }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      started = true;
      poll();
    } catch (error) {
      button.disabled = false;
      show(`No se pudo iniciar la actualización: ${error.message}`);
    }
  });
  poll();
});
