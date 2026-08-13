"""Custom pages used by the test suite.

They live here rather than inside the test module because pages have to be registered
before the urlconf is built — which is exactly the real-world constraint, and why
``panel.py`` is the place to register them.
"""

from vantage.pages import Page


class RevenuePage(Page):
    title = "Revenue"
    icon = "trending-up"
    group = "Reports"

    def get_context(self, request):
        return {"total": 1234, "orders": 56}


class BareMinimumPage(Page):
    """No title, no path, no template. Should still work."""


class GatedPage(Page):
    title = "Gated"
    permission = "testapp.view_post"


class CallableGatedPage(Page):
    title = "Callable"
    permission = staticmethod(lambda request: request.user.username == "boss")


class CustomPathPage(Page):
    title = "Ops Console"
    path = "ops/console/"
