"use strict";

(() => {
  const key = "iptv-theme";
  let saved;
  let storageWarning = "";
  try {
    saved = localStorage.getItem(key);
  } catch (error) {
    console.warn("No se pudo leer la preferencia de tema.", error);
    storageWarning = "No se puede acceder al almacenamiento del navegador. El tema puede no conservarse al cambiar de página.";
  }

  const preferred = matchMedia("(prefers-color-scheme: dark)");
  const initial = saved === "dark" || saved === "light"
    ? saved : preferred.matches ? "dark" : "light";
  document.documentElement.dataset.bsTheme = initial;

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.querySelector("#theme-toggle");
    const warning = document.querySelector("#theme-warning");

    function update() {
      const dark = document.documentElement.dataset.bsTheme === "dark";
      button.setAttribute("aria-pressed", String(dark));
      const label = dark ? "Cambiar a claro" : "Cambiar a oscuro";
      button.setAttribute("aria-label", label);
      button.title = label;
      document.querySelector("#theme-icon").textContent = dark ? "☀" : "☾";
      warning.textContent = storageWarning;
      warning.hidden = !storageWarning;
    }

    button.hidden = false;
    update();
    button.addEventListener("click", () => {
      const theme = document.documentElement.dataset.bsTheme === "dark" ? "light" : "dark";
      document.documentElement.dataset.bsTheme = theme;
      try {
        localStorage.setItem(key, theme);
        storageWarning = "";
      } catch (error) {
        console.warn("No se pudo guardar la preferencia de tema.", error);
        storageWarning = "El tema se ha cambiado, pero no se pudo guardar. Puede perderse al cambiar de página.";
      }
      update();
    });
  });
})();
