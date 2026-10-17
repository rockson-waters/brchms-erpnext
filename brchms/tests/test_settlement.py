# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Settlement: how a paid invoice becomes a claimable credit.

## What this file pins

The design claim is that a proposal becomes claimable only when money has actually
arrived on the books. `test_proposals.py` pins the other half — that the app cannot
submit a proposal — so these tests pin the half that may: the settlement hook.

Four properties matter, and each has a test:

  * an UNPAID invoice leaves the proposal draft (otherwise creating an invoice would
    grant credit);
  * a submitted **Payment Entry** against that invoice settles it, recording WHICH
    payment did it, so the books are the evidence rather than a click;
  * settlement is **idempotent** — a second payment cannot double-settle a proposal
    or overwrite the evidence of the payment that did;
  * an invoice with **no** `purchase_reference` is ignored entirely, because a hook
    that came with a credits app must not touch ordinary commerce.

## Why the fixture builds a real invoice and a real Payment Entry

A Payment Entry is a real accounting document with real validation, and the settle
path reads back `purchase_reference` from the invoice. Faking either side would test
the fake. `test_proposals.py` learned this the expensive way — a fixture that
skipped silently disabled nine tests while the suite still reported OK — so the
helpers here raise `SkipTest` carrying a REASON, which shows up in the run.

Run with:
    bench --site <site> run-tests --app brchms --module brchms.tests.test_settlement
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cstr, now_datetime

from brchms.brchms_credits import proposals, purchase as purchase_module, registry, settlement
from brchms.tests.test_proposals import (
	_cleanup_invoices_for_instances,
	_cleanup_registry_for_instances,
)

PROPOSAL = "Rest Credit Proposal"
PURCHASE = "Rest Credit Purchase"
INVOICE = "Sales Invoice"
INSTANCE = "test:settlement-instance"
OTHER_INSTANCE = "test:settlement-other"
ITEM_CODE = "REST-SMS-CREDIT"


class TestSettlement(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self._clean()
		self.customer = self._customer()
		self.instance = self._instance()

	def tearDown(self):
		self._clean()
		super().tearDown()

	# ── fixtures ─────────────────────────────────────────────────────────────

	def _clean(self):
		"""Exact scope: only rows these tests create.

		Order: PROPOSALS first -- a settled proposal carries a dynamic link to
		the Payment Entry that settled it, and ERPNext refuses to cancel or
		delete a document something else links to, so the payment cannot go
		before the proposal does. Then payments, then invoices, then the
		registry's Customers and Customer Groups (an invoice cancel
		revalidates its links, so parties go last).
		"""
		for name in frappe.get_all(
			PROPOSAL, filters={"instance_id": ("like", "test:settlement%")}, pluck="name"
		):
			# A SUBMITTED record cannot be deleted, and `force=True` does not bypass
			# that — the error it raises ("Submitted Record cannot be deleted") failed
			# every test whose settlement had just passed. Cancel first, as the people
			# who wrote test_proposals.py already had to do for Payment Entry.
			doc = frappe.get_doc(PROPOSAL, name)
			if doc.docstatus == 1:
				try:
					doc.flags.ignore_permissions = True
					doc.cancel()
				except Exception:
					# Cancelling runs before_cancel, which can refuse. A cleanup that
					# cannot finish must not mask the test result, so the row is left
					# for the next run's cleanup to try again.
					continue
			frappe.delete_doc(PROPOSAL, name, force=True, ignore_permissions=True)

		for name in frappe.get_all(
			"Payment Entry",
			filters={"reference_no": ("like", "TEST-SETTLE-%")},
			pluck="name",
		):
			doc = frappe.get_doc("Payment Entry", name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)

		# The invoices the proposal path raises for test instances, cancelled
		# while their billing parties still exist.
		_cleanup_invoices_for_instances(("test:settlement%",))

		for name in frappe.get_all(
			INVOICE, filters={"customer": ("like", "REST Settlement Test%")}, pluck="name"
		):
			doc = frappe.get_doc(INVOICE, name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc(INVOICE, name, force=True, ignore_permissions=True)

		_cleanup_registry_for_instances(("test:settlement%",))
		frappe.db.delete("Rest Instance", {"name": ("like", "test:settlement%")})

	def _customer(self):
		name = "REST Settlement Test {0}".format(now_datetime().strftime("%H%M%S%f"))
		doc = frappe.get_doc(
			{"doctype": "Customer", "customer_name": name, "customer_type": "Individual"}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	def _instance(self):
		doc = frappe.get_doc(
			{
				"doctype": "Rest Instance",
				"instance_id": INSTANCE,
				"display_name": "Settlement Test Deployment",
				"is_active": 1,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_if_duplicate=True)
		return INSTANCE

	def _proposal(self, reference):
		doc = frappe.get_doc(
			{
				"doctype": PROPOSAL,
				"instance_id": self.instance,
				"purchase_reference": reference,
				"funding_group_id": "test-group",
				"organization_id": "test-org",
				"channel": "sms",
				"units": 100,
				"amount_minor": 5000,
				"currency": self._currency(),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	def _currency(self):
		company = frappe.db.get_value("Company", {}, "name")
		return frappe.db.get_value("Company", company, "default_currency") if company else "GHS"

	def _invoice(self, reference, amount=100, instance=None):
		"""
		A submitted Sales Invoice, with the purchase reference the app would send.

		The reference is written through the CUSTOM FIELD this app installs, so the
		test also proves that field exists and is writable — a settlement that cannot
		read it is a settlement that can never fire. `instance` optionally stamps
		the deployment the invoice bills for, which is what the settlement's
		cross-instance guard keys on.
		"""
		if not frappe.db.has_column(INVOICE, "purchase_reference"):
			raise unittest.SkipTest(
				"Sales Invoice.purchase_reference is missing, so this site has not run the "
				"after_migrate hook (brchms_credits.provisioning). Run `bench migrate` first."
			)

		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			raise unittest.SkipTest("no Company exists, so no invoice can be raised")

		income = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
		)
		invoice = frappe.get_doc(
			{
				"doctype": INVOICE,
				"company": company,
				"customer": self.customer,
				"posting_date": now_datetime().date(),
				"currency": self._currency(),
				"items": [
					{
						# The item this app installs, so a site that skipped the hook is
						# reported as that rather than as an unexplained valuation error.
						"item_code": ITEM_CODE if frappe.db.exists("Item", ITEM_CODE) else None,
						"item_name": "REST settlement probe",
						"description": "REST settlement probe",
						"qty": 1,
						"rate": amount,
						"income_account": income,
					}
				],
			}
		)
		if reference:
			invoice.purchase_reference = reference
		if instance:
			invoice.instance_id = instance
		invoice.flags.ignore_permissions = True
		try:
			invoice.insert()
			invoice.submit()
		except Exception as error:  # noqa: BLE001 - the reason is the point of the skip
			raise unittest.SkipTest("could not raise a test invoice: {0}".format(error))
		return invoice.name

	def _pay(self, invoice, amount=100, reference_no="TEST-SETTLE-PE"):
		"""A submitted Payment Entry allocating `amount` to `invoice`."""
		company = frappe.db.get_value("Company", {}, "name")
		paid_from = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Receivable", "is_group": 0}, "name"
		)
		paid_to = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
			"Company", company, "default_bank_account"
		)
		if not paid_from or not paid_to:
			raise unittest.SkipTest(
				"the Company has no receivable and cash/bank account to build a payment from"
			)

		doc = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"company": company,
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": self.customer,
				"posting_date": now_datetime().date(),
				"paid_from": paid_from,
				"paid_to": paid_to,
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": reference_no,
				"reference_date": now_datetime().date(),
				"references": [
					{
						"reference_doctype": INVOICE,
						"reference_name": invoice,
						"allocated_amount": amount,
					}
				],
			}
		)
		doc.flags.ignore_permissions = True
		try:
			doc.insert()
			doc.submit()
		except Exception as error:  # noqa: BLE001 - the reason is the point of the skip
			raise unittest.SkipTest("could not raise a test payment: {0}".format(error))
		return doc.name

	def _docstatus(self, name):
		return frappe.db.get_value(PROPOSAL, name, "docstatus")

	def _fields(self, name):
		return frappe.db.get_value(
			PROPOSAL,
			name,
			["docstatus", "payment_doctype", "payment_document", "payment_reference", "settled_by"],
			as_dict=True,
		)

	# ── tests ────────────────────────────────────────────────────────────────

	def test_an_unpaid_invoice_grants_nothing(self):
		"""Raising an invoice is not payment, so the proposal must stay draft."""
		reference = "TEST-SETTLE-UNPAID-1"
		proposal = self._proposal(reference)
		self._invoice(reference)

		self.assertEqual(
			0,
			self._docstatus(proposal),
			"an unpaid invoice left the proposal claimable",
		)

	def test_paying_the_invoice_settles_the_proposal_and_records_the_payment(self):
		"""The books are the evidence: the settlement names the document that paid."""
		reference = "TEST-SETTLE-PAID-1"
		proposal = self._proposal(reference)
		invoice = self._invoice(reference)
		payment = self._pay(invoice, reference_no="TEST-SETTLE-REF-1")

		fields = self._fields(proposal)
		self.assertEqual(1, fields.docstatus, "the payment did not settle the proposal")
		self.assertEqual("Payment Entry", fields.payment_doctype)
		self.assertEqual(payment, fields.payment_document)
		self.assertEqual("TEST-SETTLE-REF-1", fields.payment_reference)
		self.assertTrue(fields.settled_by, "a settlement must name who settled it")

	def test_settlement_is_idempotent(self):
		"""
		A second invoice with the same reference, paid, must settle nothing further.

		Reachable in practice: an invoice cancelled and re-issued. A proposal is
		claimable once, so the second payment must not overwrite the first one's
		evidence — that evidence is what an audit reads.
		"""
		reference = "TEST-SETTLE-IDEMPOTENT-1"
		proposal = self._proposal(reference)
		first = self._pay(self._invoice(reference), reference_no="TEST-SETTLE-FIRST")
		first_document = frappe.db.get_value(PROPOSAL, proposal, "payment_document")

		second_invoice = self._invoice(reference, amount=10)
		self._pay(second_invoice, amount=10, reference_no="TEST-SETTLE-SECOND")

		fields = self._fields(proposal)
		self.assertEqual(1, fields.docstatus, "the proposal was settled more than once")
		self.assertEqual(first, first_document)
		self.assertEqual(
			first_document,
			fields.payment_document,
			"a second payment overwrote the evidence of the one that settled the proposal",
		)

	def test_an_invoice_with_no_reference_is_left_alone(self):
		"""Ordinary commerce must not be touched by a hook that came with a credits app."""
		reference = "TEST-SETTLE-NOREF-1"
		proposal = self._proposal(reference)
		invoice = self._invoice(None)
		self._pay(invoice, reference_no="TEST-SETTLE-PLAIN")

		self.assertEqual(
			0,
			self._docstatus(proposal),
			"an invoice with no purchase reference settled a proposal",
		)

	def test_the_hook_never_raises_into_the_payment(self):
		"""
		A settlement failure must not roll back a payment.

		The money is the fact; a bookkeeping step that could not complete is a problem
		to report. This calls the hook with an object that cannot possibly work, and
		requires it to return rather than throw.
		"""
		broken = frappe._dict(
			doctype="Payment Entry",
			name="ACC-PAY-DOES-NOT-EXIST",
			reference_no="TEST-SETTLE-BROKEN",
			references=[frappe._dict(reference_doctype=INVOICE, reference_name="ACC-SINV-0000-00000")],
		)
		try:
			settlement.settle_for_payment_entry(broken)
		except Exception as error:  # noqa: BLE001 - the failure IS the assertion
			self.fail("the hook raised into the payment: {0}".format(error))

	def test_one_deployments_payment_never_settles_anothers_proposal(self):
		"""
		The cross-instance guard. Organization and group ids are per-deployment, and
		two deployments sharing one ERPNext site may both have a proposal carrying
		the same reference -- so matching by reference ALONE would settle the wrong
		deployment's credits with this payment, silently, because both sides would
		report success.
		"""
		shared_reference = "TEST-SETTLE-XINSTANCE-1"
		mine = self._proposal(shared_reference)

		# The other deployment's proposal carries the SAME reference...
		other_instance = frappe.get_doc(
			{
				"doctype": "Rest Instance",
				"instance_id": OTHER_INSTANCE,
				"display_name": "The Other Deployment",
				"is_active": 1,
			}
		)
		other_instance.flags.ignore_permissions = True
		other_instance.insert(ignore_if_duplicate=True)

		other = frappe.get_doc(
			{
				"doctype": PROPOSAL,
				"instance_id": OTHER_INSTANCE,
				"purchase_reference": shared_reference,
				"funding_group_id": "test-group",
				"organization_id": "test-org",
				"channel": "sms",
				"units": 100,
				"amount_minor": 5000,
				"currency": self._currency(),
			}
		)
		other.flags.ignore_permissions = True
		other.insert()

		# ...and MY deployment's invoice is the one that got paid.
		invoice = self._invoice(shared_reference, instance=INSTANCE)
		self._pay(invoice, reference_no="TEST-SETTLE-XINSTANCE-REF")

		self.assertEqual(
			1,
			self._docstatus(mine),
			"the payment did not settle the deployment it was billed to",
		)
		self.assertEqual(
			0,
			self._docstatus(other.name),
			"one deployment's payment settled another deployment's proposal",
		)

	def test_a_form_purchases_intake_row_is_marked_paid_when_its_proposal_settles(self):
		"""
		The desk visibility for the public form: a paid request must stop saying
		"Invoiced", or the operator's list of people waiting on something never
		shortens.
		"""
		reference = "TEST-SETTLE-INTAKE-PAID-1"
		proposal = self._proposal(reference)
		invoice = self._invoice(reference)

		intake = frappe.get_doc(
			{
				"doctype": PURCHASE,
				"buyer_name": "Intake Test Buyer",
				"buyer_email": "intake-test-paid@example.test",
				"channel": "sms",
				"units": 100,
				"amount": 100,
				"purchase_reference": reference,
				"proposal": proposal,
				"status": "Invoiced",
			}
		)
		intake.flags.ignore_permissions = True
		intake.insert()

		self._pay(invoice, reference_no="TEST-SETTLE-INTAKE-PAID-REF")

		self.assertEqual(
			"Paid",
			frappe.db.get_value(PURCHASE, intake.name, "status"),
			"a settled proposal left the form's intake row saying Invoiced",
		)

	def test_a_settlement_against_free_text_is_refused(self):
		"""
		"Cash" and "Bank Transfer" are not documents, and the doctype says so.

		`_require_evidence_of_payment` requires the payment document to exist, because
		submitting is the moment a request becomes a claim on real credit. Free text
		would satisfy a field and prove nothing, so the escape hatch refuses it and
		points at the Payment Entry an operator should have entered instead.
		"""
		proposal = self._proposal("TEST-SETTLE-FREETEXT-1")
		with self.assertRaises(frappe.ValidationError) as caught:
			proposals.settle_proposal(
				proposal=proposal,
				payment_doctype="Bank Transfer",
				payment_document="TRF-1",
				payment_reference="STATEMENT-1",
			)

		message = str(caught.exception)
		self.assertIn("Payment Entry", message, "the refusal should say what to do instead")
		self.assertEqual(0, self._docstatus(proposal), "a refused settlement changed the proposal")

	def test_the_escape_hatch_settles_against_a_payment_with_no_invoice_reference(self):
		"""
		The case the hook cannot see: the money is on the books but the invoice it was
		entered against does not carry the purchase reference.

		The settlement must read the reference off the DOCUMENT rather than trusting
		the caller, because that reference is what a reviewer matches to a statement.
		"""
		proposal = self._proposal("TEST-SETTLE-MANUAL-1")
		invoice = self._invoice(None)
		payment = self._pay(invoice, reference_no="TEST-SETTLE-MANUAL-REF")

		proposals.settle_proposal(
			proposal=proposal,
			payment_doctype="Payment Entry",
			payment_document=payment,
			note="Bank transfer received; the invoice did not carry the reference.",
		)

		fields = self._fields(proposal)
		self.assertEqual(1, fields.docstatus)
		self.assertEqual(payment, fields.payment_document)
		self.assertEqual(
			"TEST-SETTLE-MANUAL-REF",
			fields.payment_reference,
			"the reference must come from the payment document",
		)


INTAKE_RATE_NOTE = "INTAKE-TEST-RATE-FIXTURE"


class TestPurchaseIntake(FrappeTestCase):
	"""
	The public purchase form's server half.

	## What this pins

	The purchase form is the only PUBLIC write in this app, so the two things worth
	pinning are that it produces the right attributed proposal and that it cannot be
	used to make credits real. The second is structural -- the intake row becomes a
	DRAFT proposal, and a proposal only becomes claimable when `settlement` sees it
	settled, which is a different class of test (see TestSettlement above).

	The identity requirement is the first thing pinned: a submission with no org
	slug is refused outright, because an unattributed purchase is money on the
	books with nowhere to put the credits it bought.

	The gateway step is NOT asserted as a success: `make_payment_request` needs a
	configured Paystack account with real keys, which is a deployment's job. On a
	site without one, `checkout` records everything, links the intake row, and
	refuses with the buyer's message -- which is itself pinned, because that
	refusal is the recoverable path.

	A rate row in the SITE'S OWN currency is installed for every test, because
	`checkout` re-derives the amount from the current rate: a test that depended
	on whatever a deployment happened to have configured would pass on one site
	and fail on another for reasons unrelated to the code.
	"""

	def setUp(self):
		super().setUp()
		self._clean()
		self._remove_rate()
		rate = frappe.get_doc(
			{
				"doctype": "Rest Credit Rate",
				"channel": "sms",
				"unit": "sms_segment",
				# 0.10 per segment, so 250 segments derive exactly 25.00 -- the
				# amount the fixture request carries.
				"price_per_unit_micros": 100000,
				"currency": self._currency(),
				"is_active": 1,
				"effective_from": now_datetime(),
				"notes": INTAKE_RATE_NOTE,
			}
		)
		rate.flags.ignore_permissions = True
		rate.insert()
		frappe.db.commit()

	def tearDown(self):
		self._remove_rate()
		self._clean()
		super().tearDown()

	def _remove_rate(self):
		for name in frappe.get_all("Rest Credit Rate", filters={"notes": INTAKE_RATE_NOTE}, pluck="name"):
			frappe.delete_doc("Rest Credit Rate", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _currency(self):
		company = frappe.db.get_value("Company", {}, "name")
		return frappe.db.get_value("Company", company, "default_currency") if company else "GHS"

	def _clean(self):
		"""
		Exact scope: only what these tests create.

		Invoices are cancelled while their billing parties still exist -- the
		registry's Customers (which the checkout invoices are billed to) are
		deleted LAST, because a cancel revalidates links and a missing party
		refuses it.
		"""
		_cleanup_invoices_for_instances(("test:settlement%",))

		for name in frappe.get_all(
			PURCHASE, filters={"buyer_email": ("like", "intake-test-%")}, pluck="name"
		):
			frappe.delete_doc(PURCHASE, name, force=True, ignore_permissions=True)

		for name in frappe.get_all(
			INVOICE, filters={"customer": ("like", "Intake Test Buyer%")}, pluck="name"
		):
			doc = frappe.get_doc(INVOICE, name)
			if doc.docstatus == 1:
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc(INVOICE, name, force=True, ignore_permissions=True)

		frappe.db.delete(PROPOSAL, {"instance_id": ("like", "test:settlement%")})

		for name in frappe.get_all(
			"Customer", filters={"customer_name": ("like", "Intake Test%")}, pluck="name"
		):
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)

		_cleanup_registry_for_instances(("test:settlement%",))
		frappe.db.delete("Rest Instance", {"name": ("like", "test:settlement%")})
		frappe.db.commit()

	def _register_church(self):
		"""The registry row an attributed link stands on, with a default group."""
		return registry.ensure_registration(
			instance_id=INSTANCE,
			organization_id="org-intake-1",
			organization_name="Intake Test Church",
			default_group_id="grp-intake-1",
			default_group_name="Intake Test Group",
		)

	def _request(self, **overrides):
		values = {
			"doctype": PURCHASE,
			"buyer_name": "Intake Test Buyer",
			"buyer_email": "intake-test-{0}@example.test".format(
				now_datetime().strftime("%H%M%S%f")
			),
			"channel": "sms",
			"units": 250,
			"amount": 25,
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc

	def test_a_submission_without_an_org_slug_is_refused(self):
		"""
		The identity requirement. A stranger typing "buy credits" cannot name a
		funding group, and the app grants units to a group -- so the purchase
		must arrive already attributed, or not at all.
		"""
		purchase = self._request()

		with self.assertRaises(frappe.ValidationError) as caught:
			purchase_module.checkout(purchase=purchase.name)

		message = str(caught.exception)
		self.assertIn("church's own purchase link", message)
		# Nothing was raised: no proposal, no invoice.
		self.assertFalse(frappe.db.get_value(PURCHASE, purchase.name, "proposal"))
		self.assertEqual("Requested", purchase.status)

	def test_an_unknown_slug_is_refused(self):
		purchase = self._request()

		with self.assertRaises(frappe.DoesNotExistError) as caught:
			purchase_module.checkout(purchase=purchase.name, org="no-such-slug")

		self.assertIn("no longer recognized", str(caught.exception))

	def test_a_registered_churchs_submission_becomes_an_attributed_proposal(self):
		"""
		The bridge: the buyer's request becomes a proposal carrying the church's
		organization, funding group and deployment id, with its invoice raised --
		the two artefacts the settlement and the app's reconcile poller need.
		"""
		row = self._register_church()
		purchase = self._request()
		frappe.db.commit()

		try:
			result = purchase_module.checkout(purchase=purchase.name, org=row.link_slug)
		except frappe.ValidationError as error:
			# No Payment Gateway Account on this site: the page could not be
			# opened. Everything else must still have happened.
			self.assertIn("payment page could not be opened", str(error))
			result = None

		proposal_name = frappe.db.get_value(PURCHASE, purchase.name, "proposal")
		self.assertTrue(proposal_name, "the intake row was never bridged to a proposal")

		proposal = frappe.get_doc(PROPOSAL, proposal_name)
		self.assertEqual(0, proposal.docstatus, "creating a proposal made credits claimable")
		self.assertEqual("org-intake-1", proposal.organization_id)
		self.assertEqual("grp-intake-1", proposal.funding_group_id)
		self.assertEqual(INSTANCE, proposal.instance_id)
		self.assertEqual(purchase.buyer_email, proposal.buyer_email)
		self.assertEqual("form:{0}".format(purchase.name), proposal.request_key)

		# The invoice: raised, submitted, carrying what the settlement matches by.
		self.assertTrue(proposal.invoice)
		invoice = frappe.get_doc(INVOICE, proposal.invoice)
		self.assertEqual(1, invoice.docstatus)
		self.assertEqual(INSTANCE, invoice.instance_id)
		self.assertEqual(registry.customer_for(INSTANCE, "grp-intake-1"), invoice.customer)
		self.assertEqual(250, invoice.units)
		self.assertEqual("sms", invoice.channel)
		# The channel decides the item, never the buyer.
		self.assertEqual(ITEM_CODE, invoice.items[0].item_code)

		# The intake row tells the desk the truth.
		self.assertEqual("Invoiced", frappe.db.get_value(PURCHASE, purchase.name, "status"))

		if result is not None:
			self.assertEqual(proposal_name, result["proposal"])
			self.assertTrue(result["payment_url"])

	def test_an_impossible_order_is_refused_with_readable_advice(self):
		"""
		Zero units, zero money, a bad address and no channel are all refused.

		The assertion is on the WORDS A BUYER READS, not on schema field names.
		`docs/ERROR-MESSAGE-STANDARD.md` is normative here: a rejection says which
		input is wrong and what a valid value looks like, in human labels.
		"""
		cases = (
			({"units": 0}, "how many credits"),
			({"amount": 0}, "amount you are paying"),
			({"buyer_email": "not-an-address"}, "valid email address"),
			({"channel": ""}, "which kind of credit"),
		)
		for overrides, expected in cases:
			with self.assertRaises(frappe.ValidationError) as caught:
				self._request(**overrides)
			message = str(caught.exception).lower()
			self.assertIn(
				expected,
				message,
				"the refusal for {0} should say what to do, in the buyer's words".format(overrides),
			)

	def test_a_double_submit_resolves_to_the_same_proposal(self):
		"""A reloaded page or a double click must not raise a second proposal."""
		row = self._register_church()
		purchase = self._request()
		frappe.db.commit()

		for _attempt in range(2):
			try:
				purchase_module.checkout(purchase=purchase.name, org=row.link_slug)
			except frappe.ValidationError:
				# No gateway on this site; the proposal is still raised and linked.
				pass

		self.assertEqual(
			1,
			frappe.db.count(
				PROPOSAL, {"instance_id": INSTANCE, "request_key": "form:{0}".format(purchase.name)}
			),
			"a double submit raised a second proposal for the same money",
		)
