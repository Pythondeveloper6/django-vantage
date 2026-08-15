/* The per-list customisation rail's enhancement layer.
 *
 * Strictly additive, and unusually easy to prove so: the rail is six ordinary POST
 * forms made of checkboxes, number boxes and selects. Delete this file and every one
 * of them still saves, still resets, and still reorders. What is added here is three
 * conveniences that HTML cannot express:
 *
 * 1. ↑/↓ buttons in place of the position number boxes. The numbers are the accessible,
 *    scriptless way to reorder, and they are what the server reads — the buttons only
 *    rewrite them, so the two can never disagree about the order.
 * 2. Dimming the controls a switched-off section governs, so "the stat strip is off"
 *    is visible before you read the checkbox next to every stat.
 * 3. Keeping the open/closed state of the rail between page loads, because tuning a
 *    list is iterative and a panel that snaps shut on every save is a panel nobody
 *    finishes using.
 *
 * The enhanced state is announced by a data attribute on the container rather than by
 * this file writing styles. Until it is set, the unenhanced controls are the visible
 * ones — which is the correct appearance in the seconds before a deferred script runs
 * and the permanent one if it never does.
 */
(function () {
  "use strict";

  var OPEN_KEY = "vantage-listprefs-open";

  var root = document.querySelector("[data-list-prefs]");
  if (!root) return;

  // ── open/closed, remembered ───────────────────────────────────────────────

  try {
    if (localStorage.getItem(OPEN_KEY) === "1") root.open = true;
    root.addEventListener("toggle", function () {
      try {
        localStorage.setItem(OPEN_KEY, root.open ? "1" : "0");
      } catch (e) {
        /* Safari private mode throws on write; the rail works, it just forgets. */
      }
    });
  } catch (e) {
    /* Reading can throw too. A rail that always starts closed is not a failure. */
  }

  // ── reordering ────────────────────────────────────────────────────────────

  function positions(list) {
    return Array.prototype.slice.call(list.querySelectorAll("[data-prefs-row]"));
  }

  function renumber(list) {
    positions(list).forEach(function (row, index) {
      var box = row.querySelector("[data-prefs-position]");
      if (box) box.value = String(index + 1);
      var buttons = row.querySelectorAll(".v-listprefs-move");
      if (buttons.length === 2) {
        buttons[0].disabled = index === 0;
        buttons[1].disabled = index === positions(list).length - 1;
      }
    });
  }

  function move(list, row, delta) {
    var rows = positions(list);
    var index = rows.indexOf(row);
    var target = index + delta;
    if (target < 0 || target >= rows.length) return;
    if (delta < 0) list.insertBefore(row, rows[target]);
    else list.insertBefore(rows[target], row);
    renumber(list);
    // Focus follows the row, or the keyboard user loses their place on every press.
    var button = row.querySelector(".v-listprefs-move:not(:disabled)");
    if (button) button.focus();
  }

  function button(label, glyph, list, row, delta) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "v-listprefs-move";
    el.textContent = glyph;
    el.setAttribute("aria-label", label);
    el.addEventListener("click", function (event) {
      // The row is a <label>: a click inside it would otherwise toggle the checkbox.
      event.preventDefault();
      move(list, row, delta);
    });
    return el;
  }

  Array.prototype.forEach.call(root.querySelectorAll("[data-prefs-sortable]"), function (list) {
    positions(list).forEach(function (row) {
      var name = (row.textContent || gettext("this row")).trim();
      row.appendChild(button(interpolate(gettext("Move %s up"), [name]), "↑", list, row, -1));
      row.appendChild(button(interpolate(gettext("Move %s down"), [name]), "↓", list, row, 1));
    });
    renumber(list);
  });

  // ── dimming a switched-off section ────────────────────────────────────────

  Array.prototype.forEach.call(root.querySelectorAll("[data-prefs-form]"), function (form) {
    var toggle = form.querySelector("[data-prefs-toggle]");
    var dependent = form.querySelector("[data-prefs-dependent]");
    if (!toggle || !dependent) return;

    function sync() {
      // An attribute, not a disabled fieldset: disabling the inputs would stop the
      // browser sending them, and switching a section off would then also silently
      // clear which of its fields were chosen.
      if (toggle.checked) dependent.removeAttribute("data-prefs-off");
      else dependent.setAttribute("data-prefs-off", "");
    }

    toggle.addEventListener("change", sync);
    sync();
  });

  root.setAttribute("data-prefs-enhanced", "");
})();
