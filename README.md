# brchms — the Frappe app behind the BRCHMS connection

Two jobs, in one app:

| Module | What it is |
| --- | --- |
| `brchms_credits` | The **funding authority**. `confirm_funding` records what has been paid for — once — and hands the BRCHMS app the units and money it needs to move its own credit ledger. |
| `brchms_banner` | The **announcement banner** the BRCHMS app shows on every page (maintenance, upgrades, outages). |

One app rather than two: they share one deployment, one integration user, one
image rebuild and one version. Nothing separates them enough to justify doubling
each of those.

## The contract BRCHMS speaks

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
  "unit_price_micros": "45000"
}

// response
{ "message": {
    "event_id", "instance_id", "organization_id", "funding_group_id", "channel",
    "units", "amount_minor", "currency", "unit_price_micros",
    "purchase_reference", "confirmed_at"
} }
```

**`event_id` is the idempotency key and is unique on the doctype.** Replaying a
confirmation returns the same record and creates exactly one row. This is not a
nicety: BRCHMS reaches this instance over the network, the request can succeed
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

1. **User → New**: `integration@breamal.com`, System User, and **no roles** except the two below.
2. Assign exactly these roles:
   - `BRCHMS Integration` — read the funding and banner doctypes. That is all it needs to call both methods.
   - `BRCHMS Banner Manager` — only if this user will also *author* banners. A separate credential for authoring is better.
3. **Settings → API Access → Generate Keys.** The secret is shown once.

CLI alternative (verify the signature against your v16 build before relying on it):

```bash
bench --site frontend execute frappe.core.doctype.user.user.generate_keys \
  --kwargs "{'user': 'integration@breamal.com'}"
```

Then, in the BRCHMS backend's environment:

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

`ERPNEXT_INSTANCE_ID` is not cosmetic: BRCHMS refuses a confirmation when it is
unset, because the idempotency key it builds would not be unique across
instances and could collide with another deployment's.

## Proving it by hand

```bash
curl -s -X POST https://erp.breamal.com/api/method/brchms.brchms_credits.api.confirm_funding \
  -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"instance_id":"frontend","event_id":"probe-1","purchase_reference":"PROBE-1",
       "organization_id":"1","funding_group_id":"g1","channel":"sms",
       "units":"10","amount_minor":"450","currency":"CAD","unit_price_micros":"45000"}'

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
