/* The data grid: keyboard navigation, density, columns and the quick-peek drawer.
 *
 * Dependency-free, like the rest of the runtime, and deliberately additive: the table
 * it enhances is a complete, working table before this file loads. Nothing here is
 * required to read a record, select rows or change a page.
 *
 * Three things are worth knowing before reading further.
 *
 * 1. Focus is on the ROW, never the cell. That is a forced choice, not a preference:
 *    a cell-focused grid has to let you type into a cell, which consumes the entire
 *    single-letter namespace and makes mnemonic shortcuts impossible. Row focus buys
 *    j/k/x/Space at the cost of never being a spreadsheet.
 *
 * 2. Every unmodified single-character shortcut is bound to the TABLE element, not to
 *    the document. That is WCAG 2.1.4 exemption 3 ("active only when the component
 *    has focus") and it is a structural guarantee. A document listener that checks
 *    activeElement.tagName is an approximation of the same idea that breaks the first
 *    time someone puts a rich-text editor on the page. The on/off preference and the
 *    ? cheat sheet cover the criterion's other two routes.
 *
 * 3. The URL owns "which rows"; localStorage owns "how they look". Nothing in here
 *    ever writes a column into the query string, and nothing in here reads row state
 *    out of localStorage. The one exception is the peek drawer, which puts a record
 *    id into the URL — and carries the list's own query string along with it, because
 *    losing the filters on the way into a drawer is the classic bug in this pattern.
 */
(function () {
  "use strict";

  var COLUMNS_KEY = "vantage-grid-columns:"; // + model slug
  var DENSITY_KEY = "vantage-grid-density";
  var SHORTCUTS_KEY = "vantage-grid-shortcuts";
  var LOADER_DELAY_MS = 200; // below this a spinner is noise; above it, silence looks broken

  var root = document.querySelector("[data-grid]");
  if (!root) return;
  var table = root.querySelector("table.v-table");
  if (!table) return;

  var model = root.getAttribute("data-grid-model") || "";
  var live = root.querySelector("[data-grid-live]");

  // Captured before anything pushes a record onto the history stack: after that,
  // location.search is the drawer's URL, not the list's.
  var listQuery = window.location.search;

  // ── storage ───────────────────────────────────────────────────────────────

  function readStore(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
      // Safari private mode throws, and a hand-edited value can be unparseable.
      // Either way the defaults are a working grid.
      return fallback;
    }
  }

  function writeStore(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      /* The preference is applied in the DOM already; persisting it is a nicety. */
    }
  }

  function announce(text) {
    if (live) live.textContent = text;
  }

  // ── columns ───────────────────────────────────────────────────────────────

  var columns = Array.prototype.map.call(
    table.querySelectorAll("thead th[data-col-name]"),
    function (th) {
      var index = th.getAttribute("data-col-index");
      return {
        name: th.getAttribute("data-col-name"),
        label: th.getAttribute("data-col-label") || th.textContent.trim(),
        index: index,
        th: th,
        cells: Array.prototype.slice.call(
          table.querySelectorAll('[data-col-index="' + index + '"]')
        ),
      };
    }
  );

  var columnByName = {};
  columns.forEach(function (column) {
    columnByName[column.name] = column;
  });

  var prefs = readStore(COLUMNS_KEY + model, {}) || {};
  var order = resolveOrder(prefs.order);
  var hidden = resolveHidden(prefs.hidden);

  function resolveOrder(stored) {
    var out = [];
    (stored || []).forEach(function (name) {
      // Whitelisted against the columns the server actually rendered. A stored blob
      // is user-writable; trusting it to choose columns would be an information
      // disclosure bug the day column state moves to the server.
      if (columnByName[name] && out.indexOf(name) === -1) out.push(name);
    });
    // Keyed on the field name, never the index, and anything new appends rather than
    // vanishing — otherwise adding a field to list_display would be invisible to
    // every user who has ever opened this page.
    columns.forEach(function (column) {
      if (out.indexOf(column.name) === -1) out.push(column.name);
    });
    return out;
  }

  function resolveHidden(stored) {
    var out = {};
    (stored || []).forEach(function (name) {
      if (columnByName[name]) out[name] = true;
    });
    // A table with no columns is not a state worth restoring.
    if (Object.keys(out).length >= columns.length) return {};
    return out;
  }

  function isVisible(name) {
    return !hidden[name];
  }

  function visibleNames() {
    return order.filter(isVisible);
  }

  function persistColumns() {
    writeStore(COLUMNS_KEY + model, { order: order, hidden: Object.keys(hidden) });
  }

  function applyVisibility() {
    columns.forEach(function (column) {
      var hide = !isVisible(column.name);
      column.cells.forEach(function (cell) {
        cell.hidden = hide;
      });
    });
  }

  function applyOrder() {
    // appendChild moves a cell rather than copying it, so walking the order and
    // re-appending is a full reorder — and the selection cell, which has no
    // data-col-index, stays put at the start of every row.
    Array.prototype.forEach.call(table.rows, function (row) {
      order.forEach(function (name) {
        var cell = row.querySelector(':scope > [data-col-index="' + columnByName[name].index + '"]');
        if (cell) row.appendChild(cell);
      });
    });
  }

  // ── columns popover ───────────────────────────────────────────────────────

  var columnsWrap = root.querySelector("[data-columns]");
  var columnsToggle = root.querySelector("[data-columns-toggle]");
  var columnsPanel = document.getElementById("v-grid-columns-panel");
  var columnsList = root.querySelector("[data-columns-list]");
  var columnsCount = root.querySelector("[data-columns-count]");
  var columnsSearch = root.querySelector("[data-columns-search]");

  function prettify(name) {
    var words = name.replace(/_/g, " ").trim();
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function groupColumns() {
    // Own fields first, then one section per relation. Thirty-one columns in one
    // flat list is a scrolling exercise; grouped, it is a glance.
    var own = { label: "Fields", items: [] };
    var groups = [own];
    var byPrefix = {};

    order.forEach(function (name) {
      var column = columnByName[name];
      var sep = name.indexOf("__");
      if (sep === -1) {
        own.items.push(column);
        return;
      }
      var prefix = name.slice(0, sep);
      if (!byPrefix[prefix]) {
        byPrefix[prefix] = { label: prettify(prefix), items: [] };
        groups.push(byPrefix[prefix]);
      }
      byPrefix[prefix].items.push(column);
    });

    return groups.filter(function (group) {
      return group.items.length > 0;
    });
  }

  function paintCount() {
    if (columnsCount) {
      columnsCount.textContent = "· " + visibleNames().length + " of " + columns.length;
    }
  }

  function moveTarget(name, delta) {
    // Step over hidden columns: swapping with something invisible changes the stored
    // order and nothing on screen, which reads as a broken button.
    var from = order.indexOf(name);
    var to = from + delta;
    while (to >= 0 && to < order.length && !isVisible(order[to])) to += delta;
    return to >= 0 && to < order.length ? to : -1;
  }

  function moveColumn(name, delta, direction) {
    var to = moveTarget(name, delta);
    if (to === -1) return;
    order.splice(order.indexOf(name), 1);
    order.splice(to, 0, name);
    persistColumns();
    applyOrder();
    recalcPinOffsets(); // a reorder can change which pinned column is now first
    renderColumns();

    var label = columnByName[name].label;
    var visible = visibleNames();
    announce(label + " moved to position " + (visible.indexOf(name) + 1) + " of " + visible.length);

    // The list re-rendered under the user's fingers; put focus back on the button
    // they pressed, or on its sibling if that one just ran out of room.
    var button = columnsList.querySelector(
      '[data-col-move="' + direction + '"][data-col="' + name + '"]'
    );
    if (button && button.disabled) {
      button = columnsList.querySelector(
        '[data-col-move="' + (direction === "up" ? "down" : "up") + '"][data-col="' + name + '"]'
      );
    }
    if (button) button.focus();
  }

  function moveButton(column, direction) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "v-grid-col-move";
    button.setAttribute("data-col-move", direction);
    button.setAttribute("data-col", column.name);
    button.textContent = direction === "up" ? "↑" : "↓";
    button.setAttribute(
      "aria-label",
      "Move " + column.label + (direction === "up" ? " earlier" : " later")
    );
    // Drag is not the only way to reorder — WCAG 2.5.7 is failed by drag-only, and a
    // keyboard path does not answer it, because the criterion is about pointer users
    // without the dexterity to drag. These buttons are the mechanism, not a fallback.
    button.disabled = !isVisible(column.name) || moveTarget(column.name, direction === "up" ? -1 : 1) === -1;
    button.addEventListener("click", function () {
      moveColumn(column.name, direction === "up" ? -1 : 1, direction);
    });
    return button;
  }

  function renderColumns() {
    if (!columnsList) return;
    var term = columnsSearch ? columnsSearch.value.trim().toLowerCase() : "";
    var visible = visibleNames();
    columnsList.textContent = "";
    var shown = 0;

    groupColumns().forEach(function (group) {
      var items = group.items.filter(function (column) {
        return !term || column.label.toLowerCase().indexOf(term) !== -1 ||
          column.name.toLowerCase().indexOf(term) !== -1;
      });
      if (!items.length) return;

      var heading = document.createElement("div");
      heading.className = "v-grid-col-group";
      heading.textContent = group.label;
      columnsList.appendChild(heading);

      items.forEach(function (column) {
        shown += 1;
        var item = document.createElement("div");
        item.className = "v-grid-col-item";

        var label = document.createElement("label");
        label.className = "v-grid-col-label";

        var box = document.createElement("input");
        box.type = "checkbox";
        box.checked = isVisible(column.name);
        box.setAttribute("data-col-visible", column.name);
        box.addEventListener("change", function () {
          toggleColumn(column, box);
        });

        var text = document.createElement("span");
        text.textContent = column.label;
        label.appendChild(box);
        label.appendChild(text);

        var position = document.createElement("span");
        position.className = "v-grid-col-pos";
        position.setAttribute("aria-hidden", "true");
        position.textContent = isVisible(column.name) ? String(visible.indexOf(column.name) + 1) : "—";

        item.appendChild(label);
        item.appendChild(position);
        item.appendChild(moveButton(column, "up"));
        item.appendChild(moveButton(column, "down"));
        columnsList.appendChild(item);
      });
    });

    if (!shown) {
      var empty = document.createElement("p");
      empty.className = "v-grid-col-empty";
      empty.textContent = "No column matches “" + term + "”.";
      columnsList.appendChild(empty);
    }
  }

  function toggleColumn(column, box) {
    if (!box.checked && visibleNames().length === 1) {
      // Hiding the last column leaves a table of nothing, which looks like a bug
      // rather than a preference.
      box.checked = true;
      announce("At least one column has to stay visible.");
      return;
    }
    if (box.checked) delete hidden[column.name];
    else hidden[column.name] = true;

    persistColumns();
    applyVisibility();
    recalcPinOffsets(); // hiding a column can move a pinned one into first place
    paintCount();
    renderColumns();
    announce(column.label + (box.checked ? " shown" : " hidden") + ". " + visibleNames().length +
      " of " + columns.length + " columns visible.");
  }

  function setColumnsOpen(open) {
    if (!columnsPanel || !columnsToggle) return;
    columnsPanel.hidden = !open;
    columnsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && columnsSearch) columnsSearch.focus();
  }

  if (columnsToggle && columnsPanel) {
    columnsToggle.addEventListener("click", function () {
      setColumnsOpen(columnsPanel.hidden);
    });

    // Light dismiss. A click listener on the document is not a WCAG 2.1.4 concern —
    // that criterion is about character keys — and it is the only way to catch a
    // click that lands somewhere this component does not own.
    //
    // Capture phase, and that is load-bearing: toggling a column or moving one
    // rebuilds this list, so by the time a bubbled click reached the document its
    // target would be a detached node, contains() would say false, and the popover
    // would slam shut on every click inside itself.
    document.addEventListener(
      "click",
      function (event) {
        if (columnsPanel.hidden) return;
        if (columnsWrap && !columnsWrap.contains(event.target)) setColumnsOpen(false);
      },
      true
    );

    columnsPanel.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setColumnsOpen(false);
      columnsToggle.focus(); // Esc goes back one level, and takes focus with it
    });
  }

  if (columnsSearch) {
    columnsSearch.addEventListener("input", renderColumns);
  }

  var columnsReset = root.querySelector("[data-columns-reset]");
  if (columnsReset) {
    columnsReset.addEventListener("click", function () {
      hidden = {};
      order = resolveOrder(null);
      persistColumns();
      applyVisibility();
      applyOrder();
      recalcPinOffsets();
      paintCount();
      renderColumns();
      announce("Columns reset to the panel's own order.");
    });
  }

  // ── density ───────────────────────────────────────────────────────────────

  function applyDensity(density) {
    root.setAttribute("data-density", density);
    root.querySelectorAll("[data-density-option]").forEach(function (button) {
      button.setAttribute(
        "aria-pressed",
        button.getAttribute("data-density-option") === density ? "true" : "false"
      );
    });
  }

  root.querySelectorAll("[data-density-option]").forEach(function (button) {
    button.addEventListener("click", function () {
      var density = button.getAttribute("data-density-option");
      applyDensity(density);
      // Density is stored per user, not per model: nobody wants comfortable orders
      // and compact products. Column choices are per model, because they are about
      // the fields of one thing.
      writeStore(DENSITY_KEY, density);
      announce(density === "compact" ? "Compact rows" : "Comfortable rows");
    });
  });

  // ── keyboard preference and cheat sheet ───────────────────────────────────

  var shortcutsOn = readStore(SHORTCUTS_KEY, true) !== false;
  var shortcutsBox = root.querySelector("[data-shortcuts-toggle]");
  var sheet = root.querySelector("[data-cheatsheet]");

  if (shortcutsBox) {
    shortcutsBox.checked = shortcutsOn;
    shortcutsBox.addEventListener("change", function () {
      shortcutsOn = shortcutsBox.checked;
      writeStore(SHORTCUTS_KEY, shortcutsOn);
      announce(
        shortcutsOn
          ? "Keyboard shortcuts on. Press ? for the list."
          : "Keyboard shortcuts off. Arrow keys, Home, End and Enter still work."
      );
    });
  }

  function openSheet() {
    // <dialog>.showModal() returns focus to the opener on close, so Esc lands the
    // user back on the row or the button they came from with no bookkeeping here.
    if (sheet && typeof sheet.showModal === "function" && !sheet.open) sheet.showModal();
  }

  root.querySelectorAll("[data-cheatsheet-open]").forEach(function (button) {
    button.addEventListener("click", openSheet);
  });
  root.querySelectorAll("[data-cheatsheet-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (sheet) sheet.close();
    });
  });

  // ── row navigation ────────────────────────────────────────────────────────

  var rows = Array.prototype.slice.call(table.querySelectorAll("[data-grid-row]"));
  var dataRows = rows.filter(function (row) {
    return !row.hasAttribute("data-grid-head");
  });
  var firstData = rows.indexOf(dataRows[0]);
  var current = Math.max(firstData, 0);

  function focusIndex(index, quiet) {
    if (index < 0 || index >= rows.length) return;
    rows.forEach(function (row) {
      row.setAttribute("tabindex", "-1");
      row.removeAttribute("data-current");
    });
    current = index;
    var row = rows[index];
    row.setAttribute("tabindex", "0"); // roving: exactly one tab stop, always
    if (!row.hasAttribute("data-grid-head")) row.setAttribute("data-current", "");
    if (!quiet) row.focus();
  }

  function step(delta) {
    var next = current + delta;
    if (next < 0 || next >= rows.length) return;
    focusIndex(next);
    // Arrowing with the drawer open walks the records rather than leaving a stale
    // preview behind. replaceState, so forty rows do not become forty history entries.
    if (drawerOpen && rows[next].getAttribute("data-url")) showPeek(rows[next], "replace", false);
  }

  function controlsIn(row) {
    return Array.prototype.filter.call(
      row.querySelectorAll("a[href], input, button, select"),
      function (element) {
        // offsetParent is null for anything in a hidden column, which is exactly
        // what should be skipped.
        return !element.disabled && element.offsetParent !== null;
      }
    );
  }

  function enterRow(row, from) {
    var controls = controlsIn(row);
    if (!controls.length) return;
    controls[from === -1 ? controls.length - 1 : 0].focus();
  }

  function moveWithinRow(row, delta) {
    var controls = controlsIn(row);
    var at = controls.indexOf(document.activeElement);
    var next = at + delta;
    if (next < 0 || next >= controls.length) {
      focusIndex(rows.indexOf(row)); // walked off the end of the row: back to the row
      return;
    }
    controls[next].focus();
  }

  function toggleSelection(row) {
    var box = row.querySelector("[data-select-row]");
    if (!box) return;
    box.checked = !box.checked;
    // vantage.js owns the selection model — page checkbox, select-all-matching, the
    // exclusion set. Flipping the input and firing its change event runs all of that
    // rather than reimplementing any of it here.
    box.dispatchEvent(new Event("change", { bubbles: true }));
    announce(box.checked ? "Row selected" : "Row deselected");
  }

  function openRecord(row) {
    var url = row.getAttribute("data-url");
    if (url) window.location.assign(url);
  }

  function turnPage(attribute, edge) {
    var url = root.getAttribute(attribute);
    if (!url) {
      announce(edge);
      return;
    }
    window.location.assign(url);
  }

  // Keeps the roving tabindex honest when focus arrives by Tab or by mouse instead of
  // by arrow key. It is also what paints the current-row marker for the first time:
  // marking a row before the grid has ever been touched would read as a selection.
  table.addEventListener("focusin", function (event) {
    var row = event.target.closest ? event.target.closest("[data-grid-row]") : null;
    if (!row) return;
    var index = rows.indexOf(row);
    if (index !== -1) focusIndex(index, true);
  });

  table.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    var target = event.target;
    var row = target.closest ? target.closest("[data-grid-row]") : null;
    if (!row) return;

    // Belt and braces. The listener is scoped to the table, which is the compliance
    // mechanism; this is here so that a future inline editor inside a cell can
    // contain the letter x without the grid stealing it.
    var typing =
      (target.tagName === "INPUT" && target.type !== "checkbox") ||
      target.tagName === "TEXTAREA" ||
      target.isContentEditable;
    if (typing) return;

    if (target !== row) {
      // Focus is on a control inside the row: only the keys that move it belong to us.
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        moveWithinRow(row, event.key === "ArrowRight" ? 1 : -1);
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        step(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Escape") {
        event.preventDefault();
        focusIndex(rows.indexOf(row));
      }
      return;
    }

    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        step(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        step(-1);
        return;
      case "ArrowRight":
        event.preventDefault();
        enterRow(row, 0);
        return;
      case "ArrowLeft":
        event.preventDefault();
        enterRow(row, -1);
        return;
      case "Home":
        event.preventDefault();
        focusIndex(firstData);
        return;
      case "End":
        event.preventDefault();
        focusIndex(rows.length - 1);
        return;
      case "Enter":
        event.preventDefault();
        openRecord(row);
        return;
      case "Escape":
        // Esc always goes back one level. Users experimenting with single-key
        // commands need one guaranteed-safe way out, or they stop experimenting.
        if (drawerOpen) {
          event.preventDefault();
          closeDrawer(true);
        }
        return;
    }

    // Everything past this point is a single character key, so it is everything the
    // preference is allowed to switch off. Shift is only ever part of "?".
    if (!shortcutsOn) return;
    if (event.shiftKey && event.key !== "?") return;

    switch (event.key) {
      case "j":
        event.preventDefault();
        step(1);
        break;
      case "k":
        event.preventDefault();
        step(-1);
        break;
      case "x":
        event.preventDefault();
        toggleSelection(row);
        break;
      case " ":
        event.preventDefault(); // otherwise the page scrolls out from under the peek
        showPeek(row, drawerOpen ? "replace" : "push", !drawerOpen);
        break;
      case "[":
        event.preventDefault();
        turnPage("data-prev-url", "This is the first page.");
        break;
      case "]":
        event.preventDefault();
        turnPage("data-next-url", "This is the last page.");
        break;
      case "?":
        event.preventDefault();
        openSheet();
        break;
    }
  });

  // ── quick-peek drawer ─────────────────────────────────────────────────────

  var drawer = root.querySelector("[data-grid-drawer]");
  var drawerBody = root.querySelector("[data-drawer-body]");
  var drawerTitle = root.querySelector("[data-drawer-title]");
  var drawerLink = root.querySelector("[data-drawer-open]");
  var drawerOpen = false;
  // True while the current history entry is the drawer's own, which is what closing
  // has to step off. Set by pushState, kept across replaceState, and set again when
  // the Forward button lands back on a peek entry.
  var ownsEntry = false;
  var peekToken = 0;

  function note(text) {
    var paragraph = document.createElement("p");
    paragraph.className = "v-grid-drawer-note";
    paragraph.textContent = text;
    return paragraph;
  }

  function rowLabel(row) {
    var link = row.querySelector("a.v-cell-link");
    return link ? link.textContent.trim() : "Record " + row.getAttribute("data-pk");
  }

  function showPeek(row, historyMode, focusDrawer) {
    var url = row.getAttribute("data-url");
    if (!drawer || !url) return;

    // The record id and the list's own state travel together. A drawer URL that
    // drops ?status=paid&page=3 is the single most common bug in this pattern:
    // it only shows up when someone copies the link or presses Back.
    var target = url + listQuery;
    var state = { vGridPeek: row.getAttribute("data-pk") };
    if (historyMode === "push") {
      window.history.pushState(state, "", target);
      ownsEntry = true;
    } else if (historyMode === "replace") {
      window.history.replaceState(state, "", target);
    }

    drawerOpen = true;
    drawer.hidden = false;
    drawerTitle.textContent = rowLabel(row);
    if (drawerLink) drawerLink.href = target;
    loadPeek(target);

    // Focus moves in on the first peek only, and nothing is trapped once it does:
    // this is a non-modal dialog, so Tab walks straight out of it again. Re-peeking
    // from the grid deliberately leaves focus on the row — the user is browsing the
    // list and watching the drawer, not working inside it.
    if (focusDrawer) drawer.focus();
  }

  function loadPeek(url) {
    var token = ++peekToken;
    drawerBody.setAttribute("aria-busy", "true");
    var slow = window.setTimeout(function () {
      if (token === peekToken) {
        drawerBody.textContent = "";
        drawerBody.appendChild(note("Loading…"));
      }
    }, LOADER_DELAY_MS);

    window
      .fetch(url, {
        credentials: "same-origin",
        // No server branch reads this yet; it is the seam a future HX-Request-style
        // partial renderer would hang off, and it makes peeks visible in the log.
        headers: { "X-Vantage-Peek": "1" },
      })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.text();
      })
      .then(function (html) {
        window.clearTimeout(slow);
        if (token !== peekToken) return; // a faster arrow key won this race
        var parsed = new DOMParser().parseFromString(html, "text/html");
        var detail = parsed.querySelector(".v-detail");
        var title = parsed.querySelector(".v-page-title");
        drawerBody.textContent = "";
        drawerBody.appendChild(
          detail ? document.importNode(detail, true) : note("This record has no preview.")
        );
        if (title) drawerTitle.textContent = title.textContent.trim();
        drawerBody.setAttribute("aria-busy", "false");
        announce("Previewing " + drawerTitle.textContent);
      })
      .catch(function () {
        window.clearTimeout(slow);
        if (token !== peekToken) return;
        drawerBody.textContent = "";
        drawerBody.appendChild(note("Could not load this record. Open it in full instead."));
        drawerBody.setAttribute("aria-busy", "false");
      });
  }

  function closeDrawer(userInitiated) {
    if (!drawerOpen) return;
    drawerOpen = false;
    peekToken += 1; // abandon anything still in flight
    drawer.hidden = true;
    drawerBody.textContent = "";

    if (userInitiated && ownsEntry) {
      ownsEntry = false;
      window.history.back(); // the list URL comes back with its filters intact
    }
    if (userInitiated) focusIndex(current); // Esc puts you back on the row you left
  }

  function stepPeek(delta) {
    var next = current + delta;
    while (next >= 0 && next < rows.length && !rows[next].getAttribute("data-url")) next += delta;
    if (next < 0 || next >= rows.length) return;
    focusIndex(next, true); // keep focus in the drawer; just move the grid's marker
    showPeek(rows[next], "replace", false);
  }

  if (drawer) {
    drawer.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer(true);
        return;
      }
      var target = event.target;
      if (
        target.tagName === "INPUT" ||
        target.tagName === "TEXTAREA" ||
        target.isContentEditable
      ) {
        return;
      }
      var forward = event.key === "ArrowDown" || (shortcutsOn && event.key === "j");
      var back = event.key === "ArrowUp" || (shortcutsOn && event.key === "k");
      if (forward || back) {
        event.preventDefault();
        stepPeek(forward ? 1 : -1);
      }
    });

    root.querySelectorAll("[data-drawer-close]").forEach(function (button) {
      button.addEventListener("click", function () {
        closeDrawer(true);
      });
    });
    root.querySelectorAll("[data-drawer-next]").forEach(function (button) {
      button.addEventListener("click", function () {
        stepPeek(1);
      });
    });
    root.querySelectorAll("[data-drawer-prev]").forEach(function (button) {
      button.addEventListener("click", function () {
        stepPeek(-1);
      });
    });

    // Back closes the drawer; Forward reopens it. Both arrive here, and neither is
    // allowed to touch the history stack again or the two would fight.
    window.addEventListener("popstate", function (event) {
      var state = event.state;
      if (state && state.vGridPeek) {
        var match = null;
        dataRows.forEach(function (row) {
          if (row.getAttribute("data-pk") === String(state.vGridPeek)) match = row;
        });
        if (match) {
          ownsEntry = true; // Forward landed on a peek entry; Esc should step off it
          focusIndex(rows.indexOf(match), true);
          showPeek(match, "none", !table.contains(document.activeElement));
        }
        return;
      }
      ownsEntry = false;
      closeDrawer(false);
    });
  }

  // ── column resize and pin ────────────────────────────────────────────────
  //
  // Unlike the popover above, none of this is stored in localStorage. A width or a
  // pin is a preference about *this* list for *this* person, saved through the same
  // per-list customisation rail column reorder and hide already go through — see
  // vantage/listprefs.py, section "columns" — so it follows them to another browser
  // and another device, which the popover's ambient client-only state deliberately
  // does not. What lives here is the initial state the server already rendered (read
  // straight off the header cells, never off localStorage), the drag and keyboard
  // interactions, the sticky-offset arithmetic, and a debounced background save to
  // the rail's own form.

  var MIN_COL_WIDTH = parseInt(root.getAttribute("data-col-width-min"), 10) || 60;
  var MAX_COL_WIDTH = parseInt(root.getAttribute("data-col-width-max"), 10) || 640;
  var PERSIST_DEBOUNCE_MS = 500;

  var widths = {}; // column name -> px, only for columns someone has actually sized
  var pinned = {}; // column name -> true
  columns.forEach(function (column) {
    var stored = parseInt(column.th.getAttribute("data-col-width"), 10);
    if (!isNaN(stored)) widths[column.name] = stored;
    if (column.th.hasAttribute("data-col-pinned")) pinned[column.name] = true;
  });

  function clampWidth(px) {
    return Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, Math.round(px)));
  }

  // table-layout: fixed plus the table's own `inline-size: 100%` (vantage.css) means
  // growing one column normally just steals space from its unset siblings — down to
  // nothing, since nothing here has forced them to keep any. `min-width` on a *cell*
  // does not change that; browsers do not consult it under fixed layout. What does is
  // `min-width` on the *table* itself: once that exceeds the wrap's own width, "the
  // width of the table is the greater of the specified width and the sum of the
  // column widths" (CSS Tables §self) takes over, the table grows past its container,
  // and only then does .v-table-wrap's own `overflow-x: auto` (vantage.css) put up a
  // scrollbar instead of a table of invisible columns.
  //
  // Scoped to columns someone has actually touched: a page nobody has resized or
  // pinned keeps exactly the width behaviour it always had, unset column and all.
  function recalcTableMinWidth() {
    if (!Object.keys(widths).length && !Object.keys(pinned).length) {
      table.style.removeProperty("min-inline-size");
      return;
    }
    var selectCell = table.querySelector("thead th.v-select-cell");
    var actionsCell = table.querySelector("thead th.v-row-actions-cell");
    var total = 0;
    if (selectCell) total += selectCell.getBoundingClientRect().width;
    if (actionsCell) total += actionsCell.getBoundingClientRect().width;
    visibleNames().forEach(function (name) {
      total += widths[name] != null ? widths[name] : MIN_COL_WIDTH;
    });
    table.style.minInlineSize = total + "px";
  }

  // The offset a sticky pinned cell needs is the summed *rendered* width of every
  // pinned, visible column before it — not the stored width, which a column with no
  // explicit size does not have at all. Only the browser knows that number, and only
  // after layout has run, which is why this is arithmetic done here in the DOM and
  // not a value computed once in Python and handed down. Cheap enough to redo on every
  // change: a list page has a handful of columns, not thousands.
  //
  // recalcTableMinWidth runs first and is load-bearing here, not just alongside: the
  // "rendered width" this function measures is only meaningful once the table has
  // been allowed to actually grow to fit it.
  function recalcPinOffsets() {
    recalcTableMinWidth();
    var names = order.filter(function (name) {
      return isVisible(name) && pinned[name];
    });
    var offset = 0;
    names.forEach(function (name, position) {
      var column = columnByName[name];
      column.cells.forEach(function (cell) {
        cell.style.setProperty("--v-pin-offset", offset + "px");
        cell.classList.toggle("v-col-pin-edge", position === names.length - 1);
      });
      offset += column.th.getBoundingClientRect().width;
    });
  }

  function applyWidth(name) {
    var px = widths[name];
    columnByName[name].cells.forEach(function (cell) {
      if (px == null) cell.style.removeProperty("inline-size");
      else cell.style.inlineSize = px + "px";
    });
  }

  function applyPinned(name) {
    var isPinned = !!pinned[name];
    columnByName[name].cells.forEach(function (cell) {
      if (isPinned) cell.setAttribute("data-col-pinned", "");
      else {
        cell.removeAttribute("data-col-pinned");
        cell.classList.remove("v-col-pin-edge");
        cell.style.removeProperty("--v-pin-offset");
      }
    });
    var button = table.querySelector('[data-col-pin="' + name + '"]');
    if (button) button.setAttribute("aria-pressed", isPinned ? "true" : "false");
  }

  // ── saving ──────────────────────────────────────────────────────────────

  var persistTimer = null;

  function columnsForm() {
    // Outside `root`: the rail sits in the page-action cluster above .v-grid, not
    // inside it — see the comment on <div class="v-grid"> in list.html.
    return document.querySelector("[data-prefs-columns-form]");
  }

  function syncPrefsForm(name) {
    var form = columnsForm();
    if (!form) return;
    var widthInput = form.querySelector('[data-prefs-width="' + name + '"]');
    if (widthInput) widthInput.value = widths[name] != null ? String(widths[name]) : "";
    var pinInput = form.querySelector('[data-prefs-pin="' + name + '"]');
    if (pinInput) pinInput.checked = !!pinned[name];
  }

  function schedulePersist() {
    window.clearTimeout(persistTimer);
    // Debounced rather than sent from every pointermove or every keypress: a drag is
    // dozens of intermediate widths, and the row this feature lives in (see
    // vantage/listprefs.py) writes the *whole* columns section on every save. Nobody
    // needs forty requests for one resize.
    persistTimer = window.setTimeout(persistNow, PERSIST_DEBOUNCE_MS);
  }

  function persistNow() {
    var form = columnsForm();
    if (!form || !form.action) return;
    var data = new FormData(form);
    // Always the person's own copy. A background save has no dialog to ask
    // "for everyone too?", and a drag is not that decision anyway — see
    // ListPrefsView.scope_user, which is exactly as strict about who may write the
    // site-wide row when this same form is submitted the ordinary way.
    data.set("do", "save:user");
    window.fetch(form.action, { method: "POST", credentials: "same-origin", body: data })
      .catch(function () {
        // Best effort: the change is already live in this tab. A failed background
        // save just means a reload will not remember it, and the drag can be redone.
      });
  }

  function setWidth(name, px, persist) {
    widths[name] = clampWidth(px);
    applyWidth(name);
    recalcPinOffsets();
    syncPrefsForm(name);
    if (persist !== false) schedulePersist();
  }

  function setPinned(name, isPinned, persist) {
    pinned[name] = isPinned;
    applyPinned(name);
    recalcPinOffsets();
    syncPrefsForm(name);
    if (persist !== false) schedulePersist();
    announce(columnByName[name].label + (isPinned ? " pinned." : " unpinned."));
  }

  // ── pin toggle ──────────────────────────────────────────────────────────

  table.querySelectorAll("[data-col-pin]").forEach(function (button) {
    var name = button.getAttribute("data-col-pin");
    button.addEventListener("click", function () {
      setPinned(name, !pinned[name]);
    });
  });

  // ── drag and keyboard resize ───────────────────────────────────────────

  table.querySelectorAll("[data-col-resize]").forEach(function (handle) {
    var name = handle.getAttribute("data-col-resize");
    var drag = null; // { startWidth, startX, pointerId }
    var pendingFrame = null;

    function isRtl() {
      return getComputedStyle(table).direction === "rtl";
    }

    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0 && event.pointerType === "mouse") return;
      // The handle straddles the header text next to it (see the CSS): without this,
      // starting a drag here would also fire the sort link's click a moment later.
      event.preventDefault();
      event.stopPropagation();
      drag = {
        startWidth: columnByName[name].th.getBoundingClientRect().width,
        startX: event.clientX,
        pointerId: event.pointerId,
      };
      handle.setAttribute("data-col-resizing", "");
      handle.setPointerCapture(event.pointerId);
    });

    handle.addEventListener("pointermove", function (event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      var delta = event.clientX - drag.startX;
      if (isRtl()) delta = -delta; // dragging toward the reading direction's end grows it
      if (pendingFrame) return;
      pendingFrame = window.requestAnimationFrame(function () {
        pendingFrame = null;
        if (drag) setWidth(name, drag.startWidth + delta, false);
      });
    });

    function endDrag(event) {
      if (!drag || event.pointerId !== drag.pointerId) return;
      handle.releasePointerCapture(drag.pointerId);
      handle.removeAttribute("data-col-resizing");
      drag = null;
      schedulePersist();
    }
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);

    // The keyboard equivalent WCAG 2.5.7 asks for. Reached the same way every other
    // in-row control is — ArrowRight/ArrowLeft on the focused row — but once it has
    // focus, those same two keys resize instead of moving to the next control. That
    // is a deliberate, narrow exception to the roving-tabindex contract at the top of
    // this file: it is scoped to this one element via stopPropagation, so the
    // table's own keydown handler never sees the keypress and every other control's
    // ArrowRight/ArrowLeft behaviour is untouched. Escape still bubbles, unstopped,
    // so it keeps doing what it already does for any other in-row control: hand
    // focus back to the row.
    handle.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      event.stopPropagation();
      var grows = event.key === (isRtl() ? "ArrowLeft" : "ArrowRight");
      var step = event.shiftKey ? 32 : 8;
      var current = widths[name] != null
        ? widths[name]
        : columnByName[name].th.getBoundingClientRect().width;
      setWidth(name, current + (grows ? step : -step));
    });
  });

  // A column with no explicit width can still change size across a viewport change —
  // table-layout: fixed redistributes the leftover space — and a pinned column after
  // it has to follow. Debounced to one recompute per settled resize, not one per
  // pixel the browser reports along the way.
  var viewportFrame = null;
  window.addEventListener("resize", function () {
    if (viewportFrame) return;
    viewportFrame = window.requestAnimationFrame(function () {
      viewportFrame = null;
      recalcPinOffsets();
    });
  });

  // ── boot ──────────────────────────────────────────────────────────────────

  applyDensity(readStore(DENSITY_KEY, "comfortable") === "compact" ? "compact" : "comfortable");
  applyVisibility();
  applyOrder();
  paintCount();
  renderColumns();
  // The server only marks the <th> — see the comment on data-col-pinned in list.html —
  // so the body and footer cells of an already-pinned column need this to actually
  // stick; recalcPinOffsets alone only positions cells that are pinned already.
  Object.keys(pinned).forEach(applyPinned);
  recalcPinOffsets();
  // No focusIndex() here on purpose: the server already rendered the single tab stop
  // on the first row, and the grid should look untouched until it is.
})();
