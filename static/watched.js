"use strict";

document.addEventListener("submit", async (event) => {
  const form = event.target;
  const isBanned = form.matches(".banned-form");
  const isWatchlist = form.matches(".watchlist-form");
  if (!isBanned && !isWatchlist && !form.matches(".watched-form")) return;
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
    const state = isWatchlist ? "watchlist" : isBanned ? "banned" : "watched";
    const enabled = data[state];
    form.elements[state].value = enabled ? "0" : "1";
    const label = isWatchlist ? (enabled ? "Quitar de Mi lista" : "Quiero ver") : isBanned ? (enabled ? "Desbloquear" : "No me interesa") : (enabled ? "Vista" : "Ya la vi");
    if (button.dataset?.iconOnly !== "true") button.textContent = label;
    const visibleLabel = button.querySelector?.(".detail-action-label");
    if (visibleLabel) visibleLabel.textContent = label;
    button.setAttribute("aria-label", label);
    button.title = isWatchlist ? label : isBanned ? (enabled ? "Volver a mostrar este título" : "Ocultar este título del catálogo") : (enabled ? "Marcar como no vista" : "Marcar como vista");
    button.setAttribute("aria-pressed", String(enabled));
    button.classList.toggle(isWatchlist ? "btn-primary" : isBanned ? "btn-danger" : "btn-success", enabled);
    if (isWatchlist) button.querySelector("svg").setAttribute("fill", enabled ? "currentColor" : "none");
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
