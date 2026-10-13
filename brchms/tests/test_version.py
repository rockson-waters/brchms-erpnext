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

import re
import subprocess
from pathlib import Path

import frappe
from frappe.tests.utils import FrappeTestCase

APP_ROOT = Path(frappe.get_app_path("brchms")).parent


def declared_version():
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', (APP_ROOT / "__init__.py").read_text(), re.M)
    return match.group(1) if match else None


def latest_tag():
    """The highest v* tag, or None when git is unavailable (a bench checkout may not be one)."""
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

    def test_version_matches_the_latest_release_tag(self):
        tag = latest_tag()
        if not tag:
            self.skipTest("no git tags available in this checkout")

        self.assertEqual(
            declared_version(),
            tag.lstrip("v"),
            "the app reports {0} but the latest tag is {1} -- bump __version__ in the "
            "same commit as the tag, or nobody can tell from `bench list-apps` which "
            "revision is deployed".format(declared_version(), tag),
        )
