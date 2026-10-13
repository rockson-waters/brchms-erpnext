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

## Idempotency

`create_proposal` is keyed on `(instance_id, purchase_reference)`. A retry after a
lost response returns the existing proposal rather than raising a second one, which
matters because a duplicate proposal is a request for money nobody asked for. The
reference is the lookup key because it is the same key the operator searches by
when the payment arrives, so there is exactly one identity in play instead of two
that can drift.
"""

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
	"purchase_reference",
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
	payload = {
		"instance_id": cstr(instance_id).strip() or None,
		"purchase_reference": cstr(purchase_reference).strip() or None,
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

	existing = _find_proposal(payload["instance_id"], payload["purchase_reference"])
	if existing:
		return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, existing), created=False)

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
		# Lost a race with a concurrent retry. The winner's proposal is the answer;
		# raising a second one would be a request for money nobody asked for.
		winner = _find_proposal(payload["instance_id"], payload["purchase_reference"])
		if winner:
			return _proposal_response(frappe.get_doc(PROPOSAL_DOCTYPE, winner), created=False)
		raise

	return _proposal_response(doc, created=True)


@frappe.whitelist()
def get_proposal(instance_id=None, purchase_reference=None, proposal=None):
	"""
	One proposal, by reference (the operator's key) or by name.

	Read-only, and answered as data: `settlement_state` tells the caller whether the
	money has arrived, which is the only question the app actually has. A missing
	proposal is reported as `None` rather than raised, because "not yet settled" and
	"never existed" are different answers and the caller can act on both.
	"""
	instance_id = cstr(instance_id).strip()
	purchase_reference = cstr(purchase_reference).strip()
	proposal = cstr(proposal).strip()

	if proposal:
		if not frappe.db.exists(PROPOSAL_DOCTYPE, proposal):
			return None
		name = proposal
	else:
		name = _find_proposal(instance_id, purchase_reference)

	if not name:
		return None

	doc = frappe.get_doc(PROPOSAL_DOCTYPE, name)
	if instance_id and doc.instance_id != instance_id:
		# A reference is unique per deployment, not globally. Answering for another
		# deployment's proposal would let one tenant read another's payment details.
		return None

	return _proposal_response(doc)


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
def settlement_state(instance_id=None, purchase_reference=None):
	"""
	Has the money arrived for this reference?

	Separate from `get_proposal` because this is the question the app asks on the
	hot path, and it deserves one word rather than a payload: `draft` (requested,
	unpaid), `settled` (claimable), `cancelled`, or `unknown` (no such proposal).
	"""
	instance_id = cstr(instance_id).strip()
	purchase_reference = cstr(purchase_reference).strip()

	name = _find_proposal(instance_id, purchase_reference)
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


def _find_proposal(instance_id, purchase_reference):
	"""The proposal for one deployment's reference, or None."""
	if not instance_id or not purchase_reference:
		return None

	return frappe.db.get_value(
		PROPOSAL_DOCTYPE,
		{"instance_id": instance_id, "purchase_reference": purchase_reference},
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
