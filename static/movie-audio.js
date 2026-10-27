"use strict";

(() => {
  const root = document.getElementById("movie-audio");
  if (!root) return;
  const copies = [...root.querySelectorAll("[data-audio-copy]")];
  const timers = new Map();

  function showError(copy, message) {
    const error = copy.querySelector("[data-audio-error]");
    error.textContent = message || "";
    error.hidden = !message;
  }

  function render(copy, state) {
    copy.dataset.state = JSON.stringify(state);
    const languages = copy.querySelector("[data-audio-languages]");
    languages.replaceChildren();
    const metadata = state.metadata;
    for (const code of metadata?.languages || []) {
      const badge = document.createElement("span");
      badge.className = "badge text-bg-secondary";
      badge.textContent = code;
      badge.title = "Idioma declarado en una pista de audio de esta copia";
      languages.append(badge);
    }
    if (metadata?.unknown) {
      const badge = document.createElement("span");
      badge.className = "badge text-bg-secondary";
      badge.textContent = "?";
      badge.title = "Hay pistas de audio sin idioma identificado";
      languages.append(badge);
    }
    if (!metadata || !metadata.tracks) {
      const text = document.createElement("span");
      text.className = "small text-muted";
      text.textContent = metadata ? "Sin pistas de audio" : (
        state.stale ? "Copia modificada; vuelve a comprobar" : "Sin comprobar"
      );
      languages.append(text);
    }
    const button = copy.querySelector("[data-audio-check]");
    button.disabled = state.running || copy.dataset.unavailable === "true";
    button.textContent = state.running ? "Comprobando…" : (metadata ? "↻" : "Comprobar");
    button.setAttribute("aria-label", metadata ? "Volver a comprobar el audio" : "Comprobar el audio");
    showError(copy, state.error);
  }

  async function fetchState(copy, method = "GET") {
    const options = {method, headers: {Accept: "application/json"}, signal: AbortSignal.timeout(10000)};
    if (method === "POST") options.body = new URLSearchParams({csrf: root.dataset.csrf});
    let response;
    try {
      response = await fetch(copy.dataset.endpoint, options);
    } catch {
      throw new Error("No se pudo conectar con el servidor. Recarga la ficha para recuperar el estado.");
    }
    if (response.status === 401) throw new Error("La sesión ha caducado. Recarga la página.");
    if (!response.headers.get("Content-Type")?.includes("application/json")) {
      throw new Error("Respuesta inesperada del servidor. Recarga la ficha para recuperar el estado.");
    }
    let state;
    try {
      state = await response.json();
    } catch {
      throw new Error("El servidor no devolvió una respuesta válida. Recarga la ficha para recuperar el estado.");
    }
    if (!response.ok) throw new Error(state.error || "No se pudo consultar la comprobación de audio.");
    return state;
  }

  function schedule(copy) {
    clearTimeout(timers.get(copy));
    timers.set(copy, setTimeout(async () => {
      try {
        const state = await fetchState(copy);
        render(copy, state);
        if (state.running) schedule(copy);
      } catch (error) {
        render(copy, {...JSON.parse(copy.dataset.state), running: false});
        showError(copy, error.message || "No se pudo consultar el audio. Recarga la ficha para recuperar el estado.");
      }
    }, 1000));
  }

  for (const copy of copies) {
    if (JSON.parse(copy.dataset.state).running) schedule(copy);
    copy.querySelector("[data-audio-check]").addEventListener("click", async () => {
      const previous = JSON.parse(copy.dataset.state);
      render(copy, {...previous, running: true, error: null});
      try {
        await fetchState(copy, "POST");
        schedule(copy);
      } catch (error) {
        render(copy, previous);
        showError(copy, error.message || "No se pudo iniciar la comprobación de audio.");
      }
    });
  }
  document.getElementById("audio-copy")?.addEventListener("change", event => {
    for (const copy of copies) copy.hidden = copy.dataset.audioCopy !== event.target.value;
  });
})();
