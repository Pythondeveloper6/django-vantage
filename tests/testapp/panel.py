"""Autodiscovered by Vantage at startup.

The test app deliberately registers no ``ModelPanel`` — its models are meant to go
through pure inference. It registers pages, because pages must exist before the
urlconf is built.
"""

from tests.testapp import pages
from vantage import site

for page in (
    pages.RevenuePage,
    pages.BareMinimumPage,
    pages.GatedPage,
    pages.CustomPathPage,
):
    site.register_page(page)
