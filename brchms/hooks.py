"""
BRCHMS — the Frappe app that lets the BRCHMS church-management system reach this
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
app_title = "BRCHMS"
app_publisher = "Breamal"
app_description = "Funding authority and announcement banner for the BRCHMS church-management system"
app_email = "no-reply@breamal.com"
app_license = "mit"
app_version = "0.1.0"

# The app has no desk UI of its own: it is an API surface plus two doctypes, and
# an operator reads them through the standard list views.
required_apps = ["frappe"]

# Fixtures ship the two custom roles the app needs on a fresh site. Without them,
# `install-app` on a new bench produces a site where the integration user cannot
# be granted anything, and the first API call fails with a permission error that
# says nothing about the missing role.
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", ["BRCHMS Integration", "BRCHMS Banner Manager"]]]},
]
