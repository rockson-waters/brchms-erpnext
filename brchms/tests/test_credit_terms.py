# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Commercial terms: per-organization discount, site minimum purchase, monthly free
allowances, and the settlement security gate.

## What this file pins

  * the discount arithmetic is INTEGER half-up on both sides, so the quote, the
    proposal, the invoice and the platform application cannot disagree by a
    rounding step;
  * the effective price is re-derived from `(list, basis_points)`; the quoted
    discount is LOCKED IN on the proposal and prevails even if the registry term
    changes afterwards (priced once, never re-priced);
  * the minimum is enforced on the NET amount (what the buyer pays after the
    discount), at quote, create_proposal, the public intake and checkout, and
    the conversion to minor units is exact for a currency whose fraction is not
    100 (MGA is 5);
  * the monthly allowance read states entitlements without writing anything,
    reports an entitlement that cannot be attributed, and never emits zero
    grants;
  * settling a proposal needs the `submit` permission -- the whitelisted escape
    hatch must not be a way around the settlement boundary -- and the rate
    fixture endpoints are System-Manager-only, because they write prices with
    `ignore_permissions`.

Run with:
    bench --site <site> run-tests --app brchms --module brchms.tests.test_credit_terms

This suite is destructive in the same way the rest of this app's suite is: it
creates real documents (and a settlement probe elsewhere). Run it on a
throwaway or restored site, NEVER against production. See docs/TEST-ENVIRONMENT.md.
"""

import unittest
from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, cstr, flt, now_datetime

from brchms.brchms_credits import (
	api,
	proposals,
	purchase as purchase_module,
	rate_fixtures,
	registry,
	terms,
)
from brchms.tests.test_proposals import (
	_cleanup_invoices_for_instances,
	_cleanup_registry_for_instances,
)

PROPOSAL = "Rest Credit Proposal"
PURCHASE = "Rest Credit Purchase"
ORGANIZATION = "Rest Credit Organization"
SETTINGS = "Rest Credit Settings"
RATE = "Rest Credit Rate"
INSTANCE = "test:terms-instance"
RATE_NOTE = "CREDIT-TERMS-TEST-RATE"
INTAKE_NOTE = "CREDIT-TERMS-TEST-INTAKE-RATE"
INTEGRATION_USER = "restapp@breamal.com"


def site_currency():
	"""The company's own currency -- the only one a purchase may be quoted in."""
	company = frappe.db.get_value("Company", {}, "name")
	return frappe.db.get_value("Company", company, "default_currency") if company else "GHS"


def set_minimum(amount):
	"""Set the site-wide floor (and run the doctype's own validation)."""
	doc = frappe.get_single(SETTINGS)
	doc.minimum_purchase_amount = amount
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()


class TermsTestCase(FrappeTestCase):
	"""Exact-scope cleanup: only rows these tests create."""

	def setUp(self):
		super().setUp()
		self._clean()
		set_minimum(0)

	def tearDown(self):
		self._clean()
		set_minimum(0)
		super().tearDown()

	def _clean(self):
		for name in frappe.get_all(
			PROPOSAL, filters={"instance_id": ("like", "test:terms%")}, pluck="name", limit_page_length=0
		):
			doc = frappe.get_doc(PROPOSAL, name)
			if doc.docstatus == 1:
				try:
					doc.flags.ignore_permissions = True
					doc.cancel()
				except Exception:
					continue
			frappe.delete_doc(PROPOSAL, name, force=True, ignore_permissions=True)

		frappe.db.delete(PROPOSAL, {"instance_id": ("like", "test:terms%")})
		frappe.db.delete(PURCHASE, {"buyer_name": ("like", "Terms Test%")})
		_cleanup_invoices_for_instances(("test:terms%",))
		_cleanup_registry_for_instances(("test:terms%",))
		frappe.db.delete("Rest Instance", {"name": ("like", "test:terms%")})
		for name in frappe.get_all(
			RATE, filters={"notes": ("in", [RATE_NOTE, INTAKE_NOTE])}, pluck="name"
		):
			frappe.delete_doc(RATE, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ── fixtures ────────────────────────────────────────────────────────────

	def _rate(self, price_per_unit_micros=45000, channel="sms", note=RATE_NOTE):
		doc = frappe.get_doc(
			{
				"doctype": RATE,
				"channel": channel,
				"unit": "sms_segment" if channel == "sms" else "email_message",
				"price_per_unit_micros": price_per_unit_micros,
				"currency": site_currency(),
				"is_active": 1,
				"effective_from": now_datetime(),
				"notes": note,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()
		return doc

	def _organization(
		self,
		organization_id="org-terms-1",
		name="Terms Test Church",
		discount_percent=0,
		monthly_free_sms_units=0,
		monthly_free_email_units=0,
		free_credits_rollover=0,
		default_group_id="grp-terms-1",
	):
		row = registry.ensure_registration(
			instance_id=INSTANCE,
			organization_id=organization_id,
			organization_name=name,
			default_group_id=default_group_id,
			default_group_name="Terms Test Group" if default_group_id else None,
		)
		row.discount_percent = discount_percent
		row.monthly_free_sms_units = monthly_free_sms_units
		row.monthly_free_email_units = monthly_free_email_units
		row.free_credits_rollover = free_credits_rollover
		row.flags.ignore_permissions = True
		row.save()
		frappe.db.commit()
		return row

	def _proposal_payload(self, **overrides):
		values = {
			"instance_id": INSTANCE,
			"request_key": "test-terms-request-1",
			"purchase_reference": "TERMS-TEST-0001",
			"organization_id": "org-terms-1",
			"organization_name": "Terms Test Church",
			"funding_group_id": "grp-terms-1",
			"group_name": "Terms Test Group",
			"channel": "sms",
			"units": 100,
			"amount_minor": 4500,
			"currency": site_currency(),
			"unit_price_micros": 45000,
			"instance_name": "Terms Test Church",
		}
		values.update(overrides)
		return values


class TestDiscountArithmetic(FrappeTestCase):
	"""The rule both sides re-derive: integer, half-up, basis points."""

	def test_no_discount_is_the_list_price(self):
		self.assertEqual(terms.effective_unit_price_micros(45000, 0), 45000)
		self.assertEqual(terms.effective_unit_price_micros(1, 0), 1)

	def test_a_basis_point_discount_is_exact_on_round_numbers(self):
		self.assertEqual(terms.effective_unit_price_micros(45000, 1000), 40500)
		self.assertEqual(terms.effective_unit_price_micros(1000000, 2500), 750000)

	def test_the_division_is_half_up_not_banker_or_floor(self):
		# 0.5 micro rounds UP to 1; 1.5 rounds UP to 2.
		self.assertEqual(terms.effective_unit_price_micros(1, 5000), 1)
		self.assertEqual(terms.effective_unit_price_micros(3, 5000), 2)
		# Truncation would have answered 0 and 1.
		self.assertNotEqual(terms.effective_unit_price_micros(1, 5000), 0)

	def test_a_total_discount_leaves_no_price(self):
		self.assertEqual(terms.effective_unit_price_micros(45000, 10000), 0)
		self.assertEqual(terms.effective_unit_price_micros(1, 9999), 0)

	def test_percent_to_basis_points_is_half_up(self):
		self.assertEqual(terms.basis_points_from_percent(10), 1000)
		self.assertEqual(terms.basis_points_from_percent(7.5), 750)
		self.assertEqual(terms.basis_points_from_percent(12.50), 1250)
		self.assertEqual(terms.basis_points_from_percent(99.99), 9999)
		self.assertEqual(terms.basis_points_from_percent(0), 0)
		self.assertEqual(terms.basis_points_from_percent(None), 0)

	def test_a_discount_that_rounds_to_zero_is_a_pricing_error_naming_it(self):
		error = terms.pricing_error_for(1, 9999)
		self.assertIn("discount", cstr(error).lower())

	def test_a_priced_rate_has_no_pricing_error(self):
		self.assertIsNone(terms.pricing_error_for(45000, 1000))


class TestSettingsMinimum(TermsTestCase):
	"""The site floor: non-negative, currency-precise, exact under Decimal."""

	def test_default_is_zero_and_switches_the_floor_off(self):
		self.assertEqual(terms.minimum_purchase_amount(), Decimal("0"))
		self.assertEqual(terms.minimum_purchase_minor(site_currency()), 0)
		self.assertIsNone(terms.minimum_purchase_display(site_currency()))

	def test_negative_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			set_minimum(-5)

	def test_a_figure_finer_than_the_currency_is_refused(self):
		currency = site_currency()
		fraction = terms.currency_fraction_units(currency)
		too_fine = Decimal(1) / (Decimal(fraction) * 10)
		with self.assertRaises(frappe.ValidationError):
			terms.validate_amount_precision(too_fine, currency)

	def test_minimum_is_converted_in_the_purchase_currency(self):
		currency = site_currency()
		fraction = terms.currency_fraction_units(currency)
		set_minimum(25)
		self.assertEqual(terms.minimum_purchase_minor(currency), 25 * fraction)
		self.assertTrue(terms.minimum_purchase_display(currency).startswith(currency))

	def test_mga_fraction_is_five_not_one_hundred(self):
		"""A 5:1 currency: 2 MGA is 10 minor units, and a fraction is rounded UP."""
		if not frappe.db.exists("Currency", "MGA"):
			self.skipTest("MGA is not a Currency row on this site")
		if cint(frappe.db.get_value("Currency", "MGA", "fraction_units")) != 5:
			self.skipTest("this site's MGA fraction is not 5")

		set_minimum(2)
		self.assertEqual(terms.minimum_purchase_minor("MGA"), 10)
		# 0.10 MGA is half a minor unit; a minimum rounds UP to 1, never down.
		self.assertEqual(terms.major_to_minor(Decimal("0.10"), "MGA"), 1)
		# And a configured amount the currency cannot express is refused where it
		# is validated.
		with self.assertRaises(frappe.ValidationError):
			terms.validate_amount_precision(Decimal("1.05"), "MGA")


class TestCreditRateTerms(TermsTestCase):
	"""get_credit_rate applies the REGISTRY's terms, never a caller's number."""

	def setUp(self):
		super().setUp()
		self._rate(45000)
		self.org = self._organization(discount_percent=10)

	def test_an_organization_gets_its_discount(self):
		rate = api.get_credit_rate(
			channel="sms",
			currency=site_currency(),
			organization_id="org-terms-1",
			instance_id=INSTANCE,
		)
		self.assertEqual(rate["list_price_per_unit_micros"], 45000)
		self.assertEqual(rate["price_per_unit_micros"], 40500)
		self.assertEqual(rate["discount_basis_points"], 1000)
		self.assertEqual(rate["discount_percent"], 10.0)
		self.assertFalse(rate["pricing_error"])

	def test_without_an_organization_the_list_price_stands(self):
		rate = api.get_credit_rate(channel="sms", currency=site_currency())
		self.assertEqual(rate["list_price_per_unit_micros"], 45000)
		self.assertEqual(rate["price_per_unit_micros"], 45000)
		self.assertEqual(rate["discount_basis_points"], 0)
		self.assertFalse(rate["pricing_error"])

	def test_state_carries_the_site_minimum(self):
		set_minimum(25)
		rate = api.get_credit_rate(
			channel="sms",
			currency=site_currency(),
			organization_id="org-terms-1",
			instance_id=INSTANCE,
		)
		self.assertEqual(
			rate["minimum_purchase_minor"], 25 * terms.currency_fraction_units(site_currency())
		)
		self.assertTrue(cstr(rate["minimum_purchase_amount"]).startswith(site_currency()))

	def test_another_deployments_organization_terms_are_not_applied(self):
		"""A discount is per (deployment, organization); ids are per-deployment."""
		other = api.get_credit_rate(
			channel="sms",
			currency=site_currency(),
			organization_id="org-terms-1",
			instance_id="test:terms-other",
		)
		self.assertEqual(other["discount_basis_points"], 0)
		self.assertEqual(other["price_per_unit_micros"], 45000)

	def test_a_discount_is_resolved_by_basis_points_not_by_the_request(self):
		"""There is no request parameter that names a discount, by design."""
		import inspect

		params = inspect.signature(api.get_credit_rate).parameters
		self.assertNotIn("discount_basis_points", params)
		self.assertNotIn("discount_percent", params)


class TestQuoteAndInvoice(TermsTestCase):
	"""The buyer's amount and the invoice line are the NET, snapshot-based price."""

	def setUp(self):
		super().setUp()
		self._rate(45000)
		self.org = self._organization(discount_percent=10)

	def test_quote_by_slug_applies_the_organizations_discount(self):
		result = proposals.quote(channel="sms", units=100, org=self.org.link_slug)

		self.assertEqual(result["list_price_per_unit_micros"], 45000)
		self.assertEqual(result["unit_price_micros"], 40500)
		self.assertEqual(result["amount_minor"], 405)  # 4.05
		self.assertEqual(result["list_amount_minor"], 450)  # 4.50
		self.assertEqual(result["discount_minor"], 45)  # 0.45
		self.assertEqual(result["discount_basis_points"], 1000)
		self.assertEqual(result["discount_percent"], 10.0)
		self.assertFalse(result["below_minimum"])
		self.assertFalse(result["pricing_error"])

	def test_create_proposal_snapshots_the_terms_and_the_invoice_bills_net(self):
		result = proposals.create_proposal(
			**self._proposal_payload(
				units=100,
				amount_minor=405,
				unit_price_micros=40500,
				list_unit_price_micros=45000,
				discount_basis_points=1000,
				rate_name="REST-RATE-sms-0001",
			)
		)

		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		self.assertEqual(doc.list_unit_price_micros, 45000)
		self.assertEqual(doc.unit_price_micros, 40500)
		self.assertEqual(doc.discount_basis_points, 1000)
		self.assertEqual(doc.discount_percent, 10.0)
		self.assertEqual(doc.list_amount_minor, 450)
		self.assertEqual(doc.amount_minor, 405)
		self.assertEqual(doc.discount_minor, 45)
		self.assertEqual(doc.rate_name, "REST-RATE-sms-0001")
		self.assertEqual(doc.quote_variance_minor, 0)

		invoice = frappe.get_doc("Sales Invoice", doc.invoice)
		self.assertEqual(flt(invoice.items[0].rate), 4.05)
		self.assertIn("discount", cstr(invoice.items[0].description).lower())
		self.assertIn("list price", cstr(invoice.items[0].description).lower())
		# No ERPNext discount machinery: the net price IS the rate.
		self.assertFalse(flt(invoice.get("additional_discount_percentage") or 0))
		self.assertFalse(flt(invoice.get("discount_amount") or 0))
		self.assertEqual(flt(invoice.grand_total), 4.05)

	def test_the_quoted_discount_is_locked_in_when_the_term_changes(self):
		"""
		The snapshot prevails: a purchase quoted at 5% must still be billed at
		5% after the organization's registry term moves to 10%.

		"Priced once, never re-priced" applies to the discount as much as to the
		rate card. Re-pricing here would either over-refund or strand a request
		the buyer already accepted.
		"""
		result = proposals.create_proposal(
			**self._proposal_payload(
				request_key="test-terms-locked-discount",
				purchase_reference="TERMS-TEST-LOCKED",
				amount_minor=428,  # 5% off, not the organization's current 10%
				unit_price_micros=42750,
				list_unit_price_micros=45000,
				discount_basis_points=500,
			)
		)

		self.assertEqual(result["discount_basis_points"], 500)
		self.assertEqual(result["discount_percent"], 5.0)
		self.assertEqual(result["amount_minor"], 428)
		self.assertEqual(result["list_amount_minor"], 450)
		self.assertEqual(result["discount_minor"], 22)

		doc = frappe.get_doc("Rest Credit Proposal", result["proposal"])
		self.assertEqual(doc.discount_basis_points, 500)
		self.assertEqual(doc.amount_minor, 428)

	def test_an_inconsistent_snapshot_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			proposals.create_proposal(
				**self._proposal_payload(
					amount_minor=450,
					unit_price_micros=45000,  # claims 10% but prices at list
					list_unit_price_micros=45000,
					discount_basis_points=1000,
				)
			)
		self.assertIn("disagree", str(caught.exception).lower())

	def test_a_discount_can_make_the_purchase_below_the_minimum(self):
		"""The floor is on the NET amount, so a discount can take an order under it."""
		set_minimum(5)  # 5.00 major, 500 minor for a 2-decimal currency
		result = proposals.quote(channel="sms", units=100, org=self.org.link_slug)
		self.assertTrue(result["below_minimum"], "4.05 is below a 5.00 floor")

		with self.assertRaises(frappe.ValidationError) as caught:
			proposals.create_proposal(
				**self._proposal_payload(
					units=100,
					amount_minor=405,
					unit_price_micros=40500,
					list_unit_price_micros=45000,
					discount_basis_points=1000,
				)
			)
		self.assertIn("minimum", str(caught.exception).lower())

	def test_a_discount_leaving_no_price_cannot_be_raised(self):
		"""A rounding-to-zero term is a pricing error, refused by name."""
		self._rate(1, note=INTAKE_NOTE)
		self._organization(organization_id="org-terms-1", discount_percent=99.99)

		rate = api.get_credit_rate(
			channel="sms",
			currency=site_currency(),
			organization_id="org-terms-1",
			instance_id=INSTANCE,
		)
		self.assertEqual(rate["price_per_unit_micros"], 0)
		self.assertIn("discount", cstr(rate["pricing_error"]).lower())

		with self.assertRaises(frappe.ValidationError):
			proposals.create_proposal(
				**self._proposal_payload(
					request_key="test-terms-zero",
					purchase_reference="TERMS-TEST-ZERO",
					amount_minor=0,
					unit_price_micros=0,
					list_unit_price_micros=1,
					discount_basis_points=9999,
				)
			)


class TestProposalSnapshotValidation(TermsTestCase):
	"""The doctype re-derives the price for ANY writer, not just our API."""

	def setUp(self):
		super().setUp()
		self._rate(45000)
		self._organization(discount_percent=10)

	def _doc(self, **overrides):
		values = {
			"doctype": PROPOSAL,
			"expires_at": now_datetime(),
			"instance_id": INSTANCE,
			"organization_id": "org-terms-1",
			"funding_group_id": "grp-terms-1",
			"purchase_reference": "TERMS-SNAPSHOT-0001",
			"channel": "sms",
			"units": 100,
			"amount_minor": 405,
			"currency": site_currency(),
			"unit_price_micros": 40500,
			"list_unit_price_micros": 45000,
			"discount_basis_points": 1000,
		}
		values.update(overrides)
		return frappe.get_doc(values)

	def test_a_consistent_snapshot_is_derived_and_saved(self):
		doc = self._doc()
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertEqual(doc.list_amount_minor, 450)
		self.assertEqual(doc.discount_minor, 45)

	def test_a_hand_edited_effective_price_is_refused(self):
		doc = self._doc(unit_price_micros=40000)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_the_minimum_is_not_retroactively_enforced_by_the_doctype(self):
		"""An already-quoted purchase is a fact; a raised floor must not strand it."""
		set_minimum(1000)  # far above this proposal's 4.05
		doc = self._doc()
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertEqual(doc.amount_minor, 405)


class TestBillingContextTerms(TermsTestCase):
	"""The credits page learns the same terms the purchase will use."""

	def test_billing_context_reports_discount_allowance_and_minimum(self):
		set_minimum(25)
		self._organization(
			discount_percent=12.5,
			monthly_free_sms_units=100,
			monthly_free_email_units=50,
			free_credits_rollover=1,
		)

		context = proposals.get_billing_context(
			instance_id=INSTANCE, organization_id="org-terms-1"
		)

		self.assertTrue(context["ready"])
		self.assertEqual(context["discount_percent"], 12.5)
		self.assertEqual(context["monthly_free_sms_units"], 100)
		self.assertEqual(context["monthly_free_email_units"], 50)
		self.assertEqual(context["free_credits_rollover"], 1)
		self.assertEqual(
			context["minimum_purchase_minor"], 25 * terms.currency_fraction_units(site_currency())
		)
		self.assertTrue(context["minimum_purchase_amount"])

	def test_a_registration_refresh_does_not_overwrite_negotiated_terms(self):
		row = self._organization(
			discount_percent=12.5, monthly_free_sms_units=100, free_credits_rollover=1
		)

		registry.ensure_registration(
			instance_id=INSTANCE,
			organization_id="org-terms-1",
			organization_name="Renamed Terms Church",
		)
		frappe.db.commit()

		refreshed = frappe.get_doc(ORGANIZATION, row.name)
		self.assertEqual(refreshed.discount_percent, 12.5)
		self.assertEqual(refreshed.monthly_free_sms_units, 100)
		self.assertEqual(refreshed.free_credits_rollover, 1)


class TestOrganizationTermValidation(TermsTestCase):
	def test_100_percent_is_refused(self):
		row = self._organization()
		row.discount_percent = 100
		with self.assertRaises(frappe.ValidationError):
			row.save()

	def test_negative_discount_and_three_decimals_are_refused(self):
		row = self._organization()
		row.discount_percent = -1
		with self.assertRaises(frappe.ValidationError):
			row.save()

		row = self._organization()
		row.discount_percent = 12.345
		with self.assertRaises(frappe.ValidationError):
			row.save()

	def test_negative_allowance_is_refused(self):
		row = self._organization()
		row.monthly_free_sms_units = -5
		with self.assertRaises(frappe.ValidationError):
			row.save()


class TestCreditAllowances(TermsTestCase):
	"""The allowance read states entitlements; it writes nothing."""

	def setUp(self):
		super().setUp()
		self.organization = self._organization(
			organization_id="org-terms-1",
			name="Terms Allowance Church",
			monthly_free_sms_units=100,
			monthly_free_email_units=50,
			free_credits_rollover=1,
		)

	def test_grants_carry_everything_the_app_needs_to_attribute_them(self):
		response = api.list_credit_allowances(instance_id=INSTANCE, period="2026-10")

		self.assertEqual(response["period"], "2026-10")
		self.assertEqual(len(response["grants"]), 2)

		by_channel = {grant["channel"]: grant for grant in response["grants"]}
		sms = by_channel["sms"]
		self.assertEqual(sms["organization_id"], "org-terms-1")
		self.assertEqual(sms["organization_name"], "Terms Allowance Church")
		self.assertEqual(sms["funding_group_id"], "grp-terms-1")
		self.assertEqual(sms["funding_group_name"], "Terms Test Group")
		self.assertEqual(sms["units"], 100)
		self.assertEqual(sms["currency"], site_currency())
		self.assertTrue(sms["rollover"])
		self.assertEqual(by_channel["email"]["units"], 50)
		self.assertEqual(response["skipped"], [])

	def test_zero_units_are_not_grants(self):
		row = self._organization(organization_id="org-terms-zero", name="Terms Zero Church")
		row.monthly_free_sms_units = 0
		row.monthly_free_email_units = 0
		row.flags.ignore_permissions = True
		row.save()

		response = api.list_credit_allowances(instance_id=INSTANCE, period="2026-10")
		self.assertEqual(len(response["grants"]), 2, "the two channels of the first church only")
		self.assertNotIn(
			"org-terms-zero", [grant["organization_id"] for grant in response["grants"]]
		)

	def test_an_entitlement_with_nowhere_to_grant_is_reported_not_lost(self):
		row = self._organization(
			organization_id="org-terms-nogroup",
			name="Terms No Group Church",
			monthly_free_sms_units=10,
			default_group_id=None,
		)
		row.monthly_free_sms_units = 10
		row.flags.ignore_permissions = True
		row.save()

		response = api.list_credit_allowances(instance_id=INSTANCE, period="2026-10")
		skipped = [item for item in response["skipped"] if item["organization_id"] == "org-terms-nogroup"]
		self.assertEqual(len(skipped), 1)
		self.assertEqual(skipped[0]["channel"], "sms")
		self.assertEqual(skipped[0]["units"], 10)
		self.assertTrue(skipped[0]["reason"])

	def test_the_period_defaults_to_the_current_utc_month(self):
		response = api.list_credit_allowances(instance_id=INSTANCE)
		self.assertRegex(response["period"], r"^\d{4}-(0[1-9]|1[0-2])$")

	def test_an_invalid_period_is_refused_with_the_expected_shape(self):
		for period in ("2026-13", "2026-1", "October 2026", "2026-10-01"):
			with self.assertRaises(frappe.ValidationError):
				api.list_credit_allowances(instance_id=INSTANCE, period=period)

	def test_instance_id_is_required_and_an_unknown_one_is_empty(self):
		with self.assertRaises(frappe.MandatoryError):
			api.list_credit_allowances()

		response = api.list_credit_allowances(instance_id="test:terms-unknown")
		self.assertEqual(response["grants"], [])
		self.assertEqual(response["skipped"], [])

	def test_reading_an_allowance_writes_nothing(self):
		before = frappe.db.get_value(
			ORGANIZATION, self.organization.name, "monthly_free_sms_units"
		)
		count_before = frappe.db.count(PROPOSAL, {"instance_id": INSTANCE})
		api.list_credit_allowances(instance_id=INSTANCE, period="2026-10")
		self.assertEqual(
			frappe.db.get_value(ORGANIZATION, self.organization.name, "monthly_free_sms_units"),
			before,
		)
		self.assertEqual(frappe.db.count(PROPOSAL, {"instance_id": INSTANCE}), count_before)


class TestPublicCheckoutTerms(TermsTestCase):
	"""The public form's bridge: the buyer pays the discounted price, or is refused."""

	def setUp(self):
		super().setUp()
		self._rate(100000, note=INTAKE_NOTE)  # 0.10 per unit
		self.church = self._organization(discount_percent=10)

	def _request(self, units=250, amount=None, **overrides):
		values = {
			"doctype": PURCHASE,
			"buyer_name": "Terms Test Buyer",
			"buyer_email": "terms-test-{0}@example.test".format(
				now_datetime().strftime("%H%M%S%f")
			),
			"channel": "sms",
			"units": units,
			# 250 at 0.09 effective = 22.50
			"amount": amount if amount is not None else flt(units * 9) / 100.0,
			"currency": site_currency(),
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_checkout_snapshots_the_discounted_terms(self):
		purchase = self._request()
		frappe.db.commit()

		try:
			purchase_module.checkout(purchase=purchase.name, org=self.church.link_slug)
		except frappe.ValidationError as error:
			# No gateway on a scratch site: the purchase is still bridged.
			self.assertIn("payment page could not be opened", str(error).lower())

		proposal_name = frappe.db.get_value(PURCHASE, purchase.name, "proposal")
		self.assertTrue(proposal_name, "the intake row was never bridged")

		proposal = frappe.get_doc(PROPOSAL, proposal_name)
		self.assertEqual(proposal.list_unit_price_micros, 100000)
		self.assertEqual(proposal.unit_price_micros, 90000)
		self.assertEqual(proposal.discount_basis_points, 1000)
		self.assertEqual(proposal.list_amount_minor, 2500)  # 25.00 list
		self.assertEqual(proposal.amount_minor, 2250)  # 22.50 net
		self.assertEqual(proposal.discount_minor, 250)

		invoice = frappe.get_doc("Sales Invoice", proposal.invoice)
		self.assertEqual(flt(invoice.items[0].rate), 22.50)

	def test_checkout_refuses_below_the_minimum_before_anything_is_raised(self):
		# Create the intake row first, then raise the floor. The intake validate is
		# covered by its own test; this one proves the CHECKOUT gate (which knows
		# the organization's discounted price) refuses a row that was valid when
		# it was recorded.
		purchase = self._request()
		frappe.db.commit()
		set_minimum(30)  # 30.00 floor; this order is 22.50

		with self.assertRaises(frappe.ValidationError) as caught:
			purchase_module.checkout(purchase=purchase.name, org=self.church.link_slug)
		self.assertIn("minimum", str(caught.exception).lower())
		self.assertFalse(frappe.db.get_value(PURCHASE, purchase.name, "proposal"))

	def test_the_intake_refuses_a_form_carrying_less_than_the_minimum(self):
		"""A crafted POST that skips the client-side warning still cannot be saved."""
		set_minimum(30)  # 30.00 floor
		with self.assertRaises(frappe.ValidationError) as caught:
			self._request(units=100, amount=9.00)  # 9.00
		self.assertIn("minimum", str(caught.exception).lower())

	def test_a_stale_form_amount_is_refused_rather_than_silently_repriced(self):
		purchase = self._request(amount=25.00)  # the LIST price, not the discounted 22.50
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as caught:
			purchase_module.checkout(purchase=purchase.name, org=self.church.link_slug)
		self.assertIn("does not match", str(caught.exception).lower())


class TestSettledProposalSnapshot(TermsTestCase):
	"""
	The reconcile poller's read must carry the snapshot.

	A purchase BORN ON THE PUBLIC FORM has no app-side record to read the list
	price, the discount or the rate row from; the settled-proposal payload is
	where the poller learns them. Settling here uses the instance row as the
	payment "evidence" -- this test is about the PAYLOAD, not about money, and
	creating a real Payment Entry would add ledger history for no assertion.
	"""

	def setUp(self):
		super().setUp()
		self._rate(45000)
		self._organization(discount_percent=10)

	def _settled_proposal(self):
		doc = frappe.get_doc(
			{
				"doctype": PROPOSAL,
				"instance_id": INSTANCE,
				"request_key": "test-terms-settled-1",
				"purchase_reference": "TERMS-SETTLED-0001",
				"organization_id": "org-terms-1",
				"funding_group_id": "grp-terms-1",
				"channel": "sms",
				"units": 100,
				"amount_minor": 405,
				"currency": site_currency(),
				"unit_price_micros": 40500,
				"list_unit_price_micros": 45000,
				"discount_basis_points": 1000,
				"rate_name": "REST-RATE-sms-0001",
				"payment_doctype": "Rest Instance",
				"payment_document": INSTANCE,
				"payment_reference": "TERMS-SETTLED-REF",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.flags.ignore_permissions = True
		doc.submit()
		return doc

	def test_the_poller_read_carries_the_list_price_and_discount(self):
		doc = self._settled_proposal()
		frappe.db.commit()

		listing = proposals.list_settled_proposals(instance_id=INSTANCE)
		rows = [row for row in listing["proposals"] if row["name"] == doc.name]
		self.assertEqual(len(rows), 1)

		row = rows[0]
		self.assertEqual(row["list_unit_price_micros"], 45000)
		self.assertEqual(row["unit_price_micros"], 40500)
		self.assertEqual(row["list_amount_minor"], 450)
		self.assertEqual(row["discount_basis_points"], 1000)
		self.assertEqual(row["discount_percent"], 10.0)
		self.assertEqual(row["discount_minor"], 45)
		self.assertEqual(row["rate_name"], "REST-RATE-sms-0001")


class TestSecurityGates(TermsTestCase):
	"""The whitelisted escape hatches must not be a way around the boundary."""

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("User", INTEGRATION_USER):
			self.skipTest("the Rest Integration user is not installed on this site")

	def _as_integration_user(self):
		frappe.set_user(INTEGRATION_USER)

	def test_settle_proposal_needs_the_submit_permission(self):
		self._as_integration_user()
		try:
			with self.assertRaises(frappe.PermissionError):
				proposals.settle_proposal()
		finally:
			frappe.set_user("Administrator")

	def test_rate_fixture_endpoints_are_system_manager_only(self):
		self._as_integration_user()
		try:
			with self.assertRaises(frappe.PermissionError):
				rate_fixtures.install()
			with self.assertRaises(frappe.PermissionError):
				rate_fixtures.remove()
		finally:
			frappe.set_user("Administrator")


if __name__ == "__main__":
	unittest.main()
