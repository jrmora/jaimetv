"use strict";

window.jaimetvSpa.register(() => {
  const form = document.querySelector("#diagnostic-console");
  if (!form) return;
  const commandInput = form.querySelector("#diagnostic-console-command");
  const output = document.querySelector("#diagnostic-console-output");

  form.addEventListener("submit", async event => {
    event.preventDefault();
    const command = commandInput.value.trim();
    if (!command) return;
    output.textContent += `\n\n> ${command}\n`;
    commandInput.disabled = true;
    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ command, csrf: form.dataset.csrf }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      output.textContent += result.output;
    } catch (error) {
      output.textContent += `Error: ${error.message}`;
    } finally {
      commandInput.disabled = false;
      commandInput.focus();
      output.scrollTop = output.scrollHeight;
    }
  });
});