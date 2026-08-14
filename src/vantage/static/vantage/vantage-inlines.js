/* Adding and removing inline child rows, and the ManagementForm bookkeeping that makes
 * the server believe it.
 *
 * Not to be confused with vantage-inline.js (singular), which stages cell edits on the
 * list page. That file answers [data-grid][data-inline]; this one answers
 * [data-inline-group]. Nothing here talks to the network — the parent form's own submit
 * carries every change.
 *
 * The whole feature is four numbers on a hidden ManagementForm, and getting any of them
 * wrong fails SILENTLY. A formset reads TOTAL_FORMS and then constructs exactly that many
 * prefixed forms; anything past the count is never built, never validated and never
 * saved, and no error is raised anywhere. "I added a line, pressed save, and it just
 * wasn't there" is the bug report, and it arrives with a green success message attached.
 * Two rules follow, and they are opposites on purpose:
 *
 *   1. REMOVING A SAVED ROW NEVER TOUCHES TOTAL_FORMS. It is deleted by ticking its
 *      DELETE box and leaving every input exactly where it is — the row is hidden, not
 *      unlinked. Django's formsets bind form N to the Nth row of the queryset BY
 *      POSITION, not by primary key, so pulling row 3 out of the document shifts row 4
 *      into index 3 and its typed values get written onto row 3's record. A "remove" that
 *      quietly edits a different record is the worst outcome available here, and it is
 *      what the obvious implementation does.
 *
 *   2. REMOVING AN UNSAVED ROW DOES decrement it, and renumbers. Such a row exists only
 *      in this document, so there is no slot to keep. Renumbering is safe here and only
 *      here: added rows are always appended after every saved one, so no index below
 *      INITIAL_FORMS is ever rewritten and the positional binding above stays intact.
 *
 * New rows are cloned from a <template> the server rendered with the same partial that
 * drew the others, so there is no second copy of the markup here to drift from it.
 * Django's placeholder is the literal string __prefix__ in every name, id and for.
 */
(function () {
  "use strict";

  var PLACEHOLDER = /__prefix__/g;

  function each(nodes, fn) {
    Array.prototype.forEach.call(nodes, fn);
  }

  function setup(root) {
    var prefix = root.getAttribute("data-inline-prefix");
    var total = document.getElementById("id_" + prefix + "-TOTAL_FORMS");
    var container = root.querySelector("[data-inline-rows]");
    if (!prefix || !total || !container) return;

    var template = root.querySelector("[data-inline-template]");
    var addButton = root.querySelector("[data-inline-add]");
    var limitNote = root.querySelector("[data-inline-limit]");
    var emptyNote = root.querySelector("[data-inline-empty]");
    var maxAttr = root.getAttribute("data-inline-max");
    var maxNum = maxAttr ? parseInt(maxAttr, 10) : null;

    function mine(node) {
      return node.closest("[data-inline-group]") === root;
    }

    function rows() {
      return Array.prototype.filter.call(
        container.querySelectorAll("[data-inline-row]"),
        mine
      );
    }

    function deleteBox(row) {
      return row.querySelector('input[type="checkbox"][name$="-DELETE"]');
    }

    function removed(row) {
      var box = deleteBox(row);
      return box !== null && box.checked;
    }

    /* Rows a person would say are "on the page". A ticked row is still in the document
     * and still counted by TOTAL_FORMS, but it is bookkeeping, not content, so it does
     * not count against max_num and does not keep the empty-state notice away. */
    function liveCount() {
      return rows().filter(function (row) { return !removed(row); }).length;
    }

    function paint(row) {
      var gone = removed(row);
      row.toggleAttribute("data-removed", gone);
      each(row.querySelectorAll("[data-inline-fields]"), function (part) {
        if (mine(part)) part.hidden = gone;
      });
      each(row.querySelectorAll("[data-inline-undo]"), function (part) {
        if (mine(part)) part.hidden = !gone;
      });
    }

    function refresh() {
      var live = liveCount();
      if (emptyNote) emptyNote.hidden = live > 0;
      if (addButton) {
        var full = maxNum !== null && live >= maxNum;
        addButton.disabled = full;
        if (limitNote) limitNote.hidden = !full;
      }
    }

    function renumber() {
      /* Only reached after an unsaved row is unlinked, so every index rewritten here is
       * at or above INITIAL_FORMS. See rule 2 at the top of the file. */
      rows().forEach(function (row, index) {
        each(row.querySelectorAll("[name], [id], [for]"), function (el) {
          ["name", "id", "for"].forEach(function (attribute) {
            var value = el.getAttribute(attribute);
            if (!value) return;
            var pattern = new RegExp("(^|[_-])" + prefix + "-\\d+-");
            if (!pattern.test(value)) return;
            el.setAttribute(attribute, value.replace(pattern, "$1" + prefix + "-" + index + "-"));
          });
        });
      });
    }

    function remove(row) {
      var box = deleteBox(row);
      if (box) {
        box.checked = true;
        paint(row);
        refresh();
        var undo = row.querySelector("[data-inline-restore]");
        if (undo) undo.focus();
        return;
      }
      row.parentNode.removeChild(row);
      renumber();
      /* Written from the row count rather than decremented, so a value that was already
       * wrong self-corrects instead of compounding. */
      total.value = String(rows().length);
      refresh();
      if (addButton && !addButton.disabled) addButton.focus();
    }

    function restore(row) {
      var box = deleteBox(row);
      if (!box) return;
      box.checked = false;
      paint(row);
      refresh();
      var button = row.querySelector("[data-inline-remove]");
      if (button) button.focus();
    }

    function add() {
      if (!template) return;
      var index = parseInt(total.value, 10);
      if (isNaN(index) || index < 0) index = rows().length;

      var holder = document.createElement("template");
      holder.innerHTML = template.innerHTML.replace(PLACEHOLDER, String(index)).trim();
      var row = holder.content.firstElementChild;
      if (!row) return;

      container.appendChild(row);
      total.value = String(rows().length);
      row.setAttribute("data-inline-new", "");
      reveal(row);
      refresh();

      var first = row.querySelector("input:not([type=hidden]):not([disabled]), select, textarea");
      if (first) first.focus();
    }

    /* The checkbox is the no-JavaScript path and stays in the document — it is what the
     * server reads either way. It is swapped for a button only now that there is
     * something running to do the swap, so a page where this file fails to load keeps a
     * plainer form that still works. */
    function reveal(scope) {
      each(scope.querySelectorAll("[data-inline-remove]"), function (button) {
        if (mine(button)) button.hidden = false;
      });
      each(scope.querySelectorAll("[data-inline-delete-label]"), function (label) {
        if (mine(label)) label.hidden = true;
      });
    }

    root.addEventListener("click", function (event) {
      var button = event.target.closest(
        "[data-inline-remove], [data-inline-add], [data-inline-restore]"
      );
      if (!button || !mine(button)) return;
      event.preventDefault();
      if (button.hasAttribute("data-inline-add")) {
        add();
        return;
      }
      var row = button.closest("[data-inline-row]");
      if (!row) return;
      if (button.hasAttribute("data-inline-restore")) {
        restore(row);
      } else {
        remove(row);
      }
    });

    /* The checkbox stays authoritative even while hidden: a browser restoring form state
     * on back-navigation ticks it without a click, and the row has to look like whatever
     * the server is about to read. */
    root.addEventListener("change", function (event) {
      var box = event.target;
      if (!box.name || box.name.indexOf("-DELETE") === -1) return;
      var row = box.closest("[data-inline-row]");
      if (row) paint(row);
      refresh();
    });

    reveal(root);
    rows().forEach(paint);
    refresh();
  }

  function init(scope) {
    each((scope || document).querySelectorAll("[data-inline-group]"), setup);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  } else {
    init(document);
  }
})();
