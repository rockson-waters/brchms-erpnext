# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The funding authority's API.

`POST /api/method/brchms.brchms_credits.api.confirm_funding`

## The property everything rests on

**Replaying the same `event_id` returns the SAME record.** It does not create a
second row and it does not raise.

That is not a nicety. BRCHMS reaches this instance over Cloudflare from a
different network, and the request can succeed here while the response is lost
in transit. The app then has no way to know whether the funding landed, so it
retries -- and if this method created a new row on every call, a single lost
response would leave this instance holding two records for money paid once,
which the app would then refuse as a conflict. The unique field on `event_id`
plus the read-first behaviour below is what makes the retry safe.

The read-first check and the insert are NOT atomic, so two concurrent requests
with the same key can both pass the read. The unique index is what actually
decides: the loser's insert raises `DuplicateEntryError`, and it then re-reads
and returns the winner. Without that fallback a genuine race would surface to the
app as an error rather than as the idempotent success it is.

## Why the response carries the money, not just an id

The app's local ledger needs `units`, `amount_minor`, `currency` and
`unit_price_micros` to write a funding lot, and it refuses an event whose
organization, group or channel does not match the purchase it was confirming.
Every field the app reads is therefore echoed back explicitly, and
`instance_id` is echoed too so a misdirected URL cannot confirm another
deployment's funding.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

DOCTYPE = "BRCHMS Communication Credit Funding"

# Every field the app sends. Kept as a tuple so the "missing field" message can
# name the fields rather than say "invalid request".
REQUEST_FIELDS = (
	"instance_id",
	"event_id",
	"purchase_reference",
	"organization_id",
	"funding_group_id",
	"channel",
	"units",
	"amount_minor",
	"currency",
	"unit_price_micros",
)

# The fields the response must carry for the app to write a funding lot.
REQUIRED_FIELDS = ("instance_id", "event_id", "channel", "units")


@frappe.whitelist()
def confirm_funding(
	instance_id=None,
	event_id=None,
	purchase_reference=None,
	organization_id=None,
	funding_group_id=None,
	channel=None,
	units=None,
	amount_minor=None,
	currency=None,
	unit_price_micros=None,
):
	"""
	Record one confirmed funding event, or return the one already recorded.

	Idempotent on `event_id` -- see the module docstring.
	"""
	payload = {
		"instance_id": instance_id,
		"event_id": event_id,
		"purchase_reference": purchase_reference,
		"organization_id": organization_id,
		"funding_group_id": funding_group_id,
		"channel": channel,
		"units": units,
		"amount_minor": amount_minor,
		"currency": currency,
		"unit_price_micros": unit_price_micros,
	}

	_require_fields(payload, REQUIRED_FIELDS)
	_normalise(payload)

	# ── The idempotent replay ────────────────────────────────────────────────
	existing = frappe.db.get_value(DOCTYPE, {"event_id": payload["event_id"]}, "name")
	if existing:
		return _as_response(frappe.get_doc(DOCTYPE, existing))

	doc = frappe.get_doc({"doctype": DOCTYPE, **payload})
	doc.flags.ignore_permissions = False
	doc.insert()

	return _as_response(doc)


def _require_fields(payload, fields):
	missing = [field for field in fields if payload.get(field) in (None, "")]
	if missing:
		frappe.throw(
			_("Missing required {0}: {1}.").format(
				"field" if len(missing) == 1 else "fields",
				", ".join(sorted(missing)),
			),
			frappe.MandatoryError,
		)


def _normalise(payload):
	"""
	Coerce to the stored types and refuse anything that is not one.

	Integers are converted defensively -- `cint("abc")` is 0, which would be
	silently accepted as "no units" -- so a non-numeric value is rejected by
	name instead. The app sends these as JSON strings because its own storage uses
	`biginteger`; a string "250" is a correct request and a string "two hundred"
	is not.
	"""
	for field in ("units", "amount_minor", "unit_price_micros"):
		value = payload.get(field)
		if value in (None, ""):
			payload[field] = None
			continue
		try:
			payload[field] = int(str(value).strip())
		except (TypeError, ValueError):
			frappe.throw(
				_("{0} must be a whole number (received {1}).").format(
					frappe.unscrub(field), cstr(value)
				),
				frappe.ValidationError,
			)

	for field in (
		"instance_id",
		"event_id",
		"purchase_reference",
		"organization_id",
		"funding_group_id",
		"channel",
		"currency",
	):
		value = payload.get(field)
		payload[field] = cstr(value).strip() if value not in (None, "") else None

	if payload.get("currency"):
		payload["currency"] = payload["currency"].upper()

	if payload.get("channel"):
		payload["channel"] = payload["channel"].strip().lower()


def _as_response(doc):
	"""
	The exact shape BRCHMS parses, wrapped by Frappe in `message`.

	Every value is a STRING except the two that are naturally textual, mirroring
	the app's `biginteger` discipline: its ledger parses units and money as
	BigInt, and a JSON number would silently lose precision above 2^53.
	"""
	return {
		"event_id": doc.event_id,
		"instance_id": doc.instance_id,
		"organization_id": doc.organization_id or "",
		"funding_group_id": doc.funding_group_id or "",
		"channel": doc.channel,
		"units": cstr(cint(doc.units)),
		"amount_minor": cstr(cint(doc.amount_minor)) if doc.amount_minor is not None else "",
		"currency": (doc.currency or "").upper(),
		"unit_price_micros": (
			cstr(cint(doc.unit_price_micros)) if doc.unit_price_micros is not None else ""
		),
		"purchase_reference": doc.purchase_reference or "",
		"confirmed_at": str(doc.confirmed_at or now_datetime()),
	}


@frappe.whitelist()
def get_funding(event_id=None):
	"""
	Read one funding event by its idempotency key.

	Exists so an operator (and the app's live verification harness) can ask "was
	this recorded, and exactly once?" without the API that would create it.
	"""
	if not event_id:
		frappe.throw(_("event_id is required."), frappe.MandatoryError)

	name = frappe.db.get_value(DOCTYPE, {"event_id": event_id}, "name")
	if not name:
		return None

	return _as_response(frappe.get_doc(DOCTYPE, name))


@frappe.whitelist()
def count_funding(event_id=None):
	"""
	How many records exist for one `event_id`.

	Its whole purpose is to be able to assert "exactly one" from outside: a
	replay that returned the right payload but had quietly inserted a second row
	would pass every check except this one. The unique index makes the answer
	impossible to be anything but 0 or 1 -- and this is how that is verified
	rather than assumed.
	"""
	if not event_id:
		frappe.throw(_("event_id is required."), frappe.MandatoryError)

	return frappe.db.count(DOCTYPE, {"event_id": event_id})
