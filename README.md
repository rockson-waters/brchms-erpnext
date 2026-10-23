# Rest — the Frappe app behind the Rest connection

The app is called **Rest**. The Python package, the Frappe app name, the module
directories and this repository keep the historical `brchms` identifier, because
those are the machine keys the consuming application addresses
(`brchms.brchms_credits.api.confirm_funding`). Where a human reads a name, it says
Rest.

Three jobs, in one app (the support module joined the credits and banner modules in v0.14.0):

| Module | What it is |
| --- | --- |
| `brchms_credits` | The **funding authority**. The app raises credit proposals; money becomes real only when this site settles one on the books. It also owns the buyer-identity registry, the invoice/payment-page path, the rate card the app is quoted from, the commercial terms (organization discount, site minimum, monthly free credits), and the receipt a settled purchase sends. |
| `brchms_banner` | The **announcement banner** the Rest app shows on every page (maintenance, upgrades, outages). |
| `brchms_support` | **Malfunction reports** filed from the Rest app's "Report a malfunction" button, triaged in this Desk by the `Rest Support Agent` role. |

One app rather than three: they share one deployment, one integration user, one
image rebuild and one version. Nothing separates them enough to justify tripling
each of those.

## The contract Rest speaks

All endpoints are whitelisted methods, so Frappe wraps their return value in
`message`. All use token auth: `Authorization: token <api_key>:<api_secret>`.

### The CURRENT contract: proposals (v0.10.0+)

The app no longer asks this site to record a funding event. It raises a credit
**proposal**, and money becomes real only when a person (or the gateway's own
handler) settles that proposal on the books — the boundary itself is documented in
[`docs/SETTLEMENT-BOUNDARY.md`](docs/SETTLEMENT-BOUNDARY.md). The methods the app
calls today live in `brchms.brchms_credits.proposals`:

| method | direction | why |
| --- | --- | --- |
| `create_proposal(...)` | app → site | raise a purchase request; DRAFT only. It also raises the Sales Invoice and builds the payment page when a gateway is configured, and returns `payment_url`. |
| `settlement_state(instance_id, request_key)` | app → site | one word — `draft` / `settled` / `expired` / `cancelled` / `unknown`. The hot-path question. |
| `list_settled_proposals(instance_id)` | app → site | what the app's reconcile poller applies: settlements, with the organization and funding group needed to grant against them. |
| `quote(channel, units)` | **public** | the exact amount for a number of credits, at the current rate — the purchase form's computed amount. Since v0.14.0 it applies the organization's discount and reports the site minimum (`below_minimum`), `list_amount_minor`/`discount_minor`, and `pricing_error` as data. |
| `get_credit_rate(..., organization_id, instance_id)` | app → site | the rate card price plus the organization's effective (discounted) price, list price, discount basis points, minimum and `pricing_error`. The app calls this to price a purchase; without an organization it answers list price only. |
| `get_billing_context(...)` | app → site | the selling company/currency plus the organization's terms (discount, monthly free units, rollover, minimum) for the Credits page. |
| `list_credit_allowances(instance_id, period)` | app → site | the monthly free-credit entitlement per registered organization and channel (`rollover` included). The app's daily poller grants into its own ledger; this method writes nothing. |
| `get_purchase_link(...)` | app → site | the attributed purchase URL (`?org=<slug>`); registers the caller's organization on first read. |
| `create_malfunction_report(...)` | app → site | a `brchms_support` method (not `brchms_credits`): records a user's malfunction report and optional screenshot. Idempotent on `(instance_id, client_request_id)`. |

The app never submits, cancels, writes or deletes a proposal: the `Rest Integration`
role lacks those permissions, and a settle is a decision this site makes, never one
the app can make for itself.

### Commercial terms (v0.14.0+)

Configured in the Desk, never by the tenant app:

| Where | Field | Effect |
| --- | --- | --- |
| `Rest Credit Settings` (Single) | `minimum_purchase_amount` | Site floor, applied **nominally in each organization's billing currency**. `0` disables. |
| `Rest Credit Organization` | `discount_percent` | Standing percentage off the list price for new purchases, both channels. `0 <= value < 100`. |
| `Rest Credit Organization` | `monthly_free_sms_units`, `monthly_free_email_units` | Monthly allowance granted to the organization's default funding group. |
| `Rest Credit Organization` | `free_credits_rollover` | Off (default): unused monthly units expire at month end. On: they never expire. Future grants only. |

The app-side welcome credits (set on an organization-creation form) are a one-time
grant stored by the app, not by this site; they never expire and are granted once
the organization's root funding group exists. See
[`docs/CREDIT-TERMS.md`](docs/CREDIT-TERMS.md).

`list_settled_proposals` carries the proposal's price snapshot
(`list_unit_price_micros`, `list_amount_minor`, `discount_basis_points`,
`discount_percent`, `discount_minor`, `rate_name`) so a purchase born on the public
form can be attributed after the fact.

`settle_proposal` now requires the caller to hold the proposal's `submit`
permission, and `rate_fixtures.install/remove` require System Manager. The
integration credential holds neither; the settlement boundary is documented in
[`docs/SETTLEMENT-BOUNDARY.md`](docs/SETTLEMENT-BOUNDARY.md).

### `POST /api/method/brchms.brchms_credits.api.confirm_funding` (legacy)

Kept for a `local`-provider or a manual backfill; the app has not called it since
v0.10.0. Its own contract follows.

```jsonc
// request — all money as integer STRINGS; the app stores it as biginteger
{
  "instance_id": "frontend",
  "event_id": "erpnext:frontend:<purchase documentId>",
  "purchase_reference": "PO-2026-001",
  "organization_id": "42",
  "funding_group_id": "grp123",
  "channel": "sms",
  "units": "250",
  "amount_minor": "11250",
  "currency": "CAD",
  "unit_price_micros": "450000"
}
```

**The three money fields must agree.** `units × unit_price_micros × 10⁻⁴` must equal
`amount_minor`, and the doctype refuses the record otherwise.

The exponent is `10⁻⁴`, not `10⁻⁶`, and that is worth stating plainly because it is
where the first version of this check went wrong. `unit_price_micros` is millionths
of a **major** unit (10⁶), while `amount_minor` is in **minor** units — cents (10²).
Converting micros to minor is therefore `10⁶ / 10² = 10⁴`. Using 10⁶ made the rule
100× too strict and refused every correct record.

The example is consistent: 250 units at 450 000 micros (0.45 each) = 112 500 000
micros = 11250 minor = 112.50 CAD.

// response
{ "message": {
    "event_id", "instance_id", "organization_id", "funding_group_id", "channel",
    "units", "amount_minor", "currency", "unit_price_micros",
    "purchase_reference", "confirmed_at"
} }
```

**`event_id` is the idempotency key and is unique on the doctype.** Replaying a
confirmation returns the same record and creates exactly one row. This is not a
nicety: Rest reaches this instance over the network, the request can succeed
here while the response is lost, and the app then has no way to know whether the
funding landed. Without the unique key a single lost response would leave this
instance holding two records for money paid once.

Also whitelisted:

- `brchms.brchms_credits.api.get_funding(event_id=…)` — read one record back, or `None`.
- `brchms.brchms_credits.api.count_funding(event_id=…)` — how many rows exist for one key. Its purpose is to let an outside check assert **exactly one** rather than trusting the response payload.

### `POST /api/method/brchms.brchms_banner.api.get_active_banner`

Returns the one banner to show right now, or `null`. Selection: `is_active`, then
the visibility window against the server clock, then the most recently modified
wins. **An absence is a successful `null`, never an error** — the app renders
this on every page including login.

Also whitelisted: `brchms.brchms_banner.api.set_banner(...)` — create or replace
the active banner, for driving a notice from a script during a deploy.

## Installing

### On a development bench

```bash
cd /path/to/frappe-bench
bench get-app brchms /path/to/brchms-erpnext
bench --site <site> install-app brchms
bench --site <site> run-tests --app brchms
```

`<site>` here MUST be a local or throwaway site. The suite creates a real
submitted Payment Entry and cancels it in teardown, leaving cancelled GL history
behind; run it on an isolated clone or an independent instance, never on
production. See [`docs/TEST-ENVIRONMENT.md`](docs/TEST-ENVIRONMENT.md).

### On the Dockerised production bench

**The app code must be present at image build time.** It cannot be copied into a
running container — the copy is lost on recreation, and the app will appear to
work until the next `docker compose up`. The stack on the VPS builds a `custom`
image from `apps.json`:

```jsonc
// /opt/frappe_docker/apps.json
[
  { "url": "https://github.com/frappe/erpnext", "branch": "version-16" },
  { "url": "<git url for this app>", "branch": "main" }
]
```

```bash
/opt/frappe_docker/build-custom-image.sh     # on the VPS
docker compose -f ... up -d                  # recreate with the new image
bench --site frontend install-app brchms     # once, inside the container
```

Confirm the app survived by re-running `bench version` and checking `brchms`
appears — a rebuild that silently skipped `apps.json` looks like a working
deploy until the app is reinstalled.

## Creating the integration user

**Do not use `Administrator`.** The app's credentials are long-lived and live in
a `.env` file on a different host; a token that can do everything is a bad thing
to leave lying in one.

1. **User → New**: `restapp@breamal.com`, System User, **no roles**.
2. Assign **exactly one**: `Rest Integration`.
   - `Rest Integration` — read the credit, banner and support-doctype surfaces the
     integration calls need. The whitelisted methods are the authorisation
     boundary: they insert/update with `ignore_permissions` where the product
     requires it, so the credential never needs create, write or delete on the
     doctype. Verified live: this role carries `read: 1`, `create: 0`,
     `write: 0`, `delete: 0`.
   - `Rest Banner Manager` — is for a **human** who authors notices, not for this
     user. Giving the app's credential the ability to change the notice every
     user sees is a privilege it has no use for.
   - `Rest Support Agent` — is for a **human** who triages malfunction reports in
     the Desk. It has read/write on `Rest Malfunction Report`; it is not part of
     the integration credential.
3. **Settings → API Access → Generate Keys.** The secret is shown once.

CLI alternative (verify the signature against your v16 build before relying on it):

```bash
bench --site frontend execute frappe.core.doctype.user.user.generate_keys \
  --kwargs "{'user': 'restapp@breamal.com'}"
```

Then, in the Rest backend's environment:

| Variable | Value |
| --- | --- |
| `ERPNEXT_URL` | `https://erp.breamal.com` |
| `ERPNEXT_API_KEY` | the key |
| `ERPNEXT_API_SECRET` | the secret |
| `ERPNEXT_INSTANCE_ID` | the **site name**, e.g. `frontend` |
| `ERPNEXT_TIMEOUT_MS` | optional; default `10000` |

and set `COMMUNICATION_CREDIT_PROVIDER=erpnext` and/or
`PLATFORM_BANNER_PROVIDER=erpnext` when ready. **Never commit the key or
secret.**

`ERPNEXT_INSTANCE_ID` is not cosmetic: Rest refuses a confirmation when it is
unset, because the idempotency key it builds would not be unique across
instances and could collide with another deployment's.

## Proving it by hand

**Never run these probes against the production site.** Use an isolated clone or a
separate ERPNext instance — see [`docs/TEST-ENVIRONMENT.md`](docs/TEST-ENVIRONMENT.md).

```bash
curl -s -X POST https://<test-instance>/api/method/brchms.brchms_credits.api.confirm_funding \
  -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"instance_id":"frontend-test","event_id":"probe-1","purchase_reference":"PROBE-1",
       "organization_id":"1","funding_group_id":"g1","channel":"sms",
       "units":"10","amount_minor":"450","currency":"CAD","unit_price_micros":"450000"}'

# Run the identical call again. It must return the SAME event_id and confirmed_at.
# Then prove it created ONE row, not two:
curl -s -X POST https://<test-instance>/api/method/brchms.brchms_credits.api.count_funding \
  -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  -H 'Content-Type: application/json' -d '{"event_id":"probe-1"}'
# → {"message": 1}
```

## References

- Credit terms (discount, minimum, free credits): [`docs/CREDIT-TERMS.md`](docs/CREDIT-TERMS.md)
- Test environment (never production): [`docs/TEST-ENVIRONMENT.md`](docs/TEST-ENVIRONMENT.md)
- Settlement boundary: [`docs/SETTLEMENT-BOUNDARY.md`](docs/SETTLEMENT-BOUNDARY.md)
- ERPNext (v16): <https://github.com/frappe/erpnext>
- Frappe framework: <https://github.com/frappe/frappe>
- Frappe Docker custom images: <https://github.com/frappe/frappe_docker>
