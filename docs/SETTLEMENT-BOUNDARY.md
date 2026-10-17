# The settlement boundary

**Status:** implemented, and exercised end to end since v0.11.0 — the app raises
attributed proposals that carry their own invoice and payment page, settles only
through this boundary, and reconciles settlements automatically. The app's role
still cannot submit, cancel, write or delete; nothing about the boundary moved.

This is the normative record of *who is allowed to make credits real*. It exists
because the previous arrangement had a defect that no amount of testing would have
surfaced: the app decided, and ERPNext merely wrote down what it was told.

---

## 1. The defect this replaces

`Rest Communication Credit Funding` has no `docstatus` and no status field. Its own
vocabulary says so — `confirmed_at`, "One confirmed funding event". **Creation was
confirmation.**

Two consequences followed, and the second is the one that matters:

1. The app called `confirm_funding` and a funding record appeared.
2. Because there was no unconfirmed state, nothing in ERPNext could disagree. **So
   the app's decision was the only decision there was.**

Reversing the direction of the call would not have fixed this. Whether the app
pushes a record or ERPNext pulls one, a doctype with no pending state makes whatever
arrives final. The fix had to be an **approval boundary inside ERPNext**.

## 2. The boundary

`Rest Credit Proposal` is **submittable**. That is the whole mechanism:

| docstatus | Meaning | Claimable |
| --- | --- | --- |
| 0 (draft) | requested, unpaid | **no** |
| 1 (submitted) | money received and evidenced | **yes** |
| 2 (cancelled) | settlement revoked | no |

And the permission rows:

| Role | read | create | write | **submit** | cancel | delete |
| --- | --- | --- | --- | --- | --- | --- |
| System Manager | 1 | 1 | 1 | 1 | 1 | 1 |
| Rest Integration | 1 | **1** | 0 | **0** | 0 | 0 |

The app can **ask**. It cannot **settle**. This is enforced by Frappe's permission
layer, not by which whitelisted methods happen to exist — so a leaked integration
token cannot create money even if it calls Frappe directly, bypassing this app's API
entirely.

**If `submit: 0` is ever changed to `1`, the design is void.** That is what
`TestIntegrationRoleCannotSettle` pins.

### Why submittable rather than a status field

A status field on one doctype means every report, dashboard and query that sums
funding will count unpaid requests as revenue unless each one remembers to filter. A
submittable doctype means `docstatus` *is* the filter, and the reports that matter
already apply it. The failure mode of the status approach is a set of numbers that
are wrong in the direction that flatters, discovered late.

## 3. The flow

```
group requests credits   →  app raises a PROPOSAL (draft) with a required reference
money arrives            →  operator files the payment: Payment Entry + accounting
operator settles         →  finds the proposal BY REFERENCE, links the payment, submits
app reconciles           →  claims the settled proposal, grants credits, once
never paid               →  the draft expires; nothing was ever claimable
```

### Three identities, which the first design conflated

Conflating them is what made the first version unable to serve cash at all.

| | What it is | When it exists |
| --- | --- | --- |
| **The claim code** | the proposal's own name, `PROP-00981` | at creation |
| **The customer's reference** | their invoice number, or a bank narration | if they have one |
| **The payment reference** | the bank's / gateway's / receipt's own number | only **after** payment |

The first version made one field carry all three. That fails in both directions: a
cash customer has no reference to give and cannot invent one reliably, and a gateway
transaction has none until it completes — so requiring one made cash purchases
impossible and would have blocked the gateway path too.

**`purchase_reference` is therefore OPTIONAL.** It is a convenience for matching, not
an identity.

### The claim code is not shown to customers

`PROP-00981` is internal: staff read it off a receipt, and the app passes it as gateway
transaction metadata where nobody types it. Customers deal only with their own
reference, or with none.

That decision moves the matching problem to the operator, which is why there is a
search (`find_pending_proposals`) rather than a code to type: they work with what the
customer actually brings — their reference, the group, and the money that arrived.

**Ambiguity is reported, not resolved.** Two pending proposals can match, and
returning the most recent would be right most of the time and silently wrong the rest,
settling one request against another's payment. So every candidate comes back with
`ambiguous` set and the operator chooses.

### Idempotency moved to the request key

`create_proposal` is keyed on `(instance_id, request_key)`, where `request_key` is the
deployment's own purchase id. Keying on the reference would have been wrong once it
became optional: two purchases may share one, and a missing one would have no key at
all — so a retry would raise a second request for money nobody asked for.

### The three ways money arrives

`payment_reference` records what the *money* is identified by, and it is required
**except** for a gateway-managed doctype:

- a **payment gateway** supplies its own transaction id and the linked document is
  the evidence, so nothing more is needed — demanding a reference on top would ask an
  operator to retype what the system already knows;
- a **bank transfer** has the bank's reference on the statement;
- **cash** has the receipt number issued at the desk.

For the last two it is the only trace that money moved. Without it a settlement rests
on somebody's memory, and a reconciliation a month later has nothing to match.

### `awaiting_payment` is derived, not stored

A draft with a `payment_url` **is** awaiting payment. No new status: every stored
status is a branch that has to be handled everywhere it is read, and a derived one is
handled in the single place that computes it. The gateway path and the cash path
converge on the same draft→submitted transition, which is what makes one field enough.

The gateway handoff rides ERPNext's own `Payment Request`, which already carries
`payment_gateway`, `payment_gateway_account` and `payment_url` — so the payment link
is a configuration of ERPNext's machinery rather than bespoke code.

### Why a payment document is mandatory to submit

`before_submit` requires a real `payment_doctype` + `payment_document` that exists.

Without it, the entire exercise is a claim with nothing behind it: credits would
become claimable because somebody clicked Submit. Before this field existed,
**nothing anywhere verified that a payment had happened at all** — the trust anchor
was a whitelisted method plus an API token.

## 4. Decisions, and why

### Priced once, never re-priced

The rate is resolved when the purchase is raised and snapshotted onto it. A rate
change cannot re-price a purchase already quoted, because that could fail a
confirmation for a payment that genuinely happened. The rate controller therefore
refuses to edit a rate that funding records already reference.

### The settled amount wins

If the settlement amount differs from the quote, the settled amount is authoritative
and `quote_variance_minor` records the difference. Refusing would strand money that
genuinely arrived. This is a deliberate choice: **money that moved is a fact, and the
software's job is to record it, not to reject it.**

### Expiry, not deletion

Unpaid proposals expire (`Rest Credit Proposal.expires_at`, default 30 days,
configurable). They are **not deleted** — a group that paid just after the deadline
needs a trail showing why their credits are not there.

An expired proposal can still be settled, but only **deliberately**: `before_submit`
demands a `settlement_note` explaining the lateness. Money arriving late must not be
a dead end, and a grant after expiry is exactly what somebody has to account for.

### Cancelling needs a reason

Cancelling revokes a claim that may already have been granted. The note is what
explains a balance going down.

## 5. What the app may and may not do

| | |
| --- | --- |
| Raise a proposal | **yes** (`create_proposal`) |
| Read its own proposals by reference | **yes** (`get_proposal`, `settlement_state`) |
| List what it has asked for | **yes** (`list_pending_proposals`) |
| Settle anything | **no** — not by API, not by permission |
| Edit a settlement | **no** |
| Delete or cancel | **no** |

`settlement_state` answers one word — `draft`, `settled`, `expired`, `cancelled`,
`unknown` — because "has the money arrived" is the question the app asks on the hot
path, and `unknown` (no such proposal) must stay distinguishable from `draft`
(requested, unpaid): one is a bug in the app, the other is patience.

## 6. What has changed since (v0.11.0), and what that means for the boundary

The list below was the remaining work when this document was written; it is now
done, in the same order:

1. ~~**Backend** — raise a proposal at request time~~ — done in v0.10.0.
2. ~~**Backend** — replace the confirm path with a reconcile job that reads
   settlements and applies them exactly once~~ — done in v0.11.0
   (`reconcileCreditSettlements`, every five minutes). The job GRANTS, but only
   through the same gate a human uses: `confirm` re-reads the settlement state
   (`requireSettledProposal`) before granting, and the grant is keyed on
   `(provider, external_event_id)` — unique at the database — so a double run
   grants once. **The boundary is unchanged: the app's role still cannot
   submit, cancel, write or delete.**
3. ~~**Backend** — make `purchase_reference` required on the purchase~~ — done:
   the authority always mints one when the payer had none, and the app stores
   what comes back (`erpnext_purchase_reference`).
4. ~~**Retire** the app-side confirmation authority for anything but the `local`
   provider~~ — done in v0.10.0 (the app reads the settlement instead of writing
   a funding record; manual Confirm remains the repair path).
5. **Credential** — verify the deployed integration token genuinely lacks
   `submit`, against the live site rather than against the doctype JSON. Still
   open; it needs the live walk.

### The v0.11.0 additions, in boundary terms

- **Identity.** A purchase made without an app session is attributed before any
  money moves: the app registers its organization (`Rest Credit Organization`,
  named by an opaque slug), the public form refuses a submission with no slug,
  and `checkout` bridges the intake row to a proposal that carries the
  organization, the funding group and the deployment's instance id.
- **The invoice moves onto the proposal.** `create_proposal` raises the Sales
  Invoice and builds the payment page itself, so a request raised by the app is
  payable. This is why the settlement hook now matches on
  **(instance, reference)**: the invoice carries the deployment's instance id,
  and a reference alone could match another deployment's proposal.
- **Pricing is derived, never typed twice.** `amount_minor ==
  round_half_up(units × unit_price_micros × fraction_units / 10⁶)` is enforced
  before an invoice exists, and the invoice raise re-checks it. Half-up, because
  a rate finer than the currency's coin cannot always divide evenly (5,555
  segments at 0.045 is 249.975 → 249.98). The variance is computed with the same
  rounding, so a correctly priced settlement records zero.

### A correction to §"Frappe creates a unique index only for a field used as the NAME"

That claim was written against an older Frappe. **Frappe 16.51 honours a Data
field's `unique: 1` with a GLOBAL unique index**, which contradicted the
per-deployment design: it refused the second church's identical reference, the
exact case the composite index exists to allow. The field flag is removed and
`after_migrate` drops the leftover global index; uniqueness is
`(instance_id, purchase_reference)` only.

The tests that pin all of this live in `brchms/tests/test_proposals.py` and
`brchms/tests/test_settlement.py`; the full suite is green on a scratch site.

## 7. Verification

`brchms/tests/test_proposals.py` — 23 tests, all passing against the live site.

The two that matter most:

- `test_a_proposal_is_created_as_a_DRAFT` — creating a proposal never makes anything
  claimable;
- `test_the_integration_role_cannot_submit` — the role cannot cross the boundary, so
  the API's restraint is backed by a wall rather than a promise.

And for the three ways money arrives: a cash settlement needs its receipt number and
is refused without one; a bank transfer records the bank's reference; a gateway
settlement needs no hand-entered reference, and points at a real `Payment Request`.

Also covered: idempotent create by reference; the same reference from two deployments
staying separate; exact-integer money (never `flt`, which silently rounded a
2^53-scale value earlier in this integration); expiry reported not deleted; late
settlement requiring a reason; a submitted settlement refusing edits; variance
recorded; cancellation requiring a reason; and teardown removing exactly what it
created.

> **A note on how this was tested.** The first version of these tests reported
> `OK (skipped=9)` — nine tests, including every test of the settling path, silently
> did not run because the Payment Entry fixture was refused with *"Party Type is
> mandatory"*. A skip reads exactly like a pass in the summary line. The fixture now
> builds the Customer and Payment Entry it needs, and the suite reports `OK` with no
> skips.
