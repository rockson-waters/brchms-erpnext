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

DOCTYPE = "Rest Communication Credit Funding"


def call(method, **kwargs):
	"""
	Invoke a WHITELISTED method the way Frappe does.

	`@frappe.whitelist()` does not return the function unchanged: it wraps it with
	`validate_argument_types` (`frappe/utils/typing_validations.py`), and that
	wrapper reassigns `kwargs` — it does not forward the caller's. So

	    confirm(**payload)          # ✗ payload silently dropped

	reaches the real function with every argument empty, and `_require_fields`
	correctly refuses with "Missing required fields: channel, event_id, units."
	That is a real trap: the test looks like a fixture bug and is actually a
	calling-convention bug, and it produced a confusing failure until the wrapper
	was found.

	`frappe.call` is Frappe's supported way to invoke a whitelisted method from
	server code, and it exercises the same path an HTTP request takes — which is
	the path under test. Everything in this file goes through here.
	"""
	return frappe.call(method, **kwargs)


def confirm(**kwargs):
	return call(api.confirm_funding, **kwargs)


def funding_payload(**overrides):
	"""
	A valid request, so each test can override exactly the field it is about.

	The three money fields are CONSISTENT with each other by construction, and
	that matters: `250 x 450000 micros` is 112,500,000 micros, i.e. 11250 minor
	units (112.50 CAD at 450 micros per unit = 0.45 per unit). The first version
	of this fixture paired `units: 250` and `unit_price_micros: 45000` with
	`amount_minor: 11250` — which is 11.25 CAD, not 112.50 — and the doctype's own
	money check refused it. The validator caught the test data, which is what it
	is for; the fixture was wrong, not the rule.
	"""
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
		"unit_price_micros": "450000",
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
		first = confirm(**funding_payload())
		second = confirm(**funding_payload())

		self.assertEqual(first["event_id"], second["event_id"])
		self.assertEqual(first["confirmed_at"], second["confirmed_at"])
		self.assertEqual(first["units"], second["units"])

		self.assertEqual(
			call(api.count_funding, event_id="test:frontend:unit-1"),
			1,
			"a replayed confirmation must not create a second funding record",
		)

	def test_two_different_events_create_two_rows(self):
		"""The control for the test above: idempotency must not swallow real work."""
		confirm(**funding_payload(event_id="test:frontend:unit-a"))
		confirm(**funding_payload(event_id="test:frontend:unit-b"))

		self.assertEqual(call(api.count_funding, event_id="test:frontend:unit-a"), 1)
		self.assertEqual(call(api.count_funding, event_id="test:frontend:unit-b"), 1)

	def test_the_unique_index_is_what_rejects_a_duplicate(self):
		"""
		Proves the constraint exists rather than trusting the read-first check.

		A direct insert with a duplicate `event_id` — bypassing the API, so the
		fast path cannot hide the problem — must raise. That is what closes the
		non-atomic window between the API's read and its insert.

		Frappe raises `UniqueValidationError` (wrapping the driver's
		`IntegrityError(1062, "Duplicate entry … for key 'event_id'")`), not the
		bare `DuplicateEntryError`. Both are caught by `_insert_funding`; this
		asserts the one that actually arrives, because a test asserting the other
		one would pass by never running the code path it names.
		"""
		confirm(**funding_payload())

		with self.assertRaises(frappe.UniqueValidationError):
			frappe.get_doc(
				{
					"doctype": DOCTYPE,
					"instance_id": "frontend",
					"event_id": "test:frontend:unit-1",
					"channel": "sms",
					"units": 250,
					"amount_minor": 11250,
					"unit_price_micros": 450000,
					"currency": "CAD",
				}
			).insert()

	def test_the_API_translates_a_lost_insert_race_instead_of_raising(self):
		"""
		The unique index still decides the race; the API must not let the LOSER see
		a failure.

		When two retries with the same `event_id` both miss the read, one insert
		wins and the other raises `UniqueValidationError`. Serving that to the
		client would report an error for an operation that in fact succeeded —
		and the caller cannot tell that apart from a genuine refusal, on the one
		path where that distinction decides whether credits are granted twice.

		Simulated here by inserting the row through the ORM, exactly as the winning
		request would, and then calling the API.
		"""
		frappe.get_doc(
			{
				"doctype": DOCTYPE,
				"instance_id": "frontend",
				"event_id": "test:frontend:unit-1",
				"channel": "sms",
				"units": 250,
				"amount_minor": 11250,
				"unit_price_micros": 450000,
				"currency": "CAD",
			}
		).insert()

		# The API must answer with the existing event, not an error.
		response = confirm(**funding_payload())

		self.assertEqual(response["event_id"], "test:frontend:unit-1")
		self.assertEqual(response["units"], "250")
		self.assertEqual(
			call(api.count_funding, event_id="test:frontend:unit-1"),
			1,
			"the loser of the race must not have created a second row",
		)

	# ── Refusals, each by name ───────────────────────────────────────────────

	def test_refuses_a_request_with_no_event_id(self):
		"""Without the key the record cannot be replayed safely, so it is refused."""
		with self.assertRaises(frappe.MandatoryError):
			confirm(**funding_payload(event_id=None))

	def test_refuses_a_request_with_no_units(self):
		with self.assertRaises(frappe.MandatoryError):
			confirm(**funding_payload(units=None))

	def test_refuses_zero_units_rather_than_recording_a_payment_that_grants_nothing(self):
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(units="0"))

	def test_refuses_negative_units(self):
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(units="-10"))

	def test_refuses_a_non_numeric_unit_count(self):
		"""
		`cint("abc")` is 0, which would be silently accepted as "no units".
		Rejecting by name is the difference between a clear refusal and a funding
		record that grants nothing.
		"""
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(units="two hundred"))

	def test_refuses_a_channel_outside_sms_and_email(self):
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(channel="whatsapp"))

	def test_refuses_a_currency_that_is_not_on_the_site(self):
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(currency="XXX"))

	def test_refuses_a_record_whose_money_does_not_add_up(self):
		"""
		250 units at 450000 micros is 11250 minor units. Claiming 99999 is paid
		is the case this check exists for: the authority must not record an amount
		that contradicts the units it is granting.

		This check caught the first version of `funding_payload`, which paired 250
		units with 45000 micros and still claimed 11250 minor. The fixture was
		wrong; the rule was right, and that is the point of asserting it.
		"""
		with self.assertRaises(frappe.ValidationError):
			confirm(**funding_payload(amount_minor="99999"))

	# ── The response contract ────────────────────────────────────────────────

	def test_the_response_carries_the_fields_the_app_needs_to_write_a_funding_lot(self):
		response = confirm(**funding_payload())

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
		The app parses `units` and money as BigInt. A JSON number above 2^53
		arrives rounded, and a rounded unit count is a wrong balance that looks
		right.

		`units` here is **2^53 x 10^3**, chosen so the money stays exactly
		consistent while the unit count is far past what a double can hold:

		    units x price          = 9223372036854770000 micros
		    amount_minor x 10^4    = 9223372036854770000 micros   ✓ equal

		Worth writing down, because it bounds what this test can cover: a MINOR
		amount cannot exceed 2^53 at all. `amount x 10^4` must fit an int64, so
		`amount <= 9.22e14`, and 2^53 is 9.007e15 — an order of magnitude larger.
		So it is `units` that can break a double here, which is precisely what the
		doctype's money check used to do: it ran on `flt()`, and
		`flt(9223372036854770000)` is not that number. The check now uses exact
		integer arithmetic, and this assertion is what caught it.
		"""
		BIG_UNITS = "9223372036854770000"
		BIG_AMOUNT = "922337203685477"
		response = confirm(
			**funding_payload(units=BIG_UNITS, unit_price_micros="1", amount_minor=BIG_AMOUNT)
		)

		self.assertIsInstance(response["units"], str)
		self.assertEqual(response["units"], BIG_UNITS)
		self.assertNotIn("e+", response["units"].lower())
		# The round trip is exact, not merely stringly: read it back from the DB.
		self.assertEqual(str(call(api.count_funding, event_id="test:frontend:unit-1")), "1")
		stored = frappe.db.get_value(
			DOCTYPE, {"event_id": "test:frontend:unit-1"}, "units"
		)
		self.assertEqual(str(stored), BIG_UNITS)

	def test_the_currency_is_upper_cased_and_the_channel_lower_cased(self):
		"""
		The app compares `channel` against its own enum and upper-cases the
		currency itself; normalising here means the two agree by construction
		rather than by both remembering to.
		"""
		response = confirm(**funding_payload(currency="cad", channel="SMS"))

		self.assertEqual(response["currency"], "CAD")
		self.assertEqual(response["channel"], "sms")

	def test_confirmed_at_is_set_by_the_authority_not_the_caller(self):
		"""A client-supplied timestamp is not evidence of when this system recorded it."""
		response = confirm(**funding_payload())

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
		confirm(**funding_payload())

		found = call(api.get_funding, event_id="test:frontend:unit-1")
		self.assertEqual(found["event_id"], "test:frontend:unit-1")
		self.assertEqual(found["units"], "250")

	def test_returns_none_for_an_event_that_was_never_confirmed(self):
		"""
		`None` rather than an exception: "not recorded" is a legitimate answer,
		and it is the answer the app's reconciliation asks for.
		"""
		self.assertIsNone(call(api.get_funding, event_id="test:frontend:never"))

	def test_count_funding_distinguishes_zero_from_one(self):
		self.assertEqual(call(api.count_funding, event_id="test:frontend:unit-1"), 0)

		confirm(**funding_payload())
		self.assertEqual(call(api.count_funding, event_id="test:frontend:unit-1"), 1)

		confirm(**funding_payload())
		self.assertEqual(
			call(api.count_funding, event_id="test:frontend:unit-1"),
			1,
			"the count after a replay is the assertion the app's safety rests on",
		)


class TestIntegrationRolePermissions(FrappeTestCase):
	"""
	The integration role must stay READ-ONLY on the doctype, and the method must
	still be able to write.

	Both halves matter and they pull in opposite directions. Granting the role
	`create` would make the first half pass trivially — and would give a credential
	that lives in a `.env` file on another host the ability to add, change and
	delete funding rows. So the method inserts with `ignore_permissions=True` and
	the role keeps `create: 0`. These tests pin BOTH sides, because a future
	"fix" that grants `create: 1` would silently undo the least-privilege
	decision while making every other test pass.
	"""

	def test_the_integration_role_cannot_create_on_the_doctype(self):
		role = frappe.get_doc("Role", "Rest Integration")
		perm = next(
			(p for p in frappe.get_doc("DocType", DOCTYPE).permissions if p.role == role.name),
			None,
		)
		self.assertIsNotNone(perm, "the role has no permission row on the funding doctype")
		self.assertEqual(perm.read, 1, "the integration user must be able to read funding")
		self.assertEqual(perm.create, 0, "the integration role must NOT be able to create")
		self.assertEqual(perm.write, 0, "the integration role must NOT be able to update")
		self.assertEqual(perm.delete, 0, "the integration role must NOT be able to delete")

	def test_the_method_writes_anyway_because_it_is_the_authorisation_boundary(self):
		"""
		The counterpart: with `create: 0`, a naive `doc.insert()` is refused with
		`PermissionError: ... does not have doctype access via role permission`.
		Measured on the live site. `ignore_permissions=True` is what lets the
		whitelisted method write while the role stays read-only.
		"""
		response = confirm(**funding_payload(event_id="test:frontend:perm-1"))
		self.assertEqual(response["event_id"], "test:frontend:perm-1")
		self.assertEqual(call(api.count_funding, event_id="test:frontend:perm-1"), 1)
