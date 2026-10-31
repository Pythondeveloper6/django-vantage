/* Click-to-enlarge for image thumbnails.
 *
 * Progressive enhancement over a plain link: without JavaScript, or with a modifier
 * key held (open in a new tab, a new window, download), the thumbnail is an ordinary
 * link to the original. A plain click opens the original in a native <dialog> instead,
 * which traps focus, closes on Escape and returns focus to the thumbnail by itself.
 *
 * The original is shown as an <img>, never navigated to or embedded as a document, so
 * an uploaded file cannot run script in the panel's origin through the lightbox.
 */
(function () {
  "use strict";

  var dialog = null;

  function t(text) {
    return typeof window.gettext === "function" ? window.gettext(text) : text;
  }

  function build() {
    dialog = document.createElement("dialog");
    dialog.className = "v-lightbox";
    dialog.setAttribute("aria-label", t("Image preview"));
    var close = document.createElement("button");
    close.type = "button";
    close.className = "v-lightbox-close";
    close.setAttribute("aria-label", t("Close"));
    close.textContent = "×";
    close.addEventListener("click", function () { dialog.close(); });
    var figure = document.createElement("figure");
    var img = document.createElement("img");
    img.alt = "";
    img.decoding = "async";
    var caption = document.createElement("figcaption");
    var open = document.createElement("a");
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = t("Open the original in a new tab");
    caption.appendChild(open);
    figure.appendChild(img);
    figure.appendChild(caption);
    dialog.appendChild(close);
    dialog.appendChild(figure);
    // A click on the backdrop lands on the dialog itself, not on its content.
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("close", function () { img.removeAttribute("src"); });
    document.body.appendChild(dialog);
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a.v-thumb[data-lightbox]");
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (typeof HTMLDialogElement === "undefined") return;
    event.preventDefault();
    if (!dialog) build();
    var inner = link.querySelector("img");
    var img = dialog.querySelector("img");
    img.src = link.href;
    img.alt = inner ? inner.alt : "";
    var open = dialog.querySelector("figcaption a");
    open.href = link.href;
    dialog.showModal();
  });
})();
