/* Progressive enhancement for two controls the browser renders badly.
 *
 * Both are deliberately dependency-free. Shipping CodeMirror or Dropzone would mean a
 * bundler, a build step for contributors, and a hundred kilobytes for a text box —
 * against a package whose whole premise is `pip install` and nothing else. What is
 * here is small enough to read, and everything degrades to the plain control it
 * enhances: with JavaScript off you get a textarea and a file input, which work.
 */
(function () {
  "use strict";

  // ── markdown toolbar ──────────────────────────────────────────────────────

  var ACTIONS = [
    { key: "bold", label: "B", title: "Bold  (Ctrl+B)", wrap: ["**", "**"] },
    { key: "italic", label: "I", title: "Italic  (Ctrl+I)", wrap: ["_", "_"] },
    { key: "code", label: "‹›", title: "Code", wrap: ["`", "`"] },
    { key: "link", label: "🔗", title: "Link  (Ctrl+K)", wrap: ["[", "](url)"] },
    { key: "h2", label: "H", title: "Heading", prefix: "## " },
    { key: "ul", label: "•", title: "Bullet list", prefix: "- " },
    { key: "ol", label: "1.", title: "Numbered list", prefix: "1. " },
    { key: "quote", label: "❝", title: "Quote", prefix: "> " },
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
    bar.setAttribute("aria-label", "Formatting");
    bar.setAttribute("aria-controls", field.id);

    ACTIONS.forEach(function (action) {
      var button = document.createElement("button");
      button.type = "button"; // never submit the form
      button.className = "v-md-btn";
      button.textContent = action.label;
      button.title = action.title;
      button.setAttribute("aria-label", action.title.split("  ")[0]);
      button.addEventListener("click", function () {
        apply(field, action);
      });
      bar.appendChild(button);
    });

    var count = document.createElement("span");
    count.className = "v-md-count";
    function paint() {
      var chars = field.value.length;
      count.textContent = chars + (chars === 1 ? " character" : " characters");
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

  // ── file drop zone ────────────────────────────────────────────────────────

  function humanSize(bytes) {
    var units = ["B", "KB", "MB", "GB"];
    var index = 0;
    while (bytes >= 1024 && index < units.length - 1) {
      bytes /= 1024;
      index += 1;
    }
    return (index === 0 ? bytes : bytes.toFixed(1)) + " " + units[index];
  }

  function buildDropzone(input) {
    var zone = document.createElement("div");
    zone.className = "v-drop";
    // Not a button: the real <input type=file> stays in the DOM and keeps its own
    // keyboard behaviour and its label association. This is decoration over it.
    zone.innerHTML =
      '<p class="v-drop-hint">Drop a file here, or <span class="v-drop-link">choose one</span></p>' +
      '<p class="v-drop-name" hidden></p>';

    input.parentNode.insertBefore(zone, input);
    zone.appendChild(input);
    input.classList.add("v-drop-input");

    var name = zone.querySelector(".v-drop-name");

    function describe() {
      var file = input.files && input.files[0];
      name.hidden = !file;
      if (file) {
        name.textContent = file.name + " · " + humanSize(file.size);
      }
    }

    input.addEventListener("change", describe);
    describe();

    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add("v-drop-over");
      });
    });

    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.remove("v-drop-over");
      });
    });

    zone.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      // Assigning to input.files means the normal form submit carries the file, so
      // there is no second upload path to keep in step with the first.
      input.files = files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  function init() {
    document.querySelectorAll("textarea[data-markdown]").forEach(buildToolbar);
    document.querySelectorAll('input[type="file"]').forEach(function (input) {
      if (!input.closest(".v-drop")) buildDropzone(input);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
