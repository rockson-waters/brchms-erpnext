"""
The version this app reports must match the release it claims to be.

## Why this exists

`bench list-apps` reads the version with `frappe.get_attr(app + ".__version__")`
and falls back to the literal "0.0.1" when it is missing. When the app was
installed the first time it had no `__version__` at all, so a deployed app
reported `0.0.1`; after adding one, it then reported `0.1.0` across FOUR release
tags (v0.1.0 .. v0.2.0) because the tag was bumped and the string was not.

That matters more than cosmetics: `list-apps` is how anyone checks what is
actually deployed, and a version that lags its tag silently devalues the check --
the person running it concludes the deploy did not happen, or did not happen to
the revision they asked for.

So the drift is asserted here rather than trusted. Any failure is real: the fix
is to bump `__version__` in the same commit as the tag.
"""

import json
import re
import subprocess
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

# `frappe.get_app_path("brchms")` IS the package directory that holds
# `__init__.py` -- the app's root is one level further up, at
# `apps/brchms/brchms/` for the package and `apps/brchms/` for the checkout.
# Taking `.parent` skipped past the package and looked for
# `apps/brchms/__init__.py`, which does not exist; observed on the live site.
PACKAGE_DIR = Path(frappe.get_app_path("brchms"))
APP_ROOT = PACKAGE_DIR.parent


def declared_version():
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', (PACKAGE_DIR / "__init__.py").read_text(), re.M)
    return match.group(1) if match else None


MANIFEST = APP_ROOT / ".brchms-build-manifest.json"


def requested_pin():
    """
    The branch or tag the IMAGE was built from, read from the build manifest.

    This is the check that actually matters in production. `bench init` deletes each
    app's `.git`, so a deployed container has no tags to compare against -- the
    first version of this test skipped there, reporting "no tags available" in
    precisely the environment where version drift is worth catching. The layered
    Containerfile now stamps `apps.json`'s entry for this app into the image, so
    the deployed app can be asked what it was supposed to be.
    """
    if not MANIFEST.exists():
        return None
    try:
        entry = json.loads(MANIFEST.read_text())
    except Exception:
        return None
    branch = str(entry.get("branch") or "").strip()
    return branch or None


def latest_tag():
    """
    The highest v* tag in a source checkout, or None when git is unavailable.

    Used in development, where the app IS a checkout. Returns None inside a built
    image, where git metadata has been removed -- see `requested_pin` above.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(APP_ROOT), "tag", "--list", "v*"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    tags = [t.strip() for t in out.stdout.splitlines() if t.strip()]
    if not tags:
        return None

    def key(tag):
        return [int(part) for part in re.findall(r"\d+", tag)]

    return sorted(tags, key=key)[-1]


class TestReportedVersion(FrappeTestCase):
    def test_version_is_declared(self):
        """Without this, `list-apps` reports the literal fallback "0.0.1"."""
        self.assertIsNotNone(
            declared_version(),
            "__init__.py has no __version__, so list-apps would report 0.0.1",
        )

    def test_version_matches_what_the_image_was_built_from(self):
        """
        Preferred over the git-tag check below, because this one runs DEPLOYED.

        A mismatch here means one of two things, and the message says which, since
        the fix differs: the app declares a version it is not, or `apps.json` pins a
        tag older than the app in it.
        """
        pin = requested_pin()
        if not pin:
            self.skipTest("no build manifest: not running from a built image")

        self.assertEqual(
            declared_version(),
            pin.lstrip("v"),
            "the image was built from '{0}' but the app reports version {1}. Either "
            "__version__ was not bumped with the tag, or apps.json is pinned to an "
            "older tag than the code it is building.".format(pin, declared_version()),
        )

    def test_version_matches_the_latest_release_tag(self):
        tag = latest_tag()
        if not tag:
            self.skipTest("no git metadata: a built image has no tags (see requested_pin)")

        self.assertEqual(
            declared_version(),
            tag.lstrip("v"),
            "the app reports {0} but the latest tag is {1} -- bump __version__ in the "
            "same commit as the tag, or nobody can tell from `bench list-apps` which "
            "revision is deployed".format(declared_version(), tag),
        )
