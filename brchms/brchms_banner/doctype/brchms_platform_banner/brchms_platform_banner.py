# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class BRCHMSPlatformBanner(Document):
	"""
	A notice the BRCHMS app shows on every page.

	The window is validated here so a banner that can never appear is refused at
	authoring time rather than discovered later. The app evaluates the same window
	again when it serves a banner -- two checks, because the app cannot see this
	form and this form cannot see the app's clock.
	"""

	def validate(self):
		self._validate_text()
		self._validate_window()

	def _validate_text(self):
		"""
		An active banner must say something.

		An active banner with no text is an empty coloured bar on every page of
		the app, which trains people to ignore the banner -- including the next
		one, which will matter.
		"""
		if not self.is_active:
			return

		if not (self.title or "").strip() and not (self.message or "").strip():
			frappe.throw(
				_(
					"This banner is switched on but has no title and no message, so it would "
					"appear as an empty bar on every page. Add the notice, or switch it off."
				)
			)

	def _validate_window(self):
		if not self.starts_at or not self.ends_at:
			return

		start = get_datetime(self.starts_at)
		end = get_datetime(self.ends_at)

		if end <= start:
			frappe.throw(
				_(
					"The banner would stop being visible ({0}) before it starts ({1}), so nobody "
					"would ever see it. Set the end after the start."
				).format(frappe.format(end), frappe.format(start))
			)
