"use strict";

for (const image of document.querySelectorAll("img[data-error-id]")) {
  function showImageError() {
    image.hidden = true;
    document.getElementById(image.dataset.errorId).hidden = false;
  }
  image.addEventListener("error", showImageError);
  if (image.complete && image.naturalWidth === 0) showImageError();
}
