"""The write end of the per-list customisation rail.

One POST handler, six sections, two scopes. Everything interesting about it is a
refusal:

**It is POST only, and it renders nothing.** The rail is drawn by the list page it
belongs to; this view exists to change a row and send the browser back to where it was.
A GET is answered with a redirect rather than a 405, because a preference URL that ends
up in someone's history should not greet them with an error page.

**It never trusts the form.** Validation is not "clean the input", it is the security
boundary: a field name from this POST becomes part of a query on every subsequent
render of that list. :func:`vantage.listprefs.clean_section` checks every name,
aggregate and chart target against what the panel actually exposes, and anything else
is dropped without comment. There is no error state to report because there is no
legitimate way to submit one — every control on the rail is drawn from the same
allowlist that validates it.

**Saving for everyone is a separate permission from saving for yourself.** The default
scope is the person doing it. The site-wide row is what every colleague opens tomorrow
morning, so writing it takes the same kind of permission the settings screen takes, and
a POST asking for it without that permission is refused rather than quietly downgraded
to a personal save — a silent downgrade would look exactly like success.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from vantage import listprefs
from vantage.views import PanelView

__all__ = ["ListPrefsView"]

#: What each section is called when a message names it.
SECTION_LABELS: dict[str, str] = {
    "columns": "Columns",
    "search": "Search",
    "filters": "Filters",
    "stats": "Statistics",
    "charts": "Charts",
    "summary": "Summary bar",
}


class ListPrefsView(PanelView):
    """Store or clear one section of one list's preferences."""

    http_method_names = ["get", "post"]

    #: Tuning a list is a way of looking at it, not a way of changing it. Requiring
    #: ``change`` here would mean a read-only colleague could not so much as hide a
    #: column, and nothing on this screen can alter a single row of their data.
    required_action = "view"

    def get(self, request, *args, **kwargs):
        return self.back(request)

    def post(self, request, *args, **kwargs):
        section = request.POST.get("section", "")
        if section not in listprefs.SECTIONS:
            # Only reachable from a hand-written POST. Nothing to do, and naming the
            # sections that do exist would be answering a question nobody should ask.
            return self.back(request)

        action, scope = self.intent(request)
        user = self.scope_user(request, scope)
        label = SECTION_LABELS.get(section, section.capitalize())
        whom = "everyone" if user is None else "you"

        if action == "reset":
            listprefs.reset_section(self.panel, request, section, user=user)
            messages.success(request, f"{label} reset to what the code declares, for {whom}.")
        else:
            listprefs.store_section(self.panel, request, section, request.POST, user=user)
            messages.success(request, f"{label} saved for {whom}.")

        return self.back(request)

    @staticmethod
    def intent(request) -> tuple[str, str]:
        """What was asked for, from the one button that was pressed.

        Four buttons share one name — ``save:user``, ``save:site``, ``reset:user``,
        ``reset:site`` — rather than each carrying its own, because only the pressed
        submit contributes a value and a form cannot express "this button also means
        that hidden field". One parameter is also one place for the scope check to
        read, which is what keeps the permission gate from having two doors.

        Missing or unrecognised falls to the least surprising pair: save, for the
        person doing it. That is the Enter key on a text box, not an attack.
        """
        action, _, scope = request.POST.get("do", "").partition(":")
        return (
            action if action in ("save", "reset") else "save",
            scope if scope in ("user", "site") else "user",
        )

    @staticmethod
    def scope_user(request, scope: str):
        """The row this POST is allowed to write: the person, or ``None`` for everyone."""
        if scope != "site":
            return request.user
        if not listprefs.may_set_site_default(request):
            raise PermissionDenied(
                "You do not have permission to change the default for everyone."
            )
        return None

    def back(self, request):
        """Return to the list, keeping its filters, search and page.

        ``next`` comes from the browser, so it is checked before it is followed: an
        unchecked redirect target on an authenticated page is an open redirect that
        borrows the panel's credibility.
        """
        target = request.POST.get("next") or request.GET.get("next") or ""
        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return HttpResponseRedirect(target)
        return HttpResponseRedirect(reverse(self.panel.list_url_name))
