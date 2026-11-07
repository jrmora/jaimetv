"use strict";

const docs = document.querySelector("#swagger-ui");

window.ui = SwaggerUIBundle({
  url: docs.dataset.specUrl,
  dom_id: "#swagger-ui",
  deepLinking: true,
  displayRequestDuration: true,
  requestInterceptor(request) {
    if (request.method?.toUpperCase() === "POST") {
      const url = new URL(request.url, window.location.origin);
      if (url.origin !== window.location.origin) return request;
      const isTmdbDetails = /^\/api\/tmdb\/[^/]+\/[^/]+$/.test(url.pathname);
      const body = new URLSearchParams(request.body || "");
      body.set("csrf", isTmdbDetails ? docs.dataset.tmdbCsrf : docs.dataset.syncCsrf);
      request.body = body.toString();
      request.headers["Content-Type"] = "application/x-www-form-urlencoded";
    }
    return request;
  },
});