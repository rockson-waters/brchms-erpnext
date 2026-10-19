# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Country → selling-company resolution.

The billing country belongs to the ORGANIZATION, not to the buyer's browser and
not to an individual user. Every purchase, quote and invoice resolves through
this module, so a caller cannot choose a cheaper company by sending a different
currency or an arbitrary company id.

There is deliberately no default company for an unmapped country. The operator
must add a `Rest Credit Billing Region` row first: a silent fallback would put a
Kenyan customer on a Canadian invoice and only surface at reconciliation.
"""

import frappe
from frappe import _
from frappe.utils import cstr

REGION_DOCTYPE = "Rest Credit Billing Region"


def country_name_for(code_or_name):
	"""
	Resolve an ISO 3166-1 alpha-2 code (GH, CA, US) or a country name to the
	ERPNext Country document name.

	Frappe stores the country NAME as the document id and carries the ISO code in
	`Country.code`. The app sends the ISO code because that is stable across
	renames and locales.
	"""
	text = cstr(code_or_name).strip()
	if not text:
		frappe.throw(
			_(
				"This organization has no billing country, so no company can be selected for its "
				"credits. Set Billing Country in Financial settings."
			),
			frappe.ValidationError,
		)

	# Already an existing Country name.
	by_name = frappe.db.get_value("Country", {"name": text}, "name")
	if by_name:
		return by_name

	# ISO alpha-2 / alpha-3 code. Wrapped because an older/partial Country
	# doctype may not expose `code`; a rename of the field must not turn a
	# clear "set the billing country" error into a stack trace.
	try:
		by_code = frappe.db.get_value("Country", {"code": text.upper()}, "name")
	except Exception:
		by_code = None
	if by_code:
		return by_code

	frappe.throw(
		_(
			"Unknown billing country '{0}'. Use an ISO country code such as GH, CA or US."
		).format(frappe.utils.escape_html(text)),
		frappe.ValidationError,
	)


def region_for_country(country_code, company=None):
	"""
	The active billing region for a country, optionally checking a named company.

	Raises when the country has no mapping, when the mapping is inactive, or when
	the caller tried to name a company other than the mapped one.
	"""
	country = country_name_for(country_code)
	region = frappe.db.get_value(
		REGION_DOCTYPE,
		{"country": country, "is_active": 1},
		["name", "region_name", "country", "company", "currency"],
		as_dict=True,
	)
	if not region:
		frappe.throw(
			_(
				"No billing region is configured for {0}. Ask an administrator to add a Rest "
				"Credit Billing Region row mapping {0} to a company before selling credits there."
			).format(frappe.utils.escape_html(country)),
			frappe.ValidationError,
		)

	if company and cstr(company) != cstr(region.company):
		frappe.throw(
			_(
				"{0} is billed by {1}, not {2}. The company is determined by the billing country; "
				"a caller cannot select another selling entity."
			).format(
				frappe.utils.escape_html(country),
				frappe.utils.escape_html(region.company),
				frappe.utils.escape_html(cstr(company)),
			),
			frappe.ValidationError,
		)

	company_currency = frappe.db.get_value("Company", region.company, "default_currency")
	if not company_currency:
		frappe.throw(
			_("Company {0} has no default currency.").format(
				frappe.utils.escape_html(region.company)
			),
			frappe.ValidationError,
		)
	region.currency = cstr(company_currency).upper()
	return region


def resolve_billing_context(
	country_code, company=None, currency=None, instance_id=None, organization_id=None
):
	"""
	The complete, validated billing context for an organization.

	`currency` is validated rather than converted. A caller that asks to pay in a
	currency the company does not keep is refused; silently converting would
	invent an exchange rate nobody approved.
	"""
	region = region_for_country(country_code, company=company)
	currency = cstr(currency).strip().upper() if currency else region.currency

	if currency != region.currency:
		frappe.throw(
			_(
				"The billing region for {0} uses {1}, but this request asks for {2}. Credits are "
				"sold in the company's own currency; no exchange rate is applied here."
			).format(
				frappe.utils.escape_html(region.country),
				frappe.utils.escape_html(region.currency),
				frappe.utils.escape_html(currency),
			),
			frappe.ValidationError,
		)

	return {
		"instance_id": cstr(instance_id).strip() or None,
		"organization_id": cstr(organization_id).strip() or None,
		"country": region.country,
		"country_code": _iso_code(region.country),
		"region": region.name,
		"region_name": region.region_name,
		"company": region.company,
		"currency": region.currency,
		"ready": True,
	}


def resolve_selling_company(company=None, country_code=None, currency=None, require=False):
	"""
	The selling company for a request.

	A caller that names a country gets the mapped company. A caller that names a
	company is checked against that mapping when a country is also known. A
	single-company site keeps its legacy default; a multi-company site without a
	context is refused, because guessing would pick a price from the wrong books.
	"""
	if country_code:
		context = resolve_billing_context(country_code, company=company, currency=currency)
		return context["company"]

	if company:
		if not frappe.db.exists("Company", company):
			frappe.throw(
				_("Company {0} does not exist.").format(frappe.utils.escape_html(cstr(company))),
				frappe.ValidationError,
			)
		if currency:
			company_currency = cstr(frappe.db.get_value("Company", company, "default_currency")).upper()
			if cstr(currency).strip().upper() != company_currency:
				frappe.throw(
					_("Company {0} keeps its books in {1}, not {2}.").format(
						frappe.utils.escape_html(company),
						frappe.utils.escape_html(company_currency),
						frappe.utils.escape_html(cstr(currency)),
					),
					frappe.ValidationError,
				)
		return company

	# Legacy compatibility: an older app client knows only the currency on a
	# send/purchase, not the billing country. If exactly ONE company keeps books
	# in that currency, the answer is unambiguous. Two companies sharing a
	# currency still require a real billing context, because there would be a
	# choice to make.
	if currency:
		currency = cstr(currency).strip().upper()
		companies = frappe.get_all(
			"Company",
			filters={"default_currency": currency},
			pluck="name",
			limit_page_length=0,
		)
		if len(companies) == 1:
			return companies[0]
		if len(companies) > 1:
			frappe.throw(
				_(
					"{0} companies keep their books in {1}, so a currency alone cannot choose a "
					"seller. Send the billing country or the company."
				).format(len(companies), frappe.utils.escape_html(currency)),
				frappe.ValidationError,
			)
		frappe.throw(
			_("No Company keeps its books in {0}.").format(frappe.utils.escape_html(currency)),
			frappe.ValidationError,
		)

	companies = frappe.get_all("Company", pluck="name", limit_page_length=0)
	if len(companies) == 1:
		return companies[0]

	frappe.throw(
		_(
			"This site has {0} companies, so a billing country or an explicit company is required "
			"to choose a price. Add a Rest Credit Billing Region for the customer's country."
		).format(len(companies)),
		frappe.ValidationError,
	)


def _iso_code(country_name):
	try:
		return cstr(frappe.db.get_value("Country", country_name, "code")).upper() or None
	except Exception:
		return None


def billing_is_ready(country_code, currency=None):
	"""Non-throwing readiness check for read surfaces such as purchase links."""
	try:
		context = resolve_billing_context(country_code, currency=currency)
		return True, context, None
	except Exception as error:
		return False, None, cstr(getattr(error, "message", None) or str(error))
