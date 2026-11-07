"use strict";

window.jaimetvSpa.register(({ signal }) => {
function showPosterError(image) {
  image.hidden = true;
  image.nextElementSibling.hidden = false;
}

document.addEventListener("error", (event) => {
  if (event.target.matches?.(".catalog-poster")) showPosterError(event.target);
  if (event.target.matches?.(".card-service-logo")) showPosterError(event.target);
}, { capture: true, signal });
for (const image of document.querySelectorAll(".catalog-poster, .card-service-logo")) {
  if (image.complete && !image.naturalWidth) showPosterError(image);
}
});
