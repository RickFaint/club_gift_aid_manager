# Club Gift Aid Manager

This project is a small but fuller CRUD-oriented Python implementation of the rugby club volunteer mileage and gift-aid workflow.

It includes:

- person management
- opponent team and club destinations masterfile
- trip creation and update workflows
- declaration records
- payment and donation tracking
- payments linked to one or more complete trip records
- admin audit log for record creation and changes
- optional donation-to-many-reimbursements links for reference, kept as separate money records
- summary reporting
- mileage calculation and declaration enforcement

## Run tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

The automated suite uses an isolated temporary SQLite database per test. It exercises business calculations, persistence and relationships, authorization and CSRF enforcement, CSV imports, privacy-notice upload, anonymization, and page rendering. Measure line and branch coverage with:

```bash
python -m pytest --cov=app --cov=club_gift_aid_manager --cov-branch --cov-report=term-missing -q
```

The report lists untested lines and branches; a passing test suite does not imply 100% code coverage. Client-side JavaScript syntax is checked separately with `node --check static/js/help.js`, `node --check static/js/privacy.js`, and `node --check static/js/app.js`.

## Create the first administrator

There are no built-in credentials. Initialize the database, then create the first administrator interactively:

```bash
python app.py create-admin
```

The password is entered interactively and must be at least 12 characters. Additional accounts are created by an administrator through `POST /admin/users`.

## Run the app

Set a stable, secret session key before starting the application. On PowerShell:

```powershell
$env:CLUB_GIFT_AID_SECRET = "a-long-random-secret-value"
$env:CLUB_GIFT_AID_COOKIE_SECURE = "true"
python app.py
```

Use HTTPS when `CLUB_GIFT_AID_COOKIE_SECURE=true`. The application enables database-backed authentication by default, stores password hashes (never plaintext passwords), and protects session-authenticated state-changing requests with CSRF tokens.

For production, serve the application only over HTTPS and set `CLUB_GIFT_AID_COOKIE_SECURE=true`. Protect the SQLite database and backups with appropriate disk/database encryption and strict filesystem permissions. Restrict database-file and backup access to authorised operators, test backup restoration, establish an incident response process, and review the security of the hosting platform and any reverse proxy. These operational controls must be configured and maintained by the deployment operator.

## Roles

- **Admin:** all application data, plus account creation and role/activation management.
- **Treasurer:** people, trips, declarations, donations, payments, and payment reconciliation; no account management or club-wide financial report.
- **Volunteer:** their own linked person record and trips only; cannot view financial records or other volunteers' trips.

Volunteer accounts must be linked to an existing person. Role checks and volunteer record scoping are enforced by the server, not just the UI.

The authenticated application includes a **How it works** guide with separate administrator and user workflows, including a treasurer-specific explanation of access.

The dashboard gives administrators and treasurers reminders for trips without a linked in-app reimbursement, active Gift Aid-eligible donations without an active unwithdrawn declaration link, and a missing active home ground. These are record checks, not approval or tax determinations. Administrators also see recent audit activity; volunteers see only their own trip count and recent trips.

Administrators can maintain complete mileage rate schedules from the Mileage rates page. Each schedule has an effective date and whole-pence-per-mile rates for cars (first 10,000 and over), motorcycles, bicycles, and passenger allowance. The previous schedule applies through the day before the next schedule begins. Calculations select a schedule using the trip occurrence date, including when a trip is entered after a rate change. The original built-in rates are retained as the baseline before the first configured change.

## API examples

- GET /people
- POST /people
- GET /trips
- POST /trips
- POST /trips/import (multipart CSV upload; admins may select any person, other roles are assigned to their linked person)
- GET /trips/import/template (download CSV headers; includes person_id for admins and omits assignment columns for other roles)
- GET /api/route-distance?origin_id=<id>&destination_id=<id> (road miles for trip form)
- GET /destinations (active destinations for volunteers; all destinations for admin/treasurer)
- POST/PUT/DELETE /destinations (admin/treasurer)
- POST /destinations/import (admin/treasurer; multipart CSV upload)
- GET /destinations/import/template (admin/treasurer; download supported location CSV headers)
- GET /donations
- POST /donations
- PUT /donations/<id> (edit or mark dormant/reactivate)
- GET/POST /declarations
- GET/PUT/DELETE /declarations/<id> (DELETE marks a declaration dormant; it never removes evidence)
- GET /payments
- POST /payments
- GET /admin/mileage-rates (admin only; complete effective-date mileage schedules)
- POST /admin/mileage-rates (admin only; add a complete mileage schedule)
- GET /admin/audit (admin only)
- GET /reports/summary
- GET /reports/payment-reconciliation (admin/treasurer only; totals by person)
- GET /reports/page and GET /reports/financial (admin only; club totals and volunteer trip/finance summary)
- Admin Reports page also provides date-range filters and CSV downloads for trips, donations, and payments; each export contains only the currently visible table rows.
- GET/POST /admin/users (admin only)
- PATCH /admin/users/<id> (admin only)
- POST /admin/privacy-notice (admin only; replace the shared privacy statement with a UTF-8 text file)

All POST/PUT/PATCH/DELETE endpoints require the session CSRF token. Browser JavaScript reads it from the `csrf-token` meta tag and sends it in the `X-CSRF-Token` header.

The administrator Reports page summarizes all recorded expenses paid and active donations. Its estimated Gift Aid amount is 25% of active donations flagged as Gift Aid eligible; it is an estimate, not a tax claim. The volunteer table shows trip count, total recorded miles, payment totals, active donations, and expenses minus donations. The difference is informational only and does not offset or alter either transaction.

The Reports page includes independently filterable sections for the volunteer summary, trips, donations, and payments. Each section can be filtered by volunteer. Trips also support date, tax-year, vehicle, and status filters; donations support date, Gift Aid eligibility, status, and method filters; payments support date, method, and related-donation filters. Use Export CSV to download exactly the rows currently shown in that table. The club-wide total cards are not affected by these filters. CSV files include headers; currency amounts are exported as decimal pounds.

The Privacy / GDPR button on each page displays the contents of `privacy-notice.txt` in the application folder. The supplied file is a template and must be completed and reviewed by the club before use. An administrator can replace it from the privacy-statement dialog by uploading a UTF-8 `.txt` file up to 64 KB; the application account must be able to write to its installation folder. Uploaded content is displayed as plain text, and each successful replacement is recorded in the audit log without recording the statement text.

Every visible input, selector, and text area receives a keyboard-focusable `?` help button. The help text is keyed by field ID or name in `static/js/help.js`; table search inputs receive search-specific guidance. When adding a form field, add a concise purpose description to that map. Hidden state fields and action buttons intentionally do not receive field popovers.

The trip form requests one-way driving distance from the route API when both locations are selected. It geocodes a location once and caches its coordinates in the database; a location address edit clears that cache. If a location name/address lookup does not match but a postcode exists, the geocoder retries using the postcode alone, which may be less precise than the venue entrance. The default services are the public OpenStreetMap Nominatim geocoder and OSRM demo router, which require no API key but are community services with usage limits and no availability guarantee. The application sends only location names and addresses, not volunteer details. OpenStreetMap attribution is shown on the trip form. For production or higher usage, provide a compatible hosted service using `CLUB_GIFT_AID_GEOCODER_URL` and `CLUB_GIFT_AID_ROUTER_URL`. If a route cannot be found, mileage stays blank and the form reports the problem rather than silently accepting a guessed distance.

On the Donations page, one donation can be linked to one or more existing reimbursement payments belonging to the same person. These relationships are references only: amounts are not allocated or offset. A donation linked to multiple payments appears against each payment for reference, but counts once in person-level reconciliation to avoid double counting. Donations can be edited, made dormant, and restored; dormant donations are excluded from financial totals and reconciliation.

Payments can be linked to one or more of the selected person's trips. The payment amount is calculated from, and must equal, the full reimbursement total for the selected trips. Each trip can belong to at most one payment, and linked trips cannot be deleted or reassigned to another person. Partial allocations are not supported.

Trips can be imported from the Trips page using a UTF-8 CSV; use Download CSV template to get headers appropriate to your role. Required columns are `origin_id`, `destination_id`, `trip_date` (YYYY-MM-DD), `vehicle_type` (`car`, `motorcycle`, or `bicycle`), and `declaration_accepted` (`yes`). Optional columns are `return_trip` (`yes`/`no`) and `passengers` (default `0`). Road distance is calculated from each active origin/destination pair during import, so the CSV does not need a mileage column; any legacy `miles_one_way` column is ignored. Administrators receive a `person_id` column and can assign trips to any person; treasurers and volunteers receive a template without person-assignment columns and can only create trips for their own linked person. The trip form and CSV import enforce this rule on the server. All row and route errors are listed in the upload dialog, and no trips are saved unless the complete file validates and all distances can be calculated.

The admin-only Audit log records the actor, action, record type and ID, timestamp, and changed field names for successful record operations. It does not store changed values or passwords. Existing records created before auditing was enabled have no reliable creator history and are shown as unknown; the audit page retains the latest 500 events.

Removing a person from the People page anonymizes their record rather than deleting related history. The person’s name is replaced with a non-identifying placeholder, their email, phone, notes, and linked declaration name/address/postcode/evidence reference are cleared, and any linked login is deactivated. Trips, distances, payment/donation amounts, and audit events are retained. Apply this process consistently with the club’s legal record-retention obligations and documented privacy policy.

Manage opponent teams and club grounds on the Destinations page. Flag one active location as the club home ground; it becomes the default origin when entering trips. Trip origins and destinations are selected from active locations. Existing databases are migrated automatically; historical trips without an origin or destination remain visible as legacy records.

Locations can also be imported from CSV on the Locations page. Use Download CSV template for the supported headers: `name`, `address`, `town_city`, `postcode`, `notes`, and `home_ground`. The `name` column is required, and every non-empty row must include a location name. Optional `home_ground` values are yes/no. The entire upload is validated before any locations are saved. If a location name is repeated within the CSV, the first row is imported and later duplicate rows are skipped and listed in the result. A name already present in the database is identified in an error and prevents the upload from being saved.
