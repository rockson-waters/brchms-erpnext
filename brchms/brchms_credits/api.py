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
