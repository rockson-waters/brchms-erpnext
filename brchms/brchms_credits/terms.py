# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Credit terms: an organization's discount, the site's minimum purchase, and the
monthly free-credit entitlement.

## Where each number lives, and why

* **The discount is a property of the ORGANIZATION** (`Rest Credit
  Organization.discount_percent`). It is a negotiated commercial term with that
  church, not a property of a rate row and not a number a buyer or a tenant
  application may choose. Reading it from the registry means one place to
  change it and every quote, invoice and proposal picks the same number up.
* **The minimum purchase is a property of the SITE** (`Rest Credit Settings.
  minimum_purchase_amount`), because it is a floor below which taking a card
  payment costs more than the sale -- not a per-church deal.
* **The monthly free credits are per organization and per channel, with a
  rollover switch** (`Rest Credit Organization.monthly_free_*_units`,
  `free_credits_rollover`). They are an entitlement, not money: ERPNext states
  what an organization is entitled to each month, and the app turns that into
  balance. Nothing on the money path (proposal, invoice, payment) is involved.

## Integer money, half-up

A discount is expressed in **basis points** (1/100 of a percent) so the
effective unit price can be re-derived exactly from `(list_micros,
basis_points)` on both sides. The division `list * (10000 - bp) / 10000` is
computed with integer arithmetic and ROUND HALF-UP -- the same rule as
`invoicing.minor_amount_for`, and for the same reason: a float division would
silently round money, and the two sides would then disagree by a minor unit on
precisely the purchases that were priced correctly.

## The minimum is nominal, not converted

`minimum_purchase_amount` is a single major-unit figure and is applied
NOMINALLY in whichever currency the purchase is quoted in: a site minimum of
5 means GHS 5.00 for a Ghana purchase and CAD 5.00 for a Canadian one. There is
no exchange rate in this app, and inventing one to convert a floor would price
a purchase off rate nobody approved. The conversion to minor units is exact
(`Decimal`, never `float * fraction`); where the configured amount has finer
precision than the currency's own coin the floor is rounded UP, because a
minimum must never be lowered by an unlucky fraction.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import cint, cstr

ORGANIZATION_DOCTYPE = "Rest Credit Organization"
SETTINGS_DOCTYPE = "Rest Credit Settings"
CURRENCY_DOCTYPE = "Currency"

# A percentage is stored with two decimals, so one percent is 100 basis points.
BASIS_POINTS = 10000  # the denominator: 100.00% expressed in basis points
BASIS_POINTS_PER_PERCENT = BASIS_POINTS // 100
MAX_BASIS_POINTS = BASIS_POINTS - 1  # a 100% discount would leave no price

DEFAULT_FRACTION_UNITS = 100


# ── the organization's discount ─────────────────────────────────────────────


def organization_row(instance_id=None, organization_id=None):
	"""
	The registry row an organization's terms live on, or None.

	Scoped by ``(instance_id, organization_id)`` when both are known: ids are
	per-deployment, so resolving by organization id alone could pick another
	deployment's church and apply its discount to this one. An ambiguous lookup
	(no instance id and more than one row) answers None rather than guessing --
	a wrong discount is money, and guessing it is exactly the failure this
	module exists to make impossible.
	"""
	organization_id = cstr(organization_id).strip()
	if not organization_id:
		return None
	if not frappe.db.exists("DocType", ORGANIZATION_DOCTYPE):
		return None

	filters = {"organization_id": organization_id}
	instance_id = cstr(instance_id).strip()
	if instance_id:
		filters["instance_id"] = instance_id

	names = frappe.get_all(
		ORGANIZATION_DOCTYPE, filters=filters, pluck="name", limit_page_length=0
	)
	if len(names) != 1:
		return None

	return frappe.get_doc(ORGANIZATION_DOCTYPE, names[0])


def _row_get(row, fieldname, default=None):
	"""
	Read a field from a document, tolerating a site that has not migrated yet.

	A new term field does not exist as a column until `bench migrate` installs it,
	and an API call can arrive in that window. A missing field must read as its
	default (no discount, no allowance), not turn every quote into a stack trace.
	"""
	if row is None:
		return default
	try:
		return row.get(fieldname, default)
	except (AttributeError, KeyError):
		return default


def basis_points_from_percent(percent):
	"""A stored ``discount_percent`` as integer basis points, half-up."""
	try:
		value = Decimal(str(percent if percent not in (None, "") else 0))
	except (InvalidOperation, ValueError):
		return 0

	if value <= 0:
		return 0
	if value >= 100:
		# The controller refuses 100% (it would hand out credits for nothing);
		# clamping here is a defensive floor for a hand-edited database row.
		return MAX_BASIS_POINTS

	return int((value * BASIS_POINTS_PER_PERCENT).to_integral_value(rounding=ROUND_HALF_UP))


def discount_basis_points(organization_id=None, instance_id=None):
	"""
	The organization's discount in basis points, or 0.

	A missing/ambiguous organization is 0 rather than an error: a quote without
	an organization (the app's own Credits page, a plain link) must still get a
	price, and the safe answer is the list price. The organization whose terms
	apply is resolved HERE, from the registry -- never accepted from the caller
	as a number, which is what stops a tenant application from granting itself
	a discount.
	"""
	row = organization_row(instance_id=instance_id, organization_id=organization_id)
	if not row:
		return 0
	return basis_points_from_percent(_row_get(row, "discount_percent"))


def effective_unit_price_micros(list_micros, discount_bp=0):
	"""
	The price after a basis-point discount, in micros, half-up.

	All-integer, mirroring the app's `effectiveUnitPriceMicros`: both sides
	re-derive the same integer or the quote is refused as inconsistent, so the
	rounding rule cannot drift between them.
	"""
	list_micros = int(list_micros or 0)
	basis_points = int(discount_bp or 0)

	if list_micros <= 0:
		return 0
	if basis_points <= 0:
		return list_micros
	if basis_points >= BASIS_POINTS:
		return 0

	numerator = list_micros * (BASIS_POINTS - basis_points)
	# Half-up for a non-negative rational: floor((2n + d) / 2d), d = 10000.
	return (2 * numerator + BASIS_POINTS) // (2 * BASIS_POINTS)


def pricing_error_for(list_micros, discount_bp=0):
	"""
	Why a list price + discount cannot be sold, or None.

	A discount can round an otherwise valid list price down to zero (one micro
	at 99.99%). That is a misconfigured term, not a free purchase, so it is
	reported as data -- naming the discount -- for the layer that owns the
	message to refuse.
	"""
	list_micros = int(list_micros or 0)
	basis_points = int(discount_bp or 0)
	if list_micros <= 0:
		return None
	if effective_unit_price_micros(list_micros, basis_points) > 0:
		return None

	return _(
		"The organization's {0}% discount leaves no price for a list price of {1} micros. "
		"Correct the discount on the organization, or set a higher rate."
	).format(Decimal(basis_points) / 100, list_micros)


# ── the site minimum purchase ───────────────────────────────────────────────


def settings_values():
	"""
	The settings single, as a plain dict, without a permission check.

	Read via ``db.get_single_value`` because `minimum_purchase_minor` runs on the
	public purchase form (Guest) as well as in Desk: the floor is not secret,
	and a Guest cannot be given read permission on a System-Manager single.
	"""
	values = {}
	for fieldname in ("minimum_purchase_amount",):
		try:
			values[fieldname] = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
		except Exception:
			# A site that has not migrated yet has no settings table. A missing
			# floor is 0, not a broken purchase page.
			values[fieldname] = None
	return values


def minimum_purchase_amount():
	"""The configured floor, in major units, as a Decimal (0 when unset)."""
	raw = settings_values().get("minimum_purchase_amount")
	if raw in (None, ""):
		return Decimal("0")
	try:
		return Decimal(str(raw))
	except (InvalidOperation, ValueError):
		return Decimal("0")


def default_currency():
	"""
	The currency a currency-less context (the settings single, an intake row
	before the form has quoted) is expressed in.
	"""
	candidates = (
		lambda: frappe.db.get_default("currency"),
		lambda: frappe.db.get_single_value("Global Defaults", "default_currency"),
		lambda: frappe.db.get_value("Company", {}, "default_currency"),
	)
	for candidate in candidates:
		try:
			currency = cstr(candidate()).strip().upper()
		except Exception:
			currency = ""
		if currency:
			return currency
	return "GHS"


def currency_fraction_units(currency=None):
	"""
	How many minor units one major unit has, from the Currency doctype.

	The same lookup `invoicing.currency_fraction_units` uses; duplicated here
	rather than imported so `terms` stays importable from `invoicing` without a
	module cycle. MGA is 5, not 100 -- a hardcoded factor is wrong by a clean
	multiple.
	"""
	currency = cstr(currency).strip().upper()
	if currency:
		fraction = frappe.db.get_value(CURRENCY_DOCTYPE, currency, "fraction_units")
		return cint(fraction) or DEFAULT_FRACTION_UNITS
	return cint(frappe.db.get_value(CURRENCY_DOCTYPE, default_currency(), "fraction_units")) or DEFAULT_FRACTION_UNITS


def major_to_minor(amount_major, currency=None, rounding=ROUND_CEILING):
	"""
	A major-unit amount as an exact minor-unit integer.

	``Decimal`` rather than ``float * fraction``: 1.05 * 5 is 5.25 in decimal
	and something unprintable in binary, and this figure becomes a floor. A
	currency's coin cannot represent every configured amount, so callers choose
	the rounding; the default CEILING keeps a minimum a minimum.
	"""
	currency = cstr(currency).strip().upper() or default_currency()
	try:
		amount = amount_major if isinstance(amount_major, Decimal) else Decimal(str(amount_major or 0))
	except (InvalidOperation, ValueError):
		return 0
	return int((amount * currency_fraction_units(currency)).to_integral_value(rounding=rounding))


def minimum_purchase_minor(currency=None):
	"""The minimum purchase in the given currency's minor units, 0 when unset."""
	amount = minimum_purchase_amount()
	if amount <= 0:
		return 0
	return major_to_minor(amount, currency)


def minimum_purchase_display(currency=None, minimum_minor=None):
	"""
	The floor as a buyer reads it, e.g. ``"CAD 25.00"``, or None when unset.

	``invoicing.format_minor`` renders the number; the currency code is put in
	front here because a bare "25.00" does not say 25 of what, and this string
	appears on a public page next to a payment amount.
	"""
	if minimum_minor is None:
		minimum_minor = minimum_purchase_minor(currency)
	minimum_minor = int(minimum_minor or 0)
	if minimum_minor <= 0:
		return None

	currency = cstr(currency).strip().upper() or default_currency()
	return "{0} {1}".format(currency, format_minor(minimum_minor, currency))


def format_minor(amount_minor, currency=None):
	"""A minor-unit amount at the currency's own precision, no currency code."""
	currency = cstr(currency).strip().upper() or default_currency()
	fraction = currency_fraction_units(currency)
	amount = int(amount_minor or 0)
	if fraction == 1:
		return str(amount)

	decimals = len(str(fraction - 1))
	return "{0:.{1}f}".format(Decimal(amount) / Decimal(fraction), decimals)


def validate_amount_precision(amount_major, currency=None):
	"""
	Refuse a settings amount the currency cannot express.

	The `Currency` fieldtype stores a float; this is where a figure with finer
	precision than the smallprint (0.001 in a 2-decimal currency) is caught
	instead of being silently rounded into a different floor. Returns the
	amount as a Decimal.
	"""
	currency = cstr(currency).strip().upper() or default_currency()
	try:
		amount = Decimal(str(amount_major if amount_major not in (None, "") else 0))
	except (InvalidOperation, ValueError):
		frappe.throw(_("Minimum Purchase must be an amount, not '{0}'.").format(cstr(amount_major)))

	if amount < 0:
		frappe.throw(_("Minimum Purchase cannot be negative."))

	minor = amount * currency_fraction_units(currency)
	if minor != minor.to_integral_value():
		frappe.throw(
			_(
				"Minimum Purchase must be a whole number of {0} minor units -- {0} amounts "
				"can be at most {1} decimal places, but {2} has more."
			).format(currency, len(str(currency_fraction_units(currency) - 1)), amount)
		)

	return amount


# ── assembling the terms a quote/rate response carries ─────────────────────


def terms_for(instance_id=None, organization_id=None, currency=None):
	"""
	Every commercial term that applies to one organization, in one dict.

	The organization terms are absent-safe: no row means list price, no
	discount, no allowance. The minimum is site-wide and therefore present even
	when there is no organization, which is what lets the app's own Credits page
	show the floor behind its "Buy credits" button.
	"""
	row = organization_row(instance_id=instance_id, organization_id=organization_id)
	discount_percent = float(_row_get(row, "discount_percent") or 0)

	basis_points = basis_points_from_percent(discount_percent)
	minimum_minor = minimum_purchase_minor(currency)

	return {
		"organization_name": cstr(_row_get(row, "organization_name")).strip() if row else None,
		"discount_percent": round(basis_points / 100, 2),
		"discount_basis_points": basis_points,
		"discount_note": cstr(_row_get(row, "discount_note")).strip() or None,
		"monthly_free_sms_units": cint(_row_get(row, "monthly_free_sms_units")),
		"monthly_free_email_units": cint(_row_get(row, "monthly_free_email_units")),
		"free_credits_rollover": bool(cint(_row_get(row, "free_credits_rollover"))),
		"minimum_purchase_minor": minimum_minor,
		"minimum_purchase_amount": minimum_purchase_display(currency, minimum_minor),
	}


def context_fields(instance_id=None, organization_id=None, currency=None, display_currency=None):
	"""
	The term fields `get_billing_context` adds to its answer.

	A thin projection of `terms_for`, kept separate so the shape the billing
	page consumes is named in one place (the app parses this field-by-field).
	"""
	values = terms_for(
		instance_id=instance_id, organization_id=organization_id, currency=currency
	)
	return {
		"discount_percent": values["discount_percent"],
		"discount_basis_points": values["discount_basis_points"],
		"discount_note": values["discount_note"],
		"monthly_free_sms_units": values["monthly_free_sms_units"],
		"monthly_free_email_units": values["monthly_free_email_units"],
		"free_credits_rollover": values["free_credits_rollover"],
		"minimum_purchase_minor": values["minimum_purchase_minor"],
		"minimum_purchase_amount": values["minimum_purchase_amount"],
		"minimum_purchase_currency": cstr(display_currency or currency).strip().upper() or None,
	}


def apply_to_rate(rate, terms=None):
	"""
	Fold an organization's terms into a `get_credit_rate` answer.

	`rate` is the list-price answer from `api.get_credit_rate`; this returns a
	copy where:
	  * `list_price_per_unit_micros` is the rate card's price,
	  * `price_per_unit_micros` is the effective (discounted) price,
	  * `minimum_purchase_*` and `pricing_error` describe what a purchase needs.

	A discount that leaves no price is DATA (`pricing_error`) rather than a
	throw: the layer that owns the refusal word (the app, or this app's public
	form) decides what the buyer is told.
	"""
	terms = terms or {}
	result = dict(rate)

	list_micros = int(result.get("list_price_per_unit_micros") or result.get("price_per_unit_micros") or 0)
	basis_points = int(terms.get("discount_basis_points") or 0)
	effective = effective_unit_price_micros(list_micros, basis_points)

	result["list_price_per_unit_micros"] = list_micros
	result["price_per_unit_micros"] = effective
	result["discount_basis_points"] = basis_points
	result["discount_percent"] = round(basis_points / 100, 2)
	result["minimum_purchase_minor"] = int(terms.get("minimum_purchase_minor") or 0)
	result["minimum_purchase_amount"] = terms.get("minimum_purchase_amount")
	result["pricing_error"] = pricing_error_for(list_micros, basis_points)

	return result


# ── the monthly allowance read ─────────────────────────────────────────────


PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def normalize_period(period=None):
	"""
	The allowance period, as a UTC ``YYYY-MM`` string.

	UTC rather than the site's timezone because the app grants against it and
	the two ends must name the same month; a site-level clock change must not
	move a grant into another period. The period is a LABEL for an entitlement,
	not a stored row: nothing is written by reading it, so a late or repeated
	read cannot double-grant -- the app keys its grant on the period.
	"""
	text = cstr(period).strip()
	if text:
		if not PERIOD_PATTERN.match(text):
			frappe.throw(
				_("Period must be a UTC month in YYYY-MM form, for example 2026-10."),
				frappe.ValidationError,
			)
		return text

	from datetime import datetime, timezone

	return datetime.now(timezone.utc).strftime("%Y-%m")


def allowance_rows(instance_id):
	"""Registered organizations for one deployment, ordered deterministically."""
	if not frappe.db.exists("DocType", ORGANIZATION_DOCTYPE):
		return []
	if not frappe.db.has_column(ORGANIZATION_DOCTYPE, "monthly_free_sms_units"):
		# The terms columns arrive with `bench migrate`; before that there is no
		# entitlement to report, and an empty answer is the honest one.
		return []
	return frappe.get_all(
		ORGANIZATION_DOCTYPE,
		filters={"instance_id": cstr(instance_id).strip()},
		fields=[
			"name",
			"organization_id",
			"organization_name",
			"default_group_id",
			"default_group_name",
			"billing_company",
			"billing_currency",
			"monthly_free_sms_units",
			"monthly_free_email_units",
			"free_credits_rollover",
		],
		order_by="organization_name asc, organization_id asc",
		limit_page_length=0,
	)


def allowance_currency(row):
	"""
	The currency an allowance's units are expressed in, for the app's ledger.

	`billing_currency` first (the organization's own selling currency); if the
	registration has not had a billing context yet, the mapped company's
	default currency. Units are of a `currency` for accounting provenance, not
	because free credits cost money.
	"""
	currency = cstr(_row_get(row, "billing_currency")).strip().upper()
	if currency:
		return currency
	company = cstr(_row_get(row, "billing_company")).strip()
	if company:
		return cstr(frappe.db.get_value("Company", company, "default_currency")).strip().upper() or None
	return None


def channel_allowances(row):
	"""
	The positive monthly entitlements on one organization, per channel.

	Zero rows are omitted rather than reported as grants of zero: an app
	granting "0 units" would create empty lots and ledger entries, which is
	noise, not an entitlement.
	"""
	entitlements = []
	for channel, fieldname in (
		("sms", "monthly_free_sms_units"),
		("email", "monthly_free_email_units"),
	):
		units = cint(_row_get(row, fieldname))
		if units > 0:
			entitlements.append((channel, units))
	return entitlements
