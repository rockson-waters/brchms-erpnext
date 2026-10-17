# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The buyer-identity registry: which Rest organization and funding group a
purchase made without an app session belongs to.

## The gap this closes

A public form buyer is a stranger with no account here, and a purchase that
cannot be attributed to an organization and a funding group cannot become
credits: the app grants units to a *group*, so a form that recorded only "a
buyer paid" produced money on the books with nowhere to put the credits it
bought. Registering the organizations (and their funding groups) that may buy
means the form's submission can name its target without the buyer knowing a
single internal id -- which is what makes attribution possible without asking
a customer to identify their church in ERPNext's vocabulary.

## The mapping, and why group-as-Customer

An organization maps to a **Customer Group** and each of its funding groups to
a **Customer** under it:

    Rest (root, once)
      └─ Main Church                     <- organization = Customer Group
           ├─ Main Church - Mother Church <- funding group = Customer
           └─ Youth Choir                 <- funding group = Customer

Credits belong to the group that pays for them, so the invoice's billing party
is the *group's* Customer -- a per-group statement is then a per-group
purchase history, which is what an operator reconciling payments against
credits actually reads. The organization's Customer Group is the roll-up.

## The link slug

Each registered organization owns an opaque slug (`?org=<slug>` on the
purchase form). The slug is the row's NAME, so it is unique by construction
and an attributed link can never resolve to two organizations. Opaque on
purpose: a church's name in a URL is both guessable and a disclosure, and
neither buys anything -- a forged attribution means paying real money into
another church's account, which the payment itself still has to back.

## Idempotency, and who writes these rows

Every entry point is a registration: asking for the purchase link, raising a
proposal, or a form checkout all converge here, and every one of them may be
the FIRST call a deployment ever makes. So each function finds-or-creates and
never assumes a prior step. Rows are written with `ignore_permissions` because
the whitelisted methods that call this module are the authorisation boundary
-- the same posture as the credits API, which documents why the
integration role stays read-only on the doctypes themselves.
"""

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

REGISTRY_DOCTYPE = "Rest Credit Organization"
GROUP_CHILD_DOCTYPE = "Rest Credit Organization Group"
INSTANCE_DOCTYPE = "Rest Instance"

# The once-created root under which every registered organization's Customer
# Group lives. A flat namespace under one root keeps the Customer Group tree
# readable: an operator sees "Rest" and knows everything below it came from
# the credits flow, rather than credit customers being scattered among real
# customer groups.
REST_ROOT_GROUP = "Rest"

# Bounded, because an unbounded retry against a broken constraint would spin.
SLUG_ATTEMPTS = 5


def ensure_registration(
	instance_id=None,
	organization_id=None,
	organization_name=None,
	default_group_id=None,
	default_group_name=None,
):
	"""
	The registry row for (deployment, organization), created or refreshed.

	Returns the document. Safe to call on every purchase-link read and every
	proposal raise: an existing row is updated in place (a rename in the app
	travels on the next call) and never duplicated.

	`default_group_*` is only filled, never cleared: a call that names no
	default group (a proposal raise naming some other group) must not wipe the
	one the purchase-link read registered.
	"""
	instance_id = cstr(instance_id).strip()
	organization_id = cstr(organization_id).strip()
	if not instance_id or not organization_id:
		frappe.throw(
			_("An organization must be registered with its deployment's instance id and its own organization id."),
			frappe.MandatoryError,
		)

	# The row's `instance_id` is a Link, so the deployment must exist before it
	# does -- and a first-ever purchase-link read IS first contact. Auto-created
	# here rather than requiring a prior step, exactly as the proposal path's
	# `_touch_instance` already does: a deployment that can pay for credits is a
	# deployment this site should be able to name.
	_ensure_instance_row(instance_id, display_name=organization_name)

	doc = _find_row(instance_id, organization_id)

	if not doc:
		doc = frappe.get_doc(
			{
				"doctype": REGISTRY_DOCTYPE,
				# The slug is the name, so it must exist before the insert. Its
				# uniqueness is the document name's uniqueness -- no separate
				# constraint to forget.
				"link_slug": _mint_slug(),
				"instance_id": instance_id,
				"organization_id": organization_id,
				"organization_name": cstr(organization_name).strip() or None,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		frappe.db.commit()

	updates = {}
	if organization_name and cstr(doc.organization_name).strip() != cstr(organization_name).strip():
		updates["organization_name"] = cstr(organization_name).strip()
	if default_group_id and not cstr(doc.default_group_id).strip():
		updates["default_group_id"] = cstr(default_group_id).strip()
		if default_group_name:
			updates["default_group_name"] = cstr(default_group_name).strip()

	changed = bool(updates)
	if updates:
		doc.update(updates)

	if not doc.customer_group:
		doc.customer_group = _ensure_org_customer_group(doc)
		changed = True

	if changed:
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	return doc


def ensure_group(
	instance_id=None,
	organization_id=None,
	funding_group_id=None,
	group_name=None,
	organization_name=None,
):
	"""
	The Customer a funding group's invoices are billed to, created once.

	Called from the proposal path, which is why it registers the organization
	first: a deployment's very first purchase may happen before anyone ever
	opened the purchase-link read, and requiring a prior registration step an
	operator has to remember is exactly the kind of sequencing this app has
	been removing.

	Returns the Customer's name. Never the group's display name -- two
	organizations may have groups with the same name, and matching on names
	would bill one church's group as another's.
	"""
	funding_group_id = cstr(funding_group_id).strip()
	if not funding_group_id:
		frappe.throw(
			_("A funding group must be named before its credits can be billed."),
			frappe.MandatoryError,
		)

	row = ensure_registration(
		instance_id=instance_id,
		organization_id=organization_id,
		organization_name=organization_name,
	)

	existing = _child(row, funding_group_id)
	customer = cstr(existing.get("customer")).strip() if existing else ""

	if customer and frappe.db.exists("Customer", customer):
		# A rename in the app travels like the organization's does.
		if group_name and cstr(existing.get("group_name")).strip() != cstr(group_name).strip():
			existing.group_name = cstr(group_name).strip()
			row.save(ignore_permissions=True)
			frappe.db.commit()
		return customer

	# No Customer (first purchase for this group), or the desk deleted it --
	# the row is the identity, so the fix is a fresh Customer, not a guess.
	customer = _ensure_group_customer(row, funding_group_id, group_name)

	if existing:
		existing.customer = customer
	else:
		row.append(
			"groups",
			{
				"funding_group_id": funding_group_id,
				"group_name": cstr(group_name).strip() or funding_group_id,
				"customer": customer,
			},
		)
	row.save(ignore_permissions=True)
	frappe.db.commit()

	return customer


def resolve(slug=None):
	"""
	The registry row an attributed link names, or None.

	By the document's NAME, which is the slug -- so this can never match two
	rows, and a stale or mistyped link reads as "unknown" rather than as
	somebody else's church.
	"""
	slug = cstr(slug).strip()
	if not slug:
		return None
	if not frappe.db.exists(REGISTRY_DOCTYPE, slug):
		return None
	return frappe.get_doc(REGISTRY_DOCTYPE, slug)


def customer_for(instance_id=None, funding_group_id=None):
	"""
	The Customer a proposal's invoices are billed to, or None.

	The child rows of every registered organization are searched and the parent
	checked, because funding group ids are per-deployment: another deployment
	group with the same id must not answer. None (rather than a fallback)
	because a proposal whose group was never registered is a defect to surface,
	not a guess to bill.
	"""
	instance_id = cstr(instance_id).strip()
	funding_group_id = cstr(funding_group_id).strip()
	if not instance_id or not funding_group_id:
		return None

	children = frappe.get_all(
		GROUP_CHILD_DOCTYPE,
		filters={"funding_group_id": funding_group_id},
		fields=["parent", "customer"],
		limit_page_length=0,
	)

	for child in children:
		parent_instance = frappe.db.get_value(REGISTRY_DOCTYPE, child.parent, "instance_id")
		if cstr(parent_instance).strip() == instance_id:
			return cstr(child.customer).strip() or None

	return None


# ── internals ────────────────────────────────────────────────────────────────


def _ensure_instance_row(instance_id, display_name=None):
	"""
	The deployment's own row, created on first contact.

	Minimal on purpose: the proposal path's `_touch_instance` keeps the display
	name and activity current; this only guarantees the Link resolves, because
	that is all the registry needs from it.
	"""
	if frappe.db.exists(INSTANCE_DOCTYPE, instance_id):
		return

	doc = frappe.get_doc(
		{
			"doctype": INSTANCE_DOCTYPE,
			"instance_id": instance_id,
			"display_name": cstr(display_name).strip() or instance_id,
			"erpnext_site": frappe.local.site if hasattr(frappe, "local") else None,
			"is_active": 1,
			"first_seen_at": now_datetime(),
			"last_seen_at": now_datetime(),
			"confirmed_count": 0,
		}
	)
	doc.flags.ignore_permissions = True
	try:
		doc.insert()
	except frappe.UniqueValidationError:
		pass


def _find_row(instance_id, organization_id):
	name = frappe.db.get_value(
		REGISTRY_DOCTYPE,
		{"instance_id": instance_id, "organization_id": organization_id},
		"name",
	)
	return frappe.get_doc(REGISTRY_DOCTYPE, name) if name else None


def _child(row, funding_group_id):
	for child in row.groups or []:
		if cstr(child.funding_group_id).strip() == funding_group_id:
			return child
	return None


def _mint_slug():
	"""
	An opaque, URL-safe token for an attributed link.

	The database decides uniqueness, not a pre-check: a pre-check races. Bounded
	retries against the name constraint, and a refusal (never a quiet fallback
	to a guessable name) if the constraint is somehow never satisfied.
	"""
	for _attempt in range(SLUG_ATTEMPTS):
		candidate = cstr(frappe.generate_hash(length=12)).strip()
		if candidate and not frappe.db.exists(REGISTRY_DOCTYPE, candidate):
			return candidate

	frappe.throw(
		_("Could not allocate a purchase-link slug after {0} attempts.").format(SLUG_ATTEMPTS),
		frappe.ValidationError,
	)


def _ensure_rest_root_group():
	"""The once-created `Rest` root under which every org's Customer Group lives."""
	if frappe.db.exists("Customer Group", REST_ROOT_GROUP):
		return REST_ROOT_GROUP

	# A tree doctype's root: a group with no parent. `parent_customer_group`
	# (the Customer Group tree's own fieldname, not a generic `parent`) is ""
	# or NULL depending on how the root was seeded, so both spellings are
	# tried rather than one guessed.
	parent = frappe.db.get_value(
		"Customer Group", {"is_group": 1, "parent_customer_group": ""}, "name"
	) or frappe.db.get_value(
		"Customer Group", {"is_group": 1, "parent_customer_group": ("is", "not set")}, "name"
	)

	doc = frappe.get_doc(
		{
			"doctype": "Customer Group",
			"customer_group_name": REST_ROOT_GROUP,
			"parent_customer_group": parent or "",
			"is_group": 1,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	return doc.name


def _ensure_org_customer_group(row):
	"""
	The organization's Customer Group, created once and named recognisably.

	Name collisions are disambiguated with the deployment's instance id, never
	silently reused: a Customer Group created by somebody else is somebody
	else's, and billing into it would fold one church's purchases into another's
	statements.
	"""
	candidate = cstr(row.organization_name).strip() or cstr(row.organization_id).strip()

	if not candidate or not frappe.db.exists("Customer Group", candidate):
		name = candidate
	elif _group_is_ours(candidate, row):
		return candidate
	else:
		name = "{0} ({1})".format(candidate, cstr(row.instance_id).strip())
		if frappe.db.exists("Customer Group", name) and not _group_is_ours(name, row):
			name = "{0} ({1})".format(name, cstr(frappe.generate_hash(length=6)))

	doc = frappe.get_doc(
		{
			"doctype": "Customer Group",
			"customer_group_name": name,
			"parent_customer_group": _ensure_rest_root_group(),
			"is_group": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	return doc.name


def _group_is_ours(group_name, row):
	"""
	True only when THIS row already bills under the group.

	Not "some registry row references it": two deployments may have
	organizations with the same name, and treating the first one's Customer
	Group as shared would fold the second church's purchases into the first's
	statements -- the exact failure the suffix exists to prevent. A group no
	registry row references is an operator's, and hijacking it would be just
	as wrong.
	"""
	return cstr(row.customer_group).strip() == cstr(group_name).strip()


def _ensure_group_customer(row, funding_group_id, group_name):
	"""
	The Customer for one funding group.

	Grouped under the organization's Customer Group (the roll-up) with a leaf
	territory, mirroring the buyer-customer logic this replaces: `Selling
	Settings` defaults first and a LEAF fallback, because defaulting to a tree
	root is refused by ERPNext with an error a buyer would see as a broken
	purchase page.
	"""
	territory = frappe.db.get_single_value("Selling Settings", "territory") or _leaf("Territory")

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": cstr(group_name).strip() or cstr(funding_group_id).strip(),
			"customer_type": "Individual",
			"customer_group": row.customer_group,
			"territory": territory,
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert()
	frappe.db.commit()
	return customer.name


def _leaf(doctype):
	"""
	A non-group row of a tree doctype, for use where a leaf is required.

	None when the tree has no leaves at all, so the doctype's own validation
	reports the site's problem rather than this module inventing a node to
	silence it.
	"""
	return frappe.db.get_value(doctype, {"is_group": 0}, "name")
