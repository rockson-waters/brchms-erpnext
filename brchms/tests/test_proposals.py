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
REQUEST_KEY = "test-request-0001"
INTEGRATION_USER = "restapp@breamal.com"


def proposal_payload(**overrides):
	values = {
		"instance_id": INSTANCE,
		# The deployment's own purchase id. This is the idempotency key, and it is what
		# a retry must resolve to -- not the customer's reference, which is optional.
		"request_key": REQUEST_KEY,
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
		frappe.db.delete(PROPOSAL, {"request_key": ("like", "test-request-%")})
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
			"Sales Invoice",
			filters={"customer": ("like", "REST Settlement Probe%")},
			pluck="name",
		):
			invoice = frappe.get_doc("Sales Invoice", name)
			if invoice.docstatus == 1:
				invoice.flags.ignore_permissions = True
				invoice.cancel()
			frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Payment Request",
			filters={"reference_name": ("like", "REST Settlement Probe%")},
			pluck="name",
		):
			frappe.delete_doc("Payment Request", name, force=True, ignore_permissions=True)
		for name in frappe.get_all(
			"Customer", filters={"customer_name": ("like", "REST Settlement Probe%")}, pluck="name"
		):
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def payment(self):
		"""
		One fixture Payment Entry per TEST, created on demand.

		One per test, not per call, and not shared across tests: a settlement links to
		this document, so it has to outlive every settlement the test makes while
		teardown removes it before the next test starts.
		"""
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

	def test_a_proposal_is_never_left_without_a_reference(self):
		"""
		A reference is ALWAYS present, because it is what a payment is matched against
		and it is unique. The deployment sends the payer's own when there is one; when
		there is none, one is minted. A proposal with no reference is unmatchable -- not
		hard to find, impossible.
		"""
		result = proposals.create_proposal(**proposal_payload(purchase_reference=None))

		self.assertEqual(result["state"], "draft")
		self.assertTrue(result["purchase_reference"])

	def test_the_request_key_is_what_makes_a_retry_idempotent(self):
		"""
		Not the reference: that is optional and two purchases may share one, so keying on
		it would merge distinct requests or raise duplicates for one.
		"""
		first = proposals.create_proposal(**proposal_payload(purchase_reference=None))
		second = proposals.create_proposal(**proposal_payload(purchase_reference=None))

		self.assertEqual(first["proposal"], second["proposal"])
		self.assertEqual(first["request_key"], REQUEST_KEY)

	def test_the_request_key_is_required(self):
		# Without it there is no key at all, so a retry would raise a second request
		# for money nobody asked for.
		with self.assertRaises(frappe.MandatoryError):
			proposals.create_proposal(**proposal_payload(request_key=None))

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

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)

		self.assertEqual(state["state"], "draft")
		self.assertFalse(state["proposal"]["claimable"])

	def test_settlement_state_distinguishes_never_asked_from_not_yet_paid(self):
		"""
		"no such proposal" and "requested but unpaid" are different answers, and the
		app acts differently on each: one is a bug in the app, the other is patience.
		"""
		unknown = proposals.settlement_state(instance_id=INSTANCE, request_key="test-request-nope")
		self.assertEqual(unknown["state"], "unknown")
		self.assertIsNone(unknown["proposal"])

		proposals.create_proposal(**proposal_payload())
		draft = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)
		self.assertEqual(draft["state"], "draft")

	def test_an_expired_draft_is_reported_as_expired_and_is_not_claimable(self):
		result = proposals.create_proposal(**proposal_payload())
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)

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
		doc.payment_reference = overrides.pop("payment_reference", "RECEIPT-TEST-0001")
		doc.flags.ignore_permissions = True
		doc.submit()
		return doc

	def test_a_submitted_proposal_is_claimable(self):
		doc = self.make_submitted()

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)

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
		# A hand-filed settlement needs the reference the payment carries. This test is
		# about the EXPIRY rule, but it has to satisfy the reference rule too -- which
		# is the point of the reference rule: no manual settlement escapes it.
		doc.payment_reference = "RECEIPT-4471"
		doc.flags.ignore_permissions = True
		doc.submit()

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)
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

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)
		self.assertEqual(state["state"], "cancelled")
		self.assertFalse(state["proposal"]["claimable"])


class TestPendingList(ProposalTestCase):
	def test_pending_lists_unsettled_proposals_for_one_deployment_only(self):
		proposals.create_proposal(
			**proposal_payload(request_key="test-request-a", purchase_reference="INV-TEST-A")
		)
		proposals.create_proposal(
			**proposal_payload(request_key="test-request-b", purchase_reference="INV-TEST-B")
		)
		proposals.create_proposal(
			**proposal_payload(
				request_key="test-request-c",
				purchase_reference="INV-TEST-C",
				instance_id="test:proposal-other",
			)
		)

		pending = proposals.list_pending_proposals(instance_id=INSTANCE)

		references = {row["purchase_reference"] for row in pending["proposals"]}
		self.assertEqual(references, {"INV-TEST-A", "INV-TEST-B"})

	def test_pending_flags_which_ones_have_expired(self):
		result = proposals.create_proposal(
			**proposal_payload(request_key="test-request-old", purchase_reference="INV-TEST-OLD")
		)
		frappe.db.set_value(PROPOSAL, result["proposal"], "expires_at", add_days(now_datetime(), -1))
		frappe.db.commit()
		proposals.create_proposal(
			**proposal_payload(request_key="test-request-new", purchase_reference="INV-TEST-NEW")
		)

		pending = proposals.list_pending_proposals(instance_id=INSTANCE)

		by_ref = {row["purchase_reference"]: row for row in pending["proposals"]}
		self.assertTrue(by_ref["INV-TEST-OLD"]["expired"])
		self.assertFalse(by_ref["INV-TEST-NEW"]["expired"])
		self.assertEqual(pending["expired"], 1)


class TestTheThreeWaysMoneyArrives(ProposalTestCase):
	"""
	Cash, a bank transfer, and a payment gateway leave different traces, and the
	proposal has to serve all three. The first version served only the middle one.

	  * a GATEWAY supplies its own transaction id, and the linked payment document is
	    the evidence -- so no hand-entered reference is needed on top;
	  * a BANK TRANSFER has the bank's reference on the statement;
	  * CASH has nothing except the receipt issued at the desk.

	For the last two, `payment_reference` is the only trace that money moved.
	"""

	def _settle(self, **fields):
		result = proposals.create_proposal(**proposal_payload())
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = fields.pop("payment_doctype", "Payment Entry")
		doc.payment_document = self.payment()
		for key, value in fields.items():
			doc.set(key, value)
		doc.flags.ignore_permissions = True
		doc.submit()
		return doc

	def test_cash_needs_the_receipt_number_at_the_desk(self):
		"""Nothing else vouches for a payment taken by hand, so the receipt is it."""
		doc = self._settle(payment_reference="RECEIPT-4471")

		self.assertEqual(doc.payment_reference, "RECEIPT-4471")
		self.assertEqual(doc.docstatus, 1)

	def test_cash_without_a_receipt_number_is_refused(self):
		result = proposals.create_proposal(**proposal_payload())
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = "Payment Entry"
		doc.payment_document = self.payment()
		doc.flags.ignore_permissions = True

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.submit()

		self.assertIn("receipt number", str(caught.exception))

	def test_a_bank_transfer_records_the_banks_own_reference(self):
		doc = self._settle(payment_reference="GCB-TRF-9920-1145")

		self.assertEqual(doc.payment_reference, "GCB-TRF-9920-1145")

	def test_a_gateway_settlement_needs_no_hand_entered_reference(self):
		"""
		A gateway already identifies the transaction and the linked document is the
		evidence. Demanding a reference on top would be asking an operator to retype
		something the system already knows -- which is how wrong numbers get in.
		"""
		result = proposals.create_proposal(**proposal_payload())
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = "Payment Request"
		doc.payment_document = make_test_payment_request()
		doc.flags.ignore_permissions = True
		doc.submit()

		self.assertEqual(doc.docstatus, 1)
		self.assertFalse(doc.payment_reference)
		self.assertEqual(doc.payment_doctype, "Payment Request")


class TestAwaitingPayment(ProposalTestCase):
	"""
	`awaiting_payment` is DERIVED from a payment link, not stored as a status.

	Every stored status is a branch that has to be handled everywhere it is read; a
	derived one is handled in the single place that computes it. The gateway path and
	the cash path converge on the same transition, which is what makes one field
	enough.
	"""

	def test_a_draft_with_a_payment_link_is_awaiting_payment(self):
		result = proposals.create_proposal(**proposal_payload())
		frappe.db.set_value(
			PROPOSAL, result["proposal"], "payment_url", "https://pay.example.test/abc123"
		)
		frappe.db.commit()

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)

		self.assertEqual(state["state"], "draft")
		self.assertTrue(state["proposal"]["awaiting_payment"])
		self.assertFalse(state["proposal"]["claimable"])
		self.assertEqual(
			state["proposal"]["payment_url"], "https://pay.example.test/abc123"
		)

	def test_a_draft_with_no_payment_link_is_merely_requested(self):
		proposals.create_proposal(**proposal_payload())

		state = proposals.settlement_state(instance_id=INSTANCE, request_key=REQUEST_KEY)

		self.assertFalse(state["proposal"]["awaiting_payment"])
		self.assertIsNone(state["proposal"]["payment_url"])

	def test_the_payment_url_is_not_writable_by_an_ordinary_save(self):
		"""
		The app may create the link, but the proposal is not a place a tenant
		application gets to edit after the fact.
		"""
		field = frappe.get_meta(PROPOSAL).get_field("payment_url")

		self.assertTrue(field.read_only)


class TestFindingTheRightProposal(ProposalTestCase):
	"""
	The operator's search, which is what replaced asking the customer for a code.

	The proposal code is deliberately not shown to customers, so the operator works
	with what the customer brings: their own reference, the group, and the amount that
	arrived.
	"""

	def test_finds_by_the_reference(self):
		proposals.create_proposal(**proposal_payload(purchase_reference="INV-TEST-7777"))

		found = proposals.find_pending_proposal(
			instance_id=INSTANCE, purchase_reference="INV-TEST-7777"
		)

		self.assertEqual(found["proposal"]["request_key"], REQUEST_KEY)
		self.assertIsNone(found["reason"])

	def test_a_cash_proposal_is_given_a_reference_rather_than_asking_for_one(self):
		"""
		The case the first design could not serve at all. A customer paying cash has
		nothing to quote and should not be asked to invent something, so the app MINTS
		a reference and hands it over -- which is also what makes the lookup unique.
		"""
		result = proposals.create_proposal(
			**proposal_payload(purchase_reference=None, group_name="Cash Group", amount_minor=10000)
		)

		self.assertTrue(result["purchase_reference"], "a reference must always be present")
		self.assertRegex(result["purchase_reference"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")

		found = proposals.find_pending_proposal(
			instance_id=INSTANCE, purchase_reference=result["purchase_reference"]
		)
		self.assertEqual(found["proposal"]["proposal"], result["proposal"])

	def test_a_minted_reference_never_uses_a_confusable_character(self):
		"""
		I, O, 0 and 1 are excluded. A reference is read off a slip and typed at a desk,
		and a confusable character is how a reference stops matching.
		"""
		for _ in range(20):
			self.assertNotRegex(proposals.mint_reference(), r"[IO01]")

	def test_minted_references_are_unique_in_practice(self):
		minted = {proposals.mint_reference() for _ in range(200)}
		self.assertEqual(len(minted), 200)

	def test_a_reference_cannot_be_shared_by_two_proposals(self):
		"""
		THE POINT OF THE UNIQUE INDEX. Two proposals sharing a reference would make the
		operator's lookup ambiguous, and ambiguity here means settling one request
		against another group's payment. It is made impossible rather than reported.
		"""
		proposals.create_proposal(
			**proposal_payload(request_key="test-request-a", purchase_reference="INV-TEST-DUP")
		)
		second = proposals.create_proposal(
			**proposal_payload(request_key="test-request-b", purchase_reference="INV-TEST-DUP")
		)

		# The second is the FIRST proposal: the reference identifies it, so the request
		# resolves to the proposal already carrying that reference rather than raising
		# a duplicate of it.
		self.assertFalse(second.get("created", True))
		self.assertEqual(
			frappe.db.count(PROPOSAL, {"instance_id": INSTANCE, "purchase_reference": "INV-TEST-DUP"}),
			1,
		)

	def test_a_group_and_amount_match_still_refuses_to_guess(self):
		"""
		Group and amount are not unique, so this search CAN match more than one. It
		refuses rather than picking the most recent, which would be right most of the
		time and silently wrong the rest -- and settling the wrong one attributes a
		payment to another group's request.

		The two proposals need DIFFERENT request keys: sharing one makes the second
		resolve to the first, leaving only one pending and nothing to be ambiguous
		about. That is what made the first version of this test pass a guard it never
		reached.
		"""
		proposals.create_proposal(
			**proposal_payload(
				request_key="test-request-a",
				purchase_reference="INV-TEST-AMB-A",
				group_name="Cash Group",
				amount_minor=10000,
			)
		)
		proposals.create_proposal(
			**proposal_payload(
				request_key="test-request-b",
				purchase_reference="INV-TEST-AMB-B",
				group_name="Cash Group",
				amount_minor=10000,
			)
		)

		pending = proposals.list_pending_proposals(instance_id=INSTANCE)
		self.assertEqual(pending["pending"], 2, "the fixture did not leave two pending proposals")

		with self.assertRaises(frappe.ValidationError) as caught:
			proposals.find_pending_proposal(
				instance_id=INSTANCE, group_name="Cash Group", amount_minor=10000
			)

		self.assertIn("More than one pending proposal", str(caught.exception))

	def test_an_unknown_reference_says_so_rather_than_refusing(self):
		# "nothing carries that reference" and "more than one matched" are different
		# answers; the first is the operator's typo, the second needs a decision.
		found = proposals.find_pending_proposal(
			instance_id=INSTANCE, purchase_reference="NOSUCH-REF"
		)

		self.assertIsNone(found["proposal"])
		self.assertIn("no proposal", found["reason"])

	def test_a_settled_proposal_is_not_a_candidate(self):
		"""It has already been paid for; offering it again invites paying twice."""
		result = proposals.create_proposal(**proposal_payload())
		doc = frappe.get_doc(PROPOSAL, result["proposal"])
		doc.payment_doctype = "Payment Entry"
		doc.payment_document = self.payment()
		doc.payment_reference = "RECEIPT-9001"
		doc.flags.ignore_permissions = True
		doc.submit()

		found = proposals.find_pending_proposal(instance_id=INSTANCE, amount_minor=10000)

		self.assertIsNone(found["proposal"])

	def test_another_deployments_proposals_are_never_candidates(self):
		"""One tenant must not be able to find, or settle, another's request."""
		proposals.create_proposal(
			**proposal_payload(instance_id="test:proposal-other", request_key="test-request-x")
		)

		found = proposals.find_pending_proposal(instance_id=INSTANCE)

		self.assertIsNone(found["proposal"])


class TestPurchaseLink(FrappeTestCase):
	"""
	Where a customer goes to buy credits.

	The app renders this URL and does nothing else with it: the gateway, the webform
	and the settlement all live in ERPNext, which is why switching a gateway on changes
	nothing about who vouches for money.
	"""

	def setUp(self):
		super().setUp()
		self._restore = frappe.db.get_singles_dict("Rest Credit Purchase Link") or {}
		frappe.db.delete("Singles", {"doctype": "Rest Credit Purchase Link"})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Singles", {"doctype": "Rest Credit Purchase Link"})
		for key, value in self._restore.items():
			frappe.db.set_single_value("Rest Credit Purchase Link", key, value)
		frappe.db.commit()
		super().tearDown()

	def configure(self, **values):
		doc = frappe.get_single("Rest Credit Purchase Link")
		for key, value in values.items():
			doc.set(key, value)
		doc.flags.ignore_permissions = True
		doc.save()
		return doc

	def test_reports_disabled_rather_than_failing_when_nothing_is_set(self):
		"""
		"No purchase button" is a decision, not a fault. The app must be able to tell
		the difference between a link that is switched off and a lookup that broke.
		"""
		result = proposals.get_purchase_link()

		self.assertFalse(result["enabled"])
		self.assertIn("reason", result)

	def test_a_configured_url_is_returned_with_its_label(self):
		self.configure(
			is_enabled=1,
			purchase_url="https://pay.example.test/buy",
			link_label="Top up credits",
			help_text="Pay by card or mobile money.",
		)

		result = proposals.get_purchase_link()

		self.assertTrue(result["enabled"])
		self.assertEqual(result["url"], "https://pay.example.test/buy")
		self.assertEqual(result["label"], "Top up credits")
		self.assertEqual(result["help_text"], "Pay by card or mobile money.")
		self.assertFalse(result["identifies_purchase"])

	def test_the_reference_is_appended_when_a_parameter_is_set(self):
		"""
		The difference between a link and a handoff. Without it the customer carries the
		reference across by hand -- and a reference that has to be retyped is a reference
		that stops matching.
		"""
		self.configure(
			is_enabled=1,
			purchase_url="https://pay.example.test/buy",
			reference_param="ref",
		)

		result = proposals.get_purchase_link(purchase_reference="AB12-CD34")

		self.assertEqual(result["url"], "https://pay.example.test/buy?ref=AB12-CD34")
		self.assertTrue(result["identifies_purchase"])
		self.assertEqual(result["reference"], "AB12-CD34")

	def test_an_existing_query_string_is_respected(self):
		# A URL may already carry parameters; a second `?` would break it.
		self.configure(
			is_enabled=1,
			purchase_url="https://pay.example.test/buy?tenant=stjohns",
			reference_param="ref",
		)

		result = proposals.get_purchase_link(purchase_reference="AB12-CD34")

		self.assertEqual(result["url"], "https://pay.example.test/buy?tenant=stjohns&ref=AB12-CD34")

	def test_the_reference_is_url_encoded(self):
		# A reference is data. A character that changes the query string's meaning would
		# silently send the webform the wrong thing.
		self.configure(
			is_enabled=1, purchase_url="https://pay.example.test/buy", reference_param="ref"
		)

		result = proposals.get_purchase_link(purchase_reference="A B&C=D")

		self.assertIn("A%20B%26C%3DD", result["url"])

	def test_no_parameter_means_no_reference_appended(self):
		# A plain link is the configured behaviour when no parameter is named, not a bug.
		self.configure(is_enabled=1, purchase_url="https://pay.example.test/buy")

		result = proposals.get_purchase_link(purchase_reference="AB12-CD34")

		self.assertEqual(result["url"], "https://pay.example.test/buy")
		self.assertFalse(result["identifies_purchase"])

	def test_an_http_url_is_refused_at_save(self):
		"""
		Refused when the operator types it, so they learn immediately rather than a
		customer discovering it. A payment page over plain HTTP can be rewritten in
		transit, and the customer would pay whoever got there first.
		"""
		with self.assertRaises(frappe.ValidationError) as caught:
			self.configure(is_enabled=1, purchase_url="http://pay.example.test/buy")

		self.assertIn("https://", str(caught.exception))

	def test_an_http_url_is_refused_at_read_too(self):
		"""
		Belt and braces, because the write path is not the only way a value gets in:
		a console, a data import or a patch can all bypass `validate`. The READ is what
		a customer's browser follows, so it checks as well.

		Written with `db_set` precisely because that bypasses validation -- which is the
		circumstance this guard exists for.
		"""
		doc = self.configure(is_enabled=1, purchase_url="https://pay.example.test/buy")
		frappe.db.set_value(
			"Rest Credit Purchase Link", doc.name, "purchase_url", "http://pay.example.test/buy"
		)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as caught:
			proposals.get_purchase_link()

		self.assertIn("https://", str(caught.exception))

	def test_a_parameter_name_that_is_not_query_syntax_is_refused(self):
		# `&`, `=` and spaces change what the address means, and the failure is silent:
		# the customer reaches the right host with the wrong parameters.
		with self.assertRaises(frappe.ValidationError):
			self.configure(
				is_enabled=1,
				purchase_url="https://pay.example.test/buy",
				reference_param="a&b",
			)

	def test_enabling_with_no_url_is_refused_at_save(self):
		# An enabled link with no destination shows customers a button that goes
		# nowhere, so it is refused where the operator can see it.
		with self.assertRaises(frappe.ValidationError) as caught:
			self.configure(is_enabled=1, purchase_url="")

		self.assertIn("Purchase URL", str(caught.exception))

	def test_the_read_reports_disabled_rather_than_crashing(self):
		"""
		The read must survive a state the write path would refuse, because the read is
		what a customer's page waits on: a lookup that threw would break the credits
		page rather than simply not offering a button.
		"""
		doc = self.configure(is_enabled=0, purchase_url="")
		frappe.db.set_value("Rest Credit Purchase Link", doc.name, "is_enabled", 1)
		frappe.db.commit()

		result = proposals.get_purchase_link()

		self.assertFalse(result["enabled"])
		self.assertIn("reason", result)


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


def make_test_payment_request():
	"""
	A Payment Request standing in for a gateway transaction, against a real invoice.

	ERPNext's own model for a gateway payment: it carries `payment_gateway`,
	`payment_gateway_account` and the `payment_url` the gateway fills in, and a
	gateway's handler marks it paid. Using it rather than inventing a doctype makes the
	gateway path a configuration of ERPNext's machinery instead of bespoke code.

	The reference has to be an OUTSTANDING document, not a Customer: ERPNext computes
	the amount from the reference and refuses with "Payment Entry is already created"
	when that comes to zero, which is a guard against requesting the same money twice.
	Pointing at a Customer therefore never works, and the first version of this fixture
	skipped for that reason while the suite still said OK.
	"""
	company = frappe.db.get_value("Company", {}, "name")
	if not company:
		raise unittest.SkipTest("no Company exists, so no Payment Request can be made")

	currency = frappe.db.get_value("Company", company, "default_currency") or "GHS"
	customer = _make_test_customer(suffix=now_datetime().strftime("%H%M%S%f"))
	income = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
	)

	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": company,
			"customer": customer,
			"posting_date": now_datetime().date(),
			"due_date": now_datetime().date(),
			"currency": currency,
			"items": [
				{
					"item_name": "REST gateway probe",
					"description": "REST gateway probe",
					"qty": 1,
					"rate": 100,
					"income_account": income,
				}
			],
		}
	)
	invoice.flags.ignore_permissions = True
	try:
		invoice.insert()
		invoice.submit()
	except Exception as error:  # noqa: BLE001 - reported as a skip carrying its reason
		raise unittest.SkipTest("could not create the invoice behind a Payment Request: {0}".format(error))

	doc = frappe.get_doc(
		{
			"doctype": "Payment Request",
			"company": company,
			"transaction_date": now_datetime().date(),
			"payment_request_type": "Inward",
			"currency": currency,
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name,
			# Set explicitly: ERPNext's Payment Request does not compute it on insert,
			# and it is `reqd`, so the first version failed with a bare
			# "[Payment Request, ACC-PRQ-...]: grand_total".
			"grand_total": invoice.grand_total,
			"email_to": "probe@example.test",
		}
	)
	doc.flags.ignore_permissions = True
	try:
		doc.insert()
	except Exception as error:  # noqa: BLE001 - reported as a skip carrying its reason
		raise unittest.SkipTest("could not create a Payment Request fixture: {0}".format(error))
	return doc.name


def _make_test_customer(suffix=""):
	"""A Customer to point the payment at, named so teardown can find it."""
	_name = "REST Settlement Probe" + (" " + suffix if suffix else "")
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"

	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": _name,
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
