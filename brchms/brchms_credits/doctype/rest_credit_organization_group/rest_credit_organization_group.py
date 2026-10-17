# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The child row behind `Rest Credit Organization.groups`. See that doctype's
description for why identity is the pair (parent, funding_group_id) and never
the display name.
"""

import frappe
from frappe.model.document import Document


class RestCreditOrganizationGroup(Document):
	pass
