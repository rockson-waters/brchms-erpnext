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
	if ensure_purchase_reference_field():
		created.append("Sales Invoice.purchase_reference")
	if ensure_purchase_reference_index():
		created.append("index on purchase_reference")
	created.extend(ensure_credit_items())

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
