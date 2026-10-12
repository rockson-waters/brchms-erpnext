# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Tests for the funding authority.

Run against a site with the app installed:

    bench --site frontend run-tests --app brchms

## What these tests are for

The single most important property of `confirm_funding` is that **replaying the
same `event_id` returns the same record and creates exactly one row**. That is
what makes the app's retry-after-lost-response safe, and it cannot be established
by reading the code -- the read-then-insert has a race window, and the thing that
closes it is a database constraint. So the tests below assert the ROW COUNT, not
merely the returned payload: a replay that returned the right object while
quietly inserting a second row would pass every other check.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from brchms.brchms_credits import api

DOCTYPE = "BRCHMS Communication Credit Funding"


def funding_payload(**overrides):
	"""A valid request, so each test can override exactly the field it is about."""
	payload = {
		"instance_id": "frontend",
		"event_id": "test:frontend:unit-1",
		"purchase_reference": "TEST-REF-1",
		"organization_id": "org-unit-1",
		"funding_group_id": "grp-unit-1",
		"channel": "sms",
		"units": "250",
		"amount_minor": "11250",
		"currency": "CAD",
		"unit_price_micros": "45000",
	}
	payload.update(overrides)
	return payload


class TestConfirmFunding(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# The tests own their rows. `event_id` is unique across the site, so a
		# leftover row from an interrupted run would make the next one fail in a
		# way that looks like a genuine defect.
		frappe.db.delete(DOCTYPE, {"event_id": ("like", "test:%")})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DOCTYPE, {"event_id": ("like", "test:%")})
		frappe.db.commit()
		super().tearDown()

	# ── The property everything rests on ─────────────────────────────────────

	def test_replaying_the_same_event_returns_the_same_record_and_creates_one_row(self):
		"""
		The whole reason the app can retry.

		Asserts the row COUNT as well as the payload: a second insert that was
		returned alongside the first would satisfy an equality check on the
		response and still leave this instance holding two records for money paid
		once -- which the app would then refuse as a conflict, with the duplicate
		permanent.
		"""
		first = api.confirm_funding(**funding_payload())
		second = api.confirm_funding(**funding_payload())

		self.assertEqual(first["event_id"], second["event_id"])
		self.assertEqual(first["confirmed_at"], second["confirmed_at"])
		self.assertEqual(first["units"], second["units"])

		self.assertEqual(
			api.count_funding(event_id="test:frontend:unit-1"),
			1,
			"a replayed confirmation must not create a second funding record",
		)

	def test_two_different_events_create_two_rows(self):
		"""The control for the test above: idempotency must not swallow real work."""
		api.confirm_funding(**funding_payload(event_id="test:frontend:unit-a"))
		api.confirm_funding(**funding_payload(event_id="test:frontend:unit-b"))

		self.assertEqual(api.count_funding(event_id="test:frontend:unit-a"), 1)
		self.assertEqual(api.count_funding(event_id="test:frontend:unit-b"), 1)

	def test_the_unique_index_is_what_rejects_a_duplicate(self):
		"""
		Proves the constraint exists rather than trusting the read-first check.

		A direct insert with a duplicate `event_id` must raise. If this passes,
		the race between two concurrent requests is closed by the database and the
		`DuplicateEntryError` fallback in the API has something to catch.
		"""
		api.confirm_funding(**funding_payload())

		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"instance_id": "frontend",
					"event_id": "test:frontend:unit-1",
					"channel": "sms",
					"units": 250,
				}
			).insert()

	# ── Refusals, each by name ───────────────────────────────────────────────

	def test_refuses_a_request_with_no_event_id(self):
		"""Without the key the record cannot be replayed safely, so it is refused."""
		with self.assertRaises(frappe.MandatoryError):
			api.confirm_funding(**funding_payload(event_id=None))

	def test_refuses_a_request_with_no_units(self):
		with self.assertRaises(frappe.MandatoryError):
			api.confirm_funding(**funding_payload(units=None))

	def test_refuses_zero_units_rather_than_recording_a_payment_that_grants_nothing(self):
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(units="0"))

	def test_refuses_negative_units(self):
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(units="-10"))

	def test_refuses_a_non_numeric_unit_count(self):
		"""
		`cint("abc")` is 0, which would be silently accepted as "no units".
		Rejecting by name is the difference between a clear refusal and a funding
		record that grants nothing.
		"""
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(units="two hundred"))

	def test_refuses_a_channel_outside_sms_and_email(self):
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(channel="whatsapp"))

	def test_refuses_a_currency_that_is_not_on_the_site(self):
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(currency="XXX"))

	def test_refuses_a_record_whose_money_does_not_add_up(self):
		"""
		250 units at 45000 micros is 11250 minor units. Claiming 99999 is paid
		is the case this check exists for: the authority must not record an amount
		that contradicts the units it is granting.
		"""
		with self.assertRaises(frappe.ValidationError):
			api.confirm_funding(**funding_payload(amount_minor="99999"))

	# ── The response contract ────────────────────────────────────────────────

	def test_the_response_carries_the_fields_the_app_needs_to_write_a_funding_lot(self):
		response = api.confirm_funding(**funding_payload())

		for field in (
			"event_id",
			"instance_id",
			"organization_id",
			"funding_group_id",
			"channel",
			"units",
			"amount_minor",
			"currency",
			"unit_price_micros",
			"confirmed_at",
		):
			self.assertIn(field, response, f"{field} must be in the response")

	def test_numeric_fields_are_strings_so_a_bigvalue_cannot_lose_precision(self):
		"""
		The app parses these as BigInt. A JSON number above 2^53 would arrive
		rounded, and a rounded unit count is a wrong balance that looks right.
		"""
		response = api.confirm_funding(**funding_payload(units="9007199254740993"))

		self.assertIsInstance(response["units"], str)
		self.assertEqual(response["units"], "9007199254740993")
		self.assertNotIn("e+", response["units"].lower())

	def test_the_currency_is_upper_cased_and_the_channel_lower_cased(self):
		"""
		The app compares `channel` against its own enum and upper-cases the
		currency itself; normalising here means the two agree by construction
		rather than by both remembering to.
		"""
		response = api.confirm_funding(**funding_payload(currency="cad", channel="SMS"))

		self.assertEqual(response["currency"], "CAD")
		self.assertEqual(response["channel"], "sms")

	def test_confirmed_at_is_set_by_the_authority_not_the_caller(self):
		"""A client-supplied timestamp is not evidence of when this system recorded it."""
		response = api.confirm_funding(**funding_payload())

		self.assertTrue(response["confirmed_at"])

		# And the caller cannot move it: the field is not in the request surface
		# at all, so this asserts the stored value exists rather than echoing an
		# argument back.
		stored = frappe.db.get_value(DOCTYPE, {"event_id": response["event_id"]}, "confirmed_at")
		self.assertTrue(stored)


class TestGetFunding(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.delete(DOCTYPE, {"event_id": ("like", "test:%")})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DOCTYPE, {"event_id": ("like", "test:%")})
		frappe.db.commit()
		super().tearDown()

	def test_reads_back_what_was_confirmed(self):
		api.confirm_funding(**funding_payload())

		found = api.get_funding(event_id="test:frontend:unit-1")
		self.assertEqual(found["event_id"], "test:frontend:unit-1")
		self.assertEqual(found["units"], "250")

	def test_returns_none_for_an_event_that_was_never_confirmed(self):
		"""
		`None` rather than an exception: "not recorded" is a legitimate answer,
		and it is the answer the app's reconciliation asks for.
		"""
		self.assertIsNone(api.get_funding(event_id="test:frontend:never"))

	def test_count_funding_distinguishes_zero_from_one(self):
		self.assertEqual(api.count_funding(event_id="test:frontend:unit-1"), 0)

		api.confirm_funding(**funding_payload())
		self.assertEqual(api.count_funding(event_id="test:frontend:unit-1"), 1)

		api.confirm_funding(**funding_payload())
		self.assertEqual(
			api.count_funding(event_id="test:frontend:unit-1"),
			1,
			"the count after a replay is the assertion the app's safety rests on",
		)
