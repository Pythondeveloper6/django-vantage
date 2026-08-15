/* The sidebar editor's enhancement layer.
 *
 * Strictly additive, and easy to prove so: the editor is a form of text boxes, selects
 * and number boxes. Delete this file and every group still reorders, every item still
 * moves between groups, every link is still added. What is added here is the pointing
 * device — dragging — plus the two things dragging needs in order not to be a trap:
 *
 * 1. ↑/↓ buttons on every row and every group. WCAG 2.5.7 is failed by drag-only, and a
 *    keyboard path alone does not answer it: the criterion is about pointer users
 *    without the dexterity to drag. These buttons are the mechanism, not a fallback.
 *    A row's ↑/↓ crosses into the neighbouring group at the boundary, so everything
 *    the mouse can do the buttons can do.
 * 2. The group <select> stays. It is the direct way to move an item to a distant group,
 *    and the drag handler *rewrites* it rather than replacing it — so the mouse and the
 *    keyboard put the same thing in the same POST.
 *
 * The number boxes are what the server actually reads. Nothing here invents a second
 * representation of the order; it renumbers the boxes and lets the form go on saying
 * what it always said.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-nav-editor]");
  if (!root) return;

  var container = root.querySelector("[data-nav-groups]");
  if (!container) return;

  function each(collection, callback) {
    Array.prototype.forEach.call(collection, callback);
  }

  function list(collection) {
    return Array.prototype.slice.call(collection);
  }

  // ── announcing what happened ──────────────────────────────────────────────
  //
  // A row that jumps under the cursor explains itself. A row that jumps because a
  // button was pressed does not, unless it is said out loud.

  var live = document.createElement("p");
  live.className = "v-sr-only";
  live.setAttribute("role", "status");
  live.setAttribute("aria-live", "polite");
  root.appendChild(live);

  function announce(message) {
    live.textContent = message;
  }

  function groups() {
    return list(container.querySelectorAll("[data-nav-group]"));
  }

  function itemsIn(group) {
    return list(group.querySelectorAll("[data-nav-item]"));
  }

  function listOf(group) {
    return group.querySelector("[data-nav-items]");
  }

  function nameOf(row) {
    var box = row.querySelector(".v-navedit-name");
    if (box && (box.value || box.placeholder)) return box.value || box.placeholder;
    return gettext("this item");
  }

  function headingOf(group) {
    var box = group.querySelector(".v-navedit-groupname");
    if (box && (box.value || box.placeholder)) return box.value || box.placeholder;
    return gettext("this group");
  }

  // ── keeping the form honest ───────────────────────────────────────────────

  function renumber() {
    groups().forEach(function (group, groupIndex) {
      var position = group.querySelector("[data-nav-group-pos]");
      if (position) position.value = String(groupIndex + 1);

      var rows = itemsIn(group);
      rows.forEach(function (row, index) {
        var box = row.querySelector("[data-nav-pos]");
        if (box) box.value = String(index + 1);
        // The select is the server's record of which group this row is in. A drag that
        // moved the row in the DOM and left the select alone would look right on screen
        // and save the old arrangement.
        var select = row.querySelector("[data-nav-groupsel]");
        if (select) select.value = group.getAttribute("data-uid");
      });

      var empty = group.querySelector(".v-navedit-empty");
      if (empty) empty.hidden = rows.length > 0;
    });
    syncButtons();
  }

  // ── moving with buttons ───────────────────────────────────────────────────

  function moveRow(row, delta) {
    var group = row.closest("[data-nav-group]");
    var rows = itemsIn(group);
    var index = rows.indexOf(row);
    var target = index + delta;

    if (target >= 0 && target < rows.length) {
      if (delta < 0) listOf(group).insertBefore(row, rows[target]);
      else listOf(group).insertBefore(rows[target], row);
      renumber();
      announce(
        interpolate(
          gettext("%(name)s is now %(position)s of %(total)s in %(group)s"),
          { name: nameOf(row), position: target + 1, total: rows.length, group: headingOf(group) },
          true
        )
      );
      focusMove(row, delta);
      return;
    }

    // Off the end of a group and into the next one. Without this the keyboard could
    // reorder within a group while the mouse could do more, which is not equivalence.
    var all = groups();
    var neighbour = all[all.indexOf(group) + delta];
    if (!neighbour) return;

    var into = listOf(neighbour);
    if (delta < 0) into.appendChild(row);
    else into.insertBefore(row, into.firstChild);
    renumber();
    announce(
      interpolate(
        gettext("%(name)s moved to %(group)s"),
        { name: nameOf(row), group: headingOf(neighbour) },
        true
      )
    );
    focusMove(row, delta);
  }

  function moveGroup(group, delta) {
    var all = groups();
    var target = all.indexOf(group) + delta;
    if (target < 0 || target >= all.length) return;
    if (delta < 0) container.insertBefore(group, all[target]);
    else container.insertBefore(all[target], group);
    renumber();
    announce(
      interpolate(
        gettext("%(name)s is now group %(position)s of %(total)s"),
        { name: headingOf(group), position: target + 1, total: all.length },
        true
      )
    );
    focusMove(group, delta);
  }

  function focusMove(element, delta) {
    // Focus follows the thing that moved, or a keyboard user loses their place on every
    // press. When the button they used has just become disabled — the top of the first
    // group, the bottom of the last — the opposite one takes the focus instead.
    var wanted = delta < 0 ? "up" : "down";
    var selector = element.hasAttribute("data-nav-group")
      ? "[data-nav-group-move]"
      : "[data-nav-move]";
    var buttons = list(element.querySelectorAll(selector)).filter(function (candidate) {
      return candidate.closest(
        element.hasAttribute("data-nav-group") ? "[data-nav-group]" : "[data-nav-item]"
      ) === element;
    });
    var preferred = buttons.filter(function (candidate) {
      return candidate.getAttribute(selector.slice(1, -1)) === wanted && !candidate.disabled;
    })[0];
    var fallback = buttons.filter(function (candidate) { return !candidate.disabled; })[0];
    var chosen = preferred || fallback;
    if (chosen) chosen.focus();
  }

  function syncButtons() {
    groups().forEach(function (group, groupIndex, all) {
      var up = group.querySelector('[data-nav-group-move="up"]');
      var down = group.querySelector('[data-nav-group-move="down"]');
      if (up) up.disabled = groupIndex === 0;
      if (down) down.disabled = groupIndex === all.length - 1;

      var rows = itemsIn(group);
      rows.forEach(function (row, index) {
        var rowUp = row.querySelector('[data-nav-move="up"]');
        var rowDown = row.querySelector('[data-nav-move="down"]');
        // Disabled only at the very top of the first group and the very bottom of the
        // last. Everywhere else the press crosses into the neighbouring group.
        if (rowUp) rowUp.disabled = groupIndex === 0 && index === 0;
        if (rowDown) {
          rowDown.disabled = groupIndex === all.length - 1 && index === rows.length - 1;
        }
      });
    });
  }

  function button(label, glyph, attribute, direction, handler) {
    var el = document.createElement("button");
    el.type = "button";
    el.className = "v-navedit-move";
    el.textContent = glyph;
    el.setAttribute(attribute, direction);
    el.setAttribute("aria-label", label);
    el.addEventListener("click", function (event) {
      event.preventDefault();
      handler();
    });
    return el;
  }

  function addPair(host, anchor, attribute, describe, move) {
    var down = button(describe("down"), "↓", attribute, "down", function () { move(1); });
    var up = button(describe("up"), "↑", attribute, "up", function () { move(-1); });
    host.insertBefore(down, anchor);
    host.insertBefore(up, down);
  }

  function addButtons() {
    groups().forEach(function (group) {
      var head = group.querySelector("[data-nav-group-head]");
      if (head && !head.querySelector("[data-nav-group-move]")) {
        var grip = head.querySelector("[data-nav-grip]");
        addPair(
          head,
          grip ? grip.nextSibling : head.firstChild,
          "data-nav-group-move",
          function (way) {
            return way === "down"
              ? interpolate(gettext("Move the group %s down"), [headingOf(group)])
              : interpolate(gettext("Move the group %s up"), [headingOf(group)]);
          },
          function (delta) { moveGroup(group, delta); }
        );
      }

      itemsIn(group).forEach(function (row) {
        var line = row.querySelector(".v-navedit-row");
        if (!line || line.querySelector("[data-nav-move]")) return;
        var grip = line.querySelector("[data-nav-grip]");
        addPair(
          line,
          grip ? grip.nextSibling : line.firstChild,
          "data-nav-move",
          function (way) {
            return way === "down"
              ? interpolate(gettext("Move %s down"), [nameOf(row)])
              : interpolate(gettext("Move %s up"), [nameOf(row)]);
          },
          function (delta) { moveRow(row, delta); }
        );
      });
    });
  }

  // ── dragging ──────────────────────────────────────────────────────────────
  //
  // The grip carries `draggable`, not the row: a draggable row would swallow the text
  // selection in every label box inside it. Items reorder within and between groups,
  // and whole groups reorder among themselves.

  var draggingRow = null;
  var draggingGroup = null;

  function nearest(candidates, y) {
    return candidates.reduce(
      function (closest, element) {
        var box = element.getBoundingClientRect();
        var offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
          return { offset: offset, element: element };
        }
        return closest;
      },
      { offset: Number.NEGATIVE_INFINITY, element: null }
    ).element;
  }

  function startDrag(grip, event, onStart) {
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      // Firefox will not start a drag without payload. Nothing ever reads it back —
      // the element being dragged is held in a variable right here.
      event.dataTransfer.setData("text/plain", grip.getAttribute("data-uid") || "drag");
    }
    onStart();
  }

  function armItems() {
    each(root.querySelectorAll("[data-nav-item] > .v-navedit-row > [data-nav-grip]"),
      function (grip) {
        if (grip.getAttribute("draggable") === "true") return;
        grip.setAttribute("draggable", "true");
        grip.setAttribute("title", gettext("Drag to move, or use the arrows"));
        var row = grip.closest("[data-nav-item]");

        grip.addEventListener("dragstart", function (event) {
          startDrag(grip, event, function () {
            draggingRow = row;
            row.classList.add("is-dragging");
          });
        });

        grip.addEventListener("dragend", function () {
          if (!draggingRow) return;
          draggingRow.classList.remove("is-dragging");
          announce(
            interpolate(
              gettext("%(name)s dropped into %(group)s"),
              {
                name: nameOf(draggingRow),
                group: headingOf(draggingRow.closest("[data-nav-group]")),
              },
              true
            )
          );
          draggingRow = null;
          renumber();
        });
      });

    each(root.querySelectorAll("[data-nav-items]"), function (target) {
      if (target.getAttribute("data-nav-armed") === "1") return;
      target.setAttribute("data-nav-armed", "1");

      target.addEventListener("dragover", function (event) {
        if (!draggingRow) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        var rows = list(target.querySelectorAll("[data-nav-item]")).filter(
          function (row) { return row !== draggingRow; }
        );
        var reference = nearest(rows, event.clientY);
        if (reference) target.insertBefore(draggingRow, reference);
        else target.appendChild(draggingRow);
      });

      target.addEventListener("drop", function (event) {
        if (!draggingRow) return;
        event.preventDefault();
        renumber();
      });
    });
  }

  function armGroups() {
    each(root.querySelectorAll("[data-nav-group-head] > [data-nav-grip]"), function (grip) {
      if (grip.getAttribute("draggable") === "true") return;
      grip.setAttribute("draggable", "true");
      grip.setAttribute("title", gettext("Drag to reorder the group, or use the arrows"));
      var group = grip.closest("[data-nav-group]");

      grip.addEventListener("dragstart", function (event) {
        event.stopPropagation();
        startDrag(grip, event, function () {
          draggingGroup = group;
          group.classList.add("is-dragging");
        });
      });

      grip.addEventListener("dragend", function () {
        if (!draggingGroup) return;
        draggingGroup.classList.remove("is-dragging");
        announce(interpolate(gettext("%s moved"), [headingOf(draggingGroup)]));
        draggingGroup = null;
        renumber();
      });
    });

    if (container.getAttribute("data-nav-armed") === "1") return;
    container.setAttribute("data-nav-armed", "1");

    container.addEventListener("dragover", function (event) {
      if (!draggingGroup) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      var others = groups().filter(function (group) { return group !== draggingGroup; });
      var reference = nearest(others, event.clientY);
      if (reference) container.insertBefore(draggingGroup, reference);
      else container.appendChild(draggingGroup);
    });

    container.addEventListener("drop", function (event) {
      if (!draggingGroup) return;
      event.preventDefault();
      renumber();
    });
  }

  // The select is the keyboard's way across groups, and with the position boxes hidden
  // a row that stayed where it was until the next page load would be the screen telling
  // a lie about what it is going to save.
  function watchGroupSelects() {
    each(root.querySelectorAll("[data-nav-groupsel]"), function (select) {
      select.addEventListener("change", function () {
        var row = select.closest("[data-nav-item]");
        var target = null;
        groups().forEach(function (group) {
          if (group.getAttribute("data-uid") === select.value) target = group;
        });
        if (!target || !row) return;
        listOf(target).appendChild(row);
        renumber();
        announce(
          interpolate(
            gettext("%(name)s moved to %(group)s"),
            { name: nameOf(row), group: headingOf(target) },
            true
          )
        );
        select.focus();
      });
    });
  }

  // Renaming a group has to reach the boxes that name it: the "move to group" select on
  // every row, and the aria-labels of the buttons that put things there.
  function watchHeadings() {
    each(root.querySelectorAll(".v-navedit-groupname"), function (box) {
      box.addEventListener("input", function () {
        var group = box.closest("[data-nav-group]");
        var uid = group.getAttribute("data-uid");
        var heading = headingOf(group);
        each(root.querySelectorAll("[data-nav-groupsel] option"), function (option) {
          if (option.value === uid) option.textContent = heading;
        });
      });
    });
  }

  addButtons();
  armItems();
  armGroups();
  watchGroupSelects();
  watchHeadings();
  renumber();
  root.setAttribute("data-nav-enhanced", "");
})();
