/* Drag-to-reorder for the top bar arrangement screen.
 *
 * Strictly additive. Every row already carries Move up and Move down submit buttons
 * that work with no script at all, and the server reads one hidden "order" field
 * either way. This does not replace that path — it writes into the same field.
 *
 * Three things are worth knowing.
 *
 * 1. THE GRIP IS HIDDEN IN THE MARKUP AND REVEALED HERE. A drag handle rendered by the
 *    server would be a control that does nothing whenever this file fails to load,
 *    which is exactly the affordance-without-a-behaviour problem the buttons exist to
 *    avoid. If you are reading this, the script ran, so the grip is safe to show.
 *
 * 2. THERE IS NO KEYBOARD PATH HERE, ON PURPOSE. Dragging is a pointer idiom; the
 *    keyboard equivalent is the pair of buttons already in every row, which move focus
 *    with the row they moved. Reimplementing reordering a second time for the keyboard
 *    would be a second set of bugs, and worse than the buttons.
 *
 * 3. THE ORDER FIELD IS THE ONLY OUTPUT. Nothing here posts, mutates a row's name, or
 *    touches a checkbox. Dropping a row rewrites one string, and Save is still Save.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-topbar-form]");
  var list = document.querySelector("[data-topbar-list]");
  var field = document.querySelector("[data-topbar-order]");
  if (!form || !list || !field) return;

  var dragging = null;

  function rows() {
    return Array.prototype.slice.call(list.querySelectorAll("[data-topbar-row]"));
  }

  function writeOrder() {
    field.value = rows()
      .map(function (row) {
        return row.getAttribute("data-topbar-row");
      })
      .join(" ");
  }

  function clearMarkers() {
    rows().forEach(function (row) {
      row.removeAttribute("data-drop-before");
      row.removeAttribute("data-drop-after");
    });
  }

  rows().forEach(function (row) {
    var grip = row.querySelector("[data-topbar-grip]");
    if (!grip) return;

    grip.hidden = false;
    row.setAttribute("draggable", "true");

    row.addEventListener("dragstart", function (event) {
      dragging = row;
      row.setAttribute("data-dragging", "");
      // Firefox ignores a drag that carries no data at all.
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", row.getAttribute("data-topbar-row"));
      }
    });

    row.addEventListener("dragend", function () {
      row.removeAttribute("data-dragging");
      clearMarkers();
      dragging = null;
    });

    row.addEventListener("dragover", function (event) {
      if (!dragging || dragging === row) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";

      // Which half of the row the pointer is over decides which side it lands on.
      // Measured per event rather than cached: the list reflows as rows move.
      var box = row.getBoundingClientRect();
      var after = event.clientY > box.top + box.height / 2;
      clearMarkers();
      row.setAttribute(after ? "data-drop-after" : "data-drop-before", "");
    });

    row.addEventListener("drop", function (event) {
      if (!dragging || dragging === row) return;
      event.preventDefault();
      var box = row.getBoundingClientRect();
      var after = event.clientY > box.top + box.height / 2;
      row.parentNode.insertBefore(dragging, after ? row.nextSibling : row);
      clearMarkers();
      writeOrder();
    });
  });

  // Written once on load too. A row the server ordered and nobody dragged must still
  // round-trip its position, rather than posting an empty field the server reads as
  // "no opinion" and then resolves differently.
  writeOrder();
})();
