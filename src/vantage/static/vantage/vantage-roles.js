/* The roles matrix: tri-state headers, the pending diff, the review step, and
 * keyboard movement between cells.
 *
 * Additive, like everything else in the runtime. Without this file the matrix is a
 * table of ordinary checkboxes in an ordinary form, and "Review & save" is a plain
 * submit that the server diffs against the state the page was rendered from. With it,
 * four things change:
 *
 * 1. THE DIFF IS THE PAYLOAD. On confirm, every permission checkbox is disabled and
 *    the reviewed diff goes up as two comma-separated fields. That is the same single
 *    POST, but its size follows the number of *changes* rather than the number of
 *    permissions held — which matters because Django refuses a form with more than
 *    DATA_UPLOAD_MAX_NUMBER_FIELDS fields, and a role holding every permission in a
 *    project with three hundred models has more than a thousand.
 *
 * 2. "BASE" IS WHAT THE SERVER RENDERED, NOT WHAT WAS CHECKED ON LOAD. After a
 *    conflict the page comes back with the other person's state as the base and this
 *    person's edits re-applied on top, already pending. Comparing against
 *    `defaultChecked` would lose exactly those edits from the diff.
 *
 * 3. BULK TOGGLES ONLY TOUCH WHAT YOU CAN SEE AND MAY CHANGE. A column header toggles
 *    the rows the filter currently shows, never a hidden one, and never a locked cell.
 *    "Tick every view permission" after filtering to "shop" must not quietly grant
 *    "view" on the user table.
 *
 * 4. FIELD RULES RIDE THE SAME DIFF. Each registered model has a <details> of
 *    per-field <select>s (full access, read-only, hidden). A changed one is counted,
 *    marked and listed in the review exactly like a ticked box, and goes up in the
 *    same POST as a "fields" value of "<content type>:<field>:<level>" entries.
 *
 * 5. ARROW KEYS MOVE BETWEEN CELLS. Tab still walks every control in order — this is
 *    a table of checkboxes, not an ARIA grid with a roving tabindex — but the arrows
 *    move across and down the way a spreadsheet does, because Tab through 1,200
 *    checkboxes is not a way to reach row 80.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "vantage-roles-hide-other";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function enhance(form) {
    var grid = form.querySelector("[data-roles-grid]");
    if (!grid) return;
    document.body.classList.add("v-roles-ready");

    var perms = Array.prototype.slice.call(grid.querySelectorAll("input.v-roles-perm"));
    var selects = Array.prototype.slice.call(grid.querySelectorAll("select.v-roles-field-select"));
    var diffOut = form.querySelector("[data-roles-diff]");
    var resetBtn = form.querySelector("[data-roles-reset]");
    var reviewBtn = form.querySelector("[data-roles-review]");
    var dialog = form.querySelector("[data-roles-dialog]");
    var filter = form.querySelector("[data-roles-filter]");
    var hideOther = form.querySelector("[data-roles-hide-other]");
    var noMatch = form.querySelector("[data-roles-nomatch]");
    var submitting = false;

    function isBase(input) {
      return input.hasAttribute("data-base");
    }

    function editable(input) {
      return !input.disabled;
    }

    function rowVisible(row) {
      if (row.hidden) return false;
      var body = row.parentNode;
      return !body.hasAttribute("data-roles-filtered-out");
    }

    function cellOf(input) {
      return input.closest(".v-roles-chip") || input.closest("td");
    }

    /* ── field rules ───────────────────────────────────────────────────────── */

    var LEVELS = {
      "": gettext("Full access"),
      readonly: gettext("Read-only"),
      hidden: gettext("Hidden")
    };

    function levelOf(select) {
      return select.value.split(":")[2] || "";
    }

    function pendingFields() {
      return selects.filter(function (select) {
        return !select.disabled && levelOf(select) !== select.getAttribute("data-base");
      });
    }

    function markField(select) {
      var item = select.closest(".v-roles-field");
      if (item) {
        if (!select.disabled && levelOf(select) !== select.getAttribute("data-base")) {
          item.setAttribute("data-pending", "field");
        } else {
          item.removeAttribute("data-pending");
        }
      }
      var details = select.closest("[data-roles-fields]");
      if (!details) return;
      var items = details.querySelectorAll(".v-roles-field");
      var restricted = 0;
      var moved = false;
      each(items, function (li) {
        var own = li.querySelector("select");
        if ((own && levelOf(own)) || li.hasAttribute("data-floor")) restricted++;
        if (li.hasAttribute("data-pending")) moved = true;
      });
      var label = details.querySelector("[data-roles-fields-label]");
      if (label) {
        label.textContent = restricted
          ? interpolate(ngettext("%(count)s restricted", "%(count)s restricted", restricted), { count: restricted }, true)
          : gettext("All fields");
      }
      if (moved) details.setAttribute("data-pending", "");
      else details.removeAttribute("data-pending");
    }

    /* ── pending diff ──────────────────────────────────────────────────────── */

    function pending() {
      var added = [];
      var removed = [];
      perms.forEach(function (input) {
        if (!editable(input)) return;
        if (input.checked && !isBase(input)) added.push(input);
        else if (!input.checked && isBase(input)) removed.push(input);
      });
      return { added: added, removed: removed };
    }

    function markCell(input) {
      var cell = cellOf(input);
      if (!cell) return;
      if (input.checked !== isBase(input) && editable(input)) {
        cell.setAttribute("data-pending", input.checked ? "add" : "remove");
      } else {
        cell.removeAttribute("data-pending");
      }
    }

    function renderDiff() {
      var diff = pending();
      var a = diff.added.length;
      var r = diff.removed.length;
      var f = pendingFields().length;
      if (diffOut) {
        diffOut.textContent = "";
        if (!a && !r && !f) {
          diffOut.textContent = gettext("No changes");
        } else {
          var plus = document.createElement("span");
          plus.setAttribute("data-tone", "success");
          plus.textContent = "+" + a;
          var minus = document.createElement("span");
          minus.setAttribute("data-tone", "destructive");
          minus.textContent = "−" + r;
          diffOut.appendChild(plus);
          diffOut.appendChild(document.createTextNode(" / "));
          diffOut.appendChild(minus);
          var sr = document.createElement("span");
          sr.className = "v-sr-only";
          sr.textContent =
            " " +
            interpolate(gettext("%(added)s to grant, %(removed)s to revoke"), { added: a, removed: r }, true);
          diffOut.appendChild(sr);
          if (f) {
            diffOut.appendChild(
              document.createTextNode(
                " · " + interpolate(ngettext("%(count)s field rule", "%(count)s field rules", f), { count: f }, true)
              )
            );
          }
        }
      }
      if (resetBtn) resetBtn.disabled = !a && !r && !f;
    }

    /* ── tri-state headers ─────────────────────────────────────────────────── */

    function setTri(box, members) {
      var live = members.filter(editable);
      var on = live.filter(function (i) { return i.checked; }).length;
      box.disabled = !live.length;
      box.checked = live.length > 0 && on === live.length;
      box.indeterminate = on > 0 && on < live.length;
    }

    function rowsOf(scopeEl) {
      return Array.prototype.slice.call(scopeEl.querySelectorAll("[data-roles-row]"));
    }

    function visiblePerms(rows, action) {
      var out = [];
      rows.forEach(function (row) {
        if (!rowVisible(row)) return;
        each(row.querySelectorAll("input.v-roles-perm"), function (input) {
          if (!action || input.getAttribute("data-action") === action) out.push(input);
        });
      });
      return out;
    }

    var allRows = rowsOf(grid);

    function membersOf(box) {
      var scope = box.getAttribute("data-roles-scope");
      if (scope === "row") {
        return Array.prototype.slice.call(box.closest("tr").querySelectorAll("input.v-roles-perm"));
      }
      if (scope === "app") {
        var head = box.closest("tbody");
        var rowsBody = grid.querySelector('[data-roles-rows="' + head.getAttribute("data-roles-section") + '"]');
        return visiblePerms(rowsOf(rowsBody));
      }
      if (scope === "col") return visiblePerms(allRows, box.getAttribute("data-action"));
      return visiblePerms(allRows);
    }

    var tris = Array.prototype.slice.call(grid.querySelectorAll("input.v-roles-tri"));
    var countEls = grid.querySelectorAll("[data-roles-count]");

    function refresh() {
      tris.forEach(function (box) {
        setTri(box, membersOf(box));
      });
      each(countEls, function (el) {
        var head = el.closest("tbody");
        var rowsBody = grid.querySelector('[data-roles-rows="' + head.getAttribute("data-roles-section") + '"]');
        var inputs = rowsBody.querySelectorAll("input.v-roles-perm");
        var on = 0;
        each(inputs, function (i) { if (i.checked) on++; });
        el.textContent = on + " / " + inputs.length;
      });
      renderDiff();
    }

    tris.forEach(function (box) {
      box.addEventListener("change", function () {
        var target = box.checked;
        membersOf(box).forEach(function (input) {
          if (!editable(input)) return;
          input.checked = target;
          markCell(input);
        });
        refresh();
      });
    });

    perms.forEach(function (input) {
      markCell(input);
      input.addEventListener("change", function () {
        markCell(input);
        refresh();
      });
    });

    selects.forEach(function (select) {
      markField(select);
      select.addEventListener("change", function () {
        markField(select);
        renderDiff();
      });
    });

    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        perms.forEach(function (input) {
          if (!editable(input)) return;
          input.checked = isBase(input);
          markCell(input);
        });
        selects.forEach(function (select) {
          if (select.disabled) return;
          select.value = select.getAttribute("data-key") + ":" + select.getAttribute("data-base");
          markField(select);
        });
        refresh();
      });
    }

    /* ── filter, other models, folding ─────────────────────────────────────── */

    function applyFilter() {
      var term = filter ? filter.value.trim().toLowerCase() : "";
      var hiding = hideOther && hideOther.checked;
      var shown = 0;
      each(grid.querySelectorAll("[data-roles-rows]"), function (body) {
        var section = body.getAttribute("data-roles-rows");
        var head = grid.querySelector('[data-roles-section="' + section + '"]');
        var sectionHidden = hiding && body.hasAttribute("data-roles-unregistered");
        var any = false;
        rowsOf(body).forEach(function (row) {
          var match = !sectionHidden && (!term || row.getAttribute("data-search").indexOf(term) !== -1);
          row.hidden = !match;
          if (match) any = true;
        });
        head.hidden = !any;
        if (!any) body.setAttribute("data-roles-filtered-out", "");
        else body.removeAttribute("data-roles-filtered-out");
        // A search opens every section with a match: a hit inside a folded section is
        // not a hit anyone can see.
        if (term && any) setOpen(head, true);
        if (any) shown++;
      });
      if (noMatch) noMatch.hidden = shown > 0;
      refresh();
    }

    function setOpen(head, open) {
      var btn = head.querySelector("[data-roles-disclose]");
      if (!btn) return;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      var body = document.getElementById(btn.getAttribute("aria-controls"));
      if (body) body.hidden = !open;
    }

    each(grid.querySelectorAll("[data-roles-disclose]"), function (btn) {
      btn.addEventListener("click", function () {
        setOpen(btn.closest("tbody"), btn.getAttribute("aria-expanded") !== "true");
      });
    });

    function foldAll(open) {
      each(grid.querySelectorAll("[data-roles-section]"), function (head) {
        setOpen(head, open);
      });
    }

    var expand = form.querySelector("[data-roles-expand]");
    var collapse = form.querySelector("[data-roles-collapse]");
    if (expand) expand.addEventListener("click", function () { foldAll(true); });
    if (collapse) collapse.addEventListener("click", function () { foldAll(false); });

    if (filter) {
      filter.addEventListener("input", applyFilter);
      // Enter in the filter must not submit the matrix.
      filter.addEventListener("keydown", function (event) {
        if (event.key === "Enter") event.preventDefault();
      });
    }
    if (hideOther) {
      try {
        hideOther.checked = window.localStorage.getItem(STORAGE_KEY) === "1";
      } catch (e) { /* storage blocked: default to showing everything */ }
      hideOther.addEventListener("change", function () {
        try {
          window.localStorage.setItem(STORAGE_KEY, hideOther.checked ? "1" : "0");
        } catch (e) { /* not worth failing over */ }
        applyFilter();
      });
    }

    /* ── keyboard movement ─────────────────────────────────────────────────── */

    function focusables(row) {
      return Array.prototype.slice.call(row.querySelectorAll("input[type=checkbox]")).filter(function (i) {
        return i.offsetParent !== null;
      });
    }

    function columnIndex(input) {
      var cell = input.closest("td, th");
      return Array.prototype.indexOf.call(cell.parentNode.children, cell);
    }

    function navigableRows() {
      return Array.prototype.slice.call(grid.querySelectorAll("tr")).filter(function (row) {
        return row.offsetParent !== null && focusables(row).length;
      });
    }

    grid.addEventListener("keydown", function (event) {
      var input = event.target;
      if (!input.matches || !input.matches("input[type=checkbox]")) return;
      var key = event.key;
      if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].indexOf(key) === -1) return;
      var row = input.closest("tr");
      var inRow = focusables(row);
      var at = inRow.indexOf(input);
      var rtl = document.documentElement.getAttribute("dir") === "rtl";
      var next = null;
      if (key === "ArrowLeft" || key === "ArrowRight") {
        var forward = (key === "ArrowRight") !== rtl;
        next = inRow[at + (forward ? 1 : -1)];
      } else if (key === "Home") {
        next = inRow[0];
      } else if (key === "End") {
        next = inRow[inRow.length - 1];
      } else {
        var rows = navigableRows();
        var r = rows.indexOf(row) + (key === "ArrowDown" ? 1 : -1);
        var target = rows[r];
        if (target) {
          var col = columnIndex(input);
          var candidates = focusables(target);
          next = candidates.filter(function (c) { return columnIndex(c) === col; })[0];
          if (!next) {
            // No box in that column on the next row (a model without, say, "add"):
            // land on the nearest one rather than refusing to move.
            next = candidates.reduce(function (best, c) {
              var d = Math.abs(columnIndex(c) - col);
              return !best || d < best.d ? { el: c, d: d } : best;
            }, null);
            next = next && next.el;
          }
        }
      }
      if (next) {
        event.preventDefault();
        next.focus();
      }
    });

    /* ── review and save ───────────────────────────────────────────────────── */

    function listItems(ul, inputs) {
      ul.textContent = "";
      inputs.forEach(function (input) {
        var li = document.createElement("li");
        li.textContent = input.getAttribute("data-name");
        var code = document.createElement("code");
        code.textContent = input.getAttribute("data-label");
        li.appendChild(code);
        ul.appendChild(li);
      });
      if (!inputs.length) {
        var none = document.createElement("li");
        none.className = "v-muted";
        none.textContent = gettext("Nothing");
        ul.appendChild(none);
      }
    }

    function fieldItems(ul, changed) {
      ul.textContent = "";
      changed.forEach(function (select) {
        var li = document.createElement("li");
        li.textContent =
          select.getAttribute("data-name") +
          ": " +
          LEVELS[select.getAttribute("data-base")] +
          " → " +
          LEVELS[levelOf(select)];
        var code = document.createElement("code");
        code.textContent = select.getAttribute("data-label");
        li.appendChild(code);
        ul.appendChild(li);
      });
    }

    function submitDiff(diff) {
      submitting = true;
      var ids = function (inputs) {
        return inputs.map(function (i) { return i.value; }).join(",");
      };
      var fields = pendingFields().map(function (select) { return select.value; }).join(",");
      [["mode", "diff"], ["add", ids(diff.added)], ["remove", ids(diff.removed)], ["fields", fields]].forEach(function (pair) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = pair[0];
        hidden.value = pair[1];
        form.appendChild(hidden);
      });
      each(form.querySelectorAll('[name="perm"], [name="field"]'), function (input) {
        input.disabled = true;
      });
      form.submit();
    }

    if (reviewBtn && dialog && typeof dialog.showModal === "function") {
      form.addEventListener("submit", function (event) {
        if (submitting) return;
        event.preventDefault();
        var diff = pending();
        var changed = pendingFields();
        // Nothing pending: there is nothing to review, and nothing to send.
        if (!diff.added.length && !diff.removed.length && !changed.length) return;
        listItems(dialog.querySelector("[data-roles-added]"), diff.added);
        listItems(dialog.querySelector("[data-roles-removed]"), diff.removed);
        var fieldSection = dialog.querySelector("[data-roles-fields-section]");
        if (fieldSection) {
          fieldSection.hidden = !changed.length;
          fieldItems(dialog.querySelector("[data-roles-fields-changed]"), changed);
          dialog.querySelector("[data-roles-fields-count]").textContent = "(" + changed.length + ")";
        }
        dialog.querySelector("[data-roles-added-count]").textContent = "(" + diff.added.length + ")";
        dialog.querySelector("[data-roles-removed-count]").textContent = "(" + diff.removed.length + ")";
        dialog.querySelector("[data-roles-review-summary]").textContent = interpolate(
          gettext("%(added)s to grant, %(removed)s to revoke. Saved in one step, or not at all."),
          { added: diff.added.length, removed: diff.removed.length },
          true
        );
        dialog.showModal();
        dialog.querySelector("[data-roles-confirm]").focus();
      });
      dialog.querySelector("[data-roles-cancel]").addEventListener("click", function () {
        dialog.close();
      });
      dialog.querySelector("[data-roles-confirm]").addEventListener("click", function () {
        dialog.close();
        submitDiff(pending());
      });
    }

    // Leaving with unsaved grants is the mistake worth one prompt.
    window.addEventListener("beforeunload", function (event) {
      if (submitting) return;
      var diff = pending();
      if (diff.added.length || diff.removed.length || pendingFields().length) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    applyFilter();
  }

  function boot() {
    each(document.querySelectorAll("[data-roles-matrix]"), enhance);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
