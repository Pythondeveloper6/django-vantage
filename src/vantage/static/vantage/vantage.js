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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
