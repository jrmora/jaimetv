"use strict";

window.jaimetvSpa.register(({ root, signal }) => {
  const tablist = root.querySelector("#admin-tabs");
  if (!tablist) return;
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const hashFor = tab => `#tab-${tab.dataset.tab}`;

  function select(tab, { focus = false, updateHash = true } = {}) {
    for (const item of tabs) {
      const selected = item === tab;
      const panel = root.getElementById(item.getAttribute("aria-controls"));
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      panel.classList.toggle("active", selected);
      panel.hidden = !selected;
    }
    if (updateHash) history.replaceState(history.state, "", hashFor(tab));
    if (focus) tab.focus();
  }

  select(tabs.find(tab => hashFor(tab) === location.hash) || tabs[0], { updateHash: false });

  tablist.addEventListener("click", event => {
    const tab = event.target.closest('[role="tab"]');
    if (tab && tablist.contains(tab)) select(tab);
  }, { signal });

  tablist.addEventListener("keydown", event => {
    const current = tabs.indexOf(event.target);
    if (current < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    let target;
    if (step) target = tabs[(current + step + tabs.length) % tabs.length];
    else if (event.key === "Home") target = tabs[0];
    else if (event.key === "End") target = tabs[tabs.length - 1];
    if (!target) return;
    event.preventDefault();
    select(target, { focus: true });
  }, { signal });
});
