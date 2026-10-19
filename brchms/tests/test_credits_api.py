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


def site_currency():
	"""The test company's own currency. A rate in any other currency is refused."""
	company = frappe.db.get_value("Company", {}, "name")
	return frappe.db.get_value("Company", company, "default_currency") if company else "GHS"


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
			"currency": site_currency(),
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
		rate = api.get_credit_rate(channel=self.CHANNEL, currency=site_currency())

		self.assertEqual(rate["source"], "unconfigured")
		self.assertEqual(rate["price_per_unit_micros"], 0)
		self.assertIsNone(rate["rate_name"])

	def test_returns_the_channel_default_when_no_country_matches(self):
		self.make_rate(1_000_000)

		rate = api.get_credit_rate(channel=self.CHANNEL, currency=site_currency())

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
			channel=self.CHANNEL, currency=site_currency(), destination="+233241234567"
		)

		self.assertEqual(rate["source"], "exact_country")
		self.assertEqual(rate["price_per_unit_micros"], 9_000_000)
		self.assertEqual(rate["country_code"], self.COUNTRY)

	def test_the_default_applies_to_a_destination_with_no_country_rate(self):
		self.make_rate(1_000_000)
		self.make_rate(9_000_000, country_code=self.COUNTRY)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency=site_currency(), destination="+14165550123"
		)

		self.assertEqual(rate["source"], "channel_default")
		self.assertEqual(rate["price_per_unit_micros"], 1_000_000)

	def test_an_inactive_rate_is_not_a_rate(self):
		self.make_rate(1_000_000, is_active=0)

		rate = api.get_credit_rate(channel=self.CHANNEL, currency=site_currency())

		self.assertEqual(rate["source"], "unconfigured")

	def test_a_rate_that_has_not_started_does_not_apply(self):
		"""A future price must not be charged early; it is history in waiting."""
		self.make_rate(1_000_000)
		self.make_rate(
			9_000_000, effective_from=add_days(now_datetime(), 30), country_code=self.COUNTRY
		)

		rate = api.get_credit_rate(
			channel=self.CHANNEL, currency=site_currency(), destination="+233241234567"
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
			channel=self.CHANNEL, currency=site_currency(), destination="+233241234567"
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
			channel=self.CHANNEL, currency=site_currency(), destination="+233241234567"
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

		rate = api.get_credit_rate(channel=self.CHANNEL, currency=site_currency())

		self.assertEqual(rate["price_per_unit_micros"], 2_000_000)


class TestProviderBuyingRate(FrappeTestCase):
	"""
	The buying rate is INTERNAL cost, separate from the selling rate.

	It must resolve per selling company/provider/destination and report an absent
	rate rather than a zero that would silently improve margin.
	"""

	CHANNEL = "sms"
	COUNTRY = "+233"

	def setUp(self):
		super().setUp()
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("a Company is required for a buying-rate test")
		self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		frappe.db.delete("Rest Credit Buying Rate", {"channel": self.CHANNEL})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Rest Credit Buying Rate", {"channel": self.CHANNEL})
		frappe.db.commit()
		super().tearDown()

	def make_rate(self, price, provider="nalo", country_code=None, **overrides):
		values = {
			"doctype": "Rest Credit Buying Rate",
			"company": self.company,
			"provider": provider,
			"channel": self.CHANNEL,
			"currency": self.currency,
			"price_per_unit_micros": price,
			"destination_country_code": country_code,
			"effective_from": now_datetime(),
			"is_active": 1,
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		return doc

	def test_reports_unconfigured_rather_than_zero(self):
		cost = api.get_provider_cost(
			company=self.company,
			provider="nalo",
			channel=self.CHANNEL,
			currency=self.currency,
			destination="+233241234567",
		)

		self.assertEqual(cost["source"], "unconfigured")
		self.assertEqual(cost["price_per_unit_micros"], 0)
		self.assertIsNone(cost["cost_rate_name"])

	def test_exact_destination_beats_the_provider_default(self):
		self.make_rate(40_000)
		self.make_rate(20_000, country_code=self.COUNTRY)

		cost = api.get_provider_cost(
			company=self.company,
			provider="nalo",
			channel=self.CHANNEL,
			currency=self.currency,
			destination="+233241234567",
		)

		self.assertEqual(cost["source"], "exact_country")
		self.assertEqual(cost["price_per_unit_micros"], 20_000)

	def test_the_channel_derives_the_unit_even_from_a_stale_form_default(self):
		# The desk's read-only Unit Select opens on `sms_segment`. An email buying
		# rate must not be refused just because that stale value is still in the
		# submitted doc; the unit belongs to the channel.
		doc = self.make_rate(
			150_000,
			provider="smtp",
			channel="email",
			unit="sms_segment",
		)

		self.assertEqual(doc.unit, "email_message")
		frappe.delete_doc("Rest Credit Buying Rate", doc.name, force=True, ignore_permissions=True)


	def test_a_buying_rate_in_the_wrong_currency_is_refused(self):
		wrong = "USD" if (self.currency or "").upper() != "USD" else "EUR"

		with self.assertRaises(frappe.ValidationError):
			self.make_rate(40_000, currency=wrong)


class TestBillingRegion(FrappeTestCase):
	"""
	Country -> selling company, with NO default fallback.

	The app never gets to choose a company. These pin the mapping helper the
	quote/proposal/checkout paths all use, including the refusal for a country
	nobody mapped.
	"""

	COUNTRY = "Canada"

	def setUp(self):
		super().setUp()
		from brchms.brchms_credits import billing

		self.billing = billing
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("a Company is required for a billing-region test")
		self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		for name in frappe.get_all(
			"Rest Credit Billing Region", filters={"country": self.COUNTRY}, pluck="name"
		):
			frappe.delete_doc("Rest Credit Billing Region", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		for name in frappe.get_all(
			"Rest Credit Billing Region", filters={"country": self.COUNTRY}, pluck="name"
		):
			frappe.delete_doc("Rest Credit Billing Region", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def test_a_mapped_country_resolves_to_its_company_and_currency(self):
		region = frappe.get_doc(
			{
				"doctype": "Rest Credit Billing Region",
				"region_name": "Test North America",
				"country": self.COUNTRY,
				"company": self.company,
				"is_active": 1,
			}
		)
		region.flags.ignore_permissions = True
		region.insert()

		context = self.billing.resolve_billing_context("CA")

		self.assertEqual(context["company"], self.company)
		self.assertEqual(context["currency"], self.currency)
		self.assertTrue(context["ready"])

	def test_an_unmapped_country_is_refused_rather_than_defaulted(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.billing.resolve_billing_context("TG")

		self.assertIn("billing region", str(caught.exception).lower())
