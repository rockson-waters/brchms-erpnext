# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The rate card, and the precedence the app depends on.

The app resolves a purchase price here and nowhere else, so these tests pin the
resolution order, the refusals, and the one thing the app cannot check for itself:
that a rate row is a price per ONE unit, in millionths, for the channel it names.

The funding API that used to live alongside these tests (`confirm_funding`, the
`Rest Communication Credit Funding` doctype and its read/count/forget helpers) was
retired when the settled proposal became the funding event. What remains of the
API is the rate lookup, which is the surface tested below.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe.utils import add_days, now_datetime

from brchms.brchms_credits import api


def call(method, **kwargs):
	"""
	Invoke a WHITELISTED method the way Frappe does.

	`@frappe.whitelist()` wraps the function with a type validator that
	reassigns `kwargs`, so calling the function directly silently drops the
	arguments. `frappe.call` goes through the same path an HTTP request takes,
	which is the path under test.
	"""
	return frappe.call(method, **kwargs)


class TestCreditRate(FrappeTestCase):
	"""
	The rate card, and the precedence the app depends on.

	The app's own rate card resolved rates as: an exact-country row first, then the
	channel default, then nothing. This mirrors that order exactly, because changing
	which price applies to a purchase would be a silent repricing of every church --
	the app cannot tell the difference between "ERPNext is the authority" and "ERPNext
	prices differently".
	"""

	CHANNEL = "sms"
	COUNTRY = "+233"

	def setUp(self):
		super().setUp()
		frappe.db.delete("Rest Credit Rate", {"channel": self.CHANNEL})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Rest Credit Rate", {"channel": self.CHANNEL})
		frappe.db.commit()
		super().tearDown()

	def make_rate(self, price, country_code=None, is_active=1, **overrides):
		values = {
			"doctype": "Rest Credit Rate",
			"channel": self.CHANNEL,
			"unit": "sms_segment",
			"price_per_unit_micros": price,
			"currency": "CAD",
			"country_code": country_code,
			"is_active": is_active,
			"effective_from": now_datetime(),
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		return doc

	def test_reports_unconfigured_rather_than_a_zero_when_no_rate_exists(self):
		"""
		A zero would render as "free" and the app would grant credits for nothing.
		The absence is reported as data so the APP writes the refusal sentence.
		"""
		rate = api.get_credit_rate(channel=self.CHANNEL, currency="CAD")

		self.assertEqual(rate["source"], "unconfigured")
		self.assertEqual(rate["price_per_unit_micros"], 0)
		self.assertIsNone(rate["rate_name"])

	def test_returns_the_channel_default_when_no_country_matches(self):
		self.make_rate(1_000_000)

		rate = api.get_credit_rate(channel=self.CHANNEL, currency="CAD")

		self.assertEqual(rate["source"], "channel_default")
		self.assertEqual(rate["price_per_unit_micros"], 1_000_000)
		self.assertTrue(rate["rate_name"])

	def test_an_exact_country_beats_the_channel_default(self):
		"""
		The whole reason country rates exist. If the default won, a dearer SMS
		destination would be charged at the cheap rate and nobody would see it in the
		price -- it would just look correct.
		"""
		self.make_rate(1_000_000)
		self.make_rate(9_000_000, country_code=self.COUNTRY)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency="CAD", destination="+233241234567"
		)

		self.assertEqual(rate["source"], "exact_country")
		self.assertEqual(rate["price_per_unit_micros"], 9_000_000)
		self.assertEqual(rate["country_code"], self.COUNTRY)

	def test_the_default_applies_to_a_destination_with_no_country_rate(self):
		self.make_rate(1_000_000)
		self.make_rate(9_000_000, country_code=self.COUNTRY)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency="CAD", destination="+14165550123"
		)

		self.assertEqual(rate["source"], "channel_default")
		self.assertEqual(rate["price_per_unit_micros"], 1_000_000)

	def test_an_inactive_rate_is_not_a_rate(self):
		self.make_rate(1_000_000, is_active=0)

		rate = api.get_credit_rate(channel=self.CHANNEL, currency="CAD")

		self.assertEqual(rate["source"], "unconfigured")

	def test_a_rate_that_has_not_started_does_not_apply(self):
		"""A future price must not be charged early; it is history in waiting."""
		self.make_rate(1_000_000)
		self.make_rate(
			9_000_000, effective_from=add_days(now_datetime(), 30), country_code=self.COUNTRY
		)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency="CAD", destination="+233241234567"
		)

		self.assertEqual(rate["source"], "channel_default")
		self.assertEqual(rate["price_per_unit_micros"], 1_000_000)

	def test_an_expired_rate_does_not_apply(self):
		self.make_rate(1_000_000)
		# A VALID window that contains no part of the present: it started before it
		# ended, and it ended yesterday. Setting only `effective_to` would fail the
		# window check instead, and the test would pass for the wrong reason.
		self.make_rate(
			9_000_000,
			effective_from=add_days(now_datetime(), -10),
			effective_to=add_days(now_datetime(), -1),
			country_code=self.COUNTRY,
		)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency="CAD", destination="+233241234567"
		)

		self.assertEqual(rate["source"], "channel_default")

	def test_a_country_code_is_normalised_so_it_can_actually_match(self):
		"""
		The app matches on a string, so '+233', '233' and '+233 ' are one country. A
		rate stored in a shape the comparison never produces would silently never
		apply -- the default would be charged instead and the operator would never
		learn the price they set was being ignored.
		"""
		doc = self.make_rate(9_000_000, country_code="233")
		self.assertEqual(doc.country_code, self.COUNTRY)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency="CAD", destination="+233241234567"
		)
		self.assertEqual(rate["source"], "exact_country")

	def test_refuses_a_zero_price(self):
		"""Zero hands out credits for nothing, and it validates the same as any price."""
		with self.assertRaises(frappe.ValidationError):
			self.make_rate(0)

	def test_refuses_a_price_whose_scale_is_wrong(self):
		"""A price of 100 is 0.0001, not 100.00 -- caught at entry, not at purchase."""
		with self.assertRaises(frappe.ValidationError):
			self.make_rate(10**16)

	def test_refuses_a_unit_that_contradicts_the_channel(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_rate(1_000_000, unit="email_message")

	def test_refuses_an_end_date_before_its_start(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_rate(
				1_000_000,
				effective_from=now_datetime(),
				effective_to=add_days(now_datetime(), -5),
			)

	def test_refuses_a_malformed_country_code(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_rate(1_000_000, country_code="GH")

	def test_the_newest_rate_wins_when_two_are_in_force(self):
		"""
		The app's table relied on there being one active row per scope and enforced
		nothing. Here the tiebreak is stated rather than left to whatever the database
		returns first.
		"""
		self.make_rate(1_000_000, effective_from=add_days(now_datetime(), -10))
		self.make_rate(2_000_000, effective_from=add_days(now_datetime(), -1))

		rate = api.get_credit_rate(channel=self.CHANNEL, currency="CAD")

		self.assertEqual(rate["price_per_unit_micros"], 2_000_000)


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
