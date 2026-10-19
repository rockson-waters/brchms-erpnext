# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Install-time records the purchase flow needs, created idempotently on migrate.

Three things must exist on a site before a customer can buy credits, and none of
them is expressible as a fixture that installs itself reliably:

1. **`Sales Invoice.purchase_reference`** — the link between a payment and the
   request that prompted it. Without it a payment cannot be attributed, and the
   settlement hook has nothing to key on.
2. **An index on that field** — the settlement hook looks invoices up BY it, and
   `_touch_instance`-style laziness is not available here: the reference is written
   when the invoice is created, so the read must be cheap from the first one.
3. **The items being sold** — a Sales Invoice needs an item, and a credits app whose
   sales item a human must create by hand is a credits app that fails on a fresh
   site for a bookkeeping reason.

## Why here and not `fixtures/`

`bench migrate` runs `after_migrate` on every deploy, and this module is idempotent,
so the records exist after the first migrate and are never duplicated. A
`fixtures/` entry would need `bench migrate` to have been told to import fixtures,
which is a separate step an operator can skip — and the failure would surface as a
missing field on a live site, not as a failed migration.
"""

import frappe
from frappe import _
from frappe.utils import cstr

INVOICE_DOCTYPE = "Sales Invoice"
PURCHASE_DOCTYPE = "Rest Credit Purchase"
PURCHASE_REFERENCE_FIELD = "purchase_reference"

# The channel names the app sends. Kept as data so the item code, the label and the
# channel cannot drift apart.
CREDIT_ITEMS = (
	{
		"item_code": "REST-SMS-CREDIT",
		"item_name": "Rest SMS Credits",
		"description": "Prepaid SMS segments for the Rest communications ledger.",
	},
	{
		"item_code": "REST-EMAIL-CREDIT",
		"item_name": "Rest Email Credits",
		"description": "Prepaid email messages for the Rest communications ledger.",
	},
)

ITEM_GROUP = "Services"
STOCK_UOM = "Nos"


def after_migrate():
	"""Hook body for `bench migrate`. Each step is independent and idempotent."""
	created = []
	for fieldname in ensure_purchase_input_fields():
		created.append("Sales Invoice.{0}".format(fieldname))
	if ensure_purchase_reference_index():
		created.append("index on purchase_reference")
	created.extend(ensure_credit_items())
	created.extend(ensure_purchase_web_form())
	backfilled = backfill_rate_display()
	if backfilled:
		created.append("rate display backfilled for {0} rows".format(backfilled))
	scoped = backfill_company_scoping()
	if scoped is not None:
		created.append("company scoping: {0}".format(scoped))

	return created


def backfill_company_scoping():
	"""
	Point pre-existing rate/proposal/purchase rows at their company where that is
	unambiguous.

	Never guess on a multi-company site. Existing rate rows were created when the
	site had one billing company; if there is still exactly one Company, that is
	the only correct answer. With several companies the rows are reported as
	unresolved and an operator must assign them before the rate card is used.

	Returns a short human summary for the migrate output.
	"""
	companies = frappe.get_all("Company", pluck="name", limit_page_length=0)
	counts = {}
	for doctype in ("Rest Credit Rate", "Rest Credit Proposal", "Rest Credit Purchase"):
		if not frappe.db.has_column(doctype, "company"):
			counts[doctype] = "no company column yet"
			continue
		missing = frappe.db.sql(
			"SELECT COUNT(*) FROM `tab{0}` WHERE company IS NULL OR company = ''".format(doctype)
		)[0][0]
		if not missing:
			counts[doctype] = 0
			continue
		if len(companies) == 1:
			frappe.db.sql(
				"UPDATE `tab{0}` SET company = %s WHERE company IS NULL OR company = ''".format(doctype),
				(companies[0],),
			)
			counts[doctype] = "{0} backfilled to {1}".format(missing, companies[0])
		elif doctype == "Rest Credit Rate":
			# A rate's CURRENCY often identifies its company unambiguously. That is
			# safe to use: GHS belongs to the GH company, CAD to the CAD company,
			# and only a currency shared by two companies remains unresolved.
			by_currency = {}
			for company in companies:
				currency = frappe.db.get_value("Company", company, "default_currency")
				if currency:
					by_currency.setdefault(cstr(currency).upper(), []).append(company)
			backfilled = 0
			rows = frappe.db.sql(
				"SELECT name, currency FROM `tab{0}` WHERE company IS NULL OR company = ''".format(
					doctype
				),
				as_dict=True,
			)
			for row in rows:
				owners = by_currency.get(cstr(row.currency).upper(), [])
				if len(owners) == 1:
					frappe.db.set_value(doctype, row.name, "company", owners[0], update_modified=False)
					backfilled += 1
			counts[doctype] = "{0} unresolved ({1} companies); {2} backfilled by currency".format(
				missing - backfilled, len(companies), backfilled
			)
		else:
			counts[doctype] = "{0} unresolved ({1} companies)".format(missing, len(companies))

	# A purchase's company can also be recovered from its linked proposal or
	# invoice even on a multi-company site. This is a lookup, not a guess.
	if frappe.db.has_column("Rest Credit Purchase", "company"):
		purchase_rows = frappe.db.sql(
			"SELECT name, proposal FROM `tabRest Credit Purchase` "
			"WHERE (company IS NULL OR company = '') AND proposal IS NOT NULL AND proposal != ''",
			as_dict=True,
		)
		recovered = 0
		for row in purchase_rows:
			proposal_company = frappe.db.get_value("Rest Credit Proposal", row.proposal, "company")
			if proposal_company:
				frappe.db.set_value(
					"Rest Credit Purchase", row.name, "company", proposal_company, update_modified=False
				)
				recovered += 1
		if recovered:
			counts["Rest Credit Purchase"] = "{0} recovered from proposals: {1}".format(
				counts.get("Rest Credit Purchase"), recovered
			)

	# A proposal's company can also be recovered from its invoice even on a
	# multi-company site: the invoice already knows. This is not a guess.
	if frappe.db.has_column("Rest Credit Proposal", "company"):
		rows = frappe.get_all(
			"Rest Credit Proposal",
			filters={"company": ("is", "not set"), "invoice": ("is", "set")},
			fields=["name", "invoice"],
			limit_page_length=0,
		)
		for row in rows:
			invoice_company = frappe.db.get_value("Sales Invoice", row.invoice, "company")
			if invoice_company:
				frappe.db.set_value(
					"Rest Credit Proposal", row.name, "company", invoice_company, update_modified=False
				)
				counts["Rest Credit Proposal"] = "{0}; {1} recovered from invoices".format(
					counts.get("Rest Credit Proposal"), row.name
				)

	return counts


def backfill_rate_display():
	"""
	Fill `Price per Unit` for rate rows saved before the field existed.

	The display is derived, so it must never be the source of truth; recomputing it
	on migrate keeps old rows readable without changing a single price.
	"""
	if not frappe.db.has_column("Rest Credit Rate", "price_per_unit_display"):
		return 0

	from brchms.brchms_credits.doctype.rest_credit_rate.rest_credit_rate import (
		format_micros,
	)

	updated = 0
	for row in frappe.get_all(
		"Rest Credit Rate",
		fields=["name", "price_per_unit_micros", "currency", "price_per_unit_display"],
	):
		display = format_micros(row.price_per_unit_micros, row.currency)
		if display and display != row.price_per_unit_display:
			frappe.db.set_value(
				"Rest Credit Rate", row.name, "price_per_unit_display", display,
				update_modified=False,
			)
			updated += 1
	return updated


# What the purchase form collects about the REQUEST, per channel. Kept as data so
# the fields, the web form and the app's own vocabulary cannot drift.
PURCHASE_INPUT_FIELDS = (
	{
		"fieldname": "channel",
		"label": "Channel",
		"fieldtype": "Select",
		"options": "sms\nemail",
		"insert_after": "purchase_reference",
		"description": (
			"Which communications channel these credits buy: `sms` or `email`. Stored so "
			"the settlement can be checked against the channel's rate rather than guessed "
			"from the amount."
		),
	},
	{
		"fieldname": "units",
		"label": "Units",
		"fieldtype": "Int",
		"insert_after": "channel",
		"description": (
			"How many SMS segments or emails the buyer is paying for. The app's own quote "
			"records what it expected; a settlement carrying a different number is a query, "
			"not a silence."
		),
	},
	{
		"fieldname": "instance_id",
		"label": "Rest Instance",
		"fieldtype": "Link",
		"options": "Rest Instance",
		"insert_after": "units",
		"description": (
			"Which Rest deployment this invoice bills for. Written when the invoice is "
			"raised, and read by the settlement hook: a payment settles the proposal of "
			"the deployment that raised it, never another deployment's proposal that "
			"happens to share a reference."
		),
	},
)


def ensure_purchase_input_fields():
	"""
	Create the Sales Invoice fields the purchase form collects.

	`purchase_reference` is created first because the others are positioned relative
	to it — in a different order they would attach somewhere arbitrary in the form.
	"""
	created = []
	ensure_purchase_reference_field()

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	for spec in PURCHASE_INPUT_FIELDS:
		if frappe.db.has_column(INVOICE_DOCTYPE, spec["fieldname"]):
			continue
		create_custom_field(
			INVOICE_DOCTYPE,
			{
				"fieldname": spec["fieldname"],
				"label": spec["label"],
				"fieldtype": spec["fieldtype"],
				"options": spec.get("options", ""),
				"insert_after": spec["insert_after"],
				"description": spec["description"],
			},
		)
		created.append(spec["fieldname"])

	if created:
		frappe.clear_cache(doctype=INVOICE_DOCTYPE)
		frappe.logger().info("brchms: created Sales Invoice fields %s", created)
	return created


def ensure_purchase_reference_field():
	"""
	Create `Sales Invoice.purchase_reference` if it is absent.

	Placed in Standard Selling's field order with `insert_after="po_no"` — beside the
	other customer-supplied reference fields, which is where an operator looking for
	"what is this invoice for" will look. `no_copy` because an invoice copied from
	another would otherwise inherit a reference that is already spent; a copied
	document is a new payment and must not settle somebody else's proposal.
	"""
	if frappe.db.has_column(INVOICE_DOCTYPE, PURCHASE_REFERENCE_FIELD):
		return False

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	create_custom_field(
		INVOICE_DOCTYPE,
		{
			"fieldname": PURCHASE_REFERENCE_FIELD,
			"label": "Purchase Reference",
			"fieldtype": "Data",
			"insert_after": "po_no",
			"no_copy": 1,
			"read_only": 0,
			"print_hide": 0,
			"description": _(
				"The Rest credit purchase this invoice bills for, as sent by the app. "
				"It links the payment to the request that produced it, which is what "
				"makes the credits claimable once the invoice is paid."
			),
		},
	)
	frappe.clear_cache(doctype=INVOICE_DOCTYPE)
	frappe.logger().info("brchms: created Sales Invoice.purchase_reference")
	return True


def ensure_purchase_reference_index():
	"""
	A plain index on the reference column.

	Deliberately NOT unique: two invoices may legitimately quote the same reference
	if the first was cancelled and re-issued, and a database refusal at that moment
	would surface as an unhelpful error on a legitimate sale. Uniqueness of the
	*request* is already enforced on the proposal (`unique_instance_purchase_reference`),
	which is the side that must never duplicate.
	"""
	index_name = "idx_sales_invoice_purchase_reference"
	try:
		existing = frappe.db.sql(
			"SHOW INDEX FROM `tab{0}` WHERE Key_name = %s".format(INVOICE_DOCTYPE),
			(index_name,),
		)
	except Exception:
		return False

	if existing:
		return False

	try:
		frappe.db.sql(
			"ALTER TABLE `tab{0}` ADD INDEX `{1}` (`{2}`)".format(
				INVOICE_DOCTYPE, index_name, PURCHASE_REFERENCE_FIELD
			)
		)
	except Exception as error:
		# Reported rather than raised: a missing index slows the settlement hook and
		# nothing else, and a migration that fails leaves the site half-updated.
		frappe.logger().warning("brchms: could not add the purchase_reference index: %s", error)
		return False

	frappe.logger().info("brchms: added the purchase_reference index")
	return True


def ensure_credit_items():
	"""
	Create the credit items if absent, and return the codes created.

	`is_stock_item: 0` is the important flag: credits are a prepaid service, not
	goods, and a stock item would drag warehouses, valuation and stock ledger entries
	into a sale that has none of those things.
	"""
	created = []
	item_group = ITEM_GROUP if frappe.db.exists("Item Group", ITEM_GROUP) else _fallback_item_group()
	uom = STOCK_UOM if frappe.db.exists("UOM", STOCK_UOM) else _fallback_uom()

	for spec in CREDIT_ITEMS:
		if frappe.db.exists("Item", spec["item_code"]):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": spec["item_code"],
				"item_name": spec["item_name"],
				"description": spec["description"],
				"item_group": item_group,
				"stock_uom": uom,
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"include_item_in_manufacturing": 0,
				"grant_commission": 0,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		created.append(spec["item_code"])
		frappe.logger().info("brchms: created item %s", spec["item_code"])

	return created


def _fallback_item_group():
	"""
	The item group to use when `Services` is absent.

	ERPNext seeds `Services` on a normal install, but a site created without the
	setup fixtures does not have it — and refusing to create the items would make
	the purchase flow unavailable for a reason that has nothing to do with credits.
	"""
	leaf = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	if leaf:
		return leaf
	root = frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""}, "name")
	if root:
		return root

	doc = frappe.get_doc(
		{"doctype": "Item Group", "item_group_name": ITEM_GROUP, "is_group": 0}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _fallback_uom():
	uom = frappe.db.get_value("UOM", {}, "name")
	if uom:
		return uom
	doc = frappe.get_doc({"doctype": "UOM", "uom_name": STOCK_UOM, "must_be_whole_number": 1})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


# ── the purchase form ────────────────────────────────────────────────────────

WEB_FORM_NAME = "Rest Credit Purchase"
WEB_FORM_ROUTE = "buy-credits"

# The fields the customer fills in, which must match `Rest Credit Purchase`
# EXACTLY. Frappe's `WebForm.validate_fields` refuses a field that does not exist
# on the target doctype, and that refusal is the reason this form is not backed by
# Sales Invoice: an invoice stores its line item in a child table and has no email
# field, so the three things a buyer must give — what, how many, and where the
# receipt goes — cannot live on it.
WEB_FORM_FIELDS = (
	{
		"fieldname": "buyer_name",
		"label": "Your Name",
		"fieldtype": "Data",
		"reqd": 1,
		"description": "The name this purchase is billed to.",
	},
	{
		"fieldname": "buyer_email",
		"label": "Your Email Address",
		"fieldtype": "Data",
		"options": "Email",
		"reqd": 1,
		"description": "Where the receipt goes. Used to reach you about this purchase.",
	},
	{
		"fieldname": "channel",
		"label": "Credit Type",
		"fieldtype": "Select",
		"options": "sms\nemail",
		"reqd": 1,
		"description": "SMS credits are counted in segments; email credits in messages.",
	},
	{
		"fieldname": "units",
		"label": "How Many Credits",
		"fieldtype": "Int",
		"reqd": 1,
	},
	{
		"fieldname": "amount",
		"label": "Amount You Are Paying",
		"fieldtype": "Currency",
		"reqd": 1,
		"read_only": 1,
		"description": (
			"The exact price for the credits entered, computed from your church's billing "
			"region as you type. It cannot be typed over, so what you see is what you are "
			"billed."
		),
	},
	{
		"fieldname": "currency",
		"label": "Currency",
		"fieldtype": "Data",
		"read_only": 1,
		"description": "The selling company's currency for this purchase, resolved from the organization.",
	},
)

# Set on the page: the request is created first, then the proposal, its invoice and
# the payment page are asked for in ONE call whose answer is a URL. Doing it here
# rather than with `success_url` is what makes the destination dynamic — a static
# URL could not name the purchase being paid for.
#
# The script also carries the two things the page cannot know without it:
#
#   * the ORG SLUG, read from the link's `?org=` query parameter. The form is
#     public, so this is the only way a submission says whose credits it is
#     buying — and `checkout` refuses any submission that arrives without one.
#   * the COMPUTED AMOUNT, refreshed from `quote` as the buyer types (and once on
#     load, which also wipes any amount a crafted URL tried to pre-fill).
#
# ## Why there is NO `frappe.ready(...)` wrapper
#
# There was one, and it meant NONE of this ever ran. A web form page defines
# `frappe.ready` as a collect-only shim in its `<head>`, and
# `frappe.website.trigger_ready()` drains that array once — at document ready,
# which happens BEFORE the web-form bundle builds the form and executes this
# script. So a callback registered from here lands in an array nobody drains
# again, silently: no quote, no submit redirect, nothing in the console.
#
# A web-form client script is invoked from `WebForm.make()` AFTER the fields
# exist, so it needs no ready hook at all: run at top level and use
# `frappe.web_form.after_save` for the one event that matters.
#
# ## Why the change handlers are attached to the control's DOM input
#
# `frappe.web_form.on(fieldname, handler)` sets `field.df.change`, which only a
# programmatic `set_value` reaches from here — a user's typing goes through the
# control's own `change`, so relying on `on()` silently never quotes. Rather than
# replace that handler (which would skip Frappe's parsing and leave the model
# value unset for submission), a second `change` listener is attached directly to
# the input: Frappe's own wiring still runs, and the quote refreshes alongside it.
WEB_FORM_CLIENT_SCRIPT = """
/*
 * The customer-facing page is a REST purchase page, not a tour of the billing
 * platform. Frappe's stock portal footer says "Powered by ERPNext"; hide only
 * that badge on this route so the buyer sees the church's product, not our
 * infrastructure vendor. The rest of the footer (legal/site links) remains.
 */
(function hide_platform_brand() {
	var style = document.createElement("style");
	style.textContent = ".footer-powered { display: none !important; }";
	document.head.appendChild(style);
})();

var params = new URLSearchParams(window.location.search);
var org = params.get("org");

function fieldvalue(fieldname) {
	var field = frappe.web_form.fields_dict[fieldname];
	if (!field) {
		return null;
	}
	/*
	 * Read the value with fallbacks. An Int control keeps the typed text in its
	 * input; a Select control (channel) keeps its value in the model and can
	 * report `null` from its input, so falling back to `field.value` / the doc
	 * is what keeps the quote from silently bailing.
	 */
	var raw = null;
	if (field.$input && field.$input.val) {
		raw = field.$input.val();
	}
	if (raw === null || raw === undefined || raw === "") {
		raw = field.value;
	}
	if ((raw === null || raw === undefined || raw === "") && frappe.web_form.doc) {
		raw = frappe.web_form.doc[fieldname];
	}
	return raw === null || raw === undefined || raw === "" ? null : raw;
}

function current_channel() {
	var channel = fieldvalue("channel");
	if (!channel) {
		/* The form's first option is the default selection. */
		var field = frappe.web_form.fields_dict.channel;
		var options = field && field.df && field.df.options ? field.df.options.split("\\n") : [];
		channel = options.length ? options[0] : null;
	}
	return channel;
}

function requote() {
	var units = fieldvalue("units");
	var channel = current_channel();
	if (!units || !channel) {
		return;
	}
	frappe.call({
		method: "brchms.brchms_credits.proposals.quote",
		args: { channel: channel, units: units, org: org },
		callback: function (r) {
			if (r && r.message) {
				/*
				 * Apply the CURRENCY BEFORE the amount. Frappe's Currency control
				 * formats the amount by reading the document's linked currency
				 * field; setting the amount first made it render the site default
				 * ("GHS 4.50") beside a notice that correctly said CAD. The doc is
				 * set directly as well because the hidden `currency` field has no
				 * rendered control on the public web form.
				 */
				var amount_field = frappe.web_form.fields_dict.amount;
				if (r.message.currency) {
					/*
					 * Frappe resolves a Currency field's symbol through the
					 * document's linked currency, falling back to the site
					 * default. The public form's hidden `currency` control is not
					 * rendered, so set every resolution path: the web-form doc,
					 * the control's own doc, and the boot default the fallback
					 * reads. Without the last one the amount still rendered "GHS"
					 * while the billing notice correctly said "CAD".
					 */
					if (frappe.boot && frappe.boot.sysdefaults) {
						frappe.boot.sysdefaults.currency = r.message.currency;
					}
					if (frappe.web_form.doc) {
						frappe.web_form.doc.currency = r.message.currency;
					}
					if (amount_field && amount_field.doc) {
						amount_field.doc.currency = r.message.currency;
					}
					if (frappe.web_form.fields_dict.currency) {
						frappe.web_form.set_value("currency", r.message.currency);
					}
				}
				frappe.web_form.set_value("amount", r.message.amount_major);
				if (amount_field && amount_field.refresh) {
					amount_field.refresh();
				}
				show_billing(r.message.company, r.message.currency);
			}
		}
	});
}

function show_billing(company, currency) {
	/*
	 * The buyer sees WHICH company is selling to them and in which currency.
	 * The app and the form are region-scoped: a Canadian link must never show a
	 * Ghanaian price, and the form's only context is the org slug.
	 */
	var target = $(".rest-credit-billing-note");
	if (!target.length) {
		target = $("<div class='rest-credit-billing-note text-muted small mt-2'></div>");
		var amount = frappe.web_form.fields_dict.amount;
		if (amount && amount.$wrapper) {
			amount.$wrapper.after(target);
		} else {
			return;
		}
	}
	if (company) {
		target.text(__("Billed by {0} in {1}.", [company, currency || ""]));
	}
}

function bindquotes() {
	/*
	 * The controls render their inputs lazily, after this script runs, so poll
	 * for `$input` and bind when it exists. Binding via the control is what
	 * reaches the input a user actually types into; `frappe.web_form.on()` sets
	 * `df.change`, which typing never reaches here.
	 */
	var schedule = frappe.utils.debounce(requote, 300);
	var attempts = 0;

	function trybind() {
		var units = frappe.web_form.fields_dict.units;
		var channel = frappe.web_form.fields_dict.channel;
		if (units && units.$input && channel && channel.$input) {
			units.$input.off("input.requote change.requote")
				.on("input.requote change.requote", schedule);
			channel.$input.off("input.requote change.requote")
				.on("input.requote change.requote", schedule);
			requote();
			return;
		}
		if (attempts++ < 40) {
			setTimeout(trybind, 250);
		}
	}

	trybind();
}

/*
 * The saved document's name is handed to `handle_success` and nowhere else —
 * `frappe.web_form.doc.name` is never populated — so capture it there for the
 * checkout call in `after_save`, which runs immediately afterwards.
 */
var saved_name = null;
var original_handle_success = frappe.web_form.handle_success;
frappe.web_form.handle_success = function (data) {
	saved_name = data && data.name ? data.name : null;
	return original_handle_success.apply(frappe.web_form, [data]);
};

bindquotes();

/*
 * Give Credit Type its first option as the default selection. The control has no
 * value until the visitor picks one, so the form would refuse to submit with
 * "Credit Type" missing even though the quote below can price the default.
 */
if (!fieldvalue("channel")) {
	frappe.web_form.set_value("channel", current_channel() || "sms");
	requote();
}

if (!org) {
	frappe.msgprint({
		title: __("Open this form from your church's link"),
		indicator: "red",
		message: __(
			"This purchase form must be opened from your church's own 'Buy credits' " +
				"link, so the purchase says whose credits it is buying. Ask your church " +
				"administrator for the link."
		)
	});
}

frappe.web_form.after_save = function () {
	frappe.call({
		method: "brchms.brchms_credits.purchase.checkout",
		type: "POST",
		args: {
			purchase: saved_name || (frappe.web_form.doc && frappe.web_form.doc.name),
			org: org
		},
		callback: function (r) {
			var url = r && r.message && r.message.payment_url;
			if (url) {
				frappe.web_form.success_message =
					"Purchase recorded. Taking you to the payment page...";
				window.location.replace(url);
			} else {
				frappe.msgprint(
					__("Your purchase was recorded, but the payment page could not be opened. " +
					   "Please contact us and quote " + frappe.web_form.doc.name + ".")
				);
			}
		}
	});
};
"""


def ensure_purchase_web_form():
	"""
	Create or update the customer-facing purchase form.

	## Why it is created here and not by hand in the Desk

	A Web Form made in the Desk lives only in that site's database, so a fresh
	deployment would not have it and an operator could not tell a site that never had
	one from a site whose form was deleted. Creating it on migrate means the purchase
	page arrives the same way every other thing this app needs does.

	## Why the fields are listed rather than scanned from the doctype

	`WebFormField` rows are what the rendered page reads. A scanned list would expose
	every custom field a site happens to have — including ones that are not a
	customer's business. The list is this module's data, so what the form asks for is
	reviewable in one place, and `validate_fields` checks it against the doctype on
	save.
	"""
	if not frappe.db.exists("DocType", "Web Form"):
		return []

	existing = frappe.db.get_value("Web Form", {"route": WEB_FORM_ROUTE}, "name")
	doc = frappe.get_doc("Web Form", existing) if existing else frappe.get_doc({"doctype": "Web Form"})

	doc.update(
		{
			"title": WEB_FORM_NAME,
			"route": WEB_FORM_ROUTE,
			"doc_type": PURCHASE_DOCTYPE,
			"is_standard": 0,
			"published": 1,
			# A buyer is a stranger with no account here. `login_required` with
			# `anonymous` is how Frappe expresses that: the form is public and the
			# record is created as Guest, which is why `checkout` is the only public
			# write and it raises the invoice itself.
			"login_required": 0,
			"anonymous": 1,
			"allow_multiple": 1,
			"allow_edit": 0,
			"allow_delete": 0,
			"show_list": 0,
			"button_label": "Continue to Payment",
			"success_title": "Purchase recorded",
			"success_message": "Taking you to the payment page...",
			"client_script": WEB_FORM_CLIENT_SCRIPT,
		}
	)

	doc.web_form_fields = []
	for spec in WEB_FORM_FIELDS:
		doc.append("web_form_fields", spec)

	doc.flags.ignore_permissions = True
	doc.save()

	frappe.db.commit()
	frappe.clear_cache()
	frappe.logger().info("brchms: purchase web form at /%s", WEB_FORM_ROUTE)
	return ["web form /{0}".format(WEB_FORM_ROUTE)]

