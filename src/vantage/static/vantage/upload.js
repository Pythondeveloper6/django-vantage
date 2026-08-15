/* Drag, drop, preview and remove for file and image fields.
 *
 * Everything here is an enhancement of markup that already works. The control renders
 * as <input type="file"> plus Django's clear checkbox; with this script the same DOM
 * also accepts a dropped file, shows what is about to be uploaded, states the size
 * against the server's ceiling before the round trip, and turns the checkbox into a
 * remove button. Turn the script off and none of it is missing — it is quieter.
 *
 * Three decisions worth knowing about.
 *
 * Delegation, not per-widget wiring. One set of listeners on the document handles every
 * upload control on the page, including controls added later by the inline formset
 * machinery, which is exactly the case that breaks a querySelectorAll-on-load approach.
 *
 * The widget template carries its own <script src> — a widget cannot reach the page's
 * <head>, and a strict CSP forbids the inline alternative. Several file fields therefore
 * emit several identical tags, so the guard below makes the second and third no-ops.
 *
 * Progress is deliberately indeterminate. A normal form submit is one multipart body
 * and the page cannot observe how much of it has been sent without taking the submit
 * over with XMLHttpRequest, which would mean reimplementing redirects, validation
 * re-renders and the unsaved-changes guard. An honest "uploading 4.2 MB" beats a
 * progress bar animating on a timer, which is what every panel that fakes this does.
 */
(function () {
  'use strict';

  if (window.__vantageUpload) return;
  window.__vantageUpload = true;

  var DRAG_ATTR = 'data-dragging';
  /* Object URLs are revoked when the preview they feed is replaced or undone; a page
   * that never revokes them holds every previewed image in memory until navigation. */
  var previews = new WeakMap();

  function closestUpload(node) {
    return node && node.closest ? node.closest('[data-vantage-upload]') : null;
  }

  function part(root, name) {
    return root ? root.querySelector('[data-vantage-upload-' + name + ']') : null;
  }

  function input(root) {
    return root ? root.querySelector('input[type="file"]') : null;
  }

  /* 1 decimal place under 10 MB, none above: "0.4 MB" is useful and "12.0 MB" is not. */
  function humanBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    var kb = bytes / 1024;
    if (kb < 1024) return Math.round(kb) + ' KB';
    var mb = kb / 1024;
    return (mb < 10 ? mb.toFixed(1) : Math.round(mb)) + ' MB';
  }

  function showError(root, message) {
    var box = part(root, 'error');
    if (!box) return;
    box.textContent = message || '';
    box.hidden = !message;
    root.toggleAttribute('data-invalid', Boolean(message));
  }

  function clearPreview(image) {
    var url = previews.get(image);
    if (url) {
      URL.revokeObjectURL(url);
      previews.delete(image);
    }
    image.removeAttribute('src');
    image.alt = '';
    image.hidden = true;
  }

  function reset(root) {
    var chosen = part(root, 'chosen');
    var image = part(root, 'preview');
    if (image) clearPreview(image);
    if (chosen) chosen.hidden = true;
    showError(root, '');
  }

  function describe(root, file) {
    var chosen = part(root, 'chosen');
    var name = part(root, 'chosen-name');
    var size = part(root, 'chosen-size');
    if (name) name.textContent = file.name;
    if (size) size.textContent = humanBytes(file.size);
    if (chosen) chosen.hidden = false;
  }

  /* An SVG is never previewed. Rendering one means putting an untrusted document in the
   * page, and the panel's rule is that it does not do that anywhere — see media.py. */
  function previewable(root, file) {
    return (
      root.getAttribute('data-preview') === 'image' &&
      file.type.indexOf('image/') === 0 &&
      file.type !== 'image/svg+xml'
    );
  }

  function preview(root, file) {
    var image = part(root, 'preview');
    if (!image) return;
    clearPreview(image);
    if (!previewable(root, file)) return;
    var url = URL.createObjectURL(file);
    previews.set(image, url);
    image.src = url;
    image.alt = file.name;
    image.hidden = false;
  }

  function accepted(root, file) {
    var limit = parseInt(root.getAttribute('data-max-bytes'), 10);
    if (limit > 0 && file.size > limit) {
      /* Said here as well as on the server. The server still decides — this only saves
       * the person from watching a 40 MB upload finish and then be refused. */
      showError(
        root,
        interpolate(
          gettext('That file is %(size)s. The limit is %(limit)s.'),
          { size: humanBytes(file.size), limit: humanBytes(limit) },
          true
        )
      );
      return false;
    }
    return true;
  }

  function adopt(root, file) {
    showError(root, '');
    if (!accepted(root, file)) {
      reset(root);
      return false;
    }
    describe(root, file);
    preview(root, file);
    return true;
  }

  function selected(root) {
    var field = input(root);
    if (!field) return;
    if (!field.files || !field.files.length) {
      reset(root);
      return;
    }
    if (!adopt(root, field.files[0])) field.value = '';
  }

  /* ── drag and drop ─────────────────────────────────────────────────────── */

  function stop(event) {
    event.preventDefault();
    event.stopPropagation();
  }

  document.addEventListener('dragover', function (event) {
    var zone = event.target.closest ? event.target.closest('[data-vantage-upload-drop]') : null;
    if (!zone) return;
    stop(event);
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    zone.setAttribute(DRAG_ATTR, 'true');
  });

  document.addEventListener('dragleave', function (event) {
    var zone = event.target.closest ? event.target.closest('[data-vantage-upload-drop]') : null;
    /* relatedTarget is where the pointer went. Without this check, dragging across a
     * child element fires dragleave and the zone flickers out from under the cursor. */
    if (!zone || (event.relatedTarget && zone.contains(event.relatedTarget))) return;
    zone.removeAttribute(DRAG_ATTR);
  });

  document.addEventListener('drop', function (event) {
    var zone = event.target.closest ? event.target.closest('[data-vantage-upload-drop]') : null;
    if (!zone) return;
    stop(event);
    zone.removeAttribute(DRAG_ATTR);

    var root = closestUpload(zone);
    var field = input(root);
    var files = event.dataTransfer && event.dataTransfer.files;
    if (!root || !field || !files || !files.length) return;

    /* Assigning a FileList straight across is what makes the dropped file part of the
     * ordinary form submission, so nothing about the POST is special-cased. */
    try {
      field.files = files;
    } catch (error) {
      showError(root, gettext('This browser cannot accept a dropped file. Use the button instead.'));
      return;
    }
    selected(root);
    /* Anything else watching the form — the unsaved-changes guard, an inline editor —
     * learns about the file the same way it would have from a click. */
    field.dispatchEvent(new Event('change', { bubbles: true }));
  });

  /* ── choosing, undoing, removing ───────────────────────────────────────── */

  document.addEventListener('change', function (event) {
    var field = event.target;
    if (!field.matches || !field.matches('input[type="file"]')) return;
    var root = closestUpload(field);
    if (root) selected(root);
  });

  document.addEventListener('click', function (event) {
    var undo = event.target.closest ? event.target.closest('[data-vantage-upload-undo]') : null;
    if (undo) {
      var root = closestUpload(undo);
      var field = input(root);
      if (field) field.value = '';
      reset(root);
      if (field) field.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    var box = event.target.closest ? event.target.closest('[data-vantage-upload-clear]') : null;
    if (box) {
      var current = closestUpload(box);
      var block = part(current, 'current');
      /* The checkbox stays the mechanism — it is what the server reads. This only makes
       * the consequence visible before the save rather than after it. */
      if (block) block.toggleAttribute('data-removing', box.checked);
    }
  });

  /* ── submit ────────────────────────────────────────────────────────────── */

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.querySelectorAll) return;

    form.querySelectorAll('[data-vantage-upload]').forEach(function (root) {
      var field = input(root);
      if (!field || !field.files || !field.files.length) return;
      var row = part(root, 'progress');
      var text = part(root, 'progress-text');
      if (text) {
        text.textContent = interpolate(gettext('Uploading %s…'), [humanBytes(field.files[0].size)]);
      }
      if (row) row.hidden = false;
    });
  });
})();
