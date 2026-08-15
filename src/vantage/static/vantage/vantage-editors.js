/* Progressive enhancement for one control the browser renders badly: a plain
 * <textarea> that is expected to hold markdown.
 *
 * Dependency-free on purpose. Shipping CodeMirror or a WYSIWYG package would mean a
 * bundler, a build step for contributors, and a hundred kilobytes for a text box —
 * against a package whose whole premise is `pip install` and nothing else. What is
 * here is small enough to read, and it degrades to the plain textarea it enhances:
 * with JavaScript off you get a working textarea, just without the buttons.
 *
 * Every field a RICHTEXT-classified model field renders as carries `data-markdown` —
 * see `vantage.widgets.infer_widget` — because Vantage integrates no CKEditor, TinyMCE
 * or Quill of its own: whichever package's class name triggered the classification,
 * what actually renders is this same textarea, so the same toolbar suits all of them.
 *
 * A drop zone for file inputs used to live in this file too. It is gone: every upload
 * control in the package is now built from `vantage/widgets/file.html`, which already
 * wires drag-and-drop, a preview and a byte-size check through `upload.js`. A second,
 * independent drop zone here would not enhance those controls, it would wrap them a
 * second time in different markup — so it was dead weight duplicating something better,
 * not a working feature waiting to be linked in.
 */
(function () {
  "use strict";

  var ACTIONS = [
    { key: "bold", label: "B", aria: gettext("Bold"), title: gettext("Bold  (Ctrl+B)"), wrap: ["**", "**"] },
    { key: "italic", label: "I", aria: gettext("Italic"), title: gettext("Italic  (Ctrl+I)"), wrap: ["_", "_"] },
    { key: "code", label: "‹›", aria: gettext("Code"), title: gettext("Code"), wrap: ["`", "`"] },
    { key: "link", label: "🔗", aria: gettext("Link"), title: gettext("Link  (Ctrl+K)"), wrap: ["[", "](url)"] },
    { key: "h2", label: "H", aria: gettext("Heading"), title: gettext("Heading"), prefix: "## " },
    { key: "ul", label: "•", aria: gettext("Bullet list"), title: gettext("Bullet list"), prefix: "- " },
    { key: "ol", label: "1.", aria: gettext("Numbered list"), title: gettext("Numbered list"), prefix: "1. " },
    { key: "quote", label: "❝", aria: gettext("Quote"), title: gettext("Quote"), prefix: "> " },
  ];

  function surround(field, before, after) {
    var start = field.selectionStart;
    var end = field.selectionEnd;
    var selected = field.value.slice(start, end);
    field.setRangeText(before + selected + after, start, end, "select");
    // Put the caret inside the markers when nothing was selected, so the next
    // keystroke lands where the writer expects rather than after the closing pair.
    if (start === end) {
      field.selectionStart = field.selectionEnd = start + before.length;
    }
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
  }

  function prefixLines(field, prefix) {
    var value = field.value;
    var start = value.lastIndexOf("\n", field.selectionStart - 1) + 1;
    var end = value.indexOf("\n", field.selectionEnd);
    if (end === -1) end = value.length;

    var block = value.slice(start, end);
    var already = block.split("\n").every(function (line) {
      return line.startsWith(prefix) || line === "";
    });
    var next = block
      .split("\n")
      .map(function (line) {
        if (line === "") return line;
        return already ? line.slice(prefix.length) : prefix + line;
      })
      .join("\n");

    field.setRangeText(next, start, end, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
  }

  function apply(field, action) {
    if (action.wrap) surround(field, action.wrap[0], action.wrap[1]);
    else prefixLines(field, action.prefix);
  }

  function buildToolbar(field) {
    var bar = document.createElement("div");
    bar.className = "v-md-bar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", gettext("Formatting"));
    bar.setAttribute("aria-controls", field.id);

    ACTIONS.forEach(function (action) {
      var button = document.createElement("button");
      button.type = "button"; // never submit the form
      button.className = "v-md-btn";
      button.textContent = action.label;
      button.title = action.title;
      button.setAttribute("aria-label", action.aria);
      button.addEventListener("click", function () {
        apply(field, action);
      });
      bar.appendChild(button);
    });

    var count = document.createElement("span");
    count.className = "v-md-count";
    function paint() {
      var chars = field.value.length;
      count.textContent = interpolate(
        ngettext("%(count)s character", "%(count)s characters", chars),
        { count: chars },
        true
      );
    }
    field.addEventListener("input", paint);
    paint();
    bar.appendChild(count);

    field.addEventListener("keydown", function (event) {
      if (!(event.metaKey || event.ctrlKey)) return;
      var key = event.key.toLowerCase();
      var match = { b: "bold", i: "italic", k: "link" }[key];
      if (!match) return;
      event.preventDefault();
      apply(
        field,
        ACTIONS.filter(function (a) {
          return a.key === match;
        })[0]
      );
    });

    field.parentNode.insertBefore(bar, field);
    field.classList.add("v-md-field");
  }

  function init() {
    document.querySelectorAll("textarea[data-markdown]").forEach(buildToolbar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
