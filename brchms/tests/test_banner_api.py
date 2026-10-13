# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Tests for the announcement banner.

Run against a site with the app installed:

    bench --site frontend run-tests --app brchms

## What matters here

The banner is rendered on EVERY page of the app, including the login page, and
the app is fail-open about it: `None` means "no banner", never an error. So the
two properties worth pinning are (a) an absence is a successful `None` rather
than a throw, and (b) the visibility window actually excludes a notice that is
scheduled for later -- a banner that appeared early is worse than one that
appeared late, because it announces a maintenance window that is not happening.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from brchms.brchms_banner import api

DOCTYPE = "Rest Platform Banner"


def make_banner(**overrides):
	values = {
		"doctype": DOCTYPE,
		"title": "Test notice",
		"message": "<p>The app will be read-only.</p>",
		"severity": "warning",
		"is_active": 1,
		"dismissible": 1,
	}
	values.update(overrides)
	doc = frappe.get_doc(values)
	doc.insert()
	return doc


def suspend_active_banners():
	"""
	Switch every currently-active banner off, and return what to restore.

	## Why the tests have to do this

	`get_active_banner` reads the WHOLE table and returns the newest active notice,
	so a test asserting "nothing is active" is really asserting "the database has no
	active banner" -- which stops being true the moment somebody authors one. It did:
	the first real notice created in the Desk ("First banner") broke three tests that
	had passed only because the table was empty.

	A test that passes only on an empty table is not testing the code; it is testing
	the fixture. These tests now own the state they depend on.
	"""
	names = [
		row.name
		for row in frappe.get_all(DOCTYPE, filters={"is_active": 1}, fields=["name"], limit_page_length=0)
	]
	for name in names:
		frappe.db.set_value(DOCTYPE, name, "is_active", 0)
	if names:
		frappe.db.commit()
	return names


def restore_active_banners(names):
	"""Put back exactly what `suspend_active_banners` switched off."""
	for name in names or []:
		if frappe.db.exists(DOCTYPE, name):
			frappe.db.set_value(DOCTYPE, name, "is_active", 1)
	if names:
		frappe.db.commit()

class TestGetActiveBanner(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(DOCTYPE, {"title": ("like", "Test%")})
		frappe.db.commit()
		# Own the state these tests depend on; see `suspend_active_banners`.
		self.suspended = suspend_active_banners()

	def tearDown(self):
		frappe.db.delete(DOCTYPE, {"title": ("like", "Test%")})
		frappe.db.commit()
		restore_active_banners(self.suspended)
		super().tearDown()

	# ── Absence is a success ─────────────────────────────────────────────────

	def test_returns_none_when_nothing_is_active(self):
		"""
		Not an exception. The app renders this on every page including login, and
		a thrown error there would be a banner failure in front of somebody trying
		to sign in.
		"""
		self.assertIsNone(api.get_active_banner())

	def test_returns_none_when_the_only_banner_is_switched_off(self):
		make_banner(is_active=0)
		self.assertIsNone(api.get_active_banner())

	# ── The happy path ───────────────────────────────────────────────────────

	def test_serves_an_active_banner_with_every_field_the_app_projects(self):
		make_banner()

		banner = api.get_active_banner()

		self.assertIsNotNone(banner)
		for field in ("title", "message", "severity", "dismissible", "updated_at"):
			self.assertIn(field, banner, f"{field} must be in the response")
		self.assertEqual(banner["title"], "Test notice")
		self.assertEqual(banner["severity"], "warning")

	def test_an_empty_window_is_unbounded(self):
		make_banner(starts_at=None, ends_at=None)
		self.assertIsNotNone(api.get_active_banner())

	# ── The window ───────────────────────────────────────────────────────────

	def test_does_not_serve_a_banner_scheduled_for_the_future(self):
		"""
		A notice that appears early announces a maintenance window that is not
		happening, which is worse than one that appears late.
		"""
		make_banner(starts_at=add_to_date(now_datetime(), hours=2))
		self.assertIsNone(api.get_active_banner())

	def test_does_not_serve_a_banner_whose_window_has_closed(self):
		make_banner(ends_at=add_to_date(now_datetime(), hours=-1))
		self.assertIsNone(api.get_active_banner())

	def test_serves_a_banner_whose_window_is_open(self):
		make_banner(
			starts_at=add_to_date(now_datetime(), hours=-1),
			ends_at=add_to_date(now_datetime(), hours=1),
		)
		self.assertIsNotNone(api.get_active_banner())

	# ── Selection and shape ──────────────────────────────────────────────────

	def test_the_most_recently_modified_active_banner_wins(self):
		"""
		When two notices overlap, the one the operator touched last is the one
		they mean.
		"""
		make_banner(title="Test first", message="first")
		second = make_banner(title="Test second", message="second")
		second.save()  # bumps `modified`

		banner = api.get_active_banner()
		self.assertEqual(banner["title"], "Test second")

	def test_a_very_long_message_is_capped_rather_than_served_whole(self):
		"""
		The app caps at 4000 characters because its renderer shows the message on
		one line. Capping here too means the truncation is a stated contract
		rather than whatever the layout happens to do.
		"""
		make_banner(message="x" * 9000)
		banner = api.get_active_banner()
		self.assertEqual(len(banner["message"]), api.MAX_MESSAGE_CHARS)

	def test_markup_is_passed_through_for_the_client_to_sanitise(self):
		"""
		Stripping here would mangle a legitimate `<p>` and a notice that lost its
		paragraphs is one nobody finishes. The app sanitises on render, which is
		the only place that knows the rendering context.
		"""
		make_banner(message="<p>Paragraph one</p><p>Paragraph two</p>")
		banner = api.get_active_banner()
		self.assertIn("<p>", banner["message"])


class TestBannerValidation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(DOCTYPE, {"title": ("like", "Test%")})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DOCTYPE, {"title": ("like", "Test%")})
		frappe.db.commit()
		super().tearDown()

	def test_refuses_an_active_banner_with_no_text(self):
		"""
		An active banner with nothing to say is an empty coloured bar on every
		page, which trains people to ignore banners -- including the next one that
		matters.
		"""
		with self.assertRaises(frappe.ValidationError):
			make_banner(title="", message="", is_active=1)

	def test_allows_a_textless_banner_while_it_is_switched_off(self):
		"""A draft must be saveable; the rule is about what is SERVED."""
		doc = make_banner(title="", message="", is_active=0)
		self.assertFalse(doc.is_active)

	def test_refuses_an_end_before_the_start(self):
		with self.assertRaises(frappe.ValidationError):
			make_banner(
				starts_at=add_to_date(now_datetime(), hours=2),
				ends_at=add_to_date(now_datetime(), hours=1),
			)
