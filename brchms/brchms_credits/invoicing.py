# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The invoice and payment page a credit proposal turns into.

## Why this is shared

Both doors to a purchase -- the app's "record request" and the public form's
`checkout` -- must produce the SAME artefacts: one submitted Sales Invoice
carrying the proposal's reference, and one payment page. Before this module,
only the form's path raised an invoice, so a purchase recorded in the app had
a proposal in ERPNext that could never be paid. One implementation, called
from both, is the only way the two cannot drift.

## The pricing invariant

A proposal's `(units, unit_price_micros, amount_minor)` must agree at the
currency's minor precision:

    amount_minor == round_half_up(units x micros x fraction_units / 10**6)

Rounded, not exact, because the two scales cannot always divide evenly: at
GHS 0.045 per segment, 5,555 segments cost 249.975, which no pesewa amount can
equal -- 249.98 is the honest answer. Half-up is the money convention and is
stated here rather than left to a language default (Python's `round` is
banker's). `fraction_units` is read from the `Currency` doctype, because a
5:1 currency like MGA makes a hardcoded 100 wrong by a clean multiple.

`quote()` is where the pair is DERIVED -- the form's buyer types units and the
amount is computed -- and `require_consistent()` is where it is ENFORCED
before an invoice exists. A pair that disagrees with its own price never
becomes an invoice: an inconsistent order is a request for credits nobody
priced, and surfacing it at the desk after payment is a reconciliation,
which is precisely the failure this exists to avoid.

## Currency correctness

The invoice's currency must be the Company's own. A rate configured in a
currency the company does not keep would otherwise raise an invoice that
fails -- or worse, on a site with a mismatched default, one that silently
misprices (the INR/USD incident recorded in the handoff). Refused here, at
the moment the invoice would be raised, naming both currencies.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from brchms.brchms_credits import billing, terms

INVOICE_DOCTYPE = "Sales Invoice"

# One item per channel. The channel is the buyer's choice; WHICH ITEM represents it
# is not, which is why this mapping lives here and not on the form.
CHANNEL_ITEMS = {
	"sms": "REST-SMS-CREDIT",
	"email": "REST-EMAIL-CREDIT",
}


def minor_amount_for(units, unit_price_micros, currency):
	"""
	The exact minor-unit cost of `units` at `unit_price_micros`, half-up.

	All-integer arithmetic: a float division would silently round a
	2^53-scale value, and this figure prices an invoice.
	"""
	units = int(units or 0)
	micros = int(unit_price_micros or 0)
	if units <= 0 or micros <= 0:
		return 0

	fraction = currency_fraction_units(currency)
	numerator = units * micros * fraction
	denominator = 10 ** 6
	# Half-up for a non-negative rational: floor((2n + d) / 2d).
	return (2 * numerator + denominator) // (2 * denominator)


def require_consistent(units, unit_price_micros, currency, amount_minor):
	"""
	Refuse a `(units, amount)` pair that disagrees with its own price.

	Refusal, not repair: this runs at the last moment before money is asked
	for, and quietly "fixing" the amount here would bill the buyer for
	something they did not see on the page they typed into. The message names
	the correct amount so the buyer can be told what to do.
	"""
	micros = int(unit_price_micros or 0)
	if micros <= 0:
		frappe.throw(
			_(
				"This request carries no price, so it cannot be billed. It was recorded, and "
				"nothing was charged -- quote it again so it carries the current rate."
			),
			frappe.ValidationError,
		)

	expected = minor_amount_for(units, micros, currency)
	received = int(amount_minor or 0)
	if expected != received:
		frappe.throw(
			_(
				"The amount on this request ({0}) does not match its own price: {1} credits at "
				"the quoted rate come to {2}. Go back and let the form recompute the amount."
			).format(
				format_minor(received, currency),
				cint(units),
				format_minor(expected, currency),
			),
			frappe.ValidationError,
		)
	return expected


def quote(channel=None, units=None, currency=None, company=None, discount_basis_points=None):
	"""
	What a buyer pays for a number of credits, derived -- never typed.

	Called by the purchase form's client script as the buyer types units, so
	the amount field is a computed figure the buyer cannot disagree with. The
	price is the CURRENT active rate for the channel, in the Company's own
	currency, and the amount is `minor_amount_for` -- the same rounding the
	invoice will enforce, so what the buyer sees is what they are billed.

	`discount_basis_points` is the ORGANIZATION's discount, resolved from the
	registry by the caller (`proposals.quote` for the public form). It is a
	parameter here rather than a lookup because `invoicing` must stay usable
	without an organization context (the invoice path receives the proposal's
	snapshot). The effective price and the amount are DERIVED from
	`(list, basis_points)` with the same half-up rule the app applies, so the
	amount on the page is the amount the invoice will bill.

	The answer carries the terms that decide whether the purchase may happen:
	`below_minimum` is data, not a throw, because the public form shows a
	sentence and the checkout is what refuses.
	"""
	channel = cstr(channel).strip().lower()
	if channel not in CHANNEL_ITEMS:
		frappe.throw(
			_("Choose which kind of credit you are buying: sms or email."), frappe.ValidationError
		)

	units = cint(units)
	if units <= 0:
		frappe.throw(
			_("Enter how many credits you are buying - a whole number greater than zero."),
			frappe.ValidationError,
		)

	company = billing.resolve_selling_company(company=company, currency=currency)
	currency = cstr(currency).strip().upper() or company_default_currency(company)

	rate = _current_rate(channel, currency, company)
	list_micros = int(rate["price_per_unit_micros"])
	basis_points = _basis_points(discount_basis_points)
	effective_micros = terms.effective_unit_price_micros(list_micros, basis_points)

	minimum_minor = terms.minimum_purchase_minor(currency)
	list_amount_minor = minor_amount_for(units, list_micros, currency)
	amount_minor = minor_amount_for(units, effective_micros, currency)

	return {
		"channel": channel,
		"units": units,
		"company": company,
		"currency": currency,
		# The rate card's price, before the organization's discount.
		"list_price_per_unit_micros": list_micros,
		# The price this buyer actually pays. `unit_price_micros` is the name
		# older callers read, and it is the EFFECTIVE (discounted) price.
		"unit_price_micros": int(effective_micros),
		"price_per_unit_micros": int(effective_micros),
		"discount_basis_points": basis_points,
		"discount_percent": round(basis_points / 100, 2),
		# The money, before and after the discount. The proposal snapshots these,
		# and a public form can show the saving without doing any arithmetic of
		# its own (a browser that re-derives money is a second pricing engine).
		"list_amount_minor": list_amount_minor,
		"discount_minor": list_amount_minor - amount_minor,
		"minimum_purchase_minor": minimum_minor,
		"minimum_purchase_amount": terms.minimum_purchase_display(currency, minimum_minor),
		"below_minimum": bool(minimum_minor) and amount_minor < minimum_minor,
		"pricing_error": terms.pricing_error_for(list_micros, basis_points),
		"rate_name": rate.get("rate_name"),
		"amount_minor": amount_minor,
		"amount_major": flt(amount_minor / currency_fraction_units(currency)),
	}


def _basis_points(value):
	"""The caller's discount in basis points, refused rather than rounded."""
	if value in (None, ""):
		return 0
	try:
		basis_points = int(value)
	except (TypeError, ValueError):
		frappe.throw(
			_("Discount must be a whole number of basis points, not '{0}'.").format(
				frappe.utils.escape_html(cstr(value))
			),
			frappe.ValidationError,
		)

	if basis_points < 0 or basis_points > terms.MAX_BASIS_POINTS:
		frappe.throw(
			_(
				"Discount must be between 0% and 99.99%; {0} basis points is not a price that "
				"can be given."
			).format(basis_points),
			frappe.ValidationError,
		)
	return basis_points


def raise_credit_invoice(
	*,
	customer,
	purchase_reference,
	channel,
	units,
	amount_minor,
	currency,
	unit_price_micros=None,
	list_unit_price_micros=None,
	discount_basis_points=None,
	instance_id=None,
	company=None,
):
	"""
	The Sales Invoice for a proposal, submitted so it can be paid.

	Submitted rather than draft because a Payment Request (and therefore a
	gateway page) is only valid against a submitted document, and because a
	draft invoice is not a receivable: nothing about the sale is real until it
	is submitted.

	Bills the group's registered Customer, carries the reference the payment
	will be matched by, and stamps the deployment's instance id so a payment
	settles the right deployment's proposal even when several share this
	ERPNext site.

	`unit_price_micros` is the PROPOSAL'S OWN snapshot when the caller has
	one -- priced once, never re-priced (a documented decision: a rate change
	must not re-price a purchase already quoted, because that could strand a
	payment that genuinely happened). `list_unit_price_micros` and
	`discount_basis_points` are the same snapshot's list price and discount:
	the effective price is RE-DERIVED from them here, and the invoice line is
	billed at that net rate. The consistency backstop therefore checks the
	triple against itself, not against the current rate card.
	"""
	item_code = CHANNEL_ITEMS.get(cstr(channel).strip().lower())
	if not item_code:
		frappe.throw(
			_("This purchase names channel '{0}', which has no credit item to bill.").format(
				frappe.utils.escape_html(cstr(channel))
			),
			frappe.ValidationError,
		)

	if not frappe.db.exists("Item", item_code):
		frappe.throw(
			_(
				"The credit item {0} does not exist, so this purchase cannot be billed. Run "
				"`bench migrate` to install it."
			).format(frappe.utils.escape_html(item_code)),
			frappe.DoesNotExistError,
		)

	company = billing.resolve_selling_company(company=company, currency=currency)
	if not company:
		frappe.throw(_("This site has no Company, so no invoice can be raised."), frappe.DoesNotExistError)

	company_currency = company_default_currency(company)
	currency = cstr(currency).strip().upper() or company_currency
	if currency != company_currency:
		frappe.throw(
			_(
				"This proposal is quoted in {0}, but this site's company keeps its books in {1}. "
				"An invoice in the wrong currency is a silent mispricing, so it is refused rather "
				"than raised. Set the rate card in the company's own currency."
			).format(frappe.utils.escape_html(currency), frappe.utils.escape_html(company_currency)),
			frappe.ValidationError,
		)

	# The invariant holds at the last moment before money is asked for. Both
	# doors derive the pair; this is the backstop for a hand-edited row.
	#
	# The discount is re-derived here rather than trusted: the invoice is the
	# document money is collected against, so a row whose effective price does
	# not come from its own `(list, basis_points)` is refused instead of
	# billed. `require_consistent` then checks the NET amount (what the buyer
	# actually pays) against the effective price.
	list_micros = (
		int(list_unit_price_micros)
		if list_unit_price_micros not in (None, "")
		else int(unit_price_micros or 0)
	)
	basis_points = _basis_points(discount_basis_points)
	effective_micros = terms.effective_unit_price_micros(list_micros, basis_points)

	if basis_points and list_micros <= 0:
		frappe.throw(
			_(
				"This purchase claims a {0}% discount but records no list price to discount from."
			).format(basis_points / 100),
			frappe.ValidationError,
		)

	if unit_price_micros not in (None, "") and int(unit_price_micros) != effective_micros:
		frappe.throw(
			_(
				"The price on this proposal disagrees with its own discount: {0} micros with a "
				"{1}% discount comes to {2} micros, not {3}."
			).format(list_micros, basis_points / 100, effective_micros, int(unit_price_micros)),
			frappe.ValidationError,
		)

	pricing_error = terms.pricing_error_for(list_micros, basis_points)
	if pricing_error:
		frappe.throw(pricing_error, frappe.ValidationError)

	require_consistent(units, effective_micros, currency, amount_minor)

	fraction = currency_fraction_units(currency)

	# The description names the list price and the discount because the invoice
	# line is what a buyer and a reviewer compare against the rate card later.
	# The DISCOUNT IS IN THE RATE (net price), not in ERPNext's discount
	# machinery: `additional_discount_percentage` would make the invoice total
	# disagree with the proposal's snapshot and with the app's own ledger, and
	# pricing rules could silently re-price a credit item. One price, derived
	# once.
	description = _("{0} credits for {1}").format(
		frappe.utils.escape_html(cstr(channel).upper()), cint(units)
	)
	if basis_points:
		description = _("{0} (list price {1} per unit, {2}% discount)").format(
			description,
			format_micros(list_micros, currency),
			(basis_points / 100),
		)

	invoice = frappe.get_doc(
		{
			"doctype": INVOICE_DOCTYPE,
			"company": company,
			"customer": customer,
			"posting_date": frappe.utils.nowdate(),
			"currency": currency,
			# The buyer agreed to THIS amount, so it is what they are billed.
			"purchase_reference": cstr(purchase_reference).strip() or None,
			"channel": cstr(channel).strip().lower(),
			"units": cint(units),
			# Which deployment this invoice bills for -- the settlement hook's
			# cross-instance guard. See settlement.py for why it is needed.
			"instance_id": cstr(instance_id).strip() or None,
			"items": [
				{
					"item_code": item_code,
					"description": description,
					"qty": 1,
					"rate": flt(int(amount_minor) / fraction),
				}
			],
		}
	)
	invoice.flags.ignore_permissions = True
	invoice.insert()
	invoice.submit()
	frappe.db.commit()

	return invoice.name


def payment_url_for(invoice_name, payer_email=None):
	"""
	Where this invoice can be paid, creating the gateway request if needed.

	`make_payment_request` is ERPNext's own builder. Callers have already taken
	the administrator identity -- the buyer is identified by their purchase
	record, and the gateway page they are sent to is the system's choice
	rather than theirs.

	`payer_email` is the buyer's receipt address from the purchase record. It
	MUST be supplied: without it ERPNext falls back to the owner of the invoice,
	which is Administrator, and the gateway rejects the resulting payment page
	with "Invalid email address: Administrator" -- a checkout that cannot be
	paid even though the invoice and request are valid.
	"""
	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	existing = frappe.db.get_value(
		"Payment Request",
		{"reference_doctype": INVOICE_DOCTYPE, "reference_name": invoice_name},
		"name",
	)
	if existing:
		request = frappe.get_doc("Payment Request", existing)
		if payer_email and cstr(request.email_to).strip() != cstr(payer_email).strip():
			request.db_set("email_to", cstr(payer_email).strip())
			frappe.db.commit()
		return request.get_payment_url()

	request = make_payment_request(
		dt=INVOICE_DOCTYPE,
		dn=invoice_name,
		submit_doc=True,
		return_doc=True,
		# Without an explicit recipient ERPNext uses the invoice owner
		# (Administrator), which the gateway rejects as an invalid address.
		recipient_id=cstr(payer_email).strip() or None,
		# The buyer is being redirected to the gateway in the same breath, so the
		# email ERPNext would otherwise send duplicates the page they are already
		# looking at -- and it would make the whole purchase depend on a configured
		# outgoing Email Account, turning a missing mail server into a purchase page
		# that cannot be used at all.
		mute_email=1,
	)

	return request.get_payment_url()


# ── internals ────────────────────────────────────────────────────────────────


def _current_rate(channel, currency, company=None):
	"""The active rate for a channel+company, refused readably when none is configured."""
	from brchms.brchms_credits import api

	rate = api.get_credit_rate(channel=channel, currency=currency, company=company)
	if not rate or rate.get("source") == "unconfigured" or cint(rate.get("price_per_unit_micros")) <= 0:
		frappe.throw(
			_(
				"No {0} rate is configured for this site in {1}, so credits cannot be priced yet. "
				"An administrator sets the rate card first."
			).format(frappe.utils.escape_html(cstr(channel).upper()), frappe.utils.escape_html(currency)),
			frappe.ValidationError,
		)
	return rate


def site_company():
	"""The company that bills, or None. Defaults first, as ERPNext's own forms do."""
	return frappe.defaults.get_defaults().company or frappe.db.get_value("Company", {}, "name")


def company_default_currency(company=None):
	"""The currency a selling company keeps its books in."""
	company = company or billing.resolve_selling_company()
	if not company:
		frappe.throw(_("This site has no Company, so no price can be quoted."), frappe.ValidationError)
	return frappe.db.get_value("Company", company, "default_currency") or "GHS"


def currency_fraction_units(currency):
	"""
	How many minor units make one major unit.

	Read from `Currency.fraction_units` rather than assumed to be 100: most
	currencies are 100, but one like MGA is 5, and a hardcoded factor is wrong
	by a clean multiple -- the kind of error that looks like a real number and
	survives review. One hundred is the documented fallback for a currency the
	doctype has no detail on.
	"""
	fraction = frappe.db.get_value("Currency", cstr(currency).strip().upper(), "fraction_units")
	return cint(fraction) or 100


def format_minor(amount_minor, currency):
	"""A minor-unit amount as a buyer reads it, in the currency's own precision."""
	fraction = currency_fraction_units(currency)
	amount = int(amount_minor)
	if fraction == 1:
		return "{0}".format(amount)
	decimals = len(str(fraction - 1))
	return "{0:.{1}f}".format(amount / fraction, decimals)


def format_micros(micros, currency=None):
	"""
	A per-unit price in micros as a person reads it: 45000 -> "GHS 0.045".

	Used on the invoice line to name the rate card's list price beside the
	discount. Kept here (rather than imported from the rate controller) so the
	invoice module has no dependency on a DocType module just to format a
	number; the rule is the same, and the price that is BILLED is still the
	integer in `rate`, never this string.
	"""
	micros = int(micros or 0)
	whole, fraction = divmod(abs(micros), 1_000_000)
	digits = "{0:06d}".format(fraction).rstrip("0")
	if len(digits) < 2:
		digits = digits.ljust(2, "0")
	value = "{0}.{1}".format(whole, digits)
	if micros < 0:
		value = "-" + value
	currency = cstr(currency).strip().upper()
	return "{0} {1}".format(currency, value) if currency else value
