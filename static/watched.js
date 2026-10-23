"use strict";

document.addEventListener("submit", async (event) => {
  const form = event.target;
  const isBanned = form.matches(".banned-form");
  if (!isBanned && !form.matches(".watched-form")) return;
  event.preventDefault();
  const button = form.querySelector("button");
  if (button.disabled) return;
  const errorBox = form.querySelector(".watched-error");
  button.disabled = true;
  errorBox.hidden = true;
  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: { Accept: "application/json" },
      body: new FormData(form),
    });
    if (!response.ok) {
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      throw new Error(page.querySelector('[role="alert"]')?.textContent.trim() || `Error HTTP ${response.status}. Recarga la página y vuelve a intentarlo.`);
    }
    const data = await response.json();
    const state = isBanned ? "banned" : "watched";
    const enabled = data[state];
    form.elements[state].value = enabled ? "0" : "1";
    button.textContent = isBanned ? (enabled ? "Desbloquear" : "No me interesa") : (enabled ? "Vista" : "Ya la vi");
    button.title = isBanned ? (enabled ? "Volver a mostrar esta película" : "Ocultar esta película del catálogo") : (enabled ? "Marcar como no vista" : "Marcar como vista");
    button.setAttribute("aria-pressed", String(enabled));
    button.classList.toggle(isBanned ? "btn-danger" : "btn-success", enabled);
    button.classList.toggle("btn-outline-secondary", !enabled);
    document.dispatchEvent(new CustomEvent("movie-state-changed"));
    if (isBanned && !document.querySelector("#filters") && document.querySelector(".catalog-grid")) {
      location.reload();
    }
  } catch (error) {
    errorBox.textContent = `No se pudo guardar el estado. ${error.message}`;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
  }
});
