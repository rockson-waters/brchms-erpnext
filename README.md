# Rest — the Frappe app behind the Rest connection

The app is called **Rest**. The Python package, the Frappe app name, the module
directories and this repository keep the historical `brchms` identifier, because
those are the machine keys the consuming application addresses
(`brchms.brchms_credits.api.confirm_funding`). Where a human reads a name, it says
Rest.

Two jobs, in one app:

| Module | What it is |
| --- | --- |
| `brchms_credits` | The **funding authority**. `confirm_funding` records what has been paid for — once — and hands the Rest app the units and money it needs to move its own credit ledger. |
| `brchms_banner` | The **announcement banner** the Rest app shows on every page (maintenance, upgrades, outages). |

One app rather than two: they share one deployment, one integration user, one
image rebuild and one version. Nothing separates them enough to justify doubling
each of those.

## The contract Rest speaks

Both endpoints are whitelisted methods, so Frappe wraps their return value in
`message`. Both use token auth: `Authorization: token <api_key>:<api_secret>`.

### `POST /api/method/brchms.brchms_credits.api.confirm_funding`

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
   - `Rest Integration` — read the funding and banner doctypes. That is the whole
     requirement for calling both methods: `confirm_funding` reaches the database
     as the method, not as the calling user, so the credential never needs
     create, write or delete on the doctype. Verified live: this role carries
     `read: 1`, `create: 0`, `write: 0`, `delete: 0`.
   - `Rest Banner Manager` — is for a **human** who authors notices, not for this
     user. Giving the app's credential the ability to change the notice every
     user sees is a privilege it has no use for.
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

```bash
curl -s -X POST https://erp.breamal.com/api/method/brchms.brchms_credits.api.confirm_funding \
  -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"instance_id":"frontend","event_id":"probe-1","purchase_reference":"PROBE-1",
       "organization_id":"1","funding_group_id":"g1","channel":"sms",
       "units":"10","amount_minor":"450","currency":"CAD","unit_price_micros":"450000"}'

# Run the identical call again. It must return the SAME event_id and confirmed_at.
# Then prove it created ONE row, not two:
curl -s -X POST https://erp.breamal.com/api/method/brchms.brchms_credits.api.count_funding \
  -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  -H 'Content-Type: application/json' -d '{"event_id":"probe-1"}'
# → {"message": 1}
```

## References

- ERPNext (v16): <https://github.com/frappe/erpnext>
- Frappe framework: <https://github.com/frappe/frappe>
- Frappe Docker custom images: <https://github.com/frappe/frappe_docker>
