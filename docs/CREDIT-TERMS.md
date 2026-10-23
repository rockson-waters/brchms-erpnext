# Credit terms: discount, minimum purchase and free credits

This app is the authority for what a credit purchase costs and for the recurring
free-credit entitlement. The Rest application holds the balance and charges it.
This document states the rules both sides implement.

**All money is integer minor units; all prices are integer millionths of a major
unit (micros). No float touches a money value.**

## Discount (per organization)

`Rest Credit Organization.discount_percent` is a standing percentage, `0 <= value <
100`, applied to every new purchase on both channels. It is a commercial term the
church negotiates with the platform; it is not editable by the tenant application
and it is not a request parameter.

The effective price is derived, half-up, on both sides:

```
bp                    = round(discount_percent * 100)          # basis points
effective_micros      = round_half_up(list_micros * (10000 - bp) / 10000)
amount_minor          = minor_amount_for(units, effective_micros, currency)
list_amount_minor     = minor_amount_for(units, list_micros, currency)
discount_minor        = list_amount_minor - amount_minor
```

- The app sends the list price, basis points and effective price to
  `create_proposal`; the proposal refuses a snapshot whose parts disagree.
- The proposal also refuses a snapshot whose discount differs from the
  organization's current registry term, with "the price changed while this was
  being raised; refresh the quote". A quote is priced once and never re-priced by
  a later rate or term change.
- A discount that rounds the effective price to zero is reported as
  `pricing_error` and refused; free credits are the mechanism for free units, not
  a 100% discount.
- The invoice is raised at the **net** amount. The item description names the list
  price and the discount for a reviewer; ERPNext's tax/discount/pricing-rule
  machinery is deliberately not used, so the invoice total and the proposal
  snapshot cannot disagree by a rounding implementation.
- The rate card the app shows is the effective price; the list price and the
  discount are shown beside it. The purchase row snapshots
  `list_unit_price_micros`, `discount_basis_points` and `rate_name` so a later
  change cannot rewrite what was charged.

## Minimum purchase (site-wide)

`Rest Credit Settings` is a **Single**:

| Field | Meaning |
| --- | --- |
| `minimum_purchase_amount` | The smallest credit purchase this site will sell, in major units. `0` disables. |

The figure is applied **nominally in each organization's billing currency**: `5`
means GHS 5.00 for a Ghana purchase and CAD 5.00 for a Canadian one. This app has
no exchange rate, and inventing one to convert a floor would price a purchase off
a rate nobody approved. The minor-unit conversion uses `Decimal` and the
currency's `fraction_units` (MGA is 5, not 100), rounding **up** so a floor is
never lowered by an unlucky fraction.

The floor applies to the **net** amount after discount, on both channels, and is
enforced at every door:

- `quote` returns `minimum_purchase_minor`, `minimum_purchase_amount`,
  `below_minimum` as data (the public form shows a sentence);
- `purchase.checkout` re-quotes and refuses;
- `Rest Credit Purchase.validate` refuses a crafted intake row;
- `create_proposal` refuses before a proposal or invoice exists;
- `raise_credit_invoice` is the final backstop;
- the app's purchase controller refuses with `field_errors.amount` and
  `metadata.minimum_purchase_minor`, and the dialog shows the floor.

Exactly-at-minimum is accepted. The minimum is not re-applied retroactively to an
existing quote; a raised floor can block an unpaid below-minimum request at
proposal time, with a message naming the new floor.

## Monthly free credits (per organization)

| Field | Meaning |
| --- | --- |
| `monthly_free_sms_units` | SMS segments per UTC month, granted to the organization's default funding group. |
| `monthly_free_email_units` | Email messages per UTC month, same group. |
| `free_credits_rollover` | Off: unused units expire at the end of the month. On: they never expire. |

- `list_credit_allowances(instance_id, period)` returns the current UTC month's
  positive entitlements plus any organization that has an entitlement but no
  default group, in `skipped`. It writes nothing.
- The app's daily poller grants each entitlement once, keyed on
  `erpnext-allowance:monthly:<instance>:<org>:<channel>:<period>`, with zero cost
  and a lot expiring at the next UTC month start **only when rollover is off**.
- The grant is allocated to the default/root funding group; the organization can
  share it onward through the ordinary allocation path.
- No rollover is enforced by revoking unspent, unreserved units at expiry. Units
  held by an in-flight send are deferred until they settle or release. The revoke
  moves units back to the lot and writes an `allocation_release` ledger row;
  conservation (`unallocated + Σ(allocated−consumed) + consumed == granted`) holds
  and no history is rewritten. The expired lot is then refused by `allocate` and
  excluded from spendable/summary reads.
- Rollover has no cap. Changing the checkbox affects **future** grants only: a lot
  granted with an expiry still expires; a lot granted without one still never
  expires.
- A missed month is not back-filled. An entitlement changed mid-month does not
  change that month's already-recorded grant.
- An organization whose registration has no default funding group is deferred and
  retried, never guessed.

## Welcome / starting credits (app-side, one-time)

The Rest app lets the organization's creator set starting SMS and email units on
**every user-facing creation form** (setup wizard root organization, Organizations
› New, Access › Provisioning). These are stored on the organization, immutable
after creation, and granted once by the app when the organization has a root
funding group — the provisioned child deliberately has none at creation, so the
grant is deferred rather than placed in the wrong group.

- Event key: `rest-provisioning:starting:<instance>:<org>:<channel>`.
- Cost zero, **no expiry**, regardless of the monthly rollover setting.
- Works on the `local` credit provider too; it is an app-side policy grant, not a
  purchase or an ERPNext reconciliation.
- Changing the fields after creation is refused. A second grant is impossible by
  the event key even if a row were changed in the database.

## What the app shows

- Credits page: effective rate with list price and discount, the monthly allowance
  and whether it rolls over, the site minimum on the purchase dialog.
- Purchase records: the price snapshot and discount marker.
- Compose/automation estimates: the effective (discounted) price, because that is
  what the organization's credits actually cost.

## Verification

No production testing. See [`TEST-ENVIRONMENT.md`](TEST-ENVIRONMENT.md) for the
isolated clone / independent instance procedure. Bench tests that create payments
and cancelled GL rows are run only there.
