# Testing credit changes without touching production

**The production ERPNext site is never a test target.** No `bench run-tests`, no
ad-hoc probes, no browser walk-through, no purchase, no settlement and no email
or SMS test run against it.

The reason is not only that tests write data. `brchms/tests/test_proposals.py`
deliberately creates a real, submitted Payment Entry and cancels/deletes it in
teardown; ERPNext keeps cancelled GL rows after the document is gone, so a test
run leaves cancelled ledger history behind even when every test passes. That is
acceptable only on a disposable site.

Use one of the two environments below. Both are outside production.

## Option A — isolated clone of production (preferred for realism)

1. **Back up production** (an operator action, not a test):

   ```bash
   bench --site <prod-site> backup --with-files
   ```

   Record the exact dump file names.

2. **Copy the backup to a separate bench/container**, then create a *new* site
   name and restore into it:

   ```bash
   bench new-site <test-site> --db-name <test-db> ...
   bench --site <test-site> restore <prod-dump> \
     --with-public-files <public-files-tar> \
     --with-private-files <private-files-tar>
   ```

   Do not reuse the production site name, database, Redis namespace or Docker
   compose project.

3. **Make the clone safe to test on**:

   ```bash
   bench --site <test-site> set-config mute_emails 1
   ```

   - Disable or replace every real SMS provider credential on the clone.
   - Disable every live Payment Gateway Account, or replace it with sandbox
     credentials. Verify the payment page without paying.
   - Create a dedicated `Rest Integration` API key/secret on the clone. Never
     copy production credentials into a test environment.

4. **Use a test deployment identity**. Give the test app
   `ERPNEXT_INSTANCE_ID=<test-site>` (or a distinct value such as
   `frontend-test`) and use synthetic organization/group ids for any purchase
   the app drives. The clone contains real organization ids; a test purchase
   naming one would credit a real balance *on the clone* and make the run
   ambiguous.

5. **Install/migrate and test**:

   ```bash
   bench --site <test-site> migrate
   bench --site <test-site> run-tests --app brchms
   ```

   Then point a **non-production** Strapi/UI environment at the clone
   (`ERPNEXT_URL`, test key/secret, `ERPNEXT_INSTANCE_ID=<test-site>`) and run
   the app's opt-in live/integration credit tests.

6. **Destroy the clone** after acceptance, including its copy of production
   data. A clone is a second copy of real data and is not a permanent staging
   site.

## Option B — fresh independent ERPNext instance

Use a throwaway bench/container with no production data:

```bash
bench new-site <test-site>
bench --site <test-site> install-app erpnext
bench --site <test-site> install-app brchms
```

Seed what the credit tests need: a Company, its currency, a
`Rest Credit Billing Region`, a `Rest Credit Rate` row, and a test organization
with a root group. `brchms/tests/test_credits_api.py` and
`test_proposals.py` create their own rate fixtures; the clone path is closer to
production because it exercises real configured companies and currencies.

Then run the same migrate/test commands as Option A.

## Both options

- `bench --site <test-site> run-tests --app brchms` is the only supported test
  command. Do not run modules against production "just to check".
- Payment pages are verified by asserting the invoice/payment URL exists, or by
  paying with sandbox credentials on the test site. No real charge.
- Outgoing mail is muted; no real email or SMS may be produced.
- The app's browser checks (if the user authorizes them) run against a
  non-production app environment pointed at the test ERPNext only.
- After acceptance, record which option was used, the test site name, and the
  test run output in the hand-off. A claim that is not from one of these
  environments is unverified, not "probably fine".
