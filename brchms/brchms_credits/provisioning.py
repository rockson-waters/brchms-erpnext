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

	return created


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
			"The exact price for the credits entered, computed from this site's current "
			"rate as you type. It cannot be typed over, so what you see is what you are "
			"billed."
		),
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
WEB_FORM_CLIENT_SCRIPT = """
frappe.ready(function () {
\tvar params = new URLSearchParams(window.location.search);
\tvar org = params.get("org");

\tfunction fieldvalue(fieldname) {
\t\tvar field = frappe.web_form.fields_dict[fieldname];
\t\treturn field ? field.value : null;
\t}

\tfunction requote() {
\t\tvar units = fieldvalue("units");
\t\tvar channel = fieldvalue("channel");
\t\tif (!units || !channel) {
\t\t\treturn;
\t\t}
\t\tfrappe.call({
\t\t\tmethod: "brchms.brchms_credits.proposals.quote",
\t\t\targs: { channel: channel, units: units },
\t\t\tcallback: function (r) {
\t\t\t\tif (r && r.message) {
\t\t\t\t\tfrappe.web_form.set_value("amount", r.message.amount_major);
\t\t\t\t}
\t\t\t}
\t\t});
\t}

\tfrappe.web_form.on("units", function () {
\t\trequote();
\t});
\tfrappe.web_form.on("channel", function () {
\t\trequote();
\t});
\trequote();

\tif (!org) {
\t\tfrappe.msgprint({
\t\t\ttitle: __("Open this form from your church's link"),
\t\t\tindicator: "red",
\t\t\tmessage: __(
\t\t\t\t"This purchase form must be opened from your church's own 'Buy credits' " +
\t\t\t\t\t"link, so the purchase says whose credits it is buying. Ask your church " +
\t\t\t\t\t"administrator for the link."
\t\t\t)
\t\t});
\t}

\tfrappe.web_form.after_save = function () {
\t\tfrappe.call({
\t\t\tmethod: "brchms.brchms_credits.purchase.checkout",
\t\t\ttype: "POST",
\t\t\targs: { purchase: frappe.web_form.doc.name, org: org },
\t\t\tcallback: function (r) {
\t\t\t\tvar url = r && r.message && r.message.payment_url;
\t\t\t\tif (url) {
\t\t\t\t\tfrappe.web_form.success_message =
\t\t\t\t\t\t"Purchase recorded. Taking you to the payment page...";
\t\t\t\t\twindow.location.replace(url);
\t\t\t\t} else {
\t\t\t\t\tfrappe.msgprint(
\t\t\t\t\t\t__("Your purchase was recorded, but the payment page could not be opened. " +
\t\t\t\t\t\t   "Please contact us and quote " + frappe.web_form.doc.name + ".")
\t\t\t\t\t);
\t\t\t\t}
\t\t\t}
\t\t});
\t};
});
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

