# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The credits API — the one surface the Rest app talks to.

Two read-only methods, and nothing that writes money:

  * `get_credit_rate` — the price of one service unit. The rate card lives where
    the money is, so the app learns a price here and nowhere else.
  * `_touch_instance`/`Rest Instance` — deployment auto-registration, so a
    proposal is attributable to the instance that raised it.

The app can CREATE and READ; it cannot submit, cancel, write or delete. Money is
recorded by ERPNext itself: a gateway's own entry, or an operator entering cash
or a bank transfer in the Desk, ends as a submitted Payment Entry against the
proposal's Sales Invoice, and the app grants credits only when it sees that
settlement.

## What used to be here

`confirm_funding`, and with it the `Rest Communication Credit Funding` doctype and
its read/count/forget helpers. The app asked ERPNext to WRITE a funding row, which
made the requester the authority. The settled proposal is now the event: the app
reads settlement state and grants against it, so asking ERPNext to write a second
record of the same money would duplicate it. The doctype and method are gone; the
proposal is the instrument.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime, nowdate

from brchms.brchms_credits import billing, terms

INSTANCE_DOCTYPE = "Rest Instance"


def _touch_instance(instance_id, display_name=None, erpnext_site=None):
	"""
	Record that this deployment has been heard from, and return its id.

	Auto-registration, rather than a pre-registration step an operator must
	remember: a deployment that can pay for credits is a deployment this site
	should be able to name, and requiring a hand-created row first would make the
	first real confirmation fail for a bookkeeping reason.

	Idempotent. `first_seen_at` is set once, `last_seen_at` and `confirmed_count`
	move; `display_name` is only written while it is EMPTY, so an operator's rename
	in the Desk is never overwritten by the next confirmation.
	"""
	instance_id = cstr(instance_id).strip()
	if not instance_id:
		# `_require_fields` already guarantees one; belt and braces, because a
		# Link to "" is worse than a refusal.
		frappe.throw(_("instance_id is required to record funding."), frappe.MandatoryError)

	names = frappe.get_all(INSTANCE_DOCTYPE, filters={"name": instance_id}, pluck="name")

	if not names:
		# No explicit `name`: `naming_rule: By fieldname` derives it from
		# `instance_id`, and setting both invites a mismatch.
		doc = frappe.get_doc(
			{
				"doctype": INSTANCE_DOCTYPE,
				"instance_id": instance_id,
				"display_name": display_name or instance_id,
				"erpnext_site": erpnext_site,
				"is_active": 1,
				"first_seen_at": now_datetime(),
				"last_seen_at": now_datetime(),
				"confirmed_count": 1,
			}
		)
		try:
			doc.insert(ignore_permissions=True)
		except frappe.UniqueValidationError:
			# Lost a race with a concurrent first contact: the winner created the
			# row, and this caller's event is a DIFFERENT confirmation, so falling
			# through to increment is correct -- the count tracks events, not
			# instances.
			pass
		else:
			return instance_id

	# The increment happens in SQL, not read-then-write.
	#
	# Two confirmations can arrive at once, and a read-modify-write would lose one
	# of them -- the count would end up one short, which is exactly the kind of
	# quiet disagreement this number exists to catch. `confirmed_count = confirmed_count + 1`
	# is atomic.
	set_clause = [
		"`last_seen_at` = %(last_seen_at)s",
		"`confirmed_count` = COALESCE(`confirmed_count`, 0) + 1",
	]
	if erpnext_site:
		set_clause.append("`erpnext_site` = %(erpnext_site)s")

	frappe.db.sql(
		"UPDATE `tab{0}` SET {1} WHERE `name` = %(instance_id)s".format(
			INSTANCE_DOCTYPE, ", ".join(set_clause)
		),
		{"last_seen_at": now_datetime(), "erpnext_site": erpnext_site, "instance_id": instance_id},
	)

	# An operator's name wins; only fill a blank one.
	if display_name:
		frappe.db.sql(
			"UPDATE `tab{0}` SET `display_name` = %(display_name)s "
			"WHERE `name` = %(instance_id)s "
			"AND (`display_name` IS NULL OR `display_name` = '' OR `display_name` = `instance_id`)"
			.format(INSTANCE_DOCTYPE),
			{"display_name": display_name, "instance_id": instance_id},
		)

	return instance_id


RATE_DOCTYPE = "Rest Credit Rate"
BUYING_RATE_DOCTYPE = "Rest Credit Buying Rate"
CHANNEL_UNITS = {"sms": "sms_segment", "email": "email_message"}
PROVIDER_CHANNELS = {"nalo": "sms", "twilio": "sms", "smtp": "email"}


@frappe.whitelist()
def get_provider_cost(
	company=None, provider=None, channel=None, destination=None, at=None, country_code=None, currency=None
):
	"""
	What the selling company pays the provider for one unit, for margin tracking.

	This is INTERNAL integration data: it is scoped to the selling company and
	never returned to the public purchase form or a customer UI. The app snapshots
	it on each delivery, so a later rate change does not rewrite the cost of a
	message already sent.
	"""
	channel = cstr(channel).strip().lower()
	provider = cstr(provider).strip().lower()
	if channel not in CHANNEL_UNITS:
		frappe.throw(_("Channel must be 'sms' or 'email'."), frappe.ValidationError)
	if provider not in PROVIDER_CHANNELS:
		frappe.throw(_("Provider must be nalo, twilio or smtp."), frappe.ValidationError)
	if PROVIDER_CHANNELS[provider] != channel:
		frappe.throw(
			_("{0} is not an {1} provider.").format(
				frappe.utils.escape_html(provider), frappe.utils.escape_html(channel)
			),
			frappe.ValidationError,
		)

	company = billing.resolve_selling_company(company=company, currency=currency)
	currency = cstr(frappe.db.get_value("Company", company, "default_currency")).upper()
	moment = cstr(at) if at else str(now_datetime())
	country = _buying_country_code(company, provider, channel, destination, country_code)

	base = {
		"company": company,
		"provider": provider,
		"channel": channel,
		"unit": CHANNEL_UNITS[channel],
		"currency": currency,
		"country_code": country,
		"price_per_unit_micros": 0,
		"supplier_company": None,
		"cost_rate_name": None,
		"effective_from": None,
		"effective_to": None,
		"resolved_at": str(now_datetime()),
		"source": "unconfigured",
	}

	rows = _active_buying_rates(company, provider, channel, moment)
	if country:
		for row in rows:
			if cstr(row.destination_country_code).strip() == country:
				return _as_provider_cost(row, base, "exact_country")
	for row in rows:
		if not cstr(row.destination_country_code).strip():
			return _as_provider_cost(row, base, "channel_default")
	return base


def _active_buying_rates(company, provider, channel, moment):
	rows = frappe.get_all(
		BUYING_RATE_DOCTYPE,
		filters={
			"company": company,
			"provider": provider,
			"channel": channel,
			"is_active": 1,
			"effective_from": ("<=", moment),
		},
		fields=[
			"name",
			"price_per_unit_micros",
			"currency",
			"destination_country_code",
			"supplier_company",
			"effective_from",
			"effective_to",
		],
		order_by="effective_from desc, creation desc",
		limit_page_length=0,
	)
	return [
		row for row in rows if not row.effective_to or cstr(row.effective_to)[:10] >= moment[:10]
	]


def _as_provider_cost(row, base, source):
	return {
		**base,
		"price_per_unit_micros": int(row.price_per_unit_micros),
		"currency": cstr(row.currency or base["currency"]).upper(),
		"country_code": cstr(row.destination_country_code).strip() or base["country_code"],
		"supplier_company": cstr(row.supplier_company).strip() or base["company"],
		"cost_rate_name": row.name,
		"effective_from": cstr(row.effective_from) if row.effective_from else None,
		"effective_to": cstr(row.effective_to) if row.effective_to else None,
		"source": source,
	}


def _buying_country_code(company, provider, channel, destination, country_code):
	explicit = cstr(country_code).strip()
	if explicit:
		try:
			from brchms.brchms_credits.doctype.rest_credit_buying_rate.rest_credit_buying_rate import (
				normalize_prefix,
			)

			return normalize_prefix(explicit)
		except Exception:
			frappe.throw(_("Country Code must be a dialling code such as +233."), frappe.ValidationError)

	number = cstr(destination).strip().replace(" ", "").replace("-", "")
	if not number:
		return None
	if not number.startswith("+"):
		number = "+" + number
	codes = frappe.get_all(
		BUYING_RATE_DOCTYPE,
		filters={
			"company": company,
			"provider": provider,
			"channel": channel,
			"destination_country_code": ("is", "set"),
		},
		pluck="destination_country_code",
		limit_page_length=0,
	)
	best = None
	for code in set(codes):
		candidate = cstr(code).strip()
		if candidate and number.startswith(candidate) and (best is None or len(candidate) > len(best)):
			best = candidate
	return best


@frappe.whitelist()
def billing_preflight():
	"""
	Read-only readiness report for multi-company credit billing.

	An operator runs this before switching purchases on: it lists the company
	currencies, the country mappings, and every row/mapping that still needs a
	decision. It never guesses a mapping and never writes.
	"""
	companies = frappe.get_all(
		"Company",
		fields=["name", "default_currency", "abbr"],
		order_by="name asc",
		limit_page_length=0,
	)
	regions = frappe.get_all(
		"Rest Credit Billing Region",
		fields=["name", "region_name", "country", "company", "currency", "is_active"],
		order_by="country asc",
		limit_page_length=0,
	)
	rates = frappe.get_all(
		"Rest Credit Rate",
		fields=["name", "company", "channel", "currency", "country_code", "is_active"],
		order_by="company asc, channel asc",
		limit_page_length=0,
	)
	buying_rates = frappe.get_all(
		"Rest Credit Buying Rate",
		fields=["name", "company", "provider", "channel", "currency", "destination_country_code", "is_active"],
		order_by="company asc, provider asc",
		limit_page_length=0,
	)

	def missing_company(doctype):
		if not frappe.db.has_column(doctype, "company"):
			return None
		return frappe.db.sql(
			"SELECT COUNT(*) FROM `tab{0}` WHERE company IS NULL OR company = ''".format(doctype)
		)[0][0]

	missing_regions = [row.name for row in regions if row.is_active and not row.company]
	rate_gaps = [row.name for row in rates if not row.company or not row.currency]
	org_missing = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabRest Credit Organization` "
		"WHERE billing_company IS NULL OR billing_company = ''"
	)[0][0] if frappe.db.has_column("Rest Credit Organization", "billing_company") else None

	gateways = []
	try:
		gateways = frappe.get_all(
			"Payment Gateway Account",
			fields=["name", "company", "payment_gateway"],
			limit_page_length=0,
		)
	except Exception:
		# A site without the gateway app can still run the rest of the preflight.
		pass

	return {
		"companies": companies,
		"regions": regions,
		"rates": rates,
		"buying_rates": buying_rates,
		"gateway_accounts": gateways,
		"gaps": {
			"active_regions_without_company": missing_regions,
			"rates_without_company_or_currency": rate_gaps,
			"proposals_without_company": missing_company("Rest Credit Proposal"),
			"purchases_without_company": missing_company("Rest Credit Purchase"),
			"organizations_without_billing_company": org_missing,
			"active_regions": sum(1 for row in regions if row.is_active),
			"active_buying_rates": sum(1 for row in buying_rates if row.is_active),
		},
		"ready": bool([row for row in regions if row.is_active]) and not missing_regions and not rate_gaps,
	}


@frappe.whitelist()
def get_credit_rate(
	channel=None,
	currency=None,
	destination=None,
	at=None,
	company=None,
	organization_id=None,
	country_code=None,
	instance_id=None,
):
	"""
	The price of one service unit, in millionths of a major unit.

	Read-only, and the ONLY way the Rest app learns a price. That direction is
	deliberate: the rate card lives where the money is, so the person who manages
	funding sets it once and every purchase is quoted from it -- instead of each
	purchase carrying a price somebody typed, where two people can disagree and
	nobody notices until a reconciliation.

	## Precedence, mirrored from the app

	The app already had a rate card with a documented resolution order. This is that
	same order, one for one, so switching the authority to ERPNext does not change
	which price applies to anybody:

	  1. an ACTIVE row for the destination's dialling code, inside its window
	     (`source: exact_country`);
	  2. the ACTIVE row with no country code -- the channel default
	     (`source: channel_default`);
	  3. nothing, reported honestly rather than as a zero (`source: unconfigured`).

	An exact country always beats the default. That is what makes a dearer SMS
	destination possible without re-pricing everywhere else.

	## Which of several rows wins

	The app's table resolves by window alone and relies on there being one active row
	per scope. Nothing enforced that, so a tiebreak is needed here -- the newest
	`effective_from` wins, and `creation` breaks a same-instant tie. Stated rather
	than left to whatever the database returns first.

	## Why `unconfigured` is returned instead of throwing

	The APP decides what an unconfigured rate means: its controller already refuses a
	purchase with a message naming the fix. Returning a zero here would hand out
	credits for nothing, and throwing would put the sentence in the wrong layer. So
	the absence is reported as data and the app refuses.

	## The organization's terms

	`organization_id` (and its deployment's `instance_id`) asks this method to apply
	the organization's negotiated discount, and to report the site-wide minimum
	purchase. The DISCOUNT IS RESOLVED HERE from the registry, never accepted as a
	number from the caller: a tenant application that could name its own discount
	could buy credits at any price. The answer carries:

	  * `list_price_per_unit_micros` -- the rate card's price,
	  * `price_per_unit_micros` -- the EFFECTIVE price after the discount,
	  * `discount_basis_points` / `discount_percent`,
	  * `minimum_purchase_minor` / `minimum_purchase_amount`,
	  * `pricing_error` -- a term that cannot be priced (e.g. a discount leaving
	    zero), reported as data for the same reason `unconfigured` is.

	Without an organization the answer is the plain list price with no discount,
	but still carries the site minimum: every buyer faces the same floor.

	`rate_name` is returned so a purchase can record WHICH row it was quoted from.
	Without it a later rate change leaves a quoted purchase unattributable, and
	"priced once, never re-priced" stops being checkable.
	"""
	# The company is resolved BEFORE the currency is defaulted: a multi-company
	# site must not silently quote the site default's price to another company.
	company = billing.resolve_selling_company(
		company=company, country_code=country_code, currency=currency
	)
	company_currency = cstr(frappe.db.get_value("Company", company, "default_currency")).upper()
	currency = cstr(currency).strip().upper() if currency else company_currency
	if currency != company_currency:
		frappe.throw(
			_(
				"{0} keeps its books in {1}, not {2}; a rate in another currency would be a "
				"cross-currency price nobody quoted."
			).format(
				frappe.utils.escape_html(company),
				frappe.utils.escape_html(company_currency),
				frappe.utils.escape_html(currency),
			)
		)

	channel, currency = _require_rate_keys(channel, currency)
	moment = cstr(at) if at else str(now_datetime())
	country = _dialling_code(destination, channel, company) if channel == "sms" else None

	base = {
		"channel": channel,
		"unit": CHANNEL_UNITS[channel],
		"price_per_unit_micros": 0,
		"list_price_per_unit_micros": 0,
		"discount_basis_points": 0,
		"discount_percent": 0.0,
		"minimum_purchase_minor": 0,
		"minimum_purchase_amount": None,
		"pricing_error": None,
		"currency": currency,
		"company": company,
		"country_code": country,
		"rate_name": None,
		"effective_from": None,
		"effective_to": None,
		"resolved_at": str(now_datetime()),
		"source": "unconfigured",
	}

	rate = base
	if country:
		for row in _active_rates(channel, moment, company):
			if cstr(row.country_code) == country:
				rate = _as_rate(row, base, "exact_country")
				break

	if rate is base:
		for row in _active_rates(channel, moment, company):
			if not row.country_code:
				rate = _as_rate(row, base, "channel_default")
				break

	return terms.apply_to_rate(
		rate,
		terms.terms_for(
			instance_id=instance_id,
			organization_id=organization_id,
			currency=currency,
		),
	)


def _active_rates(channel, moment, company):
	"""Active rows for a channel+company whose window contains `moment`, newest first."""
	rows = frappe.get_all(
		RATE_DOCTYPE,
		filters={
			"channel": channel,
			"company": company,
			"is_active": 1,
			"effective_from": ("<=", moment),
		},
		fields=[
			"name",
			"unit",
			"price_per_unit_micros",
			"currency",
			"company",
			"country_code",
			"effective_from",
			"effective_to",
		],
		order_by="effective_from desc, creation desc",
		limit_page_length=0,
	)
	# The upper bound is applied here rather than in SQL so a NULL `effective_to`
	# ("until further notice") is not lost to a three-valued comparison.
	return [row for row in rows if not row.effective_to or cstr(row.effective_to) >= cstr(moment)]


def _as_rate(row, base, source):
	return {
		**base,
		"unit": row.unit or base["unit"],
		"price_per_unit_micros": int(row.price_per_unit_micros),
		"currency": cstr(row.currency or base["currency"]).upper(),
		"country_code": cstr(row.country_code) if row.country_code else base["country_code"],
		"rate_name": row.name,
		"effective_from": cstr(row.effective_from) if row.effective_from else None,
		"effective_to": cstr(row.effective_to) if row.effective_to else None,
		"source": source,
	}


def _dialling_code(destination, channel, company=None):
	"""
	The dialling code of a destination number, choosing among the codes ON FILE.

	## Why this matches the rate card and not a country table

	Frappe's `Country` doctype stores ISO codes ("GH", "CA"), not dialling codes, so
	there is no country table to look a number up in. Guessing one would be worse
	than not having it: a wrong prefix prices a purchase off the wrong country, and
	the price looks perfectly plausible.

	Matching against the codes that actually appear on this channel's rate rows is
	correct by construction -- the answer is always a code some rate can be found
	for, so the lookup cannot invent a country the rate card has never heard of.

	The LONGEST match wins, so a three-digit code is not shadowed by the two-digit
	code inside it. A number matching nothing returns None, which falls through to
	the channel default rather than pricing off a guess.
	"""
	number = cstr(destination).strip().replace(" ", "").replace("-", "")
	if not number:
		return None
	if not number.startswith("+"):
		number = "+" + number

	filters = {"channel": channel, "country_code": ("is", "set")}
	if company:
		filters["company"] = company
	codes = frappe.get_all(RATE_DOCTYPE, filters=filters, pluck="country_code", limit_page_length=0)

	best = None
	for code in set(codes):
		candidate = cstr(code).strip()
		if candidate and number.startswith(candidate) and (best is None or len(candidate) > len(best)):
			best = candidate
	return best


def _require_rate_keys(channel, currency):
	channel = cstr(channel).strip().lower()
	currency = cstr(currency).strip().upper()

	if channel not in CHANNEL_UNITS:
		frappe.throw(
			_("Rate channel must be 'sms' or 'email', not '{0}'.").format(
				frappe.utils.escape_html(cstr(channel))
			),
			frappe.ValidationError,
		)

	if not currency:
		frappe.throw(_("A rate is quoted in a currency, and none was named."), frappe.ValidationError)

	return channel, currency


# ── monthly free-credit entitlements ───────────────────────────────────────


@frappe.whitelist()
def list_credit_allowances(instance_id=None, period=None):
	"""
	The monthly free-credit entitlements for one deployment.

	The app's allowance poller reads this, then grants the units into its own
	ledger. **ERPNext owns the entitlement; the app owns the balance.** One
	direction only: the app never writes an allowance here, and this method
	writes nothing at all -- a late or repeated read returns the same answer for
	the same period, so the poller's idempotency key (`period`) is what makes a
	monthly grant single, not a stored claim on this side.

	## What a grant is, and what it is not

	A grant is `units` of one channel for one organization's DEFAULT funding
	group: the group a public-form purchase would credit, so an allowance adds
	up on the same balance a purchase does. Units are not money and are not
	priced; `currency` travels only for the app ledger's provenance.

	## Positive only, and no silent loss

	An organization with no allowance on a channel contributes no grant. An
	organization that HAS an allowance but no `default_group_id` cannot be
	granted anywhere -- that is reported in `skipped` with a reason, so the
	entitlement is visible instead of silently dropped. `period` is UTC
	(`YYYY-MM`) because both ends must name the same month.

	Returns `{"period", "grants": [...], "skipped": [...]}` wherever the
	deployment has no organizations: an empty entitlement list is a successful
	answer, not an error.
	"""
	instance_id = cstr(instance_id).strip()
	if not instance_id:
		frappe.throw(_("instance_id is required."), frappe.MandatoryError)

	period = terms.normalize_period(period)
	grants = []
	skipped = []

	for row in terms.allowance_rows(instance_id):
		entitlements = terms.channel_allowances(row)
		if not entitlements:
			continue

		organization_id = cstr(row.get("organization_id")).strip()
		organization_name = cstr(row.get("organization_name")).strip() or None
		funding_group_id = cstr(row.get("default_group_id")).strip()
		funding_group_name = cstr(row.get("default_group_name")).strip() or None
		currency = terms.allowance_currency(row) or terms.default_currency()
		rollover = bool(cint(row.get("free_credits_rollover")))

		for channel, units in entitlements:
			if not funding_group_id:
				skipped.append(
					{
						"organization_id": organization_id,
						"organization_name": organization_name,
						"channel": channel,
						"units": units,
						"reason": _(
							"No default funding group is registered, so the allowance has nowhere "
							"to be granted. Open the credits page in the app once to complete "
							"registration."
						),
					}
				)
				continue

			grants.append(
				{
					"organization_id": organization_id,
					"organization_name": organization_name,
					"funding_group_id": funding_group_id,
					"funding_group_name": funding_group_name,
					"channel": channel,
					"units": units,
					"currency": currency,
					"rollover": rollover,
				}
			)

	return {"period": period, "grants": grants, "skipped": skipped}
