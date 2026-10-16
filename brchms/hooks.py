"""
Rest — the Frappe app that lets the Rest church-management system reach this
ERPNext instance.

Two jobs, deliberately in ONE app:

  brchms_credits  — the funding authority. `confirm_funding` records what has
                    been paid for, once, and hands the app the units and money it
                    needs to move its own local ledger.
  brchms_banner   — the platform announcement banner the app shows on every page
                    (maintenance, upgrades).

They share one deployment, one integration user, one image rebuild and one
version. Splitting them into two apps would double every one of those for no
separation that matters: neither is independently useful.

## Installing

The app is added to the bench and installed on the site:

    bench get-app brchms <path-or-git-url>
    bench --site <site> install-app brchms

On a Dockerised bench the app code must be present at IMAGE BUILD TIME — it cannot
be copied into a running container and survive. See README.md.
"""

app_name = "brchms"
app_title = "Rest"
app_publisher = "Breamal"
app_description = "Funding authority and announcement banner for the Rest church-management system"
app_email = "no-reply@breamal.com"
app_license = "mit"

# Read from the package rather than repeated, so the version Frappe reports and
# the version in `__init__.py` cannot drift. `frappe.get_attr(app
# + ".__version__")` is what `bench list-apps` uses; `pyproject.toml` is not
# consulted at all.
import frappe  # noqa: E402
from brchms import __version__ as app_version  # noqa: E402

# The app has no desk UI of its own: it is an API surface plus two doctypes, and
# an operator reads them through the standard list views.
required_apps = ["frappe"]

# Desk-side rendering for the funding record: a clickable link to the payment it
# references, and the sending instance's name and activity beside the id.
form_render = {
    "Rest Communication Credit Funding": {
        "payment_link": "brchms.brchms_credits.form_render.render_funding_extras",
    },
    "Rest Credit Proposal": {
        "payment_link": "brchms.brchms_credits.form_render.render_proposal_extras",
    },
}

# The same renderer on print, so a printed funding record carries its payment
# reference too.
doc_events = {
    "Rest Communication Credit Funding": {
        "before_print": "brchms.brchms_credits.form_render.render_funding_extras",
    },
    "Rest Credit Proposal": {
        "before_print": "brchms.brchms_credits.form_render.render_proposal_extras",
    },
    # Settlement. Every route to being paid — a gateway's own entry, or an operator
    # entering cash or a bank transfer by hand — ends as a submitted Payment Entry
    # referencing the Sales Invoice, so one hook here covers all of them and no
    # per-gateway code exists (or needs to). See brchms_credits/settlement.py for
    # why this may never raise into the payment.
    "Payment Entry": {
        "on_submit": "brchms.brchms_credits.settlement.settle_for_payment_entry",
    },
}

# Fixtures ship the two custom roles the app needs on a fresh site. Without them,
# `install-app` on a new bench produces a site where the integration user cannot
# be granted anything, and the first API call fails with a permission error that
# says nothing about the missing role.
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", ["Rest Integration", "Rest Banner Manager"]]]},
]


# Schema the doctype flags cannot express.
#
# A DOTTED PATH, not a function defined here. Frappe only registers a hook that is a
# string: a function written into `hooks.py` is called when Frappe collects callables,
# but `frappe.get_hooks("after_migrate")` does not list it -- so it appeared to be
# wired while never running, and the unique index it was supposed to create did not
# exist. Verified with `bench execute frappe.get_hooks`.
#
# `purchase_reference` is unique PER DEPLOYMENT, and Frappe creates a unique index
# only for a field used as the document NAME -- so leaving this to the doctype's
# `unique: 1` flag would have produced no constraint at all, and two proposals could
# share the reference the operator matches payments by.
after_migrate = [
    "brchms.brchms_credits.proposals.after_migrate",
    # The purchase flow's install-time records: the `purchase_reference` field and
    # index on Sales Invoice, and the items being sold. Idempotent, and deliberately
    # a hook rather than a `fixtures/` entry — fixtures are only imported when an
    # operator asks, and the failure of a missing field surfaces on a live site.
    "brchms.brchms_credits.provisioning.after_migrate",
]
