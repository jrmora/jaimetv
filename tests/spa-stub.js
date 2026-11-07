"use strict";

// Runs page scripts the way static/spa.js does: each init registered through
// window.jaimetvSpa.register runs immediately against the context's document.
function withSpa(context) {
  context.window = {
    ...(context.window || {}),
    jaimetvSpa: {
      register(init) {
        init({ root: context.document, signal: new AbortController().signal });
      },
      reload() {
        context.location?.reload();
      },
    },
  };
  return context;
}

module.exports = { withSpa };
