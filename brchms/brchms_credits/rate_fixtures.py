# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Test fixtures for the LIVE rate harness.

The harness used to depend on rates that happened to be configured on the site, which
made it pass against one deployment and fail against another for reasons unrelated to
the code — and it failed the moment somebody cleaned the site up. So the harness now
creates what it needs and removes exactly what it made.

Namespaced under a `LIVE-TEST` note so cleanup can never touch an operator's rate.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

RATE_DOCTYPE = "Rest Credit Rate"
FIXTURE_NOTE = "LIVE-TEST-RATE-FIXTURE"


def _require_system_manager():
	"""
	Only a System Manager may touch the rate card through these methods.

	These endpoints insert and delete RATE rows with `ignore_permissions`, which
	is pricing authority: without this check any authenticated caller -- including
	the app's read-only integration credential -- could create a cheap rate or
	DELETE a live one, and every subsequent quote would be priced from it. The
	harness that uses these methods runs as an administrator; a production
	credential never needs them.
	"""
	frappe.only_for("System Manager")


@frappe.whitelist()
def install():
	"""Create the two rates the live harness asserts on."""
	_require_system_manager()
	remove()

	now = now_datetime()
	created = []

	default = frappe.get_doc(
		{
			"doctype": RATE_DOCTYPE,
			"channel": "sms",
			"unit": "sms_segment",
			"price_per_unit_micros": 1000000,
			"currency": "CAD",
			"is_active": 1,
			"effective_from": now,
			"notes": FIXTURE_NOTE,
		}
	)
	default.flags.ignore_permissions = True
	default.insert()
	created.append(default.name)

	country = frappe.get_doc(
		{
			"doctype": RATE_DOCTYPE,
			"channel": "sms",
			"unit": "sms_segment",
			"price_per_unit_micros": 9000000,
			"currency": "CAD",
			"country_code": "+233",
			"is_active": 1,
			"effective_from": now,
			"notes": FIXTURE_NOTE,
		}
	)
	country.flags.ignore_permissions = True
	country.insert()
	created.append(country.name)

	frappe.db.commit()
	return created


@frappe.whitelist()
def remove():
	"""
	Delete exactly the fixture rows.

	Scoped by the marker note rather than by anything the caller passes, so this can
	never be pointed at an operator's real rate. Still System-Manager-only: the
	scope prevents deleting a real rate, not using the endpoint at all.
	"""
	_require_system_manager()
	names = frappe.get_all(RATE_DOCTYPE, filters={"notes": FIXTURE_NOTE}, pluck="name")
	for name in names:
		frappe.delete_doc(RATE_DOCTYPE, name, force=True, ignore_permissions=True)
	if names:
		frappe.db.commit()
	return names
