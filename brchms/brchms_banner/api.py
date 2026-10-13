# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The announcement banner's API.

`POST /api/method/brchms.brchms_banner.api.get_active_banner`

Returns the one banner the Rest app should show right now, or `None` when there
is nothing to show.

## Why "or None" is the whole contract

The app renders this on every page -- including the login page and the public
pages -- and it is deliberately FAIL-OPEN: a banner is advisory, so a failure
anywhere along the way must produce "no banner", never an error in front of a
user who is trying to sign in. Nothing in this method throws for a normal
absence: no active banner is a successful answer of `None`.

## Why the window is evaluated here

`starts_at` / `ends_at` are a property of the notice, so the authority that owns
the notice decides when it is visible. The app re-evaluates the same window as a
guard (a scheduled banner must not appear early because the app cannot see the
schedule), but an app that had to choose *which* of several rows wins would be
re-implementing this selection in a second place, and the two would drift.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

DOCTYPE = "Rest Platform Banner"

# The banner text caps the app enforces on its side. Enforced here too so an
# over-long notice is refused where it is written, rather than being silently
# truncated on the way to every page.
MAX_TITLE_CHARS = 200
MAX_MESSAGE_CHARS = 4000


@frappe.whitelist()
def get_active_banner():
	"""
	The banner to display right now, or `None`.

	Selection, in order:

	  1. `is_active` is on -- the master switch, and it wins over the window;
	  2. the visibility window covers now (an empty bound is unbounded);
	  3. the most recently modified of the candidates wins.

	Ordered by `modified` descending because when an operator schedules two
	overlapping notices, the one they touched last is the one they mean.
	"""
	now = now_datetime()

	candidates = frappe.get_all(
		DOCTYPE,
		filters={"is_active": 1},
		fields=[
			"name",
			"title",
			"message",
			"severity",
			"dismissible",
			"starts_at",
			"ends_at",
			"modified",
		],
		order_by="modified desc",
		limit_page_length=50,
	)

	for row in candidates:
		if _window_open(row, now):
			return _as_response(row)

	return None


def _window_open(row, now):
	"""True when `now` falls inside the row's window; an empty bound is unbounded."""
	start = row.get("starts_at")
	end = row.get("ends_at")

	if start and now < start:
		return False

	if end and now >= end:
		return False

	return True


def _as_response(row):
	"""
	The shape Rest projects onto its render contract.

	`message` is passed through as stored: the app runs the value through
	`sanitizeHtml()` on the client, which is the one place that can know what the
	rendering context allows. This method does NOT strip markup -- doing it here
	would silently mangle a legitimate `<p>` and a notice that lost its paragraphs
	is a notice nobody finishes reading.
	"""
	message = row.get("message") or ""
	if len(message) > MAX_MESSAGE_CHARS:
		message = message[:MAX_MESSAGE_CHARS]

	title = row.get("title") or ""
	if len(title) > MAX_TITLE_CHARS:
		title = title[:MAX_TITLE_CHARS]

	return {
		"name": row.get("name"),
		"title": title,
		"message": message,
		"severity": row.get("severity") or "info",
		"dismissible": bool(row.get("dismissible")),
		"starts_at": str(row["starts_at"]) if row.get("starts_at") else None,
		"ends_at": str(row["ends_at"]) if row.get("ends_at") else None,
		"updated_at": str(row["modified"]) if row.get("modified") else None,
	}


@frappe.whitelist()
def set_banner(title=None, message=None, severity="info", is_active=1, dismissible=1, starts_at=None, ends_at=None):
	"""
	Create a banner, or update the active one if it exists and is unchanged.

	Convenience for an operator driving this from a script during an upgrade:
	the alternative is the Desk, which is the right tool for authoring and the
	wrong one for "put a notice up now, we are deploying".

	Deliberately NOT idempotent on content -- a second call replaces the active
	banner rather than creating a duplicate, because two simultaneous notices is
	not a state the app can render.
	"""
	severity = (severity or "info").strip().lower()
	if severity not in ("info", "warning", "critical"):
		frappe.throw(
			_("Severity must be info, warning or critical (received {0}).").format(severity)
		)

	existing = frappe.get_all(
		DOCTYPE, filters={"is_active": 1}, order_by="modified desc", limit_page_length=1
	)

	doc = frappe.get_doc(DOCTYPE, existing[0].name) if existing else frappe.new_doc(DOCTYPE)

	doc.title = title
	doc.message = message
	doc.severity = severity
	doc.is_active = 1 if cint(is_active) else 0
	doc.dismissible = 1 if cint(dismissible) else 0
	doc.starts_at = starts_at or None
	doc.ends_at = ends_at or None

	if existing:
		doc.save()
	else:
		doc.insert()

	return _as_response(doc)
