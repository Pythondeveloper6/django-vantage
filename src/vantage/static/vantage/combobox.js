/* Type-to-search relation pickers.
 *
 * Enhances the control SearchSelect renders: a real <select> holding the current
 * value and a first page of options, wrapped in a div that carries the autocomplete
 * endpoint and the target model's label. Dependency-free, like the rest of the
 * runtime, and additive — nothing here is required for the form to work.
 *
 * Five things are worth knowing before reading further.
 *
 * 1. THE <select> IS STILL THE CONTROL. It is never removed, never renamed and never
 *    replaced by hidden inputs; it is moved out of sight and driven from here. The
 *    form submits exactly what it submitted before, `form.elements` still sees it,
 *    and the dirty-state guard on the change page still compares it. A combobox built
 *    out of <div>s has to reimplement submission, reset, validation and autofill, and
 *    reimplements at least one of them wrongly.
 *
 * 2. THE SERVER ALREADY SENT THE SELECTED LABEL. The box shows the current record's
 *    name on load with no request, because the option is in the markup. A picker that
 *    fetches its own value on load shows a blank field for a moment on every edit
 *    page, and an empty one forever when the endpoint is down.
 *
 * 3. TWO GUARDS AGAINST A STALE RENDER, NOT ONE. Every request aborts the one before
 *    it *and* carries a ticket that is checked before its results are painted. Abort
 *    alone is not enough: a response already in flight when abort() lands can still
 *    resolve, and then a slow "a" paints over a fast "ada". This is the classic bug
 *    in hand-written comboboxes and it only shows up on a bad connection.
 *
 * 4. THE KEYBOARD CONTRACT IS THE ONE A <select> HAS. Arrows move without committing,
 *    Enter commits, Escape closes and puts back what was there before, Tab commits
 *    the highlighted row on the way out. Leaving the field never leaves typed text
 *    that is not a real selection: whatever is in the box on blur is the label of
 *    what the <select> actually holds.
 *
 * 5. `more: true` IS TOLD, NOT HIDDEN. The endpoint answers "there are others" and
 *    the user is told to narrow the term. Silently truncating is how someone
 *    concludes their record does not exist.
 */
(function () {
  "use strict";

  /* Long enough that a touch-typist sends one request per word rather than one per
   * keystroke, short enough that it never feels like waiting. */
  var DEBOUNCE_MS = 180;

  var uid = 0;

  function enhance(root) {
    var select = root.querySelector("select");
    if (!select || select.disabled || root.getAttribute("data-combo-ready")) return;

    var url = root.getAttribute("data-url");
    var model = root.getAttribute("data-model");
    // No endpoint means no search. The plain select is still a working control with
    // the current value in it, so the honest thing is to leave it alone.
    if (!url || !model) return;
    root.setAttribute("data-combo-ready", "1");

    var multiple = !!select.multiple;
    var base = select.id || "v-combo-" + ++uid;

    /* ── the parts ─────────────────────────────────────────────────────────── */

    var field = document.createElement("div");
    field.className = "v-combo-field";

    var chips = null;
    if (multiple) {
      chips = document.createElement("span");
      chips.className = "v-combo-chips";
      field.appendChild(chips);
    }

    var input = document.createElement("input");
    input.type = "text";
    input.id = base + "-input";
    input.className = "v-combo-input";
    input.placeholder = root.getAttribute("data-placeholder") || "Type to search…";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", base + "-listbox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("autocapitalize", "none");
    input.spellcheck = false;
    // Required moves with the visible control: the browser refuses to focus an
    // invisible invalid field, and a validation bubble pointing at nothing is worse
    // than no bubble. The server enforces the same rule regardless.
    if (select.required) {
      input.required = true;
      select.required = false;
    }
    if (select.getAttribute("aria-describedby")) {
      input.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
    }
    if (select.getAttribute("aria-invalid")) {
      input.setAttribute("aria-invalid", select.getAttribute("aria-invalid"));
    }
    field.appendChild(input);

    var pop = document.createElement("div");
    pop.className = "v-combo-pop";
    pop.hidden = true;

    var list = document.createElement("ul");
    list.id = base + "-listbox";
    list.className = "v-combo-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", input.placeholder);
    pop.appendChild(list);

    var footer = document.createElement("p");
    footer.className = "v-combo-foot";
    footer.hidden = true;
    pop.appendChild(footer);

    /* The count announcement. Politely, and out of the visual layout: sighted users
     * can see how many rows there are, and a screen reader user cannot. */
    var announcer = document.createElement("span");
    announcer.className = "v-sr-only";
    announcer.setAttribute("role", "status");
    announcer.setAttribute("aria-live", "polite");

    select.insertAdjacentElement("afterend", field);
    field.insertAdjacentElement("afterend", pop);
    pop.insertAdjacentElement("afterend", announcer);

    var note = root.querySelector("[data-combo-note]");
    if (note) note.hidden = true; // it was addressed to a browser without this file

    // Out of sight, still in the form. Not `hidden` and not display:none: those make
    // the element unfocusable, and an unfocusable control is one the browser cannot
    // report a constraint violation against.
    select.setAttribute("tabindex", "-1");
    select.setAttribute("aria-hidden", "true");

    // The field's own <label> has to follow the control the user now types into,
    // or clicking it focuses something invisible and the combobox has no name at
    // all. An aria-label is the fallback, never an addition: it would override the
    // visible text and leave the spoken name saying something the label does not.
    var label = select.id
      ? document.querySelector('label[for="' + select.id + '"]')
      : null;
    if (label) label.htmlFor = input.id;
    else input.setAttribute("aria-label", input.placeholder);

    /* ── state ─────────────────────────────────────────────────────────────── */

    var rows = []; // [{ id, text, node }] currently in the listbox
    var active = -1;
    var timer = null;
    var inflight = null;
    var ticket = 0;

    /* ── reading and writing the select ────────────────────────────────────── */

    function chosen() {
      var out = [];
      for (var i = 0; i < select.options.length; i++) {
        var option = select.options[i];
        if (option.selected && option.value !== "") {
          out.push({ id: option.value, text: option.text });
        }
      }
      return out;
    }

    function currentLabel() {
      var picked = chosen();
      return picked.length ? picked[0].text : "";
    }

    function optionFor(id, text) {
      for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value === id) return select.options[i];
      }
      // Came back from the endpoint, was never rendered: the option has to exist
      // before it can be selected, or the value is dropped on submit.
      var made = new Option(text, id);
      select.add(made);
      return made;
    }

    function announce(message) {
      announcer.textContent = message;
    }

    function paintChips() {
      if (!chips) return;
      chips.textContent = "";
      chosen().forEach(function (item) {
        var chip = document.createElement("span");
        chip.className = "v-combo-chip";
        chip.appendChild(document.createTextNode(item.text));

        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "v-combo-chip-x";
        remove.setAttribute("aria-label", "Remove " + item.text);
        remove.textContent = "×";
        remove.addEventListener("click", function () {
          deselect(item.id);
          // Said out loud, because the only other feedback is a chip disappearing —
          // which is nothing at all to a screen reader. Adding already announces; the
          // two halves of the same action should not differ in whether they are heard.
          announce(item.text + " removed");
          input.focus();
        });

        chip.appendChild(remove);
        chips.appendChild(chip);
      });
    }

    function changed() {
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function commit(id, text) {
      var option = optionFor(id, text);
      if (multiple) {
        option.selected = true;
        input.value = "";
        paintChips();
        announce(text + " added");
        changed();
        // Stay open: adding several tags in a row is the whole point of a multiple.
        request("");
        return;
      }
      select.value = id;
      input.value = option.text;
      announce(option.text + " selected");
      changed();
      close(false);
    }

    function deselect(id) {
      for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value === id) {
          select.options[i].selected = false;
          announce("Removed " + select.options[i].text);
        }
      }
      paintChips();
      changed();
    }

    function clearValue() {
      if (multiple) return;
      var empty = optionFor("", "");
      empty.selected = true;
      select.value = "";
      announce("Selection cleared");
      changed();
    }

    /* ── the popup ─────────────────────────────────────────────────────────── */

    function open() {
      if (!pop.hidden) return;
      pop.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function close(restore) {
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
      if (inflight) {
        inflight.abort();
        inflight = null;
      }
      pop.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
      // Whatever is in the box when the field is left is the label of what the
      // select holds — never a half-typed term that selected nothing.
      if (restore) input.value = multiple ? "" : currentLabel();
    }

    function highlight(index) {
      if (rows[active]) rows[active].node.classList.remove("v-combo-active");
      active = index;
      if (!rows[active]) {
        input.removeAttribute("aria-activedescendant");
        return;
      }
      var node = rows[active].node;
      node.classList.add("v-combo-active");
      input.setAttribute("aria-activedescendant", node.id);
      if (node.scrollIntoView) node.scrollIntoView({ block: "nearest" });
    }

    function move(step) {
      if (!rows.length) return;
      var next = active + step;
      // No wrap, because a <select> does not wrap: reaching the end of the list is
      // information, and silently jumping back to the top hides it.
      if (next < 0) next = 0;
      if (next > rows.length - 1) next = rows.length - 1;
      highlight(next);
    }

    function paint(data) {
      var picked = {};
      chosen().forEach(function (item) {
        picked[item.id] = true;
      });

      list.textContent = "";
      rows = [];
      active = -1;
      input.removeAttribute("aria-activedescendant");

      data.results.forEach(function (result, index) {
        var node = document.createElement("li");
        node.id = base + "-option-" + index;
        node.className = "v-combo-option";
        node.setAttribute("role", "option");
        node.setAttribute("aria-selected", picked[result.id] ? "true" : "false");
        node.textContent = result.text;
        node.addEventListener("click", function () {
          commit(result.id, result.text);
        });
        list.appendChild(node);
        rows.push({ id: result.id, text: result.text, node: node });
      });

      var term = data.term || "";
      if (!data.results.length) {
        footer.hidden = false;
        footer.textContent = term ? "No matches for “" + term + "”." : "Nothing to show.";
        footer.setAttribute("data-tone", "empty");
        announce(footer.textContent);
      } else if (data.more) {
        footer.hidden = false;
        footer.removeAttribute("data-tone");
        // The endpoint says there are others. Say so, rather than letting a
        // truncated list look like the whole answer.
        footer.textContent =
          "More matches than fit here — keep typing to narrow the list.";
        announce(
          data.results.length +
            " results shown, and there are more. Keep typing to narrow the list."
        );
      } else {
        footer.hidden = true;
        announce(
          data.results.length === 1 ? "1 result" : data.results.length + " results"
        );
      }

      open();
      // Selecting the first row on arrival would make Enter commit a row nobody
      // looked at, so nothing is highlighted until an arrow key says so.
    }

    function fail() {
      list.textContent = "";
      rows = [];
      active = -1;
      footer.hidden = false;
      footer.setAttribute("data-tone", "empty");
      footer.textContent = "Could not load matches. Check your connection.";
      announce(footer.textContent);
      open();
    }

    function request(term) {
      if (inflight) inflight.abort();
      var controller = window.AbortController ? new AbortController() : null;
      inflight = controller;
      var mine = ++ticket;
      root.setAttribute("data-busy", "1");

      window
        .fetch(
          url + "?model=" + encodeURIComponent(model) + "&q=" + encodeURIComponent(term),
          {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
            signal: controller ? controller.signal : undefined,
          }
        )
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (data) {
          // The ticket, not the abort, is what makes this safe: a response can be
          // in flight when abort() lands and still resolve here afterwards.
          if (mine !== ticket) return;
          root.removeAttribute("data-busy");
          inflight = null;
          paint(data);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;
          if (mine !== ticket) return;
          root.removeAttribute("data-busy");
          inflight = null;
          fail();
        });
    }

    function schedule(term) {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        timer = null;
        request(term);
      }, DEBOUNCE_MS);
    }

    /* ── events ────────────────────────────────────────────────────────────── */

    // Typing never writes to the select. The value changes when a row is chosen, or
    // when the field is left — so a half-typed term cannot destroy the selection
    // behind the user, and every keystroke is undoable with Escape.
    input.addEventListener("input", function () {
      schedule(input.value.trim());
    });

    input.addEventListener("click", function () {
      if (pop.hidden) request(multiple ? input.value.trim() : "");
    });

    input.addEventListener("keydown", function (event) {
      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          if (pop.hidden) request(input.value.trim());
          else move(1);
          return;
        case "ArrowUp":
          event.preventDefault();
          if (pop.hidden) request(input.value.trim());
          else move(-1);
          return;
        case "Home":
          if (pop.hidden || !rows.length) return;
          event.preventDefault();
          highlight(0);
          return;
        case "End":
          if (pop.hidden || !rows.length) return;
          event.preventDefault();
          highlight(rows.length - 1);
          return;
        case "Enter":
          if (pop.hidden || active < 0) {
            // Nothing highlighted: let Enter do what it does in any text field on
            // this form, which is submit it.
            return;
          }
          event.preventDefault();
          commit(rows[active].id, rows[active].text);
          return;
        case "Escape":
          if (pop.hidden && input.value === currentLabel()) return;
          // Close and put back what was there. Never bubbles further than that: a
          // dialog or a grid behind the picker is not what Escape was aimed at.
          event.preventDefault();
          event.stopPropagation();
          close(true);
          return;
        case "Tab":
          // What a native select does: the highlighted row is the answer, and
          // leaving takes it with you.
          if (!pop.hidden && active >= 0) {
            commit(rows[active].id, rows[active].text);
            return;
          }
          commitTypedOrRestore();
          return;
        case "Backspace":
          if (multiple && input.value === "") {
            var picked = chosen();
            if (picked.length) {
              event.preventDefault();
              deselect(picked[picked.length - 1].id);
            }
          }
          return;
        default:
      }
    });

    function commitTypedOrRestore() {
      // An empty box means "no selection" — that is how a value is cleared without
      // hunting for a blank row. Anything else that was typed but never chosen is
      // not a value, so the box goes back to showing what the select holds.
      if (!multiple && input.value.trim() === "" && currentLabel() !== "") {
        clearValue();
        close(false);
        input.value = "";
        return;
      }
      close(true);
    }

    root.addEventListener("focusout", function (event) {
      if (event.relatedTarget && root.contains(event.relatedTarget)) return;
      commitTypedOrRestore();
    });

    // Keeping the caret in the input while a row is clicked is what stops focusout
    // from closing the list out from under the click.
    pop.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });

    /* ── first paint ───────────────────────────────────────────────────────── */

    paintChips();
    input.value = multiple ? "" : currentLabel();
    root.classList.add("v-combo-on");
  }

  function init() {
    document.querySelectorAll("[data-vantage-combobox]").forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Inline formsets and any other markup added after load get picked up too.
  document.addEventListener("vantage:content", init);
})();
