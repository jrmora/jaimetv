"use strict";

(() => {
  const inits = [];
  let controller = new AbortController();
  let token = 0;
  let rendered = location.pathname + location.search;

  function runPages() {
    controller.abort();
    controller = new AbortController();
    for (const init of inits) init({ root: document, signal: controller.signal });
  }

  function absolute(url) {
    return new URL(url, location.href).href;
  }

  function loadScripts(doc) {
    const present = new Set(Array.from(document.scripts, script => script.src));
    const missing = Array.from(doc.querySelectorAll("script[src]"), script => absolute(script.getAttribute("src")))
      .filter((src, index, list) => !present.has(src) && list.indexOf(src) === index);
    return missing.reduce((chain, src) => chain.then(() => new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = false;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`No se pudo cargar ${src}`));
      document.body.append(script);
    })), Promise.resolve());
  }

  function syncStyles(doc) {
    const wanted = new Set(Array.from(doc.querySelectorAll('link[rel="stylesheet"]'), link => absolute(link.getAttribute("href"))));
    for (const link of document.querySelectorAll("link[data-spa-page]")) {
      if (!wanted.has(link.href)) link.remove();
    }
    const present = new Set(Array.from(document.querySelectorAll('link[rel="stylesheet"]'), link => link.href));
    for (const source of doc.querySelectorAll('link[rel="stylesheet"]')) {
      if (present.has(absolute(source.getAttribute("href")))) continue;
      const link = document.createElement("link");
      for (const attribute of source.attributes) link.setAttribute(attribute.name, attribute.value);
      link.dataset.spaPage = "";
      document.head.append(link);
    }
  }

  function swapContent(doc) {
    const nav = document.querySelector(".navbar > .container");
    const newNav = doc.querySelector(".navbar > .container");
    const theme = nav.querySelector("#theme-toggle");
    for (const element of Array.from(nav.children)) {
      if (element.id !== "theme-toggle" && element.id !== "fullscreen-toggle") element.remove();
    }
    for (const element of Array.from(newNav.children)) {
      if (element.id !== "theme-toggle" && element.id !== "fullscreen-toggle") nav.insertBefore(element, theme);
    }
    document.querySelector("main").replaceWith(doc.querySelector("main"));
    document.title = doc.title;
  }

  function withShell(url) {
    const shell = new URL(url);
    shell.searchParams.set("shell", "1");
    return shell.href;
  }

  function withoutShell(url) {
    const clean = new URL(url);
    clean.searchParams.delete("shell");
    return clean.href;
  }

  async function navigate(url, { push }) {
    const current = ++token;
    let response;
    let html;
    try {
      response = await fetch(push ? withShell(url) : url, { headers: { Accept: "text/html" } });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("text/html")) {
        throw new Error("La respuesta no es una página HTML");
      }
      html = await response.text();
    } catch {
      location.href = url;
      return;
    }
    const doc = new DOMParser().parseFromString(html, "text/html");
    if (!doc.querySelector("main") || !doc.querySelector(".navbar > .container")) {
      location.href = url;
      return;
    }
    try {
      await loadScripts(doc);
    } catch {
      location.href = url;
      return;
    }
    if (current !== token) return;
    syncStyles(doc);
    swapContent(doc);
    if (push) {
      history.pushState(null, "", withoutShell(response.url || url));
      window.scrollTo({ top: 0, behavior: "instant" });
    }
    rendered = location.pathname + location.search;
    runPages();
  }

  window.jaimetvSpa = {
    register(init) {
      inits.push(init);
    },
    reload() {
      return navigate(location.href, { push: false });
    },
  };

  for (const link of document.querySelectorAll('link[rel="stylesheet"]:not([data-spa-base])')) {
    link.dataset.spaPage = "";
  }

  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || (link.target && link.target !== "_self") || link.hasAttribute("download")) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin) return;
    if (url.hash && url.pathname === location.pathname && url.search === location.search) return;
    event.preventDefault();
    navigate(url.href, { push: true });
  });

  window.addEventListener("popstate", () => {
    const target = location.pathname + location.search;
    if (target === rendered) return;
    navigate(location.href, { push: false });
  });

  document.addEventListener("DOMContentLoaded", runPages);
})();
