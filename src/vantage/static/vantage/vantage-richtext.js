/* A small WYSIWYG editor for every <textarea data-richtext>.
 *
 * Plain JavaScript over contenteditable, shipped as this one file: no bundle, no build
 * step, no third-party editor. It is an external script on purpose, so it runs under a
 * Content-Security-Policy that has no 'unsafe-inline' for scripts.
 *
 * The textarea stays in the page, hidden, and is what the form submits: its value is
 * the editor's HTML, copied across on every change and again just before submit. With
 * JavaScript off, the textarea simply stays visible and holds HTML.
 *
 * Nothing here is a security boundary. The server sanitises on save and again on
 * render; this file only avoids creating junk in the first place. It still never puts
 * the textarea's text into the page without filtering it first (see `clean`).
 *
 * document.execCommand is deprecated on paper and implemented by every browser, and
 * there is no replacement that does not mean writing a full editor model.
 */
(function () {
  "use strict";

  var ALLOWED_TAGS = {
    P: 1, BR: 1, STRONG: 1, B: 1, EM: 1, I: 1, U: 1, S: 1, CODE: 1, PRE: 1,
    BLOCKQUOTE: 1, UL: 1, OL: 1, LI: 1, A: 1,
    H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1
  };
  // Tags dropped together with everything inside them.
  var DROP_WITH_CONTENT = { SCRIPT: 1, STYLE: 1, TEMPLATE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1, SVG: 1, MATH: 1 };
  var SAFE_URL = /^(?:https?:|mailto:|tel:|[^:]*$)/i;

  function safeUrl(url) {
    // Control characters and spaces are stripped by browsers inside a scheme, so
    // "java\tscript:" must be judged the way the browser will read it.
    var flat = String(url).replace(/[\u0000- \u007f-\u009f]/g, "");
    return SAFE_URL.test(flat);
  }

  /* Rebuild `source`'s children into `target`, keeping only allowlisted tags and the
   * href/title of links. Works on an inert <template> fragment, so nothing loads or
   * runs while it is being read. */
  function copyClean(source, target, doc) {
    Array.prototype.forEach.call(source.childNodes, function (node) {
      if (node.nodeType === 3) {
        target.appendChild(doc.createTextNode(node.nodeValue));
        return;
      }
      if (node.nodeType !== 1) return;
      var tag = node.tagName.toUpperCase();
      if (DROP_WITH_CONTENT[tag]) return;
      if (!ALLOWED_TAGS[tag]) {
        copyClean(node, target, doc);
        return;
      }
      var copy = doc.createElement(tag.toLowerCase());
      if (tag === "A") {
        var href = node.getAttribute("href");
        if (href && safeUrl(href)) copy.setAttribute("href", href);
        var title = node.getAttribute("title");
        if (title) copy.setAttribute("title", title);
      }
      copyClean(node, copy, doc);
      target.appendChild(copy);
    });
  }

  function clean(html) {
    var template = document.createElement("template");
    template.innerHTML = html;
    var out = document.createElement("div");
    copyClean(template.content, out, document);
    return out.innerHTML;
  }

  var BUTTONS = [
    { key: "bold", label: "B", aria: gettext("Bold"), title: gettext("Bold  (Ctrl+B)"), cmd: "bold", state: "bold" },
    { key: "italic", label: "I", aria: gettext("Italic"), title: gettext("Italic  (Ctrl+I)"), cmd: "italic", state: "italic" },
    { key: "code", label: "‹›", aria: gettext("Code"), title: gettext("Code"), run: toggleCode },
    { key: "link", label: "🔗", aria: gettext("Link"), title: gettext("Link  (Ctrl+K)"), run: makeLink },
    { key: "h2", label: "H", aria: gettext("Heading"), title: gettext("Heading"), run: toggleHeading },
    { key: "ul", label: "•", aria: gettext("Bullet list"), title: gettext("Bullet list"), cmd: "insertUnorderedList" },
    { key: "ol", label: "1.", aria: gettext("Numbered list"), title: gettext("Numbered list"), cmd: "insertOrderedList" },
    { key: "quote", label: "❝", aria: gettext("Quote"), title: gettext("Quote"), run: toggleQuote }
  ];

  function exec(cmd, value) {
    document.execCommand(cmd, false, value === undefined ? null : value);
  }

  function enclosing(surface, tags) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    var node = sel.anchorNode;
    while (node && node !== surface) {
      if (node.nodeType === 1 && tags.indexOf(node.tagName) !== -1) return node;
      node = node.parentNode;
    }
    return null;
  }

  function toggleBlock(surface, tag) {
    var current = enclosing(surface, [tag.toUpperCase()]);
    exec("formatBlock", current ? "P" : tag);
  }

  function toggleHeading(surface) { toggleBlock(surface, "H2"); }
  function toggleQuote(surface) { toggleBlock(surface, "BLOCKQUOTE"); }

  function toggleCode(surface) {
    var existing = enclosing(surface, ["CODE"]);
    if (existing) {
      exec("insertHTML", existing.innerHTML);
      return;
    }
    var sel = window.getSelection();
    var text = sel ? sel.toString() : "";
    if (!text) return;
    var span = document.createElement("span");
    span.textContent = text;
    exec("insertHTML", "<code>" + span.innerHTML + "</code>");
  }

  function makeLink(surface) {
    var url = window.prompt(gettext("Link address (https://…)"), "https://");
    if (!url) return;
    url = url.trim();
    if (!safeUrl(url)) {
      window.alert(gettext("Only http, https, mailto and tel links can be added."));
      return;
    }
    exec("createLink", url);
  }

  function build(textarea) {
    var surface = document.createElement("div");
    surface.className = "v-rt-surface";
    surface.contentEditable = "true";
    surface.setAttribute("role", "textbox");
    surface.setAttribute("aria-multiline", "true");
    if (textarea.id) {
      surface.id = textarea.id + "_editor";
      var label = document.querySelector('label[for="' + textarea.id + '"]');
      if (label) {
        if (!label.id) label.id = textarea.id + "_label";
        surface.setAttribute("aria-labelledby", label.id);
        // The label points at the hidden textarea; send a click to the editor instead.
        label.addEventListener("click", function () { surface.focus(); });
      }
    }
    if (textarea.disabled || textarea.readOnly) surface.contentEditable = "false";
    surface.innerHTML = clean(textarea.value);

    var bar = document.createElement("div");
    bar.className = "v-md-bar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", gettext("Formatting"));
    if (surface.id) bar.setAttribute("aria-controls", surface.id);

    var pressable = [];
    BUTTONS.forEach(function (spec) {
      var button = document.createElement("button");
      button.type = "button"; // never submit the form
      button.className = "v-md-btn v-rt-btn";
      button.textContent = spec.label;
      button.title = spec.title;
      button.setAttribute("aria-label", spec.aria);
      if (spec.state) {
        button.setAttribute("aria-pressed", "false");
        pressable.push([button, spec.state]);
      }
      // mousedown would move focus off the editor and lose the selection.
      button.addEventListener("mousedown", function (event) { event.preventDefault(); });
      button.addEventListener("click", function () {
        surface.focus();
        if (spec.cmd) exec(spec.cmd);
        else spec.run(surface);
        sync();
        paint();
      });
      bar.appendChild(button);
    });

    var count = document.createElement("span");
    count.className = "v-md-count";
    bar.appendChild(count);

    function paint() {
      var chars = surface.textContent.length;
      count.textContent = interpolate(
        ngettext("%(count)s character", "%(count)s characters", chars),
        { count: chars },
        true
      );
      pressable.forEach(function (pair) {
        var on = false;
        try { on = document.queryCommandState(pair[1]); } catch (e) { on = false; }
        pair[0].setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    function sync() {
      var html = clean(surface.innerHTML);
      // An emptied editor leaves "<br>" or "<p><br></p>"; that is no content.
      var next = surface.textContent.trim() === "" ? "" : html;
      if (textarea.value !== next) {
        textarea.value = next;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }

    surface.addEventListener("input", function () { sync(); paint(); });
    surface.addEventListener("keyup", paint);
    surface.addEventListener("mouseup", paint);
    surface.addEventListener("blur", sync);

    // Pasted markup from a web page or office document carries styles and classes the
    // server would strip anyway; paste the text and keep the document clean.
    surface.addEventListener("paste", function (event) {
      var data = event.clipboardData;
      if (!data) return;
      event.preventDefault();
      exec("insertText", data.getData("text/plain"));
    });

    surface.addEventListener("keydown", function (event) {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      var key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        makeLink(surface);
        sync();
      }
      // Ctrl+B and Ctrl+I are the browser's own and already work in contenteditable.
    });

    var form = textarea.form;
    if (form) form.addEventListener("submit", sync);

    // A hidden control cannot be focused to show "required" to the person, so the
    // browser would refuse the submit silently. The server still enforces it.
    textarea.required = false;
    textarea.classList.add("v-rt-hidden");
    textarea.setAttribute("aria-hidden", "true");
    textarea.tabIndex = -1;
    textarea.parentNode.insertBefore(bar, textarea);
    textarea.parentNode.insertBefore(surface, textarea);
    paint();
  }

  function init() {
    document.querySelectorAll("textarea[data-richtext]").forEach(function (textarea) {
      if (!textarea.hasAttribute("data-richtext-ready")) {
        textarea.setAttribute("data-richtext-ready", "1");
        build(textarea);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
