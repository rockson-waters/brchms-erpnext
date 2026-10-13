"""
BRCHMS — funding authority and announcement banner for the BRCHMS
church-management system.
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
__version__ = "0.1.0"
