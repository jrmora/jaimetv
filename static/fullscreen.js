"use strict";

(() => {
  const button = document.querySelector("#fullscreen-toggle");
  if (!button || !document.fullscreenEnabled) return;
  const enter = document.querySelector("#fullscreen-enter");
  const exit = document.querySelector("#fullscreen-exit");
  const warning = document.querySelector("#fullscreen-warning");

  function render() {
    const active = Boolean(document.fullscreenElement);
    const label = active ? "Salir de pantalla completa" : "Pantalla completa";
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute("aria-label", label);
    button.title = label;
    enter.style.display = active ? "none" : "";
    exit.style.display = active ? "" : "none";
    exit.removeAttribute("hidden");
  }

  button.hidden = false;
  button.addEventListener("click", async () => {
    button.disabled = true;
    warning.hidden = true;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else {
        await document.documentElement.requestFullscreen();
      }
    } catch (error) {
      warning.textContent = `No se pudo cambiar el modo de pantalla completa. ${error.message}`;
      warning.hidden = false;
    } finally {
      button.disabled = false;
      render();
    }
  });
  document.addEventListener("fullscreenchange", render);
  render();
})();
