# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, now_datetime

CATEGORIES = (
	"app_error",
	"data_error",
	"access_login",
	"performance",
	"other",
)


class RestMalfunctionReport(Document):
	"""
	One malfunction report filed from the Rest app.

	The app's API owns creation, the reporter identity and the attachment; this
	controller protects the two properties the Desk must not break:

	  * the report always says what kind of problem it is and what happened;
	  * `reported_at` is the instance's clock, set once, never rewritten by an
	    edit in the Desk.
	"""

	def validate(self):
		self._validate_category()
		self._validate_description()
		self._validate_reported_at()

	def before_insert(self):
		if not self.reported_at:
			self.reported_at = now_datetime()

	def _validate_category(self):
		if self.category not in CATEGORIES:
			frappe.throw(
				_(
					"category: Choose what kind of problem this is ({0})."
				).format(", ".join(CATEGORIES))
			)

	def _validate_description(self):
		if not cstr(self.description).strip():
			frappe.throw(_("description: Say what went wrong; an empty report cannot be triaged."))

	def _validate_reported_at(self):
		"""
		The reported time is evidence, not an editable field.

		A desk edit that could move it would let a report's age be changed after
		the fact, which is the one thing an incident record should not permit.
		"""
		if self.is_new():
			return

		before = self.get_doc_before_save()
		if before and before.reported_at and self.reported_at != before.reported_at:
			frappe.throw(
				_(
					"reported_at: This is when the app recorded the report and cannot be changed. "
					"Use the Admin Notes to record when it was picked up."
				)
			)
