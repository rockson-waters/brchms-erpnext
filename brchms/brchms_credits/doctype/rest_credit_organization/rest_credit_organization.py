# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The registry row: one Rest organization, as this site will bill it.

The invariants live in `brchms.brchms_credits.registry`, not here — a registry
row is written by that module through `ignore_permissions`, because the
whitelisted methods that call it are the authorisation boundary (the same
posture as the credits API, documented there). The controller therefore
carries no validation of its own: nothing else writes these rows, and a second
implementation of the same rules is a second chance to get them wrong.
"""

import frappe
from frappe.model.document import Document


class RestCreditOrganization(Document):
	pass
