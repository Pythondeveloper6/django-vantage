/* Reveal one masked value on request, and mask it again soon after.
 *
 * The server renders every sensitive value as bullets, with the reveal endpoint's URL
 * on the wrapper and never the value itself (see vantage/sensitive.py). This script
 * only adds the button and the round trip:
 *
 *   - POST, never GET: a GET is prefetched, cached, logged and kept in history.
 *   - The value lives in one text node for REVEAL seconds (data-vantage-reveal on
 *     <body>), then the bullets come back.
 *   - It is masked again the moment the page is hidden or left — pagehide covers the
 *     back/forward cache, whose snapshot would otherwise keep the revealed value.
 *
 * Loaded only when the server says this user may reveal something, so a viewer
 * without the permission never sees a button that would only ever say no.
 */
(function () {
  "use strict";

  var body = document.body;
  var seconds = parseInt(body.getAttribute("data-vantage-reveal") || "0", 10);
  if (!seconds) return;

  var t = window.gettext || function (s) { return s; };
  var interpolate = window.interpolate || function (fmt, obj) {
    return fmt.replace(/%\((\w+)\)s/g, function (_, key) { return obj[key]; });
  };

  function csrf() {
    var input =
      document.querySelector("[data-vantage-reveal-csrf] input[name=csrfmiddlewaretoken]") ||
      document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function valueNode(wrapper) {
    return wrapper.querySelector(".v-masked-value");
  }

  function remask(wrapper) {
    var node = valueNode(wrapper);
    var button = wrapper.querySelector(".v-reveal-btn");
    if (wrapper._vantageTimer) {
      clearTimeout(wrapper._vantageTimer);
      wrapper._vantageTimer = null;
    }
    if (node && wrapper.hasAttribute("data-revealed")) {
      node.textContent = wrapper.getAttribute("data-mask-text") || "••••••••";
      node.setAttribute("aria-label", t("Hidden value"));
    }
    wrapper.removeAttribute("data-revealed");
    if (button) {
      button.textContent = t("Reveal");
      button.setAttribute("aria-pressed", "false");
      button.disabled = false;
    }
  }

  function remaskAll() {
    var shown = document.querySelectorAll("[data-vantage-masked][data-revealed]");
    for (var i = 0; i < shown.length; i++) remask(shown[i]);
  }

  function say(wrapper, message) {
    var status = wrapper.querySelector(".v-reveal-status");
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
    if (message) {
      setTimeout(function () {
        status.textContent = "";
        status.hidden = true;
      }, 5000);
    }
  }

  function reveal(wrapper) {
    var button = wrapper.querySelector(".v-reveal-btn");
    var url = wrapper.getAttribute("data-reveal-url");
    if (!url || !button) return;
    button.disabled = true;
    say(wrapper, "");

    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-CSRFToken": csrf(), Accept: "application/json" },
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () { return {}; })
          .then(function (data) { return { ok: response.ok, data: data }; });
      })
      .then(function (result) {
        button.disabled = false;
        if (!result.ok || typeof result.data.value !== "string") {
          say(wrapper, result.data.error || t("This value could not be revealed."));
          return;
        }
        var node = valueNode(wrapper);
        node.textContent = result.data.value;
        node.removeAttribute("aria-label");
        wrapper.setAttribute("data-revealed", "");
        button.textContent = t("Hide");
        button.setAttribute("aria-pressed", "true");
        var ttl = parseInt(result.data.expires_in || seconds, 10) || seconds;
        wrapper._vantageTimer = setTimeout(function () { remask(wrapper); }, ttl * 1000);
        say(wrapper, interpolate(t("Hidden again in %(seconds)s seconds."), { seconds: ttl }, true));
      })
      .catch(function () {
        button.disabled = false;
        say(wrapper, t("This value could not be revealed."));
      });
  }

  function enhance(root) {
    var wrappers = (root || document).querySelectorAll("[data-vantage-masked][data-reveal-url]");
    for (var i = 0; i < wrappers.length; i++) {
      var wrapper = wrappers[i];
      if (wrapper.querySelector(".v-reveal-btn")) continue;
      var node = valueNode(wrapper);
      if (node) wrapper.setAttribute("data-mask-text", node.textContent);
      var label = wrapper.getAttribute("data-reveal-label") || "";

      var button = document.createElement("button");
      button.type = "button";
      button.className = "v-reveal-btn";
      button.textContent = t("Reveal");
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", interpolate(t("Reveal %(field)s"), { field: label }, true));
      wrapper.appendChild(button);

      var status = document.createElement("span");
      status.className = "v-reveal-status";
      status.setAttribute("role", "status");
      status.hidden = true;
      wrapper.appendChild(status);
    }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest(".v-reveal-btn");
    if (!button) return;
    var wrapper = button.closest("[data-vantage-masked]");
    if (!wrapper) return;
    // A masked cell can sit inside a row link or a selectable row; the click is ours.
    event.preventDefault();
    event.stopPropagation();
    if (wrapper.hasAttribute("data-revealed")) remask(wrapper);
    else reveal(wrapper);
  });

  window.addEventListener("pagehide", remaskAll);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") remaskAll();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { enhance(document); });
  } else {
    enhance(document);
  }
})();
