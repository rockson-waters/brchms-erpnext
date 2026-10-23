# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The proposal API: what a Rest deployment may do, and nothing more.

## The shape of the boundary

A deployment can RAISE a proposal. It cannot settle one -- that requires the
`submit` permission, which the `Rest Integration` role deliberately does not hold,
so the restriction is enforced by Frappe's permission layer rather than by which
whitelisted methods happen to exist. A leaked integration token can therefore
create requests and read its own records, and cannot make a single credit real.

## Three identities, and why they are separate

Conflating them is what made the first version unable to serve cash at all.

  * **The claim code** is the proposal's own name (`PROP-00981`). Internal, never
    shown to customers, and never typed by them. It is what a gateway echoes back in
    its transaction metadata and what staff read off a receipt.
  * **The reference** (`purchase_reference`) is what a payment is matched against, and
    it is ALWAYS present and UNIQUE PER DEPLOYMENT. The deployment sends the payer's own
    if there is one (an invoice number, a bank narration); when there is none -- a cash
    customer, or a gateway transaction that has not happened yet -- the app MINTS a
    short readable one. Minting rather than asking is the whole point: the payer is
    handed a reference instead of being asked to invent one, which people do badly and
    then misremember.
  * **The payment reference** (`payment_reference`) is what the MONEY is identified
    by: the bank's transaction reference, the gateway's id, or the receipt number
    issued at the desk. It exists only after payment, so it cannot be asked for
    before one.

## Idempotency

Two keys, because they answer different questions and both are needed:

  * `(instance_id, request_key)` -- the deployment's own purchase id, so a retry after
    a lost response returns the SAME proposal rather than raising a second one;
  * `(instance_id, purchase_reference)` -- a database uniqueness constraint, so the
    operator's lookup can never match two proposals. Ambiguity is impossible rather
    than reported, which is stronger: a warning depends on somebody reading it.

A duplicate proposal is a request for money nobody asked for, sitting in the
operator's list waiting to be paid twice. Both keys exist to make that unreachable.
"""

import secrets
from urllib.parse import quote as url_quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, now_datetime

from brchms.brchms_credits import billing, invoicing, registry, terms

PROPOSAL_DOCTYPE = "Rest Credit Proposal"
INSTANCE_DOCTYPE = "Rest Instance"

# How long an unpaid proposal stays claimable.
#
# Long enough for a bank transfer to clear and short enough that the operator's
# pending list stays meaningful. Configurable per deployment, because a front desk
# taking cash and a treasurer waiting on a transfer want genuinely different
# windows -- and the deadline is stored ON the proposal, so both sides always agree
# on which one applied when it was raised.
DEFAULT_EXPIRY_DAYS = 30

REQUIRED_PROPOSAL_FIELDS = (
	"instance_id",
	"request_key",
	"organization_id",
	"funding_group_id",
	"channel",
	"units",
	"amount_minor",
	"currency",
)


@frappe.whitelist()
def create_proposal(
	instance_id=None,
	request_key=None,
	purchase_reference=None,
	organization_id=None,
	organization_name=None,
	funding_group_id=None,
	group_name=None,
	channel=None,
	units=None,
	amount_minor=None,
	currency=None,
	unit_price_micros=None,
	instance_name=None,
	buyer_email=None,
	notes=None,
	country_code=None,
	list_unit_price_micros=None,
	discount_basis_points=None,
	rate_name=None,
):
	"""
	Raise a request to buy credits, or return the one already raised for this reference.

	DRAFT, always. There is no argument that makes this submit -- only a holder of
	the `submit` permission can do that, and the app's role does not have it.

	Also raises the INVOICE and, when a gateway is configured, the payment page
	(see `ensure_page`). A proposal with no way to be paid is a request for money
	the customer cannot act on, which is the gap this closes: both doors now
	hand the buyer somewhere to pay. An infrastructure failure there (no
	gateway account, no mail-configured anything) is LOGGED and leaves the
	proposal payable-later -- `settlement_state` and `checkout` both heal it
	-- while a DATA failure (an inconsistent price, a currency this site
	cannot bill) is refused loudly here, before anything exists to reconcile.
	"""
	request_key = cstr(request_key).strip() or None
	country_code = cstr(country_code).strip().upper() or None

	payload = {
		"instance_id": cstr(instance_id).strip() or None,
		"purchase_reference": _cap(cstr(purchase_reference).strip()) or None,
		"organization_id": cstr(organization_id).strip() or None,
		"organization_name": _cap(cstr(organization_name).strip()) or None,
		"funding_group_id": cstr(funding_group_id).strip() or None,
		"group_name": _cap(cstr(group_name).strip()) or None,
		"channel": cstr(channel).strip().lower() or None,
		"units": units,
		"amount_minor": amount_minor,
		"currency": cstr(currency).strip().upper() or None,
		"unit_price_micros": unit_price_micros,
		# The price snapshot: what the rate card said, what the organization's
		# discount was, and the effective price between them. Normalised below.
		"list_unit_price_micros": list_unit_price_micros,
		"discount_basis_points": discount_basis_points,
		"discount_percent": None,
		"list_amount_minor": None,
		"discount_minor": None,
		"rate_name": _cap(cstr(rate_name).strip(), 140) or None,
		"buyer_email": _cap(cstr(buyer_email).strip(), 140) or None,
		"notes": _cap(cstr(notes).strip(), 2000) or None,
		# The deployment's own purchase id. Not a user-visible reference: it is the
		# idempotency key, and it must be stable across the app's retries.
		"request_key": request_key,
		# Filled from the billing-region mapping below, never from a caller's
		# choice of company.
		"company": None,
		"billing_country": None,
	}

	_missing = [f for f in REQUIRED_PROPOSAL_FIELDS if payload.get(f) in (None, "")]
	if _missing:
		frappe.throw(
			_("Missing required fields: {0}.").format(", ".join(sorted(_missing))),
			frappe.MandatoryError,
		)

	for field in ("units", "amount_minor", "unit_price_micros"):
		payload[field] = _as_integer(payload[field], field)

	# The deployment must exist before anything is raised against it, and this is
	# also where its display name and activity come from.
	_touch_instance(payload["instance_id"], display_name=instance_name)

	# ── The idempotent retry, BEFORE any re-pricing or limit ────────────────
	#
	# A retry after a lost response arrives with the same request key, possibly
	# after an operator changed the rate, the organization's discount or the
	# minimum. It must answer the proposal that already exists: refusing it
	# because a term moved would turn a successful (but unacknowledged) request
	# into a purchase nobody can pay. Only a genuinely NEW request is priced and
	# checked against the terms in force now.
	existing = _find_proposal(payload["instance_id"], request_key)
	if existing:
		doc = frappe.get_doc(PROPOSAL_DOCTYPE, existing)
		# A proposal raised before its invoice could be built (or by an older
		# version) is healed here rather than left permanently unpayable.
		_ensure_page_quietly(doc)
		return _proposal_response(doc, created=False)

	# ── The price snapshot: list price, discount, effective price ───────────
	#
	# The app quotes from `get_credit_rate` and sends back the snapshot it showed
	# the buyer. The discount is NOT accepted as a free-standing number: the
	# registry is the authority, so a caller-supplied discount must name the same
	# discount the organization actually has. That is what stops a tenant
	# application -- or a crafted request -- from granting itself a cheaper
	# price, while still letting the purchase prove it priced what the page
	# displayed.
	#
	# A caller that sends no snapshot at all (an older app client, or a
	# hand-raised proposal) gets the legacy behaviour: its own unit price is the
	# list price and there is no discount. That can only ever charge the list
	# price, never less, so the permissive path is the safe direction.
	list_supplied = list_unit_price_micros not in (None, "")
	discount_supplied = discount_basis_points not in (None, "")

	if list_supplied:
		payload["list_unit_price_micros"] = _as_integer(
			list_unit_price_micros, "list_unit_price_micros"
		)

	if discount_supplied:
		payload["discount_basis_points"] = _as_integer(
			discount_basis_points, "discount_basis_points"
		)
	else:
		payload["discount_basis_points"] = 0

	if payload["discount_basis_points"] > terms.MAX_BASIS_POINTS:
		frappe.throw(
			_(
				"Discount must be between 0% and 99.99%; {0} basis points is not a price that "
				"can be charged."
			).format(payload["discount_basis_points"]),
			frappe.ValidationError,
		)

	if not list_supplied:
		if payload["discount_basis_points"]:
			frappe.throw(
				_(
					"This request claims a {0}% discount but names no list price to discount "
					"from."
				).format(payload["discount_basis_points"] / 100),
				frappe.ValidationError,
			)
		payload["list_unit_price_micros"] = payload["unit_price_micros"]

	list_micros = payload["list_unit_price_micros"]
	basis_points = payload["discount_basis_points"]
	effective_micros = terms.effective_unit_price_micros(list_micros, basis_points)

	if effective_micros != payload["unit_price_micros"]:
		frappe.throw(
			_(
				"The price on this request disagrees with its own discount: {0} micros with a "
				"{1}% discount comes to {2} micros, not {3}. Re-quote the purchase."
			).format(
				list_micros,
				basis_points / 100,
				effective_micros,
				payload["unit_price_micros"],
			),
			frappe.ValidationError,
		)

	pricing_error = terms.pricing_error_for(list_micros, basis_points)
	if pricing_error:
		frappe.throw(pricing_error, frappe.ValidationError)

	if list_supplied or discount_supplied:
		configured_bp = terms.discount_basis_points(
			payload["organization_id"], payload["instance_id"]
		)
		if basis_points != configured_bp:
			# Refused rather than silently re-priced in either direction: the buyer
			# was shown one price and the organization's term is another. A purchase
			# that quietly charged the new discount could over-refund; one that
			# quietly charged the old price would bill against a term that no longer
			# exists.
			frappe.throw(
				_(
					"The price of this purchase changed while it was being raised. Refresh the "
					"quote and try again."
				),
				frappe.ValidationError,
			)

	payload["discount_percent"] = round(basis_points / 100, 2)
	payload["list_amount_minor"] = invoicing.minor_amount_for(
		payload["units"], list_micros, payload["currency"]
	)
	discount_minor = payload["list_amount_minor"] - payload["amount_minor"]
	if discount_minor < 0:
		frappe.throw(
			_(
				"The amount on this request ({0}) is greater than its own list amount ({1}); a "
				"discount cannot increase what the buyer pays."
			).format(payload["amount_minor"], payload["list_amount_minor"]),
			frappe.ValidationError,
		)
	payload["discount_minor"] = discount_minor

	if payload["channel"] not in ("sms", "email"):
		frappe.throw(
			_("Channel must be 'sms' or 'email', not '{0}'.").format(
				frappe.utils.escape_html(cstr(payload["channel"]))
			)
		)

	# ── The two data refusals, before anything exists to reconcile ──────────
	#
	# 1. The price triple must agree with itself at the currency's minor
	#    precision. Both doors DERIVE the pair; this catches a hand-edited or
	#    buggy caller before an invoice exists, because an inconsistent order
	#    is a request for credits nobody priced.
	invoicing.require_consistent(
		payload["units"], payload["unit_price_micros"], payload["currency"], payload["amount_minor"]
	)

	# 2. Resolve the SELLING COMPANY from the organization's billing country.
	#    The caller may not choose a company or a currency independently: a
	#    cheaper company would otherwise be one request parameter away. A
	#    single-company site keeps its legacy implicit default; a multi-company
	#    site without a mapped country is refused.
	registration = registry.ensure_registration(
		instance_id=payload["instance_id"],
		organization_id=payload["organization_id"],
		organization_name=payload["organization_name"],
		country_code=country_code,
		currency=payload["currency"],
		require_billing=False,
	)

	effective_country = cstr(registration.billing_country).strip() or country_code
	if effective_country:
		context = billing.resolve_billing_context(
			effective_country,
			currency=payload["currency"],
			instance_id=payload["instance_id"],
			organization_id=payload["organization_id"],
		)
		payload["company"] = context["company"]
		payload["billing_country"] = context["country"]
	else:
		payload["company"] = billing.resolve_selling_company(currency=payload["currency"])

	company_currency = cstr(frappe.db.get_value("Company", payload["company"], "default_currency")).upper()
	if cstr(payload["currency"]).upper() != company_currency:
		frappe.throw(
			_(
				"This request is quoted in {0}, but {1} keeps its books in {2}. An invoice in "
				"the wrong currency is a silent mispricing, so it is refused rather than raised."
			).format(
				frappe.utils.escape_html(cstr(payload["currency"])),
				frappe.utils.escape_html(payload["company"]),
				frappe.utils.escape_html(company_currency),
			),
			frappe.ValidationError,
		)

	# ── The site's minimum purchase ─────────────────────────────────────────
	#
	# Authoritative here, at the API that raises the request: the quote and the
	# public form show the floor, the checkout refuses below it, and this is the
	# gate a crafted or app-driven request cannot step around. It compares the
	# NET amount (what the buyer pays after any discount), because that is what
	# the payment floor is about. Refused before any proposal/invoice exists.
	minimum_minor = terms.minimum_purchase_minor(payload["currency"])
	if minimum_minor and payload["amount_minor"] < minimum_minor:
		frappe.throw(
			_(
				"The minimum credit purchase is {0}. This purchase comes to {1}; add more credits "
				"and try again."
			).format(
				terms.minimum_purchase_display(payload["currency"], minimum_minor),
				invoicing.format_minor(payload["amount_minor"], payload["currency"]),
			),
			frappe.ValidationError,
		)

	# The reference is always present, and never invented by the payer.
	#
	# A cash customer has nothing to quote and should not be asked to make something
	# up; a gateway transaction has no reference until it completes. So when the
	# deployment sends none, one is MINTED and handed over -- which is also what makes
	# the operator's lookup unambiguous, because the reference is unique.
	supplied_reference = payload.get("purchase_reference")
	if not supplied_reference:
		return _insert_with_minted_reference(payload, request_key)

	doc = _insert_proposal(payload, request_key)
	if doc is None:
		# The reference is taken by an existing proposal for this deployment. An
		# identical retry lands here, and so does a genuine collision on a
		# customer-supplied reference -- both resolve to the same answer.
		winner = _find_by_reference(payload["instance_id"], supplied_reference)
		if winner:
			doc = frappe.get_doc(PROPOSAL_DOCTYPE, winner)
			_ensure_page_quietly(doc)
			return _proposal_response(doc, created=False)
		frappe.throw(
			_(
				"Reference '{0}' is already used by another proposal for this deployment. Use a "
				"different reference, or settle the existing proposal."
			).format(frappe.utils.escape_html(cstr(supplied_reference))),
			frappe.ValidationError,
		)

	_ensure_page_quietly(doc)
	return _proposal_response(doc, created=True)


# ── the invoice and the payment page ─────────────────────────────────────────


def ensure_page(doc):
	"""
	The invoice and payment page for a proposal, raised if absent.

	Idempotent: a proposal that already has its invoice answers from what is
	stored, and re-asking for the payment page returns the SAME page rather
	than minting a second one for the same money.

	RAISES on failure -- callers decide what that means. `checkout` lets it
	reach the buyer (they must know there is nowhere to pay); proposal
	creation and the state reads catch it (see `_ensure_page_quietly`) so an
	unconfigured gateway leaves the request recorded and healable rather than
	unraised.

	Runs as Administrator for the same reason `checkout` does: creating an
	invoice is accounting work, ERPNext's `account_perm_check` will not do it
	for the caller this method arrived as, and the deployment -- not the buyer
	-- is the party doing the billing.
	"""
	if doc.invoice:
		return {
			"invoice": doc.invoice,
			"payment_url": invoicing.payment_url_for(doc.invoice, payer_email=doc.buyer_email),
		}

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		customer = registry.ensure_group(
			instance_id=doc.instance_id,
			organization_id=doc.organization_id,
			funding_group_id=doc.funding_group_id,
			group_name=doc.group_name,
			organization_name=doc.organization_name,
			country_code=doc.billing_country,
			currency=doc.currency,
			require_billing=bool(cstr(doc.billing_country).strip()),
		)

		invoice_name = invoicing.raise_credit_invoice(
			customer=customer,
			purchase_reference=doc.purchase_reference,
			channel=doc.channel,
			units=doc.units,
			amount_minor=doc.amount_minor,
			currency=doc.currency,
			unit_price_micros=doc.unit_price_micros,
			list_unit_price_micros=doc.list_unit_price_micros or doc.unit_price_micros,
			discount_basis_points=doc.discount_basis_points or 0,
			instance_id=doc.instance_id,
			company=doc.company or _company_for_doc(doc),
		)

		# The invoice is recorded the moment it exists, BEFORE the payment page
		# is asked for. The page needs a configured gateway and can fail on a
		# site that has none; if the link were written after it, a failed page
		# would leave the invoice unrecorded -- and the next attempt would mint
		# a SECOND invoice for the same money.
		doc.db_set({"invoice": invoice_name})
		frappe.db.commit()

		url = invoicing.payment_url_for(invoice_name, payer_email=doc.buyer_email)
		doc.db_set({"payment_url": url})
		frappe.db.commit()

		return {"invoice": invoice_name, "payment_url": url}
	finally:
		frappe.set_user(previous_user)


def _ensure_page_quietly(doc):
	"""
	`ensure_page`, but a failure is recorded rather than raised.

	For proposal creation and the state reads: the request is already a valid
	operational record, and refusing to raise it because a gateway account is
	missing would throw away a request somebody asked for. The proposal stands
	unpayable, the error is on the log for the operator, and the next
	`settlement_state` read or `checkout` retries it.
	"""
	if doc.docstatus != 0:
		return
	try:
		ensure_page(doc)
	except Exception:
		frappe.log_error(
			title="brchms: could not raise the payment page for proposal {0}".format(doc.name),
			message=frappe.get_traceback(),
		)


def _company_for_doc(doc):
	"""The selling company for a proposal, resolved from its stored billing country."""
	country = cstr(doc.billing_country).strip()
	if country:
		return billing.resolve_billing_context(
			country,
			currency=doc.currency,
			instance_id=doc.instance_id,
			organization_id=doc.organization_id,
		)["company"]
	return billing.resolve_selling_company(currency=doc.currency)


@frappe.whitelist(allow_guest=True)
def quote(channel=None, units=None, currency=None, org=None, country_code=None):
	"""
	What a buyer pays for a number of credits, derived -- never typed.

	Thin, and public on purpose: the purchase form's client script calls it as
	the buyer types, so the amount on the page is computed from the same rate
	and the same rounding the invoice will enforce. A buyer cannot submit an
	amount that disagrees with the price, because the amount field is filled
	by this, not by them.

	`org` is the registered organization slug from the purchase link. It is how
	a public buyer is priced from THEIR billing region without ever naming a
	company or currency. A public page must not be able to enumerate another
	region's prices, so an unknown/missing org on a multi-company site is refused
	rather than defaulted.
	"""
	company = None
	row = registry.resolve(org) if cstr(org).strip() else None
	if cstr(org).strip() and not row:
		frappe.throw(
			_("This purchase link's organization is not registered. Ask your church for a fresh link."),
			frappe.ValidationError,
		)
	if row:
		company = cstr(row.billing_company).strip() or None
		if not currency and cstr(row.billing_currency).strip():
			currency = row.billing_currency
		if not company and cstr(row.billing_country).strip():
			company = billing.resolve_billing_context(
				row.billing_country, currency=currency
			)["company"]
	elif cstr(country_code).strip():
		company = billing.resolve_billing_context(country_code, currency=currency)["company"]

	# The organization is the authority for its own discount. It is resolved from
	# the registry row the link names -- never taken from the page -- so a public
	# quote cannot ask for a cheaper price than the church's term.
	discount_basis_points = (
		terms.discount_basis_points(row.organization_id, row.instance_id) if row else 0
	)

	return invoicing.quote(
		channel=channel,
		units=units,
		currency=currency,
		company=company,
		discount_basis_points=discount_basis_points,
	)


@frappe.whitelist()
def get_billing_context(
	instance_id=None,
	organization_id=None,
	organization_name=None,
	country_code=None,
	currency=None,
):
	"""
	The selling company/currency for an organization, or why it has none.

	Read-only and intentionally total: an unmapped country is returned as
	`ready: false` with the missing mapping named, so the app can show a billing
	blocker on the credits page instead of turning the whole page into an error.
	"""
	registration = registry.ensure_registration(
		instance_id=instance_id,
		organization_id=organization_id,
		organization_name=organization_name,
		country_code=country_code,
		currency=currency,
		require_billing=False,
	)

	def _term_fields(effective_currency):
		"""
		The organization's commercial terms, for whichever answer this call gives.

		Included even when billing is not ready: a church with an unmapped country
		still has a discount and a monthly allowance, and hiding them behind the
		billing blocker would tell the operator the wrong thing about what is
		missing.
		"""
		return terms.context_fields(
			instance_id=cstr(registration.instance_id).strip(),
			organization_id=cstr(registration.organization_id).strip(),
			currency=effective_currency,
			display_currency=cstr(registration.billing_currency).strip() or effective_currency,
		)

	effective_country = cstr(registration.billing_country).strip() or cstr(country_code).strip()
	if effective_country:
		ready, context, reason = billing.billing_is_ready(effective_country, currency=currency)
		if ready:
			return {
				"ready": True,
				"company": context["company"],
				"currency": context["currency"],
				"country": context["country"],
				"country_code": context["country_code"],
				"region": context["region"],
				"region_name": context["region_name"],
				"reason": None,
				**_term_fields(context["currency"]),
			}
		return {
			"ready": False,
			"company": None,
			"currency": cstr(currency).strip().upper() or None,
			"country": effective_country,
			"country_code": cstr(country_code).strip().upper() or None,
			"region": None,
			"region_name": None,
			"reason": reason,
			**_term_fields(cstr(currency).strip().upper() or None),
		}

	# Legacy single-company sites may not have a billing country yet. Keep them
	# working; a multi-company site is refused by resolve_selling_company.
	try:
		company = billing.resolve_selling_company(currency=currency)
	except Exception as error:
		return {
			"ready": False,
			"company": None,
			"currency": cstr(currency).strip().upper() or None,
			"country": None,
			"country_code": None,
			"region": None,
			"region_name": None,
			"reason": cstr(getattr(error, "message", None) or str(error)),
			**_term_fields(cstr(currency).strip().upper() or None),
		}

	return {
		"ready": True,
		"company": company,
		"currency": cstr(frappe.db.get_value("Company", company, "default_currency")).upper() or None,
		"country": None,
		"country_code": None,
		"region": None,
		"region_name": None,
		"reason": None,
		**_term_fields(
			cstr(frappe.db.get_value("Company", company, "default_currency")).upper() or currency
		),
	}


@frappe.whitelist()
def list_settled_proposals(instance_id=None, since=None, limit=200):
	"""
	Settled proposals for one deployment, newest settlement first.

	The app's reconcile poller reads this: a settlement here is the only thing
	that makes credits claimable, and the app grants against what this
	returns. Read-only, scoped to the caller's own deployment, and carrying
	every field the app needs to attribute a grant -- including the funding
	group, because a form-born purchase has no app-side record to read the
	group from.
	"""
	instance_id = cstr(instance_id).strip()
	if not instance_id:
		frappe.throw(_("instance_id is required."), frappe.MandatoryError)

	filters = {"instance_id": instance_id, "docstatus": 1}
	if cstr(since).strip():
		filters["confirmed_at"] = (">=", cstr(since).strip())

	rows = frappe.get_all(
		PROPOSAL_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"purchase_reference",
			"request_key",
			"organization_id",
			"organization_name",
			"funding_group_id",
			"group_name",
			"channel",
			"units",
			"amount_minor",
			"currency",
			"company",
			"billing_country",
			"unit_price_micros",
			# The price snapshot travels with the settlement: a purchase BORN ON
			# THE PUBLIC FORM has no app-side record to read the list price, the
			# organization's discount or the rate row from, so the poller that
			# creates that record learns them here. Without them the app would
			# record the net amount with no attributable gross or term.
			"list_unit_price_micros",
			"list_amount_minor",
			"discount_basis_points",
			"discount_percent",
			"discount_minor",
			"rate_name",
			"buyer_email",
			"invoice",
			"payment_url",
			"confirmed_at",
			"settled_by",
		],
		order_by="confirmed_at desc",
		limit_page_length=max(1, min(cint(limit) or 200, 500)),
	)

	return {"proposals": rows, "settled": len(rows)}


def _insert_with_minted_reference(payload, request_key):
	"""
	Insert with a freshly minted reference, retrying if the mint collides.

	The DATABASE decides whether a reference is taken, not a pre-check: a pre-check
	races, and two callers can both see a reference free. The unique index is the
	authority, and the retry is what makes the mint safe.

	Bounded, because an unbounded retry on a genuinely broken constraint would spin.
	"""
	for _attempt in range(MINT_ATTEMPTS):
		candidate = dict(payload)
		candidate["purchase_reference"] = mint_reference()

		doc = _insert_proposal(candidate, request_key)
		if doc is not None:
			_ensure_page_quietly(doc)
			return _proposal_response(doc, created=True)

		# The insert failed. If it failed because the REQUEST KEY now exists, a
		# concurrent retry won and its proposal is the answer.
		winner = _find_proposal(payload["instance_id"], request_key)
		if winner:
			winner_doc = frappe.get_doc(PROPOSAL_DOCTYPE, winner)
			_ensure_page_quietly(winner_doc)
			return _proposal_response(winner_doc, created=False)

	frappe.throw(
		_(
			"Could not allocate a unique reference after {0} attempts. Nothing was raised; "
			"please retry."
		).format(MINT_ATTEMPTS),
		frappe.ValidationError,
	)


def _insert_proposal(payload, request_key):
	"""
	Insert one proposal, or answer None when it lost a race.

	Re-reads the winner rather than letting a raw integrity error escape, because the
	loser of a legitimate retry has SUCCEEDED -- the proposal exists, once -- and
	should be told so rather than shown a database error.
	"""
	doc = frappe.get_doc(
		{
			"doctype": PROPOSAL_DOCTYPE,
			"expires_at": _expiry_for(now_datetime()),
			**payload,
		}
	)

	try:
		doc.flags.ignore_permissions = True
		doc.insert()
	except frappe.UniqueValidationError:
		return None
	except frappe.DuplicateEntryError:
		return None

	return doc


@frappe.whitelist()
def get_proposal(instance_id=None, request_key=None, proposal=None):
	"""
	One proposal, by the deployment's request key, or by its code.

	Read-only, and answered as data: `settlement_state` tells the caller whether the
	money has arrived, which is the only question the app actually has. A missing
	proposal is reported as `None` rather than raised, because "not yet settled" and
	"never existed" are different answers and the caller can act on both.
	"""
	instance_id = cstr(instance_id).strip()
	request_key = cstr(request_key).strip()
	proposal = cstr(proposal).strip()

	if proposal:
		if not frappe.db.exists(PROPOSAL_DOCTYPE, proposal):
			return None
		name = proposal
	else:
		name = _find_proposal(instance_id, request_key)

	if not name:
		return None

	doc = frappe.get_doc(PROPOSAL_DOCTYPE, name)
	if instance_id and doc.instance_id != instance_id:
		# A reference is unique per deployment, not globally. Answering for another
		# deployment's proposal would let one tenant read another's payment details.
		return None

	# A draft with no invoice may be pre-gateway or pre-upgrade; heal it so the
	# answer includes a payment page the buyer can still use. Quietly, because a
	# read must not fail because a gateway is not yet configured.
	_ensure_page_quietly(doc)

	return _proposal_response(doc)


@frappe.whitelist()
def find_pending_proposal(
	instance_id=None,
	purchase_reference=None,
	group_name=None,
	amount_minor=None,
):
	"""
	The one proposal a payment belongs to, for the operator at the desk.

	## Ambiguity is impossible, not reported

	`purchase_reference` is unique per deployment, so a reference identifies exactly
	one proposal. That is stronger than the previous version, which returned every
	candidate and set an `ambiguous` flag: a warning depends on somebody reading it,
	while a unique index does not.

	Where a reference is NOT supplied, the search falls back to group and amount --
	and can still match more than one, because those are not unique. That case is
	refused with a message saying so rather than guessed at: settling one request
	against another's payment is exactly the failure this design exists to prevent.

	Answers `{"proposal": <payload>|None, "reason": ...}`. `None` means nothing
	matched; a refusal means more than one did.
	"""
	instance_id = cstr(instance_id).strip()
	if not instance_id:
		frappe.throw(_("instance_id is required."), frappe.MandatoryError)

	reference = cstr(purchase_reference).strip()

	if reference:
		name = _find_by_reference(instance_id, reference)
		if not name:
			return {"proposal": None, "reason": "no proposal carries that reference"}
		doc = frappe.get_doc(PROPOSAL_DOCTYPE, name)
		if doc.docstatus != 0:
			return {
				"proposal": _proposal_response(doc),
				"reason": "this proposal is already {0}".format(
					"settled" if doc.docstatus == 1 else "cancelled"
				),
			}
		return {"proposal": _proposal_response(doc), "reason": None}

	# No reference. Narrow by what the operator knows, and refuse to guess.
	filters = {"instance_id": instance_id, "docstatus": 0}
	if cstr(group_name).strip():
		filters["group_name"] = cstr(group_name).strip()
	if amount_minor not in (None, ""):
		filters["amount_minor"] = cint(amount_minor)

	matches = frappe.get_all(
		PROPOSAL_DOCTYPE,
		filters=filters,
		fields=["name"],
		order_by="creation desc",
		limit_page_length=2,
	)

	if not matches:
		return {"proposal": None, "reason": "nothing pending matches"}

	if len(matches) > 1:
		frappe.throw(
			_(
				"More than one pending proposal matches, and settling the wrong one would attribute "
				"a payment to another group's request. Narrow it by the payment reference, or by the "
				"exact amount that arrived."
			),
			frappe.ValidationError,
		)

	return {"proposal": _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, matches[0].name)), "reason": None}


@frappe.whitelist()
def list_pending_proposals(instance_id=None, limit=50):
	"""
	Unsettled proposals for one deployment.

	For the app to reconcile what it has asked for against what has arrived -- and
	for an operator to be able to see the same list, which is why it is computed
	rather than stored.
	"""
	instance_id = cstr(instance_id).strip()
	if not instance_id:
		frappe.throw(_("instance_id is required."), frappe.MandatoryError)

	rows = frappe.get_all(
		PROPOSAL_DOCTYPE,
		filters={"instance_id": instance_id, "docstatus": 0},
		fields=["name", "purchase_reference", "channel", "units", "amount_minor", "currency", "expires_at"],
		order_by="creation asc",
		limit_page_length=max(1, min(cint(limit) or 50, 200)),
	)

	now = now_datetime()
	out = []
	for row in rows:
		expired = bool(row.expires_at) and cstr(row.expires_at) < cstr(now)
		out.append({**row, "expired": expired})

	return {"proposals": out, "pending": len(out), "expired": sum(1 for r in out if r["expired"])}


@frappe.whitelist()
def settlement_state(instance_id=None, request_key=None):
	"""
	Has the money arrived for this request?

	Separate from `get_proposal` because this is the question the app asks on the
	hot path, and it deserves one word rather than a payload: `draft`, `settled`,
	`expired`, `cancelled`, or `unknown` (no such proposal).
	"""
	instance_id = cstr(instance_id).strip()
	request_key = cstr(request_key).strip()

	name = _find_proposal(instance_id, request_key)
	if not name:
		return {"state": "unknown", "proposal": None}

	doc = frappe.get_doc(PROPOSAL_DOCTYPE, name)

	if doc.docstatus == 1:
		state = "settled"
	elif doc.docstatus == 2:
		state = "cancelled"
	else:
		# A draft with no invoice is healable: the app's "Check status" is what
		# recovers a payment page for a purchase raised before the gateway was
		# configured. Quietly -- the state must still be answerable.
		_ensure_page_quietly(doc)
		expired = bool(doc.expires_at) and cstr(doc.expires_at) < cstr(now_datetime())
		state = "expired" if expired else "draft"

	return {"state": state, "proposal": _proposal_response(doc)}


@frappe.whitelist()
def settle_proposal(proposal=None, purchase_reference=None, payment_doctype=None,
                    payment_document=None, payment_reference=None, note=None):
	"""
	Settle a proposal against a payment document that exists on the books.

	## Why a REAL document is required, and not free text

	`Rest Credit Proposal._require_evidence_of_payment` states the rule and this
	follows it rather than working around it: submitting is the moment a request
	becomes a claim on real credit, so the thing that vouches for the money must
	exist in the system. The three ways money arrives all end as a document here —
	a gateway writes a Payment Request / Integration Request, and an operator taking
	cash or a bank transfer writes a **Payment Entry** (which is also what settles
	the invoice, so the automatic hook covers that case without this method).

	So an attempt to settle against "Bank Transfer", "Cash" or any other free text
	is refused, and the message says what to do instead.

	## When this is needed at all

	The ordinary path is a submitted Payment Entry, settled automatically by
	`brchms_credits.settlement`. This is for a payment whose document exists but
	whose invoice did not carry a `purchase_reference` — or a settlement the hook
	logged and deliberately did not complete.

	## Who may call it

	Only a user who holds the `submit` permission on the proposal. That check is
	the FIRST thing this method does, before it reads a proposal or a payment
	document: the method submits with `ignore_permissions`, so without this gate
	the whitelisted endpoint would be a way around the permission wall that is
	the entire settlement boundary. The app's `Rest Integration` role does not
	hold `submit`, so a leaked integration token cannot use this escape hatch.
	"""
	if not frappe.has_permission(PROPOSAL_DOCTYPE, "submit"):
		frappe.throw(
			_(
				"Settling a credit proposal needs the submit permission on Rest Credit Proposal. "
				"The settlement boundary exists so a request cannot become claimable without a "
				"person (or a gateway's own handler) filing the payment."
			),
			frappe.PermissionError,
		)

	from brchms.brchms_credits import settlement as settlement_module

	name = cstr(proposal).strip()
	if not name and cstr(purchase_reference).strip():
		name = frappe.db.get_value(
			PROPOSAL_DOCTYPE, {"purchase_reference": cstr(purchase_reference).strip()}, "name"
		) or ""
	if not name:
		frappe.throw(
			_("Name the proposal, or give the purchase reference of one that exists."),
			frappe.DoesNotExistError,
		)

	doctype = cstr(payment_doctype).strip()
	document = cstr(payment_document).strip()
	if not doctype or not document:
		frappe.throw(
			_(
				"Name the payment document that settled this: its type and its name. A settlement "
				"has to point at a record on the books, because that record is the evidence money "
				"arrived. For cash or a bank transfer, enter the Payment Entry first and name it here."
			),
			frappe.MandatoryError,
		)

	if not frappe.db.exists("DocType", doctype):
		frappe.throw(
			_(
				"'{0}' is not a document type in this system. Cash and bank transfers are recorded "
				"as a Payment Entry — create that first, then settle against it."
			).format(frappe.utils.escape_html(doctype)),
			frappe.ValidationError,
		)

	# The real document, loaded — not a stand-in. Its own reference number is what a
	# reviewer will match against a bank statement, so it is read from the document
	# rather than taken on the caller's word.
	payment = frappe.get_doc(doctype, document)
	payment.flags.ignore_permissions = True

	reference = cstr(payment_reference).strip() or cstr(payment.get("reference_no")).strip()

	result = {
		"invoice": None,
		"purchase_reference": None,
		"proposal": name,
		"outcome": None,
		"settled": False,
	}

	settlement_module.settle_proposal_for_payment(
		proposal_name=name,
		payment=payment,
		payment_doctype=doctype,
		payment_document=document,
		payment_reference=reference,
		invoice_name=None,
		result=result,
	)

	if cstr(note).strip() and result.get("settled"):
		frappe.db.set_value(
			PROPOSAL_DOCTYPE, name, "settlement_note", cstr(note).strip(), update_modified=True
		)

	if not result.get("settled"):
		frappe.throw(
			_("The proposal was not settled ({0}).").format(result.get("outcome")),
			frappe.ValidationError,
		)

	return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, name), created=None)


# ── internals ────────────────────────────────────────────────────────────────


def _proposal_response(doc, created=None):
	"""
	The proposal's state, as both sides understand it.

	`claimable` is the single fact that matters and is derived here rather than left
	to the caller: submitted AND not expired. Deriving it in one place is what stops
	the app and a report from disagreeing about the same row.
	"""
	expired = bool(doc.expires_at) and cstr(doc.expires_at) < cstr(now_datetime())

	if doc.docstatus == 1:
		state = "settled"
	elif doc.docstatus == 2:
		state = "cancelled"
	elif expired:
		state = "expired"
	else:
		state = "draft"

	awaiting_payment = state == "draft" and bool(cstr(doc.payment_url).strip())

	response = {
		"proposal": doc.name,
		"purchase_reference": doc.purchase_reference,
		"instance_id": doc.instance_id,
		"organization_id": doc.organization_id,
		"funding_group_id": doc.funding_group_id,
		"channel": doc.channel,
		"units": int(doc.units or 0),
		"amount_minor": int(doc.amount_minor or 0),
		"currency": doc.currency,
		"company": cstr(doc.company) if doc.company else None,
		"billing_country": cstr(doc.billing_country) if doc.billing_country else None,
		"unit_price_micros": int(doc.unit_price_micros or 0),
		# The price snapshot: the rate card's list price, the organization's
		# discount, and the money it took off. The app records the purchase
		# against these, so a later term change cannot rewrite what was charged.
		"list_unit_price_micros": int(doc.list_unit_price_micros or 0),
		"list_amount_minor": int(doc.list_amount_minor or 0),
		"discount_minor": int(doc.discount_minor or 0),
		"discount_basis_points": int(doc.discount_basis_points or 0),
		"discount_percent": float(doc.discount_percent or 0),
		"rate_name": cstr(doc.rate_name) if doc.rate_name else None,
		"state": state,
		# The one fact the app acts on. Submitted, and not past its deadline.
		"claimable": state == "settled",
		"expired": expired,
		# Derived, not stored. A payment link exists and nobody has paid yet -- so the
		# "awaiting payment" state needs no status of its own, and therefore no branch
		# anywhere else.
		"awaiting_payment": awaiting_payment,
		"payment_url": cstr(doc.payment_url) if doc.payment_url else None,
		# The invoice this proposal raised, so both sides can point an operator at
		# the same document when a payment is in question.
		"invoice": cstr(doc.invoice) if doc.invoice else None,
		"request_key": doc.request_key,
		"payment_reference": doc.payment_reference,
		"expires_at": cstr(doc.expires_at) if doc.expires_at else None,
		"settled_at": cstr(doc.confirmed_at) if doc.confirmed_at else None,
		"settled_by": doc.settled_by,
		"quote_variance_minor": int(doc.quote_variance_minor or 0),
		"payment_doctype": doc.payment_doctype,
		"payment_document": doc.payment_document,
		"has_payment_evidence": bool(doc.payment_doctype and doc.payment_document),
		# Where the receipt goes. Optional: a proposal may be raised by the app
		# without one, and the receipt step skips rather than fails in that case.
		"buyer_email": cstr(doc.buyer_email) if doc.buyer_email else None,
	}

	if created is not None:
		response["created"] = created

	return response


def _find_by_reference(instance_id, purchase_reference):
	"""
	The proposal one reference identifies, or None.

	At most one, by the unique index -- which is the point of making the reference
	unique. Before that, this could return several and the operator had to choose.
	"""
	if not instance_id or not purchase_reference:
		return None

	return frappe.db.get_value(
		PROPOSAL_DOCTYPE,
		{"instance_id": instance_id, "purchase_reference": purchase_reference},
		"name",
	)


def _find_proposal(instance_id, request_key):
	"""
	The proposal for one deployment's request key, or None.

	Keyed on the REQUEST KEY rather than the customer's reference, because the
	reference is optional and two purchases may legitimately share one.
	"""
	if not instance_id or not request_key:
		return None

	return frappe.db.get_value(
		PROPOSAL_DOCTYPE,
		{"instance_id": instance_id, "request_key": request_key},
		"name",
	)


def _expiry_for(moment):
	days = cint(frappe.conf.get("rest_credit_proposal_expiry_days")) or DEFAULT_EXPIRY_DAYS
	return add_to_date(moment, days=days)


def _as_integer(value, fieldname):
	"""
	An exact integer, or a refusal.

	Never `flt`: it is an IEEE double and it silently rounded a 2^53-scale value
	during this integration. These are money and units.
	"""
	text = cstr(value).strip()
	try:
		number = int(text)
	except (TypeError, ValueError):
		frappe.throw(
			_("{0} must be a whole number, not '{1}'.").format(fieldname, frappe.utils.escape_html(text))
		)

	if number < 0:
		frappe.throw(_("{0} cannot be negative.").format(fieldname))

	return number


# Readable alphabet: no I, O, 0 or 1, so a reference cannot be misread off a slip or
# mistyped at a desk. Ambiguity between characters is how a reference stops matching.
REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERENCE_LENGTH = 8

# Bounded, because an unbounded retry against a broken constraint would spin.
MINT_ATTEMPTS = 5


def mint_reference():
	"""
	A short, readable, unique-per-deployment reference.

	Handed to the payer rather than requested from them. A cash customer has nothing
	to quote and should not be asked to invent something; a gateway transaction has no
	reference until it completes. Minting closes both gaps with one mechanism, and it
	is what makes the operator's lookup unambiguous.

	Shape `XXXX-XXXX` from an alphabet without I/O/0/1, so it survives being read aloud
	and written on a receipt. Collisions are handled by the caller retrying against the
	database's uniqueness constraint -- the constraint decides, not a pre-check, because
	a pre-check races.
	"""
	return "{0}-{1}".format(
		"".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH // 2)),
		"".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH // 2)),
	)


def _cap(text, limit=200):
	"""Remote input rendered in the Desk. A label is a label."""
	return text[:limit] if text and len(text) > limit else text


def _touch_instance(instance_id, display_name=None):
	"""
	Register the deployment on first contact, and keep its activity current.

	The proposal links to this, so the row must exist first or the Link resolves to
	nothing. `display_name` is only filled while blank or still defaulted, so an
	operator's rename is never overwritten.
	"""
	if not frappe.db.exists(INSTANCE_DOCTYPE, instance_id):
		doc = frappe.get_doc(
			{
				"doctype": INSTANCE_DOCTYPE,
				"instance_id": instance_id,
				"display_name": display_name or instance_id,
				"erpnext_site": frappe.local.site if hasattr(frappe, "local") else None,
				"is_active": 1,
				"first_seen_at": now_datetime(),
				"last_seen_at": now_datetime(),
				"confirmed_count": 0,
			}
		)
		try:
			doc.flags.ignore_permissions = True
			doc.insert()
		except frappe.UniqueValidationError:
			pass

	frappe.db.sql(
		"UPDATE `tab{0}` SET `last_seen_at` = %(now)s WHERE `name` = %(instance)s".format(
			INSTANCE_DOCTYPE
		),
		{"now": now_datetime(), "instance": instance_id},
	)

	if display_name:
		frappe.db.sql(
			"UPDATE `tab{0}` SET `display_name` = %(display_name)s "
			"WHERE `name` = %(instance)s "
			"AND (`display_name` IS NULL OR `display_name` = '' OR `display_name` = `instance_id`)"
			.format(INSTANCE_DOCTYPE),
			{"display_name": display_name, "instance": instance_id},
		)


def after_migrate():
	"""
	Called by `bench migrate` via the hook in `hooks.py`.

	Idempotent, and silent when its work is already done -- a migration runs on every
	deploy and must not fail because its work was already done.
	"""
	created = []
	if ensure_uniqueness_constraint():
		created.append("the per-deployment unique index")
	if drop_global_reference_index():
		created.append("dropped the global purchase_reference unique index")
	if created:
		frappe.logger().info("brchms: purchase_reference indexes: %s", ", ".join(created))


def drop_global_reference_index():
	"""
	Remove the GLOBAL unique index the field's old `unique: 1` flag created.

	This Frappe honours `unique: 1` on a Data field with a single-column unique
	index -- unlike the version the composite index was written for, where the
	flag produced nothing. The flag is gone from the doctype, but a site that
	migrated while it was set still carries the index, and it quietly contradicts
	the design: it refuses the second deployment's identical reference (which the
	composite index exists to allow) with a bare duplicate-key error.

	Idempotent: absent means done.
	"""
	rows = frappe.db.sql(
		"SHOW INDEX FROM `tab{0}` WHERE Key_name = %s AND Non_unique = 0".format(
			PROPOSAL_DOCTYPE
		),
		("purchase_reference",),
		as_dict=True,
	)
	# The composite's leading column is instance_id, so a single-column unique
	# index of this exact name is the rogue one.
	if not rows:
		return False

	try:
		frappe.db.sql(
			"ALTER TABLE `tab{0}` DROP INDEX `purchase_reference`".format(PROPOSAL_DOCTYPE)
		)
	except Exception as error:
		frappe.logger().warning(
			"brchms: could not drop the global purchase_reference unique index: %s", error
		)
		return False

	return True


def ensure_uniqueness_constraint():
	"""
	Put the database constraint that makes ambiguity impossible.

	## Why this is not just `"unique": 1` on the field

	Frappe creates a unique index only for a field used as the document NAME. This
	doctype is named by series (`PROP-00981`) and identifies payments by
	`purchase_reference`, so the field flag alone would have produced no constraint at
	all -- the setting would have looked right, and two proposals could have shared a
	reference exactly as before.

	## Why the key is composite

	Uniqueness is per DEPLOYMENT. Two churches may legitimately both use `INV-001`, and
	a global constraint would refuse the second one for no reason.

	Idempotent, so `bench migrate` can run it every time.
	"""
	table = "tab{0}".format(PROPOSAL_DOCTYPE)
	index_name = "unique_instance_purchase_reference"

	try:
		existing = frappe.db.sql(
			"SHOW INDEX FROM `{0}` WHERE Key_name = %s".format(table), (index_name,)
		)
	except Exception:
		return False

	if existing:
		return False

	# `frappe.db.add_unique` is the right call, not a raw `ALTER TABLE`. Frappe refuses
	# DDL through `db.sql` inside a transaction ("This statement can cause implicit
	# commit"), and the migration IS inside one -- so the first version threw and the
	# index was never created. `add_unique` commits around the statement itself.
	frappe.db.add_unique(
		PROPOSAL_DOCTYPE, ["instance_id", "purchase_reference"], constraint_name=index_name
	)
	return True


PURCHASE_LINK_DOCTYPE = "Rest Credit Purchase Link"


@frappe.whitelist()
def get_purchase_link(
	purchase_reference=None,
	organization_id=None,
	organization_name=None,
	funding_group_id=None,
	funding_group_name=None,
	instance_id=None,
	country_code=None,
	currency=None,
):
	"""
	Where a customer goes to buy credits, and the customer-facing help text.

	The app renders this and does NOTHING else with it: the gateway, the webform and
	the settlement all live here, which is why switching a gateway on changes nothing
	about who vouches for money.

	## Attribution: the app registers, the link carries the slug

	When the app calls with its own organization (and the default -- normally root
	-- funding group), that organization is REGISTERED here and the returned URL
	carries its slug: `...?org=<slug>`. That is the whole identity handoff -- the
	public form's submission is attributed to a registered organization, so a buyer
	who never opens the app still buys credits that land somewhere specific. The
	slug is opaque because a church's name in a URL is both guessable and a
	disclosure, and buys nothing.

	Registration is find-or-create and never fails the link: a link that cannot
	attribute is still renderable, and the reason is on the error log. The
	refusal that DOES exist is on the form's side -- a submission with no slug is
	refused outright by `checkout`, because an unattributed purchase is money on
	the books with nowhere to put the credits it bought.

	## Why the reference is appended here rather than by the app

	One URL cannot say WHICH purchase is being paid. If `reference_param` is set, the
	app is handed a URL that already identifies the purchase -- `?ref=AB12-CD34` --
	so the webform arrives pre-filled and nothing is retyped. Without it the customer
	would have to carry the reference across by hand, and a reference that has to be
	retyped is a reference that stops matching.

	The appending happens HERE so the rule lives with the URL it applies to. An app
	that built the query string itself would have to know the parameter name, and two
	places would then be able to disagree about it.

	Returns `{"enabled": False}` when nothing usable is configured -- which the app
	must treat as "no purchase button", not as an error. A disabled link is a decision,
	not a fault.
	"""
	settings = frappe.get_single(PURCHASE_LINK_DOCTYPE)

	if not settings.is_enabled:
		return {"enabled": False, "reason": "the purchase link is switched off"}

	url = cstr(settings.purchase_url).strip()
	if not url:
		return {"enabled": False, "reason": "no purchase URL is set"}

	if not url.lower().startswith("https://"):
		# Refused rather than rendered: a payment page over plain HTTP can be
		# rewritten in transit, and the customer would pay whoever got there first.
		frappe.throw(
			_(
				"The purchase URL must start with https://. A payment page served over plain HTTP "
				"can be rewritten in transit, and the customer would be paying whoever got there "
				"first."
			),
			frappe.ValidationError,
		)

	# ── Attribution ────────────────────────────────────────────────────────
	#
	# Register the organization the caller names, and carry its slug on the URL.
	# Fail-open on purpose: the link is a convenience on a page whose real job is
	# the ledger, and a registration problem must not take the button away.
	registered = None
	registration_error = None
	if cstr(instance_id).strip() and cstr(organization_id).strip():
		try:
			registered = registry.ensure_registration(
				instance_id=instance_id,
				organization_id=organization_id,
				organization_name=organization_name,
				default_group_id=funding_group_id,
				default_group_name=funding_group_name,
				country_code=country_code,
				currency=currency,
			)
		except Exception as error:
			registration_error = cstr(getattr(error, "message", None) or str(error))
			frappe.log_error(
				title="brchms: could not register the organization behind the purchase link",
				message=frappe.get_traceback(),
			)

	if registration_error:
		# An attributed link MUST have a billing identity. Handing back a plain
		# link would let a buyer reach a form whose quote can never resolve, so the
		# button is withheld and the reason is available for the credits page.
		return {
			"enabled": False,
			"reason": "The organization's purchase link could not be prepared: {0}".format(
				registration_error
			),
			"organization_name": cstr(organization_name).strip() or None,
			"organization_slug": None,
			"organization_registered": False,
			"billing_ready": False,
			"billing_reason": registration_error,
		}

	reference = cstr(purchase_reference).strip()
	param = cstr(settings.reference_param).strip()

	handoff_url = url
	if registered:
		handoff_url = _append_param(handoff_url, "org", registered.link_slug)
	if reference and param:
		handoff_url = _append_param(handoff_url, param, reference)

	billing_snapshot = _purchase_link_billing(registered, country_code, currency)
	if registered and not billing_snapshot["billing_ready"]:
		# A link with nowhere to bill is not a purchase button. Keep the
		# configuration's URL available for diagnosis, but tell the app not to
		# offer it until the region is mapped.
		return {
			"enabled": False,
			"reason": billing_snapshot["billing_reason"],
			**billing_snapshot,
			"organization_name": cstr(registered.organization_name).strip() or None,
			"organization_slug": cstr(registered.link_slug).strip() or None,
			"organization_registered": True,
		}

	return {
		"enabled": True,
		"url": handoff_url,
		"label": cstr(settings.link_label).strip() or "Buy credits",
		"help_text": cstr(settings.help_text).strip() or None,
		**billing_snapshot,
		# True when the URL carries the purchase, so the app can say the reference is
		# already attached rather than asking the customer for it again.
		"identifies_purchase": bool(reference and param),
		"reference": reference or None,
		# The organization this link is attributed to, when the caller registered
		# one. The app renders it ("Buy credits for Main Church") so the buyer
		# knows whose balance they are funding before they type anything.
		"organization_name": cstr(registered.organization_name).strip()
		if registered
		else (cstr(organization_name).strip() or None),
		"organization_slug": cstr(registered.link_slug).strip() if registered else None,
		"organization_registered": bool(registered),
	}


def _purchase_link_billing(registered, country_code=None, currency=None):
	"""
	The selling company/currency for an attributed purchase link, and whether the
	link can currently be paid.

	Legacy single-company sites with no billing country keep working through the
	single-company fallback; a multi-company site with no region is not "ready"
	and the app hides the button rather than quoting the site default's price.
	"""
	empty = {
		"billing_company": None,
		"billing_currency": None,
		"billing_region": None,
		"billing_country": None,
		"billing_ready": False,
		"billing_reason": None,
	}
	if not registered:
		return {**empty, "billing_reason": "the organization was not registered"}

	country = cstr(registered.billing_country).strip() or cstr(country_code).strip()
	requested_currency = cstr(currency).strip().upper() or cstr(registered.billing_currency).strip() or None

	if cstr(registered.billing_company).strip():
		return {
			"billing_company": registered.billing_company,
			"billing_currency": cstr(registered.billing_currency).strip().upper() or None,
			"billing_region": registered.billing_region,
			"billing_country": registered.billing_country,
			"billing_ready": True,
			"billing_reason": None,
		}

	if country:
		ready, context, reason = billing.billing_is_ready(country, currency=requested_currency)
		if ready:
			return {
				"billing_company": context["company"],
				"billing_currency": context["currency"],
				"billing_region": context["region"],
				"billing_country": context["country"],
				"billing_ready": True,
				"billing_reason": None,
			}
		return {**empty, "billing_country": country, "billing_reason": reason}

	try:
		company = billing.resolve_selling_company(currency=requested_currency)
	except Exception as error:
		return {
			**empty,
			"billing_reason": cstr(getattr(error, "message", None) or str(error)),
		}

	return {
		**empty,
		"billing_company": company,
		"billing_currency": cstr(frappe.db.get_value("Company", company, "default_currency")).upper() or None,
		"billing_ready": True,
	}


def _append_param(url, key, value):
	"""One URL, one query parameter, URL-encoded, with the right separator.

	`url_quote` rather than a bare `quote`: this module also defines the
	PRICING method `quote` (the form's live price read), and a bare name would
	resolve to whichever definition came last -- the kind of collision that
	reads as a mystery failure at the call site.
	"""
	separator = "&" if "?" in url else "?"
	return "{0}{1}{2}={3}".format(url, separator, url_quote(key), url_quote(cstr(value), safe=""))
