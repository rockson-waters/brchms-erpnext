# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The funding authority's API.

`POST /api/method/brchms.brchms_credits.api.confirm_funding`

## The property everything rests on

**Replaying the same `event_id` returns the SAME record.** It does not create a
second row and it does not raise.

That is not a nicety. Rest reaches this instance over Cloudflare from a
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
from frappe.utils import cint, cstr, now_datetime, nowdate

DOCTYPE = "Rest Communication Credit Funding"

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
	organization_name=None,
	group_name=None,
	instance_name=None,
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
		# Labels, not keys. Optional, because a confirmation must never fail for
		# want of a church name -- the ids are what the money is keyed on.
		"organization_name": organization_name,
		"group_name": group_name,
	}
	# Deliberately NOT in `payload`: it names the sending INSTANCE, not this
	# funding record, so putting it here would fail the doctype's field check.
	instance_display_name = cstr(instance_name).strip() or None

	_require_fields(payload, REQUIRED_FIELDS)
	_normalise(payload)

	# ── The idempotent replay ────────────────────────────────────────────────
	#
	# The fast path: the event is already recorded, so return it unchanged. This
	# is the ordinary replay, and it must not create a second row or raise.
	existing = frappe.db.get_value(DOCTYPE, {"event_id": payload["event_id"]}, "name")
	if existing:
		return _as_response(frappe.get_doc(DOCTYPE, existing))

	return _as_response(_insert_funding(payload, instance_display_name))


def _insert_funding(payload, instance_display_name=None):
	"""
	Insert one funding record, treating a duplicate key as a replay.

	## Why this exists rather than a bare `doc.insert()`

	The read above and the insert below are NOT atomic. Two requests carrying the
	same `event_id` — which is exactly what a retry after a lost response looks
	like — can both miss the read and both reach `insert()`. The unique index is
	what decides that race, and without the handling below the loser is served a
	raw `UniqueValidationError`:

	    frappe.exceptions.UniqueValidationError: ('BRCHMS Communication Credit
	    Funding', 'cpdr95levf', IntegrityError(1062, "Duplicate entry
	    'test:frontend:unit-1' for key 'event_id'"))

	Observed on the live site. The caller would see an error for an operation
	that in fact SUCCEEDED — the funding is recorded, once — and the correct HTTP
	answer to the retry is the existing event, not a 417. Worse, a `mandatory` or
	`validation` failure raised here would leave the client unable to tell "it
	worked, here is the row" from "it failed", on the one call path where that
	distinction decides whether credits are granted twice.

	So the loser re-reads and returns the winner. The unique index remains the
	authority; this only stops it surfacing as a failure.

	## Permissions, and why the insert ignores them

	The integration user has **read-only** access to this doctype, deliberately:
	the `Rest Integration` role carries `read: 1` and `create: 0`, so a leaked
	key cannot add, change or delete rows. Only this method may write one.

	That is why the insert sets `ignore_permissions=True`. Without it Frappe
	refuses with:

	    PermissionError: User integration@breamal.com does not have doctype access
	    via role permission for document Rest Communication Credit Funding

	— measured against the live site, on the first end-to-end call. The
	whitelisted method IS the authorisation boundary, and the roles stay read-only,
	so this widens nothing. It is the opposite choice from granting `create: 1` to
	a credential that lives in a `.env` file on a different host.
	"""
	# The whitelisted method is the authorisation boundary; see the module
	# docstring. The integration role stays read-only on the doctype itself.
	# Register (or refresh) the sending deployment BEFORE the row lands, so the
	# Link on this record always resolves. Doing it after would leave a funding
	# record pointing at nothing if the process died in between.
	payload["instance_id"] = _touch_instance(
		payload.get("instance_id"),
		display_name=instance_display_name,
		erpnext_site=frappe.local.site if hasattr(frappe, "local") else None,
	)

	doc = frappe.get_doc({"doctype": DOCTYPE, **payload})
	doc.flags.ignore_permissions = True

	try:
		doc.insert()
	except (frappe.UniqueValidationError, frappe.exceptions.DuplicateEntryError):
		# Lost the race: another request inserted this `event_id` first. Return
		# theirs — it is the same funding event, and the read above proved the
		# key is what identifies it.
		winner = frappe.db.get_value(DOCTYPE, {"event_id": payload["event_id"]}, "name")
		if not winner:
			# The key is unique and the insert was rejected, so a row must exist.
			# If it does not, something other than our index refused the insert
			# and swallowing that would be a lie.
			raise
		return frappe.get_doc(DOCTYPE, winner)

	return doc


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
		"organization_name",
		"group_name",
	):
		value = payload.get(field)
		text = cstr(value).strip() if value not in (None, "") else None
		# Capped, because this is remote input rendered in the Desk. A label is a
		# label; an essay here would be a rendering problem, not a payment.
		if text and len(text) > 200:
			text = text[:200]
		payload[field] = text

	if payload.get("currency"):
		payload["currency"] = payload["currency"].upper()

	if payload.get("channel"):
		payload["channel"] = payload["channel"].strip().lower()


def _as_response(doc):
	"""
	The exact shape Rest parses, wrapped by Frappe in `message`.

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


# The only event ids this API will ever delete. `forget_funding` refuses
# anything else, so a caller cannot use it to remove real funding.
TEST_EVENT_PREFIX = "erpnext-live-"


@frappe.whitelist()
def forget_funding(prefix=TEST_EVENT_PREFIX):
	"""
	Delete funding records whose `event_id` starts with `prefix`.

	## Why this exists rather than the caller deleting rows

	The integration user is deliberately READ-ONLY on this doctype (`read: 1`,
	`create: 0`, `write: 0`, `delete: 0`), and only this method writes, via
	`ignore_permissions`. So a caller cannot delete its own probe records — a
	`DELETE /api/resource/...` returns HTTP 403.

	That was discovered the hard way: the live verification harness *reported*
	cleanup on every run while the deletes were being refused, and probe rows
	accumulated until a later run failed on a duplicate `event_id`. A cleanup
	path that lies about succeeding is worse than no cleanup.

	## The narrowness is the point

	This is the ONE destructive operation the app exposes, and it is deliberately
	handicapped:

	  * it deletes only rows whose `event_id` starts with `erpnext-live-` — the
	    namespace the Rest verification harness mints for itself — so it cannot
	    touch a real funding record even if called with a hostile prefix
	    (`prefix` is accepted for the caller's convenience but must still contain
	    the guard below);
	  * it returns what it actually deleted, so a caller can report the truth
	    rather than assuming;
	  * it refuses an empty or too-short prefix outright, because `LIKE '%'` would
	    be every row in the table.
	"""
	prefix = (prefix or "").strip()

	if len(prefix) < len(TEST_EVENT_PREFIX) or not prefix.startswith(TEST_EVENT_PREFIX):
		frappe.throw(
			_(
				"This method only removes {0}* records; it is the Rest verification harness's "
				"namespace and refusing anything else is what stops it being used on real funding. "
				"Received prefix {1}."
			).format(TEST_EVENT_PREFIX, prefix or "(empty)")
		)

	names = frappe.get_all(
		DOCTYPE,
		filters={"event_id": ("like", f"{prefix}%")},
		pluck="name",
		limit_page_length=0,
	)

	deleted = []
	for name in names:
		# `ignore_permissions` for the same reason the insert uses it: this
		# method is the authorisation boundary, not the caller's role.
		frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
		deleted.append(name)

	frappe.db.commit()

	return {"prefix": prefix, "deleted": deleted, "count": len(deleted)}


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
CHANNEL_UNITS = {"sms": "sms_segment", "email": "email_message"}


@frappe.whitelist()
def get_credit_rate(channel=None, currency=None, destination=None, at=None):
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

	`rate_name` is returned so a purchase can record WHICH row it was quoted from.
	Without it a later rate change leaves a quoted purchase unattributable, and
	"priced once, never re-priced" stops being checkable.
	"""
	channel, currency = _require_rate_keys(channel, currency)
	moment = cstr(at) if at else str(now_datetime())
	country = _dialling_code(destination, channel) if channel == "sms" else None

	base = {
		"channel": channel,
		"unit": CHANNEL_UNITS[channel],
		"price_per_unit_micros": 0,
		"currency": currency,
		"country_code": country,
		"rate_name": None,
		"effective_from": None,
		"effective_to": None,
		"resolved_at": str(now_datetime()),
		"source": "unconfigured",
	}

	if country:
		for row in _active_rates(channel, moment):
			if cstr(row.country_code) == country:
				return _as_rate(row, base, "exact_country")

	for row in _active_rates(channel, moment):
		if not row.country_code:
			return _as_rate(row, base, "channel_default")

	return base


def _active_rates(channel, moment):
	"""Active rows for a channel whose window contains `moment`, newest first."""
	rows = frappe.get_all(
		RATE_DOCTYPE,
		filters={"channel": channel, "is_active": 1, "effective_from": ("<=", moment)},
		fields=[
			"name",
			"unit",
			"price_per_unit_micros",
			"currency",
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


def _dialling_code(destination, channel):
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

	codes = frappe.get_all(
		RATE_DOCTYPE,
		filters={"channel": channel, "country_code": ("is", "set")},
		pluck="country_code",
		limit_page_length=0,
	)

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
