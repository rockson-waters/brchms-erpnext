# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Tests for the malfunction-report API.

Run against a LOCAL or THROWAWAY site only:

    bench --site <throwaway-site> run-tests --app brchms

Never against production. The suite creates real `Rest Malfunction Report` rows
and private files, and cleans up only the rows its own naming-series watermark
identifies (same rule as `test_banner_api.py`).

## What matters here

The app's credential holds the read-only `Rest Integration` role, so this method
is the whole authorisation boundary: it validates, rate-limits, deduplicates and
writes with `ignore_permissions`. The tests pin each of those four, plus the
screenshot being stored as a PRIVATE file — a report screenshot can show any
screen in the app.
"""

import base64

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime

from brchms.brchms_support import api

DOCTYPE = "Rest Malfunction Report"

# A 1x1 transparent PNG, small enough to embed and a real decodable image.
PNG_BYTES = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode()


def series_watermark():
	"""The highest number currently used in the REST-MFR-##### naming series."""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"name": ("like", "REST-MFR-%")},
		fields=["name"],
		limit_page_length=0,
	)
	highest = 0
	for row in rows:
		try:
			highest = max(highest, int(str(row.name).rsplit("-", 1)[-1]))
		except (TypeError, ValueError):
			continue
	return highest


def created_between(low, high):
	"""Reports whose naming-series number the run created (`low` exclusive)."""
	rows = frappe.get_all(
		DOCTYPE,
		filters={"name": ("like", "REST-MFR-%")},
		fields=["name"],
		limit_page_length=0,
	)
	out = []
	for row in rows:
		try:
			number = int(str(row.name).rsplit("-", 1)[-1])
		except (TypeError, ValueError):
			continue
		if low < number <= high:
			out.append(row.name)
	return out


def delete_created(low, high):
	"""Delete exactly what the run created, including its private files."""
	for name in created_between(low, high):
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
			fields=["name"],
			limit_page_length=0,
		)
		for file in files:
			frappe.delete_doc("File", file.name, ignore_permissions=True, force=True)
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)


class TestMalfunctionReportAPI(FrappeTestCase):
	def setUp(self):
		self.watermark = series_watermark()
		# A unique deployment per test keeps the hourly rate-limit counter and the
		# idempotency key from colliding across tests.
		self.instance_id = "test-support-{0}".format(frappe.generate_hash(length=10))
		self.request_id = "test-request-{0}".format(frappe.generate_hash(length=10))

	def tearDown(self):
		delete_created(self.watermark)
		try:
			frappe.cache().delete_value(api._rate_key(self.instance_id))
		except Exception:
			pass

	def payload(self, **overrides):
		data = {
			"instance_id": self.instance_id,
			"client_request_id": self.request_id,
			"organization_id": "org-test",
			"organization_name": "Test Church",
			"reporter_user_id": "user-1",
			"reporter_name": "Ada Lovelace",
			"reporter_email": "ada@example.test",
			"category": "app_error",
			"description": "Saving a person shows an error and nothing is stored.",
			"page_url": "/people/123?tab=issues",
			"app_version": "2.0.0-test",
			"user_agent": "Mozilla/5.0 (test)",
			"screenshot_base64": PNG_BASE64,
			"screenshot_mime": "image/png",
			"screenshot_filename": "Person form.png",
		}
		data.update(overrides)
		return data

	# ── Recording ────────────────────────────────────────────────────────────

	def test_records_a_report_with_a_private_screenshot(self):
		result = api.create_malfunction_report(**self.payload())

		self.assertEqual(result["status"], "new")
		self.assertTrue(result["reported_at"])
		self.assertTrue(result["name"].startswith("REST-MFR-"))

		row = frappe.get_doc(DOCTYPE, result["name"])
		self.assertEqual(row.instance_id, self.instance_id)
		self.assertEqual(row.client_request_id, self.request_id)
		self.assertEqual(row.category, "app_error")
		self.assertEqual(row.priority, "medium")
		self.assertEqual(row.source, "Rest app")
		self.assertEqual(row.organization_name, "Test Church")
		self.assertEqual(row.reporter_user_id, "user-1")
		self.assertIn("nothing is stored", row.description)

		file_row = frappe.db.get_value(
			"File",
			{"attached_to_doctype": DOCTYPE, "attached_to_name": row.name},
			["file_url", "is_private", "file_name"],
			as_dict=True,
		)
		self.assertIsNotNone(file_row, "the screenshot was not saved as an attached file")
		self.assertEqual(file_row.is_private, 1)
		self.assertEqual(file_row.file_name, "Person-form.png")
		self.assertTrue(file_row.file_url.endswith(".png"))
		self.assertEqual(row.screenshot, file_row.file_url)

	def test_reported_at_uses_the_instance_clock_and_cannot_be_edited(self):
		result = api.create_malfunction_report(**self.payload())
		row = frappe.get_doc(DOCTYPE, result["name"])
		self.assertIsNotNone(get_datetime(row.reported_at))

		row.reported_at = add_to_date(row.reported_at, hours=3)
		with self.assertRaises(frappe.ValidationError) as caught:
			row.save(ignore_permissions=True)
		self.assertIn("reported_at:", str(caught.exception))

	# ── Idempotency ──────────────────────────────────────────────────────────

	def test_a_replay_returns_the_same_report_and_creates_one_row(self):
		first = api.create_malfunction_report(**self.payload())
		second = api.create_malfunction_report(**self.payload())

		self.assertEqual(first["name"], second["name"])
		self.assertEqual(
			frappe.db.count(
				DOCTYPE,
				{"instance_id": self.instance_id, "client_request_id": self.request_id},
			),
			1,
		)

	def test_two_deployments_may_reuse_the_same_request_id(self):
		first = api.create_malfunction_report(**self.payload())
		second = api.create_malfunction_report(
			**self.payload(instance_id="{0}-other".format(self.instance_id))
		)
		try:
			self.assertNotEqual(first["name"], second["name"])
		finally:
			# The second row belongs to a different instance; remove it here
			# because the watermark cleanup runs in tearDown for the whole set.
			delete_created(self.watermark)

	# ── Validation ───────────────────────────────────────────────────────────

	def assert_refused(self, field, **overrides):
		with self.assertRaises(frappe.ValidationError) as caught:
			api.create_malfunction_report(**self.payload(**overrides))
		self.assertIn("{0}:".format(field), str(caught.exception))
		self.assertEqual(
			frappe.db.count(DOCTYPE, {"instance_id": self.instance_id}),
			0,
			"a refused report must leave no row behind",
		)

	def test_refuses_an_empty_description(self):
		self.assert_refused("description", description="   ")

	def test_refuses_an_unknown_category(self):
		self.assert_refused("category", category="printer_on_fire")

	def test_refuses_an_invalid_request_id(self):
		self.assert_refused("client_request_id", client_request_id="short")

	def test_refuses_a_missing_instance(self):
		self.assert_refused("instance_id", instance_id="")

	def test_refuses_a_missing_reporter(self):
		self.assert_refused("reporter_user_id", reporter_user_id="")

	def test_refuses_an_unsupported_screenshot_type(self):
		self.assert_refused("screenshot_mime", screenshot_mime="image/gif")

	def test_refuses_malformed_base64(self):
		self.assert_refused("screenshot_base64", screenshot_base64="not-base64!!")

	def test_refuses_a_screenshot_over_the_size_cap(self):
		original = api.MAX_SCREENSHOT_BYTES
		api.MAX_SCREENSHOT_BYTES = 16
		try:
			self.assert_refused("screenshot_base64")
		finally:
			api.MAX_SCREENSHOT_BYTES = original

	def test_refuses_screenshot_metadata_without_the_image(self):
		self.assert_refused("screenshot_base64", screenshot_base64="")

	def test_a_report_without_a_screenshot_is_valid(self):
		result = api.create_malfunction_report(
			**self.payload(screenshot_base64="", screenshot_mime="", screenshot_filename="")
		)
		row = frappe.get_doc(DOCTYPE, result["name"])
		self.assertFalse(row.screenshot)

	def test_refuses_an_over_long_description(self):
		self.assert_refused("description", description="x" * (api.MAX_DESCRIPTION_CHARS + 1))

	# ── Rate limit ───────────────────────────────────────────────────────────

	def test_hourly_rate_limit_refuses_new_reports_but_not_replays(self):
		original = api.RATE_LIMIT_PER_HOUR
		api.RATE_LIMIT_PER_HOUR = 1
		try:
			first = api.create_malfunction_report(**self.payload())
			with self.assertRaises(frappe.ValidationError) as caught:
				api.create_malfunction_report(
					**self.payload(client_request_id="second-request-{0}".format(frappe.generate_hash(length=8)))
				)
			self.assertIn("instance_id:", str(caught.exception))

			# The UI retries a lost response with the SAME request id; that must
			# still answer with the row, not with the rate limit.
			replay = api.create_malfunction_report(**self.payload())
			self.assertEqual(first["name"], replay["name"])
		finally:
			api.RATE_LIMIT_PER_HOUR = original
			frappe.cache().delete_value(api._rate_key(self.instance_id))

	# ── Schema, index and roles ──────────────────────────────────────────────

	def test_the_composite_unique_index_is_installed_and_idempotent(self):
		api.ensure_uniqueness_constraint()
		rows = frappe.db.sql(
			"SHOW INDEX FROM `tab{0}` WHERE Key_name = %s".format(DOCTYPE),
			(api.UNIQUE_INDEX_NAME,),
			as_dict=True,
		)
		self.assertTrue(rows, "the unique index was not installed")
		# One row per indexed column (instance_id, client_request_id); both
		# columns belong to the same non-unique flag 0.
		self.assertEqual({row.Column_name for row in rows}, {"instance_id", "client_request_id"})
		self.assertTrue(all(row.Non_unique == 0 for row in rows))
		self.assertFalse(api.ensure_uniqueness_constraint(), "the second call should be a no-op")

	def test_the_support_role_can_triage_but_not_create_or_delete(self):
		rows = frappe.get_all(
			"DocPerm",
			filters={"parent": DOCTYPE},
			fields=["role", "read", "write", "create", "delete", "report", "export", "print", "share"],
			limit_page_length=0,
		)
		by_role = {row.role: row for row in rows}

		support = by_role.get("Rest Support Agent")
		self.assertIsNotNone(support, "Rest Support Agent has no permission on the doctype")
		self.assertEqual(support.read, 1)
		self.assertEqual(support.write, 1)
		self.assertEqual(support.create, 0)
		self.assertEqual(support.delete, 0)

		manager = by_role.get("System Manager")
		self.assertIsNotNone(manager, "System Manager cannot administer the doctype")
		self.assertEqual(manager.create, 1)
		self.assertEqual(manager.delete, 1)

		self.assertTrue(frappe.db.exists("Role", "Rest Support Agent"))
