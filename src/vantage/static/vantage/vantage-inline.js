/* Inline cell editing: staging, live validation, and conflict resolution.
 *
 * Dependency-free and additive, like the rest of the runtime. Before this file loads
 * the table already shows every value and every control already holds the right one;
 * what is missing is only the ability to *send* a change.
 *
 * Five things are worth knowing before reading further.
 *
 * 1. NOTHING HERE AUTOSAVES. Edits accumulate in `staged` and go to the server only
 *    when the bar's own button is pressed. The grid is a view onto someone else's
 *    production database: the review step is the feature, not the friction.
 *
 * 2. A REJECTED CELL KEEPS ITS TYPED VALUE. Reverting on failure is the "helpful"
 *    behaviour that quietly destroys work — the user does not notice until much later,
 *    and by then they cannot reconstruct what they typed. Retry and Revert are offered
 *    as buttons instead, so undoing is something a person chose.
 *
 * 3. A FAILED VALIDATION *REQUEST* IS NOT A VALIDATION FAILURE. Live validation is an
 *    affordance. If the network drops, the cell is marked unchecked and the Save button
 *    stays live: the server validates again on commit anyway, and letting a blip block
 *    a save would make an optional feature into a single point of failure.
 *
 * 4. THE GRID OWNS THE ROW; THIS FILE OWNS THE CELL. Row focus, j/k/x/Space/Enter and
 *    the peek drawer all belong to vantage-grid.js and none of them are rebound here.
 *    Cell mode is entered with `e` — a key the grid does not use — and left with Esc.
 *    While a control has focus it consumes only the keys it legitimately owns, and it
 *    stops those from reaching the grid rather than asking the grid to ignore them.
 *
 * 5. THE VERSION TOKEN IS THE ONE THE PAGE WAS RENDERED WITH. It is read off the row's
 *    data-version and never refreshed except from a server response. Re-reading it at
 *    save time would make it agree with the database by construction, and the lock
 *    would silently never fire.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-grid][data-inline]");
  if (!root) return;

  // Named ...-url rather than matching the buttons' data-inline-save / -discard: two
  // different things called the same name in one subtree is a selector waiting to
  // pick up the wrong element.
  var saveUrl = root.getAttribute("data-inline-save-url");
  var validateUrl = root.getAttribute("data-inline-validate-url");
  if (!saveUrl) return;

  var table = root.querySelector("table.v-table");
  var bar = root.querySelector("[data-inline-bar]");
  var countEl = root.querySelector("[data-inline-count]");
  var noteEl = root.querySelector("[data-inline-note]");
  var saveButton = root.querySelector("[data-inline-save]");
  var discardButton = root.querySelector("[data-inline-discard]");
  var live = root.querySelector("[data-inline-live]");
  var dialog = root.querySelector("[data-inline-conflict]");
  if (!table || !bar) return;

  var VALIDATE_DEBOUNCE_MS = 350;

  // pk -> { row, version, cells: {field: cell} }
  var rows = {};
  // Every cell whose value differs from what the server rendered. A Set of the cell
  // objects themselves rather than a map keyed on pk+field: a composite string key
  // needs a separator, and any separator is a character some primary key contains.
  var staged = new Set();
  var saving = false;

  function announce(text) {
    if (live) live.textContent = text;
  }

  function csrf() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  // ── reading and writing a control ─────────────────────────────────────────
  //
  // Three shapes cover every kind on the allowlist: a checkbox reports `checked`, a
  // <select> and every <input> report `value`. The baseline is taken from the DOM's
  // own defaults (defaultChecked / the option carrying the `selected` attribute)
  // rather than from a data- attribute the server would have had to format twice —
  // one formatter, and it is the browser's.

  function readControl(control) {
    if (control.type === "checkbox") return control.checked;
    return control.value;
  }

  function writeControl(control, value) {
    if (control.type === "checkbox") {
      control.checked = Boolean(value);
      return;
    }
    control.value = value === null || value === undefined ? "" : String(value);
  }

  function baselineOf(control) {
    if (control.type === "checkbox") return control.defaultChecked;
    if (control.tagName === "SELECT") {
      var chosen = control.querySelector("option[selected]");
      // A <select> with nothing marked selected falls back to the first option, which
      // is what the browser itself does.
      return chosen ? chosen.value : control.options.length ? control.options[0].value : "";
    }
    return control.defaultValue;
  }

  // ── the cell model ────────────────────────────────────────────────────────

  Array.prototype.forEach.call(table.querySelectorAll("[data-inline-row]"), function (tr) {
    var pk = tr.getAttribute("data-pk");
    var entry = { row: tr, version: tr.getAttribute("data-version"), cells: {} };
    rows[pk] = entry;

    Array.prototype.forEach.call(tr.querySelectorAll("[data-inline-cell]"), function (host) {
      var control = host.querySelector("[data-inline-input]");
      if (!control) return;
      var cell = {
        pk: pk,
        field: host.getAttribute("data-field"),
        label: host.getAttribute("data-label") || host.getAttribute("data-field"),
        host: host,
        control: control,
        mark: host.querySelector("[data-inline-mark]"),
        errorEl: host.querySelector("[data-inline-error]"),
        baseline: baselineOf(control),
        state: "", // "" | "staged" | "invalid" | "rejected" | "saved" | "unchecked"
        messages: [],
        pending: null, // AbortController for an in-flight validation
        timer: null,
      };
      entry.cells[cell.field] = cell;
      bind(cell);
    });
  });

  function eachCell(fn) {
    Object.keys(rows).forEach(function (pk) {
      Object.keys(rows[pk].cells).forEach(function (field) {
        fn(rows[pk].cells[field], rows[pk]);
      });
    });
  }

  function isDirty(cell) {
    return readControl(cell.control) !== cell.baseline;
  }

  // ── staging ───────────────────────────────────────────────────────────────

  function restage(cell) {
    if (isDirty(cell)) {
      staged.add(cell);
      if (cell.state !== "invalid" && cell.state !== "rejected") cell.state = "staged";
    } else {
      staged.delete(cell);
      if (cell.state === "staged" || cell.state === "unchecked") cell.state = "";
    }
    paintCell(cell);
    paintBar();
  }

  function stagedCount() {
    return staged.size;
  }

  function stagedRows() {
    // Folded by row on the way out: one entry per record, carrying the version that
    // record was rendered with. The server needs the row, not the cell — a changeset
    // is a set of partial updates, and each one is claimed and saved as a unit.
    var byPk = {};
    var order = [];
    staged.forEach(function (cell) {
      if (!byPk[cell.pk]) {
        byPk[cell.pk] = { pk: cell.pk, version: rows[cell.pk].version, fields: {} };
        order.push(cell.pk);
      }
      byPk[cell.pk].fields[cell.field] = readControl(cell.control);
    });
    return order.map(function (pk) {
      return byPk[pk];
    });
  }

  // ── painting ──────────────────────────────────────────────────────────────

  function paintCell(cell) {
    cell.host.setAttribute("data-state", cell.state);
    cell.control.setAttribute(
      "aria-invalid",
      cell.state === "invalid" || cell.state === "rejected" ? "true" : "false"
    );

    if (cell.messages.length) {
      cell.errorEl.textContent = cell.messages.join(" ");
      cell.errorEl.hidden = false;
    } else {
      cell.errorEl.textContent = "";
      cell.errorEl.hidden = true;
    }

    if (cell.mark) {
      cell.mark.setAttribute(
        "title",
        cell.state === "staged"
          ? "Not saved yet"
          : cell.state === "saved"
            ? "Saved"
            : cell.state === "unchecked"
              ? "Could not be checked"
              : cell.state === "invalid" || cell.state === "rejected"
                ? "Rejected"
                : ""
      );
    }
  }

  function paintBar() {
    var count = stagedCount();
    bar.hidden = count === 0;
    countEl.textContent = count === 1 ? "1 change" : count + " changes";
    // The number goes on the control being pressed, not only beside it: nobody should
    // commit twelve edits under a button that just says "Save".
    saveButton.textContent = count === 1 ? "Save 1 change" : "Save " + count + " changes";
    saveButton.disabled = saving || count === 0;
    discardButton.disabled = saving;
  }

  function setNote(text, tone) {
    noteEl.textContent = text || "";
    if (tone) noteEl.setAttribute("data-tone", tone);
    else noteEl.removeAttribute("data-tone");
  }

  // ── leaving the page with work in it ──────────────────────────────────────
  //
  // Registered once and gated on the staged count rather than added and removed,
  // because a listener that is sometimes absent is a listener that is absent on the
  // one path nobody tested.

  window.addEventListener("beforeunload", function (event) {
    if (!stagedCount() || saving) return;
    event.preventDefault();
    // Browsers ignore the string and show their own copy; returnValue is still what
    // triggers the prompt at all in the older ones.
    event.returnValue = "";
    return "";
  });

  // ── live validation ───────────────────────────────────────────────────────

  function validate(cell) {
    if (!validateUrl) return;
    if (cell.pending) cell.pending.abort();
    var controller = new AbortController();
    cell.pending = controller;

    window
      .fetch(validateUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({
          pk: cell.pk,
          field: cell.field,
          value: readControl(cell.control),
        }),
        signal: controller.signal,
      })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then(function (data) {
        if (cell.pending !== controller) return; // a newer answer already landed
        cell.pending = null;
        cell.messages = data.valid ? [] : data.errors || [];
        cell.state = data.valid ? (isDirty(cell) ? "staged" : "") : "invalid";
        paintCell(cell);
        if (!data.valid) announce(cell.label + ": " + cell.messages.join(" "));
      })
      .catch(function (error) {
        if (error && error.name === "AbortError") return;
        if (cell.pending !== controller) return;
        cell.pending = null;
        // A network error is not a validation error. The cell is marked as unchecked
        // and Save stays enabled — the server validates again on commit, which is the
        // check that was always authoritative.
        cell.messages = [];
        if (cell.state !== "rejected") cell.state = isDirty(cell) ? "unchecked" : "";
        paintCell(cell);
      });
  }

  function scheduleValidation(cell) {
    window.clearTimeout(cell.timer);
    cell.timer = window.setTimeout(function () {
      if (isDirty(cell)) validate(cell);
    }, VALIDATE_DEBOUNCE_MS);
  }

  // ── per-cell events ───────────────────────────────────────────────────────

  function bind(cell) {
    var control = cell.control;

    control.addEventListener("input", function () {
      // Typing clears a stale complaint immediately: leaving the old message under a
      // value it no longer describes is worse than showing nothing.
      if (cell.state === "invalid" || cell.state === "rejected") {
        cell.messages = [];
        cell.state = "staged";
      }
      restage(cell);
      scheduleValidation(cell);
    });

    control.addEventListener("change", function () {
      restage(cell);
      // A <select> or a checkbox has no meaningful "still typing" state, so its
      // answer is worth asking for at once.
      window.clearTimeout(cell.timer);
      if (isDirty(cell)) validate(cell);
    });

    control.addEventListener("blur", function () {
      window.clearTimeout(cell.timer);
      if (isDirty(cell) && cell.state !== "invalid") validate(cell);
    });

    control.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === "Escape" || event.key === "Enter") {
        // Esc goes back one level; Enter confirms and does the same. Both are stopped
        // here rather than left to bubble: the grid would open the record on Enter,
        // and the empty #v-inline-form would otherwise take an implicit submit.
        event.preventDefault();
        event.stopPropagation();
        window.clearTimeout(cell.timer);
        if (isDirty(cell)) validate(cell);
        leaveCellMode(cell);
        return;
      }

      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        // The control owns its own vertical keys — stepping a number, opening a date
        // picker, walking a <select>'s options. Stopping propagation is how cell mode
        // takes them back from the grid's row navigation without rebinding anything
        // the grid registered.
        event.stopPropagation();
        return;
      }

      if (
        (event.key === "ArrowLeft" || event.key === "ArrowRight") &&
        control.tagName === "SELECT"
      ) {
        // Left/Right on a <select> silently changes its value in every browser. The
        // default is suppressed so the keypress means only "next cell", and the event
        // is left to bubble so the grid is the one that moves the focus.
        event.preventDefault();
      }
    });
  }

  // ── cell mode ─────────────────────────────────────────────────────────────
  //
  // Row-level focus is primary and stays primary. `e` is an explicit opt-in, chosen
  // because the grid does not bind it, and the transition is announced because a
  // keyboard user has no other way to know the meaning of every other key just changed.

  function cellsInRow(tr) {
    return Array.prototype.filter.call(
      tr.querySelectorAll("[data-inline-input]"),
      function (control) {
        return !control.disabled && control.offsetParent !== null;
      }
    );
  }

  function leaveCellMode(cell) {
    var tr = rows[cell.pk].row;
    tr.focus();
    announce("Left cell mode.");
  }

  table.addEventListener("keydown", function (event) {
    if (event.key !== "e" || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) {
      return;
    }
    var tr = event.target.closest ? event.target.closest("[data-inline-row]") : null;
    // Only from the row itself. Inside a control, `e` is a letter.
    if (!tr || event.target !== tr) return;
    var controls = cellsInRow(tr);
    if (!controls.length) return;
    event.preventDefault();
    controls[0].focus();
    if (controls[0].select) controls[0].select();
    announce(
      "Cell mode. " +
        controls.length +
        (controls.length === 1 ? " editable cell" : " editable cells") +
        ". Escape to leave."
    );
  });

  // ── committing ────────────────────────────────────────────────────────────

  function clearOutcomes() {
    eachCell(function (cell) {
      if (cell.state === "saved") {
        cell.state = "";
        paintCell(cell);
      }
    });
  }

  function save() {
    if (saving || !stagedCount()) return;
    var payload = { changes: stagedRows() };
    // Remembered before the request so the outcome can be marked on the cells that
    // were actually sent. The response carries every editable value of every saved
    // row — that is how a server-normalised value gets back into an untouched
    // control — but ticking a cell nobody edited would report work that never
    // happened.
    var sent = new Set(staged);
    saving = true;
    clearOutcomes();
    setNote("Saving…");
    paintBar();

    window
      .fetch(saveUrl, {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify(payload),
      })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            return { status: response.status, data: data };
          });
      })
      .then(function (result) {
        saving = false;
        if (result.status === 200) return applySaved(result.data, sent);
        if (result.status === 409) return showConflicts(result.data);
        if (result.status === 422) return applyErrors(result.data);
        if (result.status === 403) {
          setNote("You do not have permission to change these records.", "error");
        } else if (result.status === 404) {
          setNote("One of these records no longer exists. Reload the page.", "error");
        } else {
          setNote("The server refused the changeset (" + result.status + ").", "error");
        }
        paintBar();
        announce(noteEl.textContent);
      })
      .catch(function () {
        saving = false;
        // Nothing was reverted and nothing was cleared: the changeset is still staged
        // and the button says Save again, which is the whole recovery story.
        setNote("Could not reach the server. Your changes are still here — try again.", "error");
        paintBar();
        announce(noteEl.textContent);
      });
  }

  function applySaved(data, sent) {
    (data.rows || []).forEach(function (row) {
      var entry = rows[row.pk];
      if (!entry) return;
      // The new token, from the response and nowhere else. Without this the next save
      // of the same row would present the version it has just superseded.
      entry.version = row.version;
      if (row.version) entry.row.setAttribute("data-version", row.version);
      else entry.row.removeAttribute("data-version");

      Object.keys(row.values || {}).forEach(function (field) {
        var cell = entry.cells[field];
        if (!cell) return;
        // Show what was stored, not what was typed: the server may have normalised
        // "3.5" to "3.50", and a cell that disagrees with the database reports a
        // change on the next save that nobody made.
        writeControl(cell.control, row.values[field]);
        cell.baseline = readControl(cell.control);
        cell.messages = [];
        cell.state = sent.has(cell) ? "saved" : "";
        staged.delete(cell);
        paintCell(cell);
      });
    });
    setNote(data.saved === 1 ? "1 change saved." : data.saved + " changes saved.", "success");
    paintBar();
    announce(noteEl.textContent);
  }

  function applyErrors(data) {
    var total = 0;
    (data.errors || []).forEach(function (row) {
      var entry = rows[row.pk];
      if (!entry) return;
      Object.keys(row.fields || {}).forEach(function (field) {
        total += 1;
        var cell = entry.cells[field];
        if (!cell) return;
        // The typed value stays exactly where it is. Only the marking changes.
        cell.messages = row.fields[field];
        cell.state = "rejected";
        paintCell(cell);
      });
    });
    setNote(
      "Nothing was saved: " +
        (total === 1 ? "one value was rejected" : total + " values were rejected") +
        ". Fix them and save again, or discard.",
      "error"
    );
    paintBar();
    announce(noteEl.textContent);

    var first = (data.errors || [])[0];
    if (first && rows[first.pk]) {
      var field = Object.keys(first.fields || {})[0];
      var cell = rows[first.pk].cells[field];
      if (cell) cell.control.focus();
    }
  }

  // ── conflict resolution ───────────────────────────────────────────────────

  var conflictState = null;

  function showConflicts(data) {
    conflictState = data.conflicts || [];
    if (!dialog || !conflictState.length) {
      setNote("Someone else changed these records. Reload the page.", "error");
      paintBar();
      return;
    }

    var rowsEl = dialog.querySelector("[data-conflict-rows]");
    var summary = dialog.querySelector("[data-conflict-summary]");
    rowsEl.textContent = "";
    var fieldCount = 0;

    conflictState.forEach(function (conflict) {
      var block = document.createElement("section");
      block.className = "v-inline-conflict-record";

      var heading = document.createElement("h3");
      heading.className = "v-inline-conflict-record-title";
      heading.textContent = conflict.label || "Record " + conflict.pk;
      block.appendChild(heading);

      Object.keys(conflict.fields || {}).forEach(function (field) {
        var diff = conflict.fields[field];
        fieldCount += 1;
        block.appendChild(diffRow(conflict, field, diff));
      });

      rowsEl.appendChild(block);
    });

    summary.textContent =
      (conflictState.length === 1 ? "This record" : conflictState.length + " records") +
      " changed while you were editing. Nothing was saved. " +
      (fieldCount === 1
        ? "The field below is shown both ways — choose a side."
        : "The " +
          fieldCount +
          " fields below are shown both ways — choose a side for each, or take one" +
          " side for all of them.");

    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    announce(summary.textContent);
  }

  function diffRow(conflict, field, diff) {
    var wrap = document.createElement("div");
    wrap.className = "v-inline-diff";
    wrap.setAttribute("data-conflicting", diff.conflict ? "true" : "false");

    var name = document.createElement("div");
    name.className = "v-inline-diff-field";
    var cell = rows[conflict.pk] && rows[conflict.pk].cells[field];
    name.textContent = cell ? cell.label : field;
    wrap.appendChild(name);

    var group = "v-conflict-" + conflict.pk + "-" + field;
    wrap.appendChild(sideChoice(group, "mine", "Yours", diff.mine_display, diff.conflict));
    wrap.appendChild(sideChoice(group, "theirs", "Theirs", diff.theirs_display, !diff.conflict));
    return wrap;
  }

  function sideChoice(group, side, title, display, checked) {
    var label = document.createElement("label");
    label.className = "v-inline-diff-side";
    label.setAttribute("data-side", side);

    var radio = document.createElement("input");
    radio.type = "radio";
    radio.name = group;
    radio.value = side;
    radio.checked = Boolean(checked);
    radio.setAttribute("data-conflict-choice", side);

    var text = document.createElement("span");
    var strong = document.createElement("strong");
    strong.textContent = title;
    var value = document.createElement("span");
    value.className = "v-inline-diff-value";
    value.textContent = display;
    text.appendChild(strong);
    text.appendChild(value);

    label.appendChild(radio);
    label.appendChild(text);
    return label;
  }

  function setAllSides(side) {
    if (!dialog) return;
    Array.prototype.forEach.call(
      dialog.querySelectorAll('[data-conflict-choice="' + side + '"]'),
      function (radio) {
        radio.checked = true;
      }
    );
  }

  function resolve() {
    if (!conflictState) return;

    conflictState.forEach(function (conflict) {
      var entry = rows[conflict.pk];
      if (!entry) return;
      // Adopt the version the server just reported. Retrying against the token that
      // already lost would conflict again forever.
      entry.version = conflict.version;
      if (conflict.version) entry.row.setAttribute("data-version", conflict.version);

      Object.keys(conflict.fields || {}).forEach(function (field) {
        var cell = entry.cells[field];
        if (!cell) return;
        var chosen = dialog.querySelector(
          'input[name="v-conflict-' + conflict.pk + "-" + field + '"]:checked'
        );
        var side = chosen ? chosen.value : "theirs";
        var diff = conflict.fields[field];

        if (side === "theirs") {
          // Their value becomes both the control's value and its baseline, so the
          // field drops out of the changeset entirely rather than being re-submitted
          // as an edit that agrees with the database.
          writeControl(cell.control, diff.theirs);
          cell.baseline = readControl(cell.control);
        } else {
          writeControl(cell.control, diff.mine);
        }
        cell.messages = [];
        cell.state = "";
        restage(cell);
      });
    });

    conflictState = null;
    if (dialog.open) dialog.close();

    if (stagedCount()) {
      setNote("Resolved against the current version. Save again to apply.");
      paintBar();
      announce(noteEl.textContent);
    } else {
      setNote("Their version kept. Nothing left to save.");
      paintBar();
      announce(noteEl.textContent);
    }
  }

  if (dialog) {
    dialog.querySelector("[data-conflict-mine]").addEventListener("click", function () {
      setAllSides("mine");
      resolve();
    });
    dialog.querySelector("[data-conflict-theirs]").addEventListener("click", function () {
      setAllSides("theirs");
      resolve();
    });
    dialog.querySelector("[data-conflict-merge]").addEventListener("click", resolve);
    dialog.querySelector("[data-conflict-cancel]").addEventListener("click", function () {
      // Cancel changes nothing at all — not the controls, not the versions. The
      // changeset is still staged and still conflicting, which is the truth.
      conflictState = null;
      if (dialog.open) dialog.close();
      setNote("Still unresolved. Nothing was saved.", "error");
      paintBar();
    });
  }

  // ── discard ───────────────────────────────────────────────────────────────

  discardButton.addEventListener("click", function () {
    var count = stagedCount();
    if (!count) return;
    if (
      !window.confirm(
        count === 1
          ? "Discard 1 unsaved change?"
          : "Discard all " + count + " unsaved changes?"
      )
    ) {
      return;
    }
    eachCell(function (cell) {
      writeControl(cell.control, cell.baseline);
      cell.messages = [];
      cell.state = "";
      staged.delete(cell);
      paintCell(cell);
    });
    setNote("Changes discarded.");
    paintBar();
    announce("All unsaved changes discarded.");
  });

  saveButton.addEventListener("click", save);

  // ── boot ──────────────────────────────────────────────────────────────────
  //
  // No initial paint of the cells: a page that has never been touched should look
  // untouched. Only the bar is settled, and it settles hidden.
  paintBar();
})();
