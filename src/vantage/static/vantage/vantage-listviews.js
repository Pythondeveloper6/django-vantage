/* The Kanban board: drag and drop, plus the "Move to…" menu, upgraded to a fetch.
 *
 * Dependency-free and additive, like the grid. The board is complete before this file
 * loads: every card's "Move to…" menu is a real form that posts to the move endpoint
 * and comes back to the board. This file adds two things on top — dragging a card
 * between columns, and doing either kind of move without a page load.
 *
 * Two rules shape the code.
 *
 * 1. Drag is never the only way. HTML5 drag and drop does not fire on touch screens
 *    and cannot be driven from a keyboard, so the menu is not a fallback, it is the
 *    other front door, and both doors end in the same move() below.
 *
 * 2. The move is optimistic, the failure is loud. A card goes to its new column the
 *    moment it is dropped, because a board that waits a round trip before reacting
 *    feels broken. If the server refuses — no permission, a validation rule, somebody
 *    else moved it first — the card goes back where it was, the reason is written on
 *    the card and announced, and nothing pretends the move happened.
 */
(function () {
  "use strict";

  var board = document.querySelector("[data-kanban]");
  if (!board) return;
  var moveUrl = board.getAttribute("data-kanban-move-url");
  if (!moveUrl) return;
  var live = board.querySelector("[data-kanban-live]");

  function csrf() {
    var input = board.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function announce(text) {
    if (live) live.textContent = text;
  }

  function columnOf(node) {
    return node.closest("[data-kanban-col]");
  }

  function columnLabel(column) {
    var title = column.querySelector(".v-board-col-title");
    return title ? title.textContent.trim() : "";
  }

  function cardTitle(card) {
    var title = card.querySelector(".v-board-card-title");
    return title ? title.textContent.trim() : "";
  }

  function setCount(column, delta) {
    var count = column.querySelector("[data-kanban-count]");
    if (!count) return;
    var next = Math.max(0, (parseInt(count.textContent, 10) || 0) + delta);
    count.textContent = String(next);
    count.setAttribute(
      "aria-label",
      interpolate(ngettext("%(count)s record", "%(count)s records", next), { count: next }, true)
    );
    var list = column.querySelector("[data-kanban-cards]");
    var empty = column.querySelector("[data-kanban-empty]");
    if (empty) empty.hidden = !!(list && list.children.length);
  }

  function showError(card, text) {
    var note = card.querySelector(".v-board-error");
    if (!note) {
      note = document.createElement("p");
      note.className = "v-board-error";
      card.appendChild(note);
    }
    note.textContent = text;
  }

  function clearError(card) {
    var note = card.querySelector(".v-board-error");
    if (note) note.remove();
  }

  /* After a move the card's menu must offer every column except the one it is in now.
   * Rebuilt from the columns themselves rather than patched, so it cannot drift. */
  function rebuildMenu(card) {
    var form = card.querySelector("[data-kanban-form]");
    if (!form) return;
    var here = columnOf(card);
    Array.prototype.forEach.call(form.querySelectorAll("button[name=value]"), function (b) {
      b.remove();
    });
    Array.prototype.forEach.call(board.querySelectorAll("[data-kanban-col]"), function (col) {
      if (col === here) return;
      var button = document.createElement("button");
      button.type = "submit";
      button.className = "v-board-move-option";
      button.name = "value";
      button.value = col.getAttribute("data-value");
      button.textContent = columnLabel(col);
      form.appendChild(button);
    });
    var version = form.querySelector("input[name=version]");
    if (version) version.value = card.getAttribute("data-version") || "";
  }

  function describeFailure(status, body) {
    if (status === 403) return gettext("You do not have permission to move this card.");
    if (status === 404) return gettext("This record no longer exists. Reload the page.");
    if (status === 409) {
      return gettext("Somebody else changed this record a moment ago. Reload and try again.");
    }
    if (status === 422 && body && body.errors && body.errors.length) {
      var messages = [];
      body.errors.forEach(function (error) {
        Object.keys(error.fields || {}).forEach(function (name) {
          messages = messages.concat(error.fields[name]);
        });
      });
      if (messages.length) return messages.join(" ");
    }
    return gettext("The card could not be moved.");
  }

  function move(card, target) {
    var source = columnOf(card);
    if (!source || !target || source === target || card.hasAttribute("data-busy")) return;

    var list = target.querySelector("[data-kanban-cards]");
    var home = card.parentNode;
    var after = card.nextSibling;
    var title = cardTitle(card);
    var label = columnLabel(target);

    clearError(card);
    list.insertBefore(card, list.firstChild);
    setCount(source, -1);
    setCount(target, 1);
    card.setAttribute("data-busy", "");

    var value;
    try {
      value = JSON.parse(target.getAttribute("data-value"));
    } catch (e) {
      value = target.getAttribute("data-value");
    }

    function undo(text) {
      home.insertBefore(card, after);
      setCount(target, -1);
      setCount(source, 1);
      card.removeAttribute("data-busy");
      showError(card, text);
      announce(interpolate(gettext("%(card)s was not moved: %(reason)s"), { card: title, reason: text }, true));
    }

    fetch(moveUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf(),
        Accept: "application/json",
      },
      body: JSON.stringify({
        pk: card.getAttribute("data-pk"),
        value: value,
        version: card.getAttribute("data-version") || null,
        field: board.getAttribute("data-kanban-field") || null,
      }),
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            return { status: response.status, body: body };
          });
      })
      .then(function (result) {
        if (result.status === 200 && result.body && result.body.ok) {
          card.removeAttribute("data-busy");
          card.setAttribute("data-version", result.body.version || "");
          rebuildMenu(card);
          announce(interpolate(gettext("Moved %(card)s to %(column)s."), { card: title, column: label }, true));
          return;
        }
        undo(describeFailure(result.status, result.body));
      })
      .catch(function () {
        undo(gettext("The connection failed. The card was not moved."));
      });
  }

  // ── the menu ──────────────────────────────────────────────────────────────

  board.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-kanban-form]");
    if (!form) return;
    var button = event.submitter;
    if (!button || button.name !== "value") return;
    event.preventDefault();
    var card = form.closest("[data-kanban-card]");
    var key = button.value;
    var target = Array.prototype.find.call(
      board.querySelectorAll("[data-kanban-col]"),
      function (col) {
        return col.getAttribute("data-value") === key;
      }
    );
    var menu = form.closest("details");
    if (menu) menu.open = false;
    move(card, target);
    // Focus follows the card, so a keyboard user is not dropped back at the top of
    // the page with no idea where the card they just moved has gone.
    var link = card.querySelector(".v-board-card-title");
    if (link) link.focus();
  });

  // Escape closes an open menu and returns focus to its trigger.
  board.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var menu = event.target.closest("details[data-kanban-menu]");
    if (!menu || !menu.open) return;
    menu.open = false;
    var summary = menu.querySelector("summary");
    if (summary) summary.focus();
  });

  // ── drag and drop ─────────────────────────────────────────────────────────

  var dragged = null;

  board.addEventListener("dragstart", function (event) {
    var card = event.target.closest && event.target.closest("[data-kanban-card]");
    if (!card) return;
    dragged = card;
    card.setAttribute("data-dragging", "");
    event.dataTransfer.effectAllowed = "move";
    // Firefox will not start a drag without data on the transfer.
    event.dataTransfer.setData("text/plain", card.getAttribute("data-pk") || "");
  });

  board.addEventListener("dragend", function () {
    if (dragged) dragged.removeAttribute("data-dragging");
    dragged = null;
    Array.prototype.forEach.call(board.querySelectorAll("[data-drop-target]"), function (col) {
      col.removeAttribute("data-drop-target");
    });
  });

  board.addEventListener("dragover", function (event) {
    if (!dragged) return;
    var column = event.target.closest && event.target.closest("[data-kanban-col]");
    if (!column) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (!column.hasAttribute("data-drop-target")) {
      Array.prototype.forEach.call(board.querySelectorAll("[data-drop-target]"), function (col) {
        col.removeAttribute("data-drop-target");
      });
      column.setAttribute("data-drop-target", "");
    }
  });

  board.addEventListener("drop", function (event) {
    if (!dragged) return;
    var column = event.target.closest && event.target.closest("[data-kanban-col]");
    if (!column) return;
    event.preventDefault();
    column.removeAttribute("data-drop-target");
    move(dragged, column);
  });
})();
