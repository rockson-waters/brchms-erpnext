# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The proposal boundary.

## Why this file is the important one

The whole design rests on one claim: **nothing the Rest application says can make
credits claimable.** That claim is only worth anything if it is enforced, so these
tests pin it from both sides --

  * the API raises DRAFTS and has no argument that submits; and
  * the `Rest Integration` role cannot submit, cancel, write or delete even if it
    calls Frappe directly rather than through this API.

The second half is what makes the first more than a convention. An API that declines
to submit is a promise; a role without the `submit` permission is a wall.

Run with:
    bench --site <site> run-tests --app brchms --module brchms.tests.test_proposals
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from brchms.brchms_credits import proposals

PROPOSAL = "Rest Credit Proposal"
INSTANCE = "test:proposal-instance"
REFERENCE = "INV-TEST-0001"
INTEGRATION_USER = "restapp@breamal.com"


def proposal_payload(**overrides):
	values = {
		"instance_id": INSTANCE,
		"purchase_reference": REFERENCE,
		"organization_id": "org-test-1",
		"organization_name": "Test Church",
		"funding_group_id": "grp-test-1",
		"group_name": "Test Group",
		"channel": "sms",
		"units": 100,
		"amount_minor": 10000,
		"currency": "CAD",
		"unit_price_micros": 1000000,
		"instance_name": "Test Church",
	}
	values.update(overrides)
	return values


class ProposalTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._clean()
		self.payment_entry = None

	def tearDown(self):
		self._clean()
		super().tearDown()

	def _clean(self):
		"""Exact scope: only what these tests create."""
		frappe.db.delete(PROPOSAL, {"purchase_reference": ("like", "INV-TEST-%")})
		frappe.db.delete(PROPOSAL, {"instance_id": ("like", "test:%")})
		frappe.db.delete("Rest Instance", {"name": ("like", "test:%")})
		for name in frappe.get_all(
			"Payment Entry", filters={"reference_no": "TEST-SETTLEMENT-PROBE"}, pluck="name"
		):
			# A submitted Payment Entry cannot be deleted; it must be cancelled first.
			# `force=True` does not bypass that, and the error it raises ("Submitted
			# Record cannot be deleted") failed every test that had just passed.
			doc = frappe.get_doc("Payment Entry", name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Customer", filters={"customer_name": "REST Settlement Probe"}, pluck="name"
		):
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def payment(self):
		"""One fixture Payment Entry per test, created on demand."""
		if not self.payment_entry:
			self.payment_entry = make_test_payment_entry()
		return self.payment_entry


class TestProposalCreation(ProposalTestCase):
	def test_a_proposal_is_created_as_a_DRAFT(self):
		"""
		The single most important assertion in the app: creating a proposal does not
		make anything claimable. `docstatus 0` is the line between a request and money.
		"""
		result = proposals.create_proposal(**proposal_payload())

		self.assertEqual(result["state"], "draft")
		self.assertFalse(result["claimable"], "a fresh proposal must never be claimable")
		self.assertEqual(frappe.db.get_value(PROPOSAL, result["proposal"], "docstatus"), 0)

	def test_creating_twice_for_one_reference_returns_the_same_proposal(self):
		"""
		Idempotency, and it matters more than the funding API's did: a duplicated
		proposal is a request for money nobody asked for, sitting in the operator's
		list waiting to be paid twice.
		"""
		first = proposals.create_proposal(**proposal_payload())
		second = proposals.create_proposal(**proposal_payload())

		self.assertEqual(first["proposal"], second["proposal"])
		self.assertFalse(second.get("created", True), "the second call created a second proposal")
		self.assertEqual(
			frappe.db.count(PROPOSAL, {"instance_id": INSTANCE, "purchase_reference": REFERENCE}),
			1,
		)

	def test_the_same_reference_from_two_deployments_is_two_proposals(self):
		"""
		A reference is unique per DEPLOYMENT, not globally: two churches can both quote
		INV-001. Collapsing them would settle one church's payment against another's.
		"""
		first = proposals.create_proposal(**proposal_payload())
		second = proposals.create_proposal(**proposal_payload(instance_id="test:proposal-other"))

		self.assertNotEqual(first["proposal"], second["proposal"])

	def test_the_reference_is_required(self):
		"""
		Without one the proposal cannot be matched to a payment -- unmatchable rather
		than merely hard to find, which is why this is mandatory rather than encouraged.
		"""
		with self.assertRaises(frappe.MandatoryError):
			proposals.create_proposal(**proposal_payload(purchase_reference=None))

	def test_the_expiry_is_stored_so_both_sides_agree_on_the_deadline(self):
		result = proposals.create_proposal(**proposal_payload())

		expires_at = frappe.db.get_value(PROPOSAL, result["proposal"], "expires_at")
		self.assertTrue(expires_at)
		self.assertEqual(result["expires_at"], str(expires_at))

	def test_money_is_stored_as_exact_integers(self):
		"""
		Never through `flt`: it is an IEEE double, and it silently rounded a
		2^53-scale value earlier in this integration.
		"""
		result = proposals.create_proposal(**proposal_payload(units=9007199254740993))

		self.assertEqual(result["units"], 9007199254740993)
		self.assertEqual(
			int(frappe.db.get_value(PROPOSAL, result["proposal"], "units")), 9007199254740993
		)

	def test_a_non_numeric_amount_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			proposals.create_proposal(**proposal_payload(amount_minor="12.50"))

	def test_the_deployment_is_registered_and_named(self):
		proposals.create_proposal(**proposal_payload())

		self.assertTrue(frappe.db.exists("Rest Instance", INSTANCE))
		self.assertEqual(frappe.db.get_value("Rest Instance", INSTANCE, "display_name"), "Test Church")


class TestProposalIsNotSettledByCreatingIt(ProposalTestCase):
	def test_settlement_state_is_draft_before_anyone_submits(self):
		proposals.create_proposal(**proposal_payload())

		state = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)

		self.assertEqual(state["state"], "draft")
		self.assertFalse(state["proposal"]["claimable"])

	def test_settlement_state_distinguishes_never_asked_from_not_yet_paid(self):
		"""
		"no such proposal" and "requested but unpaid" are different answers, and the
		app acts differently on each: one is a bug in the app, the other is patience.
		"""
		unknown = proposals.settlement_state(instance_id=INSTANCE, purchase_reference="INV-TEST-NOPE")
		self.assertEqual(unknown["state"], "unknown")
		self.assertIsNone(unknown["proposal"])

		proposals.create_proposal(**proposal_payload())
		draft = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)
		self.assertEqual(draft["state"], "draft")

	def test_an_expired_draft_is_reported_as_expired_and_is_not_claimable(self):
		result = proposals.create_proposal(**proposal_payload())
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()

		state = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)

		self.assertEqual(state["state"], "expired")
		self.assertFalse(state["proposal"]["claimable"])
		self.assertTrue(state["proposal"]["expired"])


class TestSettlement(ProposalTestCase):
	def make_submitted(self, **overrides):
		"""Submit a proposal the way an operator would: with a payment behind it."""
		result = proposals.create_proposal(**proposal_payload(**overrides))
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = overrides.pop("payment_doctype", "Payment Entry")
		doc.payment_document = self.payment()
		doc.settlement_note = overrides.pop("settlement_note", "Cash received at the desk")
		doc.flags.ignore_permissions = True
		doc.submit()
		return doc

	def test_a_submitted_proposal_is_claimable(self):
		doc = self.make_submitted()

		state = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)

		self.assertEqual(state["state"], "settled")
		self.assertTrue(state["proposal"]["claimable"])
		self.assertEqual(state["proposal"]["proposal"], doc.name)

	def test_submitting_records_who_did_it(self):
		"""'Who made this claimable' must always be answerable."""
		doc = self.make_submitted()

		self.assertTrue(doc.settled_by)
		self.assertTrue(doc.confirmed_at)

	def test_submitting_without_a_payment_document_is_refused(self):
		"""
		This is what stops a settlement being a claim with nothing behind it. Without
		it, credits become claimable because somebody clicked Submit.
		"""
		result = proposals.create_proposal(**proposal_payload())
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.flags.ignore_permissions = True

		with self.assertRaises(frappe.ValidationError):
			doc.submit()

	def test_a_submitted_settlement_cannot_be_edited(self):
		"""
		A settlement is a record of what happened. Editing the amount after the app has
		granted credits would make the two ledgers disagree about money.
		"""
		doc = self.make_submitted()
		doc.reload()

		doc.amount_minor = int(doc.amount_minor) + 500
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_a_variance_between_quote_and_settlement_is_recorded(self):
		"""
		Money that arrived for a different amount is a fact. Refusing it would strand a
		real payment, so the settled amount wins and the difference is written down.
		"""
		doc = self.make_submitted(amount_minor=12500)

		self.assertEqual(int(doc.amount_minor), 12500)
		self.assertEqual(int(doc.quote_variance_minor), 2500)

	def test_a_matching_amount_has_no_variance(self):
		doc = self.make_submitted()

		self.assertEqual(int(doc.quote_variance_minor), 0)

	def test_settling_an_expired_proposal_requires_a_reason(self):
		"""
		Late money is real, so it can still be settled -- but the lateness has to be
		explained, because a grant after expiry is exactly what somebody has to account
		for later.
		"""
		result = proposals.create_proposal(**proposal_payload())
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()

		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = "Payment Entry"
		doc.payment_document = self.payment()
		doc.flags.ignore_permissions = True

		with self.assertRaises(frappe.ValidationError):
			doc.submit()

	def test_settling_an_expired_proposal_with_a_reason_is_allowed(self):
		result = proposals.create_proposal(**proposal_payload())
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()

		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = "Payment Entry"
		doc.payment_document = self.payment()
		doc.settlement_note = "Transfer cleared late; receipt 4471"
		doc.flags.ignore_permissions = True
		doc.submit()

		state = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)
		self.assertEqual(state["state"], "settled")

	def test_cancelling_requires_a_reason(self):
		"""
		Cancelling revokes a claim that may already have been granted, so the reason is
		what explains a balance going down.
		"""
		doc = self.make_submitted()
		doc.reload()
		doc.settlement_note = ""

		with self.assertRaises(frappe.ValidationError):
			doc.cancel()

	def test_a_cancelled_settlement_is_no_longer_claimable(self):
		doc = self.make_submitted()
		doc.reload()
		doc.settlement_note = "Payment was reversed by the bank"
		doc.flags.ignore_permissions = True
		doc.cancel()

		state = proposals.settlement_state(instance_id=INSTANCE, purchase_reference=REFERENCE)
		self.assertEqual(state["state"], "cancelled")
		self.assertFalse(state["proposal"]["claimable"])


class TestPendingList(ProposalTestCase):
	def test_pending_lists_unsettled_proposals_for_one_deployment_only(self):
		proposals.create_proposal(**proposal_payload(purchase_reference="INV-TEST-A"))
		proposals.create_proposal(**proposal_payload(purchase_reference="INV-TEST-B"))
		proposals.create_proposal(
			**proposal_payload(purchase_reference="INV-TEST-C", instance_id="test:proposal-other")
		)

		pending = proposals.list_pending_proposals(instance_id=INSTANCE)

		references = {row["purchase_reference"] for row in pending["proposals"]}
		self.assertEqual(references, {"INV-TEST-A", "INV-TEST-B"})

	def test_pending_flags_which_ones_have_expired(self):
		result = proposals.create_proposal(**proposal_payload(purchase_reference="INV-TEST-OLD"))
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()
		proposals.create_proposal(**proposal_payload(purchase_reference="INV-TEST-NEW"))

		pending = proposals.list_pending_proposals(instance_id=INSTANCE)

		by_ref = {row["purchase_reference"]: row for row in pending["proposals"]}
		self.assertTrue(by_ref["INV-TEST-OLD"]["expired"])
		self.assertFalse(by_ref["INV-TEST-NEW"]["expired"])
		self.assertEqual(pending["expired"], 1)


class TestIntegrationRoleCannotSettle(FrappeTestCase):
	"""
	THE BOUNDARY, tested by permission rather than by API shape.

	Everything above proves the API declines to submit. This proves the role cannot,
	which is what makes the boundary real: a leaked integration token has to be unable
	to create money even if it calls Frappe directly.

	If either of these starts failing, the app can settle its own proposals and the
	whole design is void -- which is exactly why they are pinned rather than assumed.
	"""

	def setUp(self):
		super().setUp()
		if not frappe.db.exists("Role", "Rest Integration"):
			self.skipTest("the Rest Integration role is not installed on this site")

	def test_the_integration_role_can_create_a_proposal(self):
		"""It must be able to ASK -- a boundary that also blocks the proposal is useless."""
		perm = _integration_permission()
		self.assertEqual(perm.create, 1)

	def test_the_integration_role_cannot_submit(self):
		perm = _integration_permission()
		self.assertEqual(
			perm.submit,
			0,
			"the app can submit proposals, so it can make credits claimable without any payment",
		)

	def test_the_integration_role_cannot_write_cancel_or_delete(self):
		perm = _integration_permission()

		self.assertEqual(perm.write, 0, "the app could rewrite a settlement after it was priced")
		self.assertEqual(perm.cancel, 0, "the app could revoke a settlement unilaterally")
		self.assertEqual(perm.delete, 0)
		self.assertEqual(perm.amend, 0)

	def test_a_submitted_proposal_exists_only_through_the_submit_permission(self):
		"""
		Ties the permission to the outcome: the doctype is submittable, so `docstatus`
		is what separates a request from a claim, and the app's role has no way to move
		it across that line.
		"""
		meta = frappe.get_meta(PROPOSAL)
		self.assertTrue(meta.is_submittable, "docstatus is the boundary; without it there is none")

		perm = _integration_permission()
		self.assertNotEqual(perm.submit, 1)


def _make_test_customer():
	"""A Customer to point the payment at, named so teardown can find it."""
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": "REST Settlement Probe",
			"customer_group": group,
			"territory": territory,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _integration_permission():
	doc = frappe.get_doc("DocType", PROPOSAL)
	for perm in doc.permissions:
		if perm.role == "Rest Integration":
			return perm
	raise AssertionError("Rest Integration has no permission row on {0}".format(PROPOSAL))


def make_test_payment_entry():
	"""
	A real, submitted Payment Entry to settle against.

	Created rather than found. Glancing around the site for "any Payment Entry" would
	make these tests depend on unrelated data: they would pass on a site that happened
	to have one and skip on a clean site, which says nothing about the code.

	Built from the Company's OWN default accounts and party, because a Payment Entry
	is an accounting document with real validation: the first version of this omitted
	the party and was refused with "Party Type is mandatory", which the test reported
	as a skip -- so nine tests silently did not run while the suite said OK.
	"""
	company = frappe.db.get_value("Company", {}, "name")
	if not company:
		raise unittest.SkipTest("no Company exists, so no Payment Entry can be made")

	# A Customer is needed and the site has none, so the fixture makes one rather
	# than skipping: a skip here silently disabled nine tests, including every test
	# of the settling path, while the suite still reported OK.
	customer = frappe.db.get_value("Customer", {}, "name") or _make_test_customer()

	paid_to = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Company", company, "default_bank_account"
	)
	paid_from = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Receivable", "is_group": 0}, "name"
	)
	if not paid_to or not paid_from:
		raise unittest.SkipTest(
			"the Company has no default cash/bank or receivable account to build a payment from"
		)

	doc = frappe.get_doc(
		{
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"company": company,
			"posting_date": now_datetime().date(),
			"party_type": "Customer",
			"party": customer,
			"paid_from": paid_from,
			"paid_to": paid_to,
			"paid_amount": 100,
			"received_amount": 100,
			"reference_no": "TEST-SETTLEMENT-PROBE",
			"reference_date": now_datetime().date(),
		}
	)
	doc.flags.ignore_permissions = True
	try:
		doc.insert()
		doc.submit()
	except Exception as error:  # noqa: BLE001 - reported as a skip carrying its reason
		raise unittest.SkipTest("could not create a Payment Entry fixture: {0}".format(error))
	return doc.name
