/* The ⌘K command palette.
 *
 * Dependency-free and deliberately unclever, like the rest of the runtime. Two data
 * sources with two different latencies: commands are fetched once and filtered here,
 * because round-tripping every keystroke of "das" to find "Dashboard" is not a
 * palette; records are fetched from the server on a debounce, because only the
 * database knows them and only the server may decide which of them this user is
 * allowed to see.
 *
 * The dialog is a native <dialog> opened with showModal(). That buys the top layer,
 * the backdrop, inert-ing the page behind it and a correct focus trap — four things
 * hand-written palettes routinely get wrong.
 *
 * Accessibility is the combobox/listbox pattern: the input keeps DOM focus at all
 * times and points at the highlighted row with aria-activedescendant, so arrow keys
 * never move focus and the announcement follows the highlight. Cmd/Ctrl+K carries a
 * modifier, so WCAG 2.1.4 (Character Key Shortcuts, Level A) does not apply to it;
 * every unmodified key this file listens for is bound to the dialog or the trigger,
 * never to the document.
 */
(function () {
  "use strict";

  var DEBOUNCE_MS = 160;
  var COMMAND_LIMIT = 8;

  var dialog = document.getElementById("v-palette");
  var trigger = document.getElementById("v-palette-trigger");

  if (trigger) {
    // The Mac glyph is wrong everywhere else, and a wrong shortcut hint is worse
    // than none. Rendered server-side as ⌘K and corrected here.
    var apple = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    var hint = document.querySelector("[data-palette-hint]");
    if (hint && !apple) hint.textContent = "Ctrl K";
  }

  if (!dialog || typeof dialog.showModal !== "function") return;

  var input = document.getElementById("v-palette-input");
  var list = document.getElementById("v-palette-list");
  var empty = dialog.querySelector("[data-palette-empty]");
  var busy = dialog.querySelector("[data-palette-busy]");
  var status = dialog.querySelector("[data-palette-status]");

  var searchUrl = dialog.getAttribute("data-search-url");
  var commandsUrl = dialog.getAttribute("data-commands-url");
  var resultsUrl = dialog.getAttribute("data-results-url");

  var commands = [];
  var icons = {};
  var commandsLoaded = false;
  var records = null;
  var minLength = 2;

  var rows = [];
  var active = -1;
  var timer = null;
  var inflight = null;
  var sequence = 0;
  var announced = "";

  var handlers = {
    /* Reuses the header toggle rather than reimplementing it, so the palette can
     * never disagree with the button about what "toggle" means. */
    "theme.toggle": function () {
      var button = document.querySelector("[data-vantage-theme-toggle]");
      if (button) button.click();
    },
  };

  /* ── data ─────────────────────────────────────────────────────────────── */

  function loadCommands() {
    if (commandsLoaded || !commandsUrl) return;
    commandsLoaded = true;
    request(commandsUrl).then(function (data) {
      if (!data) return;
      commands = data.commands || [];
      addIcons(data.icons);
      if (dialog.open) render();
    });
  }

  function request(url) {
    return fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        /* An offline palette still lists nothing rather than breaking the page. */
        return null;
      });
  }

  function addIcons(map) {
    if (!map) return;
    Object.keys(map).forEach(function (name) {
      icons[name] = map[name];
    });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    var query = input.value.trim();
    if (query.length < minLength) {
      records = null;
      sequence += 1; // invalidate anything still in the air
      setBusy(false);
      render();
      return;
    }
    // Commands re-filter on this keystroke; only the record lookup waits.
    render();
    timer = setTimeout(function () {
      lookup(query);
    }, DEBOUNCE_MS);
  }

  function lookup(query) {
    if (!searchUrl) return;
    if (inflight) inflight.abort();
    var controller = window.AbortController ? new AbortController() : null;
    inflight = controller;
    var ticket = ++sequence;
    setBusy(true);

    fetch(searchUrl + "?q=" + encodeURIComponent(query), {
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
      signal: controller ? controller.signal : undefined,
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        // A slow early request must never overwrite a fast later one.
        if (ticket !== sequence || !data) return;
        records = data;
        minLength = data.min_length || minLength;
        addIcons(data.icons);
        setBusy(false);
        render();
      })
      .catch(function () {
        if (ticket !== sequence) return;
        setBusy(false);
      });
  }

  function setBusy(state) {
    if (busy) busy.hidden = !state;
  }

  /* ── matching ─────────────────────────────────────────────────────────── */

  function score(text, query) {
    var haystack = text.toLowerCase();
    var needle = query.toLowerCase();
    if (haystack === needle) return 1000;

    var index = haystack.indexOf(needle);
    if (index === 0) return 800 - haystack.length;
    if (index > 0) {
      var boundary = !/[a-z0-9]/.test(haystack.charAt(index - 1));
      return (boundary ? 600 : 400) - haystack.length;
    }
    return subsequence(haystack, needle);
  }

  /* Every character of the query, in order but not adjacent: "cst" finds
   * "Customers". Runs of adjacent matches score higher, which is what makes an
   * abbreviation beat a coincidence. */
  function subsequence(haystack, needle) {
    var at = 0;
    var points = 0;
    var run = 0;
    for (var i = 0; i < needle.length; i++) {
      var found = haystack.indexOf(needle.charAt(i), at);
      if (found < 0) return -1;
      run = found === at && i > 0 ? run + 1 : 0;
      points += 10 + run * 5;
      at = found + 1;
    }
    return points;
  }

  function rank(command, query) {
    var best = score(command.label, query);
    var extras = (command.keywords || []).concat([command.hint || "", command.section || ""]);
    for (var i = 0; i < extras.length; i++) {
      if (!extras[i]) continue;
      // A keyword match is real but weaker than a match on the visible label.
      var value = score(String(extras[i]), query) / 2;
      if (value > best) best = value;
    }
    return best;
  }

  function matchingCommands(query) {
    if (!query) return commands.slice();
    var scored = [];
    for (var i = 0; i < commands.length; i++) {
      var value = rank(commands[i], query);
      if (value > 0) scored.push({ command: commands[i], score: value, index: i });
    }
    scored.sort(function (a, b) {
      return b.score - a.score || a.index - b.index;
    });
    return scored
      .slice(0, COMMAND_LIMIT)
      .map(function (entry) {
        return entry.command;
      });
  }

  /* ── rendering ────────────────────────────────────────────────────────── */

  function groupsFor(query) {
    var groups = [];
    var matched = matchingCommands(query);
    var section = null;

    matched.forEach(function (command) {
      if (!section || section.label !== command.section) {
        section = { label: command.section, options: [] };
        groups.push(section);
      }
      section.options.push({
        label: command.label,
        sublabel: "",
        hint: command.hint,
        icon: command.icon,
        url: command.url,
        callback: command.callback,
      });
    });

    if (records && records.query === query) {
      records.groups.forEach(function (group) {
        groups.push({
          label: group.label,
          options: group.results.map(function (hit) {
            return {
              label: hit.label,
              sublabel: hit.sublabel,
              hint: "",
              icon: group.icon,
              url: hit.url,
              callback: null,
            };
          }),
        });
      });
    }

    if (query.length >= minLength && resultsUrl) {
      groups.push({
        label: "Search",
        options: [
          {
            label: "See all results for “" + query + "”",
            sublabel: "",
            hint: "",
            icon: "search",
            url: resultsUrl + "?q=" + encodeURIComponent(query),
            callback: null,
          },
        ],
      });
    }

    return groups;
  }

  function render() {
    var query = input.value.trim();
    var groups = groupsFor(query);

    list.textContent = "";
    rows = [];
    active = -1;

    groups.forEach(function (group, groupIndex) {
      if (!group.options.length) return;

      var holder = document.createElement("li");
      holder.setAttribute("role", "presentation");

      var heading = document.createElement("div");
      heading.className = "v-palette-section";
      heading.id = "v-palette-group-" + groupIndex;
      heading.textContent = group.label;
      holder.appendChild(heading);

      var inner = document.createElement("ul");
      inner.setAttribute("role", "group");
      inner.setAttribute("aria-labelledby", heading.id);

      group.options.forEach(function (option) {
        inner.appendChild(optionNode(option));
      });

      holder.appendChild(inner);
      list.appendChild(holder);
    });

    input.setAttribute("aria-expanded", rows.length ? "true" : "false");
    if (empty) empty.hidden = rows.length > 0;
    if (rows.length) select(0);
    else input.removeAttribute("aria-activedescendant");
    announce(rows.length, query);
  }

  function optionNode(option) {
    var node = document.createElement("li");
    node.id = "v-palette-option-" + rows.length;
    node.className = "v-palette-option";
    node.setAttribute("role", "option");
    node.setAttribute("aria-selected", "false");

    var glyph = icons[option.icon];
    if (glyph) {
      var wrap = document.createElement("span");
      // Trusted markup: path data from the server's own fixed icon table, never
      // from anything a user typed.
      wrap.innerHTML = svg(glyph);
      node.appendChild(wrap.firstChild);
    }

    var label = document.createElement("span");
    label.className = "v-palette-label";
    label.textContent = option.label;
    if (option.sublabel) {
      var sub = document.createElement("span");
      sub.className = "v-palette-sub";
      sub.textContent = option.sublabel;
      label.appendChild(sub);
    }
    node.appendChild(label);

    if (option.hint) {
      var hintNode = document.createElement("span");
      hintNode.className = "v-palette-hint";
      hintNode.textContent = option.hint;
      node.appendChild(hintNode);
    }

    var index = rows.length;
    node.addEventListener("mousemove", function () {
      if (active !== index) select(index, false);
    });
    node.addEventListener("click", function () {
      activate(option);
    });

    rows.push({ node: node, option: option });
    return node;
  }

  function svg(paths) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ' +
      'focusable="false">' +
      paths +
      "</svg>"
    );
  }

  function announce(count, query) {
    if (!status) return;
    var message = !query
      ? count + (count === 1 ? " command" : " commands")
      : count + (count === 1 ? " result" : " results") + " for " + query;
    // Re-setting identical text says nothing in most screen readers and says it
    // twice in the rest.
    if (message === announced) return;
    announced = message;
    status.textContent = message;
  }

  /* ── selection ────────────────────────────────────────────────────────── */

  function select(index, scroll) {
    if (!rows.length) return;
    if (active >= 0 && rows[active]) rows[active].node.setAttribute("aria-selected", "false");
    active = (index + rows.length) % rows.length;
    var node = rows[active].node;
    node.setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", node.id);
    if (scroll !== false) node.scrollIntoView({ block: "nearest" });
  }

  function activate(option) {
    if (!option) return;
    close();
    if (option.callback) {
      var handler = handlers[option.callback];
      if (handler) handler();
      return;
    }
    if (option.url) window.location.assign(option.url);
  }

  /* ── open and close ───────────────────────────────────────────────────── */

  function open(seed) {
    if (dialog.open) return;
    loadCommands();
    records = null;
    announced = "";
    dialog.showModal();
    input.value = seed || "";
    render();
    input.focus();
    if (input.value) schedule();
  }

  function close() {
    if (dialog.open) dialog.close();
  }

  dialog.addEventListener("close", function () {
    if (timer) clearTimeout(timer);
    if (inflight) inflight.abort();
    sequence += 1;
    setBusy(false);
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");

    // The browser restores focus to whatever opened the dialog, which for a
    // shortcut-opened one is <body>: focus lost, and the next Tab restarts from the
    // top of the page. Deferred by a task because the close event fires *before* the
    // restore, so this has to check where focus actually landed rather than guess.
    window.setTimeout(function () {
      if (!trigger) return;
      var landed = document.activeElement;
      if (!landed || landed === document.body) trigger.focus();
    }, 0);
  });

  // A click on the backdrop lands on the dialog element itself; the panel stops it.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) close();
  });

  var dismiss = dialog.querySelector("[data-palette-close]");
  if (dismiss) dismiss.addEventListener("click", close);

  input.addEventListener("input", schedule);

  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      select(active + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      select(active - 1);
    } else if (event.key === "Home" && rows.length) {
      event.preventDefault();
      select(0);
    } else if (event.key === "End" && rows.length) {
      event.preventDefault();
      select(rows.length - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      activate(active >= 0 && rows[active] ? rows[active].option : null);
    } else if (event.key === "Escape" && input.value) {
      // Esc goes back one level, and a typed query is a level. The second Esc
      // closes, which is the dialog's own behaviour.
      event.preventDefault();
      input.value = "";
      schedule();
    }
  });

  /* ── the way in ───────────────────────────────────────────────────────── */

  document.addEventListener("keydown", function (event) {
    // Modified, so WCAG 2.1.4 does not apply and a document listener is honest here.
    if (event.key !== "k" && event.key !== "K") return;
    if (!(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
    event.preventDefault();
    if (dialog.open) close();
    else open(trigger ? trigger.value : "");
  });

  if (trigger) {
    trigger.addEventListener("click", function () {
      open(trigger.value);
    });

    trigger.addEventListener("keydown", function (event) {
      // Enter is left alone: the form submits to the results page, which is the
      // same destination with or without this script.
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Enter" || event.key === "Tab" || event.key === "Escape") return;
      if (event.key.length !== 1 && event.key !== "ArrowDown" && event.key !== "Backspace") {
        return;
      }
      event.preventDefault();
      open(event.key.length === 1 ? trigger.value + event.key : trigger.value);
    });
  }

  // Warmed on idle so the first ⌘K has nothing to wait for.
  if (window.requestIdleCallback) window.requestIdleCallback(loadCommands);
  else window.setTimeout(loadCommands, 1200);

  window.vantage = window.vantage || {};
  window.vantage.palette = {
    open: open,
    close: close,
    /* Project hook: window.vantage.palette.handle("ops.flush", fn) wires a command
     * registered in Python with callback="ops.flush" to its behaviour here. */
    handle: function (name, fn) {
      handlers[name] = fn;
    },
  };
})();
