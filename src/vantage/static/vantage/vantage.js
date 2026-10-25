/* django-vantage runtime.
 *
 * Deliberately small and dependency-free. Anything that can be done on the server is
 * done on the server; this covers only what needs to happen in the browser.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "vantage-theme";

  /* The theme the page is painted in right now: "light" or "dark", never "system". */
  function currentTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "dark" || explicit === "light") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  /* What the *user* chose, which is a third thing: "system" is a real answer and the
   * account menu offers it, even though the bar's two-state toggle cannot say it. */
  function chosenTheme() {
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark") return stored;
    } catch (e) {
      /* Safari private mode. Fall through to what the server rendered. */
    }
    var explicit = document.documentElement.getAttribute("data-theme");
    return explicit === "dark" || explicit === "light" ? explicit : "system";
  }

  function paintTheme() {
    var effective = currentTheme();
    document.querySelectorAll("[data-theme-icon]").forEach(function (node) {
      node.hidden = node.getAttribute("data-theme-icon") !== effective;
    });
    var chosen = chosenTheme();
    document.querySelectorAll("[data-theme-choice]").forEach(function (node) {
      /* aria-checked inside the menu, where the buttons are menuitemradios;
       * aria-pressed on the account page, where they are ordinary toggle buttons.
       * Whichever the markup declared is the one kept in step — writing the other
       * would put two conflicting states on one control. */
      var attribute = node.hasAttribute("aria-pressed") ? "aria-pressed" : "aria-checked";
      node.setAttribute(attribute, node.getAttribute("data-theme-choice") === chosen ? "true" : "false");
    });
  }

  function applyTheme(theme, endpoint) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
      try {
        localStorage.setItem(STORAGE_KEY, theme);
      } catch (e) {
        /* Safari private mode throws; the cookie below still persists the choice. */
      }
    } else {
      /* "Follow the device": forget the choice rather than storing today's answer to
       * it, or the panel stops following the device the moment the device changes. */
      document.documentElement.removeAttribute("data-theme");
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch (e) {
        /* Nothing was stored, so nothing needs removing. */
      }
    }
    paintTheme();

    if (!endpoint) return;
    var body = new FormData();
    body.append("theme", theme);
    var token = document.querySelector("input[name=csrfmiddlewaretoken]");
    fetch(endpoint, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: token ? { "X-CSRFToken": token.value } : {},
    }).catch(function () {
      /* The theme is already applied and stored locally; the cookie is a nicety. */
    });
  }

  /* Move focus to the error summary a failed save just rendered.
   *
   * The server does its half correctly: the summary is role="alert", it has real text,
   * tabindex="-1" so it can receive focus, and the offending fields carry aria-invalid
   * and aria-describedby. What was missing is the last step. After a full page reload
   * focus is on <body>, so a screen-reader user is told nothing went wrong and has to
   * explore the page to discover the save failed — and a keyboard user starts tabbing
   * from the top of the document rather than at the problem.
   *
   * role="alert" alone is not enough here. It announces on insertion into a live
   * document; on a fresh page load the whole document arrives at once and there is no
   * insertion to notice. Focus is what makes it read.
   */
  function focusErrorSummary() {
    var summary = document.getElementById("v-form-summary");
    if (!summary) return;
    // preventScroll, then scroll deliberately: focusing an element near the top of a
    // long form is fine, but the browser's own jump is abrupt and lands differently
    // depending on where the summary sits.
    try {
      summary.focus({ preventScroll: true });
    } catch (err) {
      summary.focus();
    }
    if (summary.scrollIntoView) {
      summary.scrollIntoView({ block: "nearest" });
    }
  }

  function init() {
    paintTheme();
    focusErrorSummary();

    document.querySelectorAll("[data-vantage-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        applyTheme(currentTheme() === "dark" ? "light" : "dark", button.getAttribute("formaction"));
      });
    });

    /* The account menu's three-way choice, and the same control on the account page.
     * Each one is a real submit button attached to a real form, so this only has to
     * take over the reload — with no script they post and the server redirects back. */
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.addEventListener("click", function (event) {
        event.preventDefault();
        applyTheme(
          button.getAttribute("data-theme-choice"),
          button.form && button.form.getAttribute("action")
        );
      });
    });

    /* Following the device means noticing when the device changes its mind. */
    var system = window.matchMedia("(prefers-color-scheme: dark)");
    if (system.addEventListener) system.addEventListener("change", paintTheme);
    else if (system.addListener) system.addListener(paintTheme);

    initSidebar();
    initUserMenu();

    document.querySelectorAll(".v-toast-close").forEach(function (button) {
      button.addEventListener("click", function () {
        button.closest(".v-toast").remove();
      });
    });

    // Auto-dismiss, but only what is safe to miss. An error stays until dismissed:
    // it usually names something the user still has to act on.
    document.querySelectorAll('.v-toast:not([data-level="error"])').forEach(function (toast) {
      setTimeout(function () {
        toast.remove();
      }, 6000);
    });

    initSelection();

    // "/" focuses search. WCAG 2.1.4 (Level A) requires an unmodified character
    // shortcut to be switchable off, remappable, or focus-scoped; checking
    // activeElement.tagName is an approximation of scoping, not scoping — it misses
    // contenteditable, custom comboboxes and shadow DOM. So it also honours the same
    // on/off preference the grid uses, which the cheat sheet exposes.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      // The grid writes this key through JSON.stringify, so "off" is the string
      // "false". An earlier version compared against "0", a value nothing ever
      // writes, and the toggle silenced every shortcut except this one.
      try {
        if (localStorage.getItem("vantage-grid-shortcuts") === "false") return;
      } catch (e) {
        /* No storage, no preference: the default is on. */
      }
      var active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable)) {
        return;
      }
      var search = document.querySelector('input[type="search"]');
      if (search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    });
  }

  /* ── the account menu ──────────────────────────────────────────────────────
   *
   * The template ships a link to the account page and a panel that is `hidden`. That
   * is the whole no-JavaScript story, and it is a complete one: every option in the
   * menu, sign-out included, is on the page the link points at.
   *
   * This function is the enhancement over it. It is the only place the markup is
   * allowed to start claiming to be a menu, because until it runs, it is not one.
   *
   * What "a real menu" means here, from the WAI-ARIA menu button pattern:
   *
   *   - opens on click, on Enter and on Space, and on ArrowDown/ArrowUp — the last
   *     two land on the first and the last item respectively;
   *   - ArrowUp/ArrowDown move between items and wrap at both ends;
   *   - Home and End jump to the first and last;
   *   - Escape closes it and puts focus back on the button, which is the half people
   *     forget: closing a menu and dropping focus on <body> loses a keyboard user
   *     their place in the page entirely;
   *   - Tab is trapped, and cycles inside the menu rather than escaping it;
   *   - one tab stop, not eight: every item is tabindex="-1" and focus is moved by
   *     this code, so the menu costs a keyboard user exactly one Tab whether it is
   *     open or shut.
   */

  function initUserMenu() {
    var root = document.querySelector("[data-usermenu]");
    if (!root) return;
    var trigger = root.querySelector("[data-usermenu-trigger]");
    var panel = root.querySelector("[data-usermenu-panel]");
    if (!trigger || !panel) return;

    trigger.setAttribute("role", "button");
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", panel.id);

    function items() {
      var found = panel.querySelectorAll('[role="menuitem"], [role="menuitemradio"]');
      return Array.prototype.filter.call(found, function (node) {
        return !node.disabled && !node.hidden;
      });
    }

    function isOpen() {
      return !panel.hidden;
    }

    function open(index) {
      panel.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      var list = items();
      if (!list.length) return;
      list[index < 0 ? list.length + index : index].focus();
    }

    function close(refocus) {
      if (!isOpen()) return;
      panel.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (refocus) trigger.focus();
    }

    function move(step) {
      var list = items();
      if (!list.length) return;
      var at = Array.prototype.indexOf.call(list, document.activeElement);
      var next = at === -1 ? (step > 0 ? 0 : list.length - 1) : (at + step + list.length) % list.length;
      list[next].focus();
    }

    trigger.addEventListener("click", function (event) {
      /* A modified click is someone asking for the account page in a new tab. The
       * href is still there for exactly that, so this gets out of the way. */
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (isOpen()) close(true);
      else open(0);
    });

    trigger.addEventListener("keydown", function (event) {
      /* Space does not activate a link, and this is a link until the line above
       * turned it into a button. Enter is prevented too, or the browser follows the
       * href with the synthesised click that Enter generates. */
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        open(0);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        open(0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        open(-1);
      }
    });

    panel.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        move(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        move(-1);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        var list = items();
        var edge = event.key === "Home" ? list[0] : list[list.length - 1];
        if (edge) edge.focus();
      } else if (event.key === "Tab") {
        event.preventDefault();
        move(event.shiftKey ? -1 : 1);
      }
    });

    /* A click anywhere else dismisses it, without stealing focus back — the user is
     * already on their way somewhere and moving their focus would fight them. */
    document.addEventListener("click", function (event) {
      if (isOpen() && !root.contains(event.target)) close(false);
    });
  }

  var SIDEBAR_KEY = "vantage-sidebar-collapsed";

  function initSidebar() {
    var toggle = document.getElementById("v-sidebar-toggle");
    var sidebar = document.getElementById("v-sidebar");
    var shell = sidebar && sidebar.closest(".v-shell");
    if (!toggle || !sidebar || !shell) return;

    // Two behaviours behind one control, because they are the same intent at
    // different widths: on a narrow screen the sidebar is an overlay that opens and
    // shuts; on a wide one it collapses to an icon rail and stays put.
    function narrow() {
      return window.matchMedia("(max-width: 60rem)").matches;
    }

    function collapsed() {
      return shell.getAttribute("data-collapsed") === "true";
    }

    function paint() {
      var isCollapsed = collapsed();
      toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
      toggle.setAttribute(
        "aria-label",
        isCollapsed ? gettext("Expand navigation") : gettext("Collapse navigation")
      );
    }

    try {
      if (localStorage.getItem(SIDEBAR_KEY) === "1") {
        shell.setAttribute("data-collapsed", "true");
      }
    } catch (e) {
      /* Safari private mode. The sidebar simply starts expanded. */
    }
    paint();

    toggle.addEventListener("click", function () {
      if (narrow()) {
        var open = sidebar.getAttribute("data-open") === "true";
        sidebar.setAttribute("data-open", open ? "false" : "true");
        toggle.setAttribute("aria-expanded", open ? "false" : "true");
        return;
      }
      var next = !collapsed();
      shell.setAttribute("data-collapsed", next ? "true" : "false");
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
      } catch (e) {
        /* Not persisting is survivable; not collapsing would not be. */
      }
      paint();
    });
  }

  function initSelection() {
    var bar = document.querySelector("[data-action-bar]");
    if (!bar) return;

    var form = document.getElementById("v-action-form");
    var pageBox = form.querySelector("[data-select-page]");
    var rows = Array.prototype.slice.call(form.querySelectorAll("[data-select-row]"));
    var countEl = form.querySelector("[data-selection-count]");
    var allMatching = form.querySelector("[data-select-all-matching]");
    var allFlag = form.querySelector("[data-select-all]");
    var total = Number(bar.getAttribute("data-total") || rows.length);
    var acrossPages = false;

    function selected() {
      return rows.filter(function (row) { return row.checked; });
    }

    function paint() {
      var chosen = selected().length;
      bar.hidden = chosen === 0 && !acrossPages;

      if (acrossPages) {
        countEl.textContent = interpolate(gettext("All %s matching records selected"), [total]);
      } else {
        countEl.textContent = interpolate(
          ngettext("%(count)s record selected", "%(count)s records selected", chosen),
          { count: chosen },
          true
        );
      }

      // Offering "select all matching" before the page is fully ticked would be
      // ambiguous about what it adds to.
      allMatching.hidden = acrossPages || chosen === 0 || chosen < rows.length || total <= rows.length;
      allFlag.value = acrossPages ? "1" : "";

      if (pageBox) {
        pageBox.checked = chosen > 0 && chosen === rows.length;
        pageBox.indeterminate = chosen > 0 && chosen < rows.length;
      }
    }

    if (pageBox) {
      pageBox.addEventListener("change", function () {
        acrossPages = false;
        rows.forEach(function (row) { row.checked = pageBox.checked; });
        paint();
      });
    }

    rows.forEach(function (row) {
      row.addEventListener("change", function () {
        // Unticking a row after "select all matching" narrows the set rather than
        // discarding it: that is the common case, not an edge one.
        if (acrossPages && !row.checked) {
          var excluded = document.createElement("input");
          excluded.type = "hidden";
          excluded.name = "excluded";
          excluded.value = row.value;
          excluded.setAttribute("data-exclusion", row.value);
          form.appendChild(excluded);
        } else if (acrossPages && row.checked) {
          var stale = form.querySelector('[data-exclusion="' + row.value + '"]');
          if (stale) stale.remove();
        }
        paint();
      });
    });

    allMatching.addEventListener("click", function () {
      acrossPages = true;
      rows.forEach(function (row) { row.checked = true; });
      paint();
    });

    paint();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
