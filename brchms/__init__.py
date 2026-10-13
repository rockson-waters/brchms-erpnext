"""
Rest — funding authority and announcement banner for the Rest
church-management system.

NOTE ON NAMES: the APP is "Rest" (see `app_title` in hooks.py, and the two
doctypes). The Python package, the Frappe app name, the module directories and
the repository keep the historical `brchms` identifier, because those are the
machine keys that the consuming application addresses:
`brchms.brchms_credits.api.confirm_funding`. Renaming them is a cross-repository
change with a window where the two sides disagree, and it buys nothing anyone
reads. Where a human sees a name, it says Rest.
"""

# Frappe reads an app's version with `frappe.get_attr(app + ".__version__")` and
# falls back to the literal string "0.0.1" when the attribute is missing
# (`frappe/utils/__init__.py`, v16). It does NOT read `pyproject.toml`.
#
# That is why this has to exist despite the packaging metadata already carrying
# it: with this file empty, `bench --site <site> list-apps` reports brchms as
# `0.0.1` and the real version is invisible exactly where an operator looks for
# it. Observed on the first deploy, which is how it was found.
#
# `hooks.py` reads THIS value rather than repeating it, so the two cannot drift.
__version__ = "0.5.0"
