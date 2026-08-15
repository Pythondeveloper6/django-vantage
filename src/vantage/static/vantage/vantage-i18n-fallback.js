/* Stand-ins for gettext()/ngettext()/interpolate()/pgettext()/npgettext(), defined only
 * if Django's own JavaScriptCatalog script did not already define them.
 *
 * That script is loaded immediately before this one — see base.html — and on a normal
 * deployment it always wins the race: this file only ever finds an empty window. It
 * exists for the times that is not true. The catalogue is a view, not a static file, so
 * `collectstatic` cannot be the reason it is missing, but the route can still 404 (an
 * old urls.py that has not picked up the newest vantage.urls.build()), a proxy or CDN
 * can strip an unrecognised script, or a network blip can drop just this one request
 * while every deferred script after it still runs. Whatever the cause, every other file
 * in this directory calls gettext() and friends without checking first, so this file has
 * to run before any of them do, or the panel throws "gettext is not defined" on load and
 * stops working entirely rather than degrading to English.
 *
 * These fall back to the literal source string — never a blank one — exactly like the
 * server-side catalogue does for an msgid with no translation. A project running with
 * JavaScript entirely off never reaches any of this: nothing here executes, and the
 * server-rendered page it already has works exactly as before this file existed.
 */
(function (global) {
  "use strict";

  if (typeof global.gettext === "function") return;

  global.gettext_noop = function (msgid) {
    return msgid;
  };

  global.gettext = function (msgid) {
    return msgid;
  };

  global.ngettext = function (singular, plural, count) {
    return count === 1 ? singular : plural;
  };

  global.pgettext = function (context, msgid) {
    return msgid;
  };

  global.npgettext = function (context, singular, plural, count) {
    return count === 1 ? singular : plural;
  };

  /* Mirrors django.utils.translation's JS interpolate(): %s placeholders against an
   * array, or %(name)s placeholders against an object when `named` is truthy. */
  global.interpolate = function (fmt, obj, named) {
    if (named) {
      return fmt.replace(/%\(\w+\)s/g, function (match) {
        return String(obj[match.slice(2, -2)]);
      });
    }
    var i = 0;
    return fmt.replace(/%s/g, function () {
      return String(obj[i++]);
    });
  };
})(window);
