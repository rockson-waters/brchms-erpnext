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
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, now_datetime

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
	notes=None,
):
	"""
	Raise a request to buy credits, or return the one already raised for this reference.

	DRAFT, always. There is no argument that makes this submit -- only a holder of
	the `submit` permission can do that, and the app's role does not have it.
	"""
	request_key = cstr(request_key).strip() or None

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
		"notes": _cap(cstr(notes).strip(), 2000) or None,
		# The deployment's own purchase id. Not a user-visible reference: it is the
		# idempotency key, and it must be stable across the app's retries.
		"request_key": request_key,
	}

	_missing = [f for f in REQUIRED_PROPOSAL_FIELDS if payload.get(f) in (None, "")]
	if _missing:
		frappe.throw(
			_("Missing required fields: {0}.").format(", ".join(sorted(_missing))),
			frappe.MandatoryError,
		)

	for field in ("units", "amount_minor", "unit_price_micros"):
		payload[field] = _as_integer(payload[field], field)

	if payload["channel"] not in ("sms", "email"):
		frappe.throw(
			_("Channel must be 'sms' or 'email', not '{0}'.").format(
				frappe.utils.escape_html(cstr(payload["channel"]))
			)
		)

	# The deployment must exist before anything is raised against it, and this is
	# also where its display name and activity come from.
	_touch_instance(payload["instance_id"], display_name=instance_name)

	existing = _find_proposal(payload["instance_id"], request_key)
	if existing:
		return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, existing), created=False)

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
			return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, winner), created=False)
		frappe.throw(
			_(
				"Reference '{0}' is already used by another proposal for this deployment. Use a "
				"different reference, or settle the existing proposal."
			).format(frappe.utils.escape_html(cstr(supplied_reference))),
			frappe.ValidationError,
		)

	return _proposal_response(doc, created=True)


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
			return _proposal_response(doc, created=True)

		# The insert failed. If it failed because the REQUEST KEY now exists, a
		# concurrent retry won and its proposal is the answer.
		winner = _find_proposal(payload["instance_id"], request_key)
		if winner:
			return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, winner), created=False)

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
		expired = bool(doc.expires_at) and cstr(doc.expires_at) < cstr(now_datetime())
		state = "expired" if expired else "draft"

	return {"state": state, "proposal": _proposal_response(doc)}


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
		"unit_price_micros": int(doc.unit_price_micros or 0),
		"state": state,
		# The one fact the app acts on. Submitted, and not past its deadline.
		"claimable": state == "settled",
		"expired": expired,
		# Derived, not stored. A payment link exists and nobody has paid yet -- so the
		# "awaiting payment" state needs no status of its own, and therefore no branch
		# anywhere else.
		"awaiting_payment": awaiting_payment,
		"payment_url": cstr(doc.payment_url) if doc.payment_url else None,
		"request_key": doc.request_key,
		"payment_reference": doc.payment_reference,
		"expires_at": cstr(doc.expires_at) if doc.expires_at else None,
		"settled_at": cstr(doc.confirmed_at) if doc.confirmed_at else None,
		"settled_by": doc.settled_by,
		"quote_variance_minor": int(doc.quote_variance_minor or 0),
		"payment_doctype": doc.payment_doctype,
		"payment_document": doc.payment_document,
		"has_payment_evidence": bool(doc.payment_doctype and doc.payment_document),
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

	Idempotent, and silent when the index is already there -- a migration runs on every
	deploy and must not fail because its work was already done.
	"""
	if ensure_uniqueness_constraint():
		frappe.logger().info("brchms: added the unique purchase_reference index")


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
def get_purchase_link(purchase_reference=None):
	"""
	Where a customer goes to buy credits, and the customer-facing help text.

	The app renders this and does NOTHING else with it: the gateway, the webform and
	the settlement all live here, which is why switching a gateway on changes nothing
	about who vouches for money.

	## Why the reference is appended here rather than by the app

	One URL cannot say WHICH purchase is being paid. If `reference_param` is set, the
	app is handed a URL that already identifies the purchase -- `?ref=AB12-CD34` -- so
	the webform arrives pre-filled and nothing is retyped. Without it the customer
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

	reference = cstr(purchase_reference).strip()
	param = cstr(settings.reference_param).strip()

	handoff_url = url
	if reference and param:
		separator = "&" if "?" in url else "?"
		handoff_url = "{0}{1}{2}={3}".format(
			url, separator, quote(param), quote(reference, safe="")
		)

	return {
		"enabled": True,
		"url": handoff_url,
		"label": cstr(settings.link_label).strip() or "Buy credits",
		"help_text": cstr(settings.help_text).strip() or None,
		# True when the URL carries the purchase, so the app can say the reference is
		# already attached rather than asking the customer for it again.
		"identifies_purchase": bool(reference and param),
		"reference": reference or None,
	}
