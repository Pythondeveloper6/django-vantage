/* The dashboard editor's enhancement layer.
 *
 * Strictly additive, and easy to prove so: the editor is one ordinary POST form made
 * of number boxes, selects and checkboxes. Delete this file and every one of them
 * still saves, still resets, still reorders and still adds a card. What is added here
 * is four things HTML cannot express:
 *
 * 1. Dragging a card to move it. The number boxes stay in the form and stay the thing
 *    the server reads — dragging only rewrites them — so the two can never disagree
 *    about the order that will be saved.
 * 2. A keyboard equivalent of the dragging, because a rearrangement you can only do
 *    with a mouse is a rearrangement half the people using the panel cannot do. ↑/↓
 *    buttons move the card and take the focus with them.
 * 3. Hiding the chart-only fields when the shape is a stat card, and the columns
 *    belonging to models other than the one chosen. Both are noise, not errors: the
 *    server validates the prefix regardless, so with scripting off the longer lists
 *    are merely longer.
 * 4. Nothing else. No layout, no colour, no state kept anywhere.
 *
 * The enhanced state is announced by a data attribute on the form rather than by this
 * file writing styles. Until it is set the unenhanced controls are the visible ones,
 * which is the correct appearance in the seconds before a deferred script runs and the
 * permanent one if it never does.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-arrange]");
  if (!form) return;

  var grid = form.querySelector("[data-arrange-grid]");

  // ── reordering ────────────────────────────────────────────────────────────

  function slots() {
    if (!grid) return [];
    return Array.prototype.slice.call(grid.querySelectorAll("[data-arrange-slot]"));
  }

  function renumber() {
    var rows = slots();
    rows.forEach(function (slot, index) {
      var box = slot.querySelector("[data-arrange-position]");
      if (box) box.value = String(index + 1);
      var buttons = slot.querySelectorAll(".v-arrange-move");
      if (buttons.length === 2) {
        buttons[0].disabled = index === 0;
        buttons[1].disabled = index === rows.length - 1;
      }
    });
  }

  function move(slot, delta) {
    var rows = slots();
    var index = rows.indexOf(slot);
    var target = index + delta;
    if (target < 0 || target >= rows.length) return;
    if (delta < 0) grid.insertBefore(slot, rows[target]);
    else grid.insertBefore(rows[target], slot);
    renumber();
  }

  function moveButton(slot, delta, label, glyph) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "v-arrange-move";
    button.textContent = glyph;
    button.setAttribute("aria-label", label);
    button.addEventListener("click", function () {
      move(slot, delta);
      // Focus follows the card, or a keyboard user loses their place on every press.
      if (!button.disabled) button.focus();
      else {
        var sibling = slot.querySelector(".v-arrange-move:not(:disabled)");
        if (sibling) sibling.focus();
      }
    });
    return button;
  }

  // ── dragging ──────────────────────────────────────────────────────────────

  var dragged = null;

  function clearTargets() {
    slots().forEach(function (slot) {
      slot.removeAttribute("data-drop-target");
    });
  }

  function wireDragging(slot, bar) {
    bar.setAttribute("draggable", "true");

    bar.addEventListener("dragstart", function (event) {
      dragged = slot;
      slot.setAttribute("data-dragging", "");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        // Firefox refuses to start a drag without payload, and the payload is never
        // read: the element being moved is held in `dragged`.
        try {
          event.dataTransfer.setData("text/plain", "");
        } catch (e) {
          /* Some browsers throw on an empty string; the drag still works. */
        }
      }
    });

    bar.addEventListener("dragend", function () {
      slot.removeAttribute("data-dragging");
      clearTargets();
      dragged = null;
      renumber();
    });

    slot.addEventListener("dragover", function (event) {
      if (!dragged || dragged === slot) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      clearTargets();
      slot.setAttribute("data-drop-target", "");
    });

    slot.addEventListener("drop", function (event) {
      if (!dragged || dragged === slot) return;
      event.preventDefault();
      var rows = slots();
      var from = rows.indexOf(dragged);
      var to = rows.indexOf(slot);
      if (from < 0 || to < 0) return;
      if (from < to) grid.insertBefore(dragged, slot.nextSibling);
      else grid.insertBefore(dragged, slot);
      clearTargets();
      renumber();
    });
  }

  slots().forEach(function (slot) {
    var bar = slot.querySelector("[data-arrange-handle]");
    if (!bar) return;
    var name = (slot.querySelector(".v-arrange-name") || {}).textContent || "this card";
    name = name.trim();
    bar.appendChild(moveButton(slot, -1, "Move " + name + " earlier", "↑"));
    bar.appendChild(moveButton(slot, 1, "Move " + name + " later", "↓"));
    wireDragging(slot, bar);
  });
  renumber();

  // ── the add form ──────────────────────────────────────────────────────────

  var add = form.querySelector("[data-arrange-add]");
  if (add) {
    var model = add.querySelector("[data-arrange-model]");
    var shape = add.querySelector("[data-arrange-shape]");
    var mode = add.querySelector("[data-arrange-mode]");

    function syncModel() {
      var chosen = model ? model.value : "";
      Array.prototype.forEach.call(add.querySelectorAll("[data-arrange-scoped]"), function (select) {
        var groups = select.querySelectorAll("optgroup[data-arrange-for]");
        Array.prototype.forEach.call(groups, function (group) {
          var mine = group.getAttribute("data-arrange-for") === chosen;
          group.hidden = !mine;
          group.disabled = !mine;
        });
        // A selection belonging to a model that is no longer chosen would be dropped
        // by the server anyway; clearing it here means the form says what it means.
        if (select.value && select.value.indexOf(chosen + ":") !== 0) select.value = "";
      });
    }

    function syncShape() {
      var chart = shape && shape.value === "chart";
      var when = mode ? mode.value : "";
      Array.prototype.forEach.call(add.querySelectorAll("[data-arrange-chart]"), function (field) {
        var only = field.getAttribute("data-arrange-when");
        field.hidden = !chart || (only !== null && only !== when);
      });
    }

    if (model) model.addEventListener("change", syncModel);
    if (shape) shape.addEventListener("change", syncShape);
    if (mode) mode.addEventListener("change", syncShape);
    syncModel();
    syncShape();
  }

  form.setAttribute("data-arrange-enhanced", "");
})();
