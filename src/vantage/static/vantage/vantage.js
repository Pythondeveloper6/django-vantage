/* django-vantage runtime.
 *
 * Deliberately small and dependency-free. Anything that can be done on the server is
 * done on the server; this covers only what needs to happen in the browser.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "vantage-theme";

  function currentTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "dark" || explicit === "light") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function paintToggle(theme) {
    document.querySelectorAll("[data-theme-icon]").forEach(function (node) {
      node.hidden = node.getAttribute("data-theme-icon") !== theme;
    });
  }

  function applyTheme(theme, endpoint) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (e) {
      /* Safari private mode throws; the cookie below still persists the choice. */
    }
    paintToggle(theme);

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

  function init() {
    paintToggle(currentTheme());

    document.querySelectorAll("[data-vantage-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        applyTheme(currentTheme() === "dark" ? "light" : "dark", button.getAttribute("formaction"));
      });
    });

    var toggle = document.getElementById("v-sidebar-toggle");
    var sidebar = document.getElementById("v-sidebar");
    if (toggle && sidebar) {
      toggle.addEventListener("click", function () {
        var open = sidebar.getAttribute("data-open") === "true";
        sidebar.setAttribute("data-open", open ? "false" : "true");
        toggle.setAttribute("aria-expanded", open ? "false" : "true");
      });
    }

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

    // "/" focuses search, the convention every data-heavy app shares. Scoped so it
    // never fires while the user is typing somewhere else (WCAG 2.1.4).
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
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
        countEl.textContent = "All " + total + " matching records selected";
      } else {
        countEl.textContent = chosen + (chosen === 1 ? " record" : " records") + " selected";
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
