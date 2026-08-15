/* django-vantage — the background-job progress bars.
 *
 * Strictly additive. The page is a server-rendered table: every bar already shows the
 * right number when it arrives, every finished job already shows its summary, and the
 * error CSV is already a link. Delete this file and the page still tells the truth —
 * it just stops updating without a refresh.
 *
 * Three rules, and the first two are the whole reason this file is careful:
 *
 * 1. **Stop when the job is terminal.** The server says so; this does not guess from a
 *    list of statuses it would have to keep in step with Python. A bar that keeps
 *    polling after the job has finished is one request per second per open tab,
 *    forever, and it is the usual way a progress UI becomes a load problem.
 * 2. **Stop when the answer stops changing or the server stops answering.** The
 *    interval backs off while nothing moves and after an error, and gives up entirely
 *    after a long run of failures. A worker that died must not leave a browser
 *    hammering a 500 for the rest of the afternoon.
 * 3. **Do nothing while the tab is hidden.** A dashboard left open on a second monitor
 *    for a week is the common case, not the rare one.
 *
 * Polling rather than SSE is a deployment decision, not a preference: an event stream
 * needs ASGI and holds a worker per watcher, and this package supports WSGI.
 */
(function () {
  "use strict";

  var BASE_MS = 1500;
  /* Idle backoff. A job whose counts have not moved for several ticks is either
   * between chunks or wedged; either way, asking four times a second helps nobody. */
  var MAX_MS = 15000;
  var GIVE_UP_AFTER_ERRORS = 5;

  var bars = document.querySelectorAll("[data-job-poll]");
  if (!bars.length) return;

  Array.prototype.forEach.call(bars, function (root) {
    watch(root, root.getAttribute("data-job-poll"));
  });

  function watch(root, url) {
    var delay = BASE_MS;
    var errors = 0;
    var lastProcessed = -1;
    var timer = null;

    tick();

    function schedule() {
      timer = window.setTimeout(tick, delay);
    }

    function stop() {
      if (timer) window.clearTimeout(timer);
      root.removeAttribute("data-job-poll");
    }

    function tick() {
      /* Hidden tab: skip the request, keep the timer. Cheaper than tearing the
       * watcher down and rebuilding it on visibilitychange, and it means a tab
       * brought back to the front is at most one interval out of date. */
      if (document.hidden) return schedule();

      fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error(String(response.status));
          return response.json();
        })
        .then(function (data) {
          errors = 0;
          render(root, data);

          if (data.terminal) return stop();

          /* Moving: poll at the base rate. Still: back off geometrically. */
          if (data.processed !== lastProcessed) {
            delay = data.poll_ms || BASE_MS;
            lastProcessed = data.processed;
          } else {
            delay = Math.min(delay * 1.5, MAX_MS);
          }
          schedule();
        })
        .catch(function () {
          errors += 1;
          if (errors >= GIVE_UP_AFTER_ERRORS) return stop();
          delay = Math.min(delay * 2, MAX_MS);
          schedule();
        });
    }
  }

  function render(root, data) {
    var fill = root.querySelector("[data-job-fill]");
    var track = root.querySelector("[data-job-fill-track]");
    var counts = root.querySelector("[data-job-counts]");
    var link = root.querySelector("[data-job-errors]");

    root.setAttribute("data-job-status", data.status);
    if (fill) fill.style.setProperty("--v-job-percent", data.percent);
    if (track) track.setAttribute("aria-valuenow", data.percent);

    if (counts) {
      /* textContent, never innerHTML. Every string here is server-composed but it
       * quotes model data — an error message contains whatever was in the row — and
       * the moment that is written as markup the report becomes stored XSS. */
      counts.textContent = data.terminal
        ? data.summary || data.status_label
        : data.failed
          ? interpolate(
              gettext("%(processed)s of %(total)s, %(failed)s failed"),
              { processed: format(data.processed), total: format(data.total), failed: format(data.failed) },
              true
            )
          : interpolate(
              gettext("%(processed)s of %(total)s"),
              { processed: format(data.processed), total: format(data.total) },
              true
            );
    }

    if (link && data.errors_url) {
      link.setAttribute("href", data.errors_url);
      link.removeAttribute("hidden");
    }

    var badge = document.querySelector('[data-job-row="' + data.id + '"] [data-job-badge]');
    if (badge) {
      badge.setAttribute("data-status", data.status);
      badge.textContent = data.status_label;
    }
  }

  function format(value) {
    try {
      return Number(value).toLocaleString();
    } catch (e) {
      return String(value);
    }
  }
})();
