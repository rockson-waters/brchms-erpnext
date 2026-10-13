# The settlement boundary

**Status:** implemented on the ERPNext side; the Rest backend still calls the old
funding API and has not been migrated.

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

### Why the reference carries the matching

`purchase_reference` is **required**. The operator finds the proposal by the
reference the customer quotes when they pay. A proposal with no reference is not
"hard to find" — it is **unmatchable**, which is why this is mandatory rather than
encouraged.

The app's own `document_id` (a 24-character CUID2 such as
`u3dc02t5dez6rcprqglrpy7w`) never reaches an operator. It remains what it always
was: the internal identifier that makes reconciliation idempotent.

Uniqueness is per **`(instance_id, purchase_reference)`**, not global — two churches
may legitimately both quote `INV-001`, and collapsing them would settle one church's
payment against another's. Enforced by the API's idempotent create, which uses the
same key the operator searches by, so there is one identity in play rather than two
that can drift.

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

## 6. Not yet done

The Rest backend has **not** been migrated. It still calls `confirm_funding`, which
writes funding records directly. Until that changes:

- the boundary exists in ERPNext but nothing exercises it;
- the app cannot read settlements, so `get_funding`/`count_funding` remain unused;
- `COMMUNICATION_CREDIT_PROVIDER=erpnext` does not yet mean "ERPNext settles, the app
  reconciles".

The remaining work, in dependency order:

1. **Backend** — raise a proposal at request time instead of confirming at the end.
2. **Backend** — replace the confirm path with a reconcile job that reads settlements
   and applies them exactly once, idempotently.
3. **Backend** — make `purchase_reference` required on the purchase.
4. **Retire** the app-side confirmation authority for anything but the `local`
   provider.
5. **Credential** — verify the deployed integration token genuinely lacks `submit`,
   against the live site rather than against the doctype JSON.

## 7. Verification

`brchms/tests/test_proposals.py` — 23 tests, all passing against the live site.

The two that matter most:

- `test_a_proposal_is_created_as_a_DRAFT` — creating a proposal never makes anything
  claimable;
- `test_the_integration_role_cannot_submit` — the role cannot cross the boundary, so
  the API's restraint is backed by a wall rather than a promise.

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
