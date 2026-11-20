# Club Gift Aid Manager

This project is a small CRUD-oriented Python implementation of the club volunteer mileage and gift-aid workflow.

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

## How the application is structured

The application uses Flask for HTTP routing, Jinja for page shells, browser-side JavaScript for interactive forms, Pydantic DTOs for boundary validation, SQLAlchemy models/repositories for object-oriented persistence, and SQLite for storage. The public functions in `club_gift_aid_manager/db.py` remain as a compatibility facade while persistence moves behind models and repositories. Route code should call that facade or a service rather than issue SQL directly.

```text
app.py                              WSGI and command-line entry point
club_gift_aid_manager/
  factory.py                        Flask creation, configuration, hooks and headers
  auth/decorators.py                reusable role and recent-login checks
  mfa/                              TOTP enrollment, challenges, recovery and reset
  views/                             Flask Blueprints and HTTP request/response logic
  dtos/                              Pydantic input/output shapes
  services/                          calculations, routing and background imports
  models/                            SQLAlchemy table mappings
  repositories/                     persistence contracts and implementations
  database.py                       SQLAlchemy engine and unit-of-work lifecycle
  db.py                             compatibility facade, schema and complex queries
  security/                          field encryption and protected-path helpers
  domain/roles.py                   authoritative application roles
templates/                           Jinja page shells
static/js/                           browser API calls, rendering and help behaviour
tests/                               unit, route, security, persistence and workflow tests
```

### Flask application lifecycle

`app.py` imports `create_app()` and exposes the resulting `app` object for a WSGI server. `create_app()` performs the following work in order:

1. Reads and validates deployment, session, encryption, database, CSP and HSTS configuration. Production starts only with a stable session secret, secure cookies, and a protected absolute database location outside the checkout.
2. Creates the Flask instance with this repository's `templates` and `static` directories.
3. Registers each module in `club_gift_aid_manager/views` as a Blueprint. A Blueprint groups related routes; it does not create another Flask application.
4. Registers request hooks and the shared `ValidationError` handler.
5. Creates or migrates the database schema before accepting traffic.

For every request, Flask then follows this lifecycle:

```text
request
  -> before_request: create CSP nonce, resolve session user, reject stale sessions,
                     enforce CSRF on writes, begin one write transaction
  -> Blueprint route: authorize role, validate DTO, call service/database facade
  -> after_request: commit or roll back the write transaction, add security headers
  -> teardown_request: roll back any transaction left open after an exception
```

The `before_request` hook is the common authentication boundary. Public endpoints are explicitly allowlisted. Every other endpoint resolves `session["user_id"]` against the database and checks `session_version`, account activity, and authentication state. POST, PUT, PATCH and DELETE requests must also provide the session's CSRF token. HTML requests redirect to the login page; API requests receive JSON errors.

Administrators and treasurers must also complete TOTP multi-factor authentication.
After password verification they receive only a short-lived pending session, which
cannot access application pages or APIs. First login requires enrollment with an
authenticator app, confirmation of a six-digit code, and acknowledgement that ten
single-use recovery codes have been stored securely. Later logins accept a fresh TOTP
or consume one recovery code. Replayed TOTP time steps are rejected, repeated failures
are temporarily locked, and MFA enrollment or reset invalidates existing sessions.

The `after_request` hook adds CSP, anti-framing, MIME-sniffing, referrer, permissions and cache-control headers. Production responses also receive HSTS. A request-specific nonce permits only the scripts and styles intentionally emitted by the templates.

### Blueprints and route handlers

Route modules have deliberately narrow responsibilities:

- `auth.py`: login, logout, constant-work password verification and persistent throttling.
- `people.py`: people records, administrator DSAR export and anonymization.
- `trips.py`: trip claims, mileage calculations, road distance and CSV imports.
- `destinations.py`: location maintenance and CSV imports.
- `finance.py`: declarations, donations and reimbursement payments.
- `admin.py`: users, mileage schedules, audit history and privacy notices.
- `reports.py`: role-scoped dashboards, reconciliation and financial reports.
- `main.py`: dashboard pages, public privacy notices and import-job polling.

Decorators are evaluated from bottom to top by Python. A typical protected route registers a Flask URL around a `roles_required(...)` wrapper, which checks `g.current_user` before the actual view runs. Particularly sensitive administrator writes also use `recent_authentication_required()`. UI visibility is only a convenience; these server-side checks remain authoritative.

Route handlers use `request.args` for read-only query parameters, `get_payload()` for JSON or form bodies, and `request.files` for bounded uploads. They return either `render_template(...)` for pages or `jsonify(...)` for APIs. Expected validation failures raise `ValidationError`, which the application-wide error handler converts to a consistent JSON response.

### DTO, service, model and repository flow

Pydantic DTOs describe data crossing a trust boundary. They reject unknown fields, normalize types, enforce lengths and ranges, and keep credential-bearing request shapes separate from safe response shapes. DTO validation does not replace authorization or database relationship checks.

Services contain work that does not belong to HTTP or table-mapping code. Mileage calculations use `Decimal` and return integer pence. Routing validates public outbound targets, refuses redirects, geocodes uncached locations and stores coordinates. CSV work runs through a bounded in-process executor so uploads cannot create an unlimited number of worker tasks.

SQLAlchemy model classes map Python attributes and relationships to SQLite tables. Sensitive model columns use `EncryptedText`, which encrypts on binding and decrypts on result loading. Repository interfaces describe persistence operations without Flask dependencies; concrete SQLAlchemy repositories receive a `Session`. `database.unit_of_work()` owns that session and commits successful work or rolls it back on an exception.

`db.py` remains the stable application-facing compatibility API for routes and administrative scripts. Runtime domain persistence is implemented with SQLAlchemy models, repositories and unit-of-work sessions. Direct SQLite access is deliberately confined to request-connection ownership and startup schema migrations, partial indexes and immutability triggers that are specific to SQLite. An architecture test prevents runtime domain functions from reopening raw connections.

### Browser interaction

Jinja templates render authenticated page shells, the current role, a CSRF meta tag, CSP nonce and privacy notice. `static/js/app.js` loads role-permitted JSON collections and renders escaped values into tables. Its `api()` helper sends JSON, adds the CSRF header to every non-GET request, and turns unsuccessful responses into visible errors. Server authorization is still required even when a button is hidden in JavaScript.

The road-distance operation is POST-only because it may call external services and cache coordinates. The routing service resolves configured hosts before connecting, permits only public addresses, rejects URL credentials and does not follow redirects into another network.

### Transactions and audit records

State-changing HTTP requests open one request transaction before route execution. Record changes and their audit event therefore commit together. An error response normally rolls the transaction back; permission denials and recorded authentication failures are intentionally committed so their security events are retained. Teardown always performs a final rollback as a safety net.

Audit records contain actor, action, entity, timestamp and changed field names, never submitted values or passwords. SQLite triggers prevent normal update or deletion of audit rows. Signed exports provide evidence outside the live database, but operators must store those exports separately.

## Configuration reference

| Variable | Purpose |
| --- | --- |
| `CLUB_GIFT_AID_ENV` | `development` or `production`; production enables strict startup checks and HSTS. |
| `CLUB_GIFT_AID_SECRET` | Stable Flask session-signing secret; at least 32 characters in production. |
| `CLUB_GIFT_AID_COOKIE_SECURE` | Must be true in production so session cookies are HTTPS-only. |
| `CLUB_GIFT_AID_PROXY_MODE` | `none` by default; set to `pythonanywhere` only when hosted behind PythonAnywhere's load balancer. |
| `CLUB_GIFT_AID_TRUSTED_HOSTS` | Comma-separated accepted hostnames; required in production. |
| `CLUB_GIFT_AID_LOG_LEVEL` | Structured JSON application log level; defaults to `INFO`. |
| `CLUB_GIFT_AID_DB` | Production SQLite path; must be absolute, private, and outside the checkout. |
| `GIFT_AID_ENC_KEY` | Comma-separated Fernet keys, newest first, used for encrypted columns. |
| `GIFT_AID_BLIND_INDEX_KEY` | Independent base64 key for deterministic exact-match indexes. |
| `CLUB_GIFT_AID_GEOCODER_URL` | Optional compatible public geocoding endpoint. |
| `CLUB_GIFT_AID_ROUTER_URL` | Optional compatible public road-routing endpoint. |
| `CLUB_GIFT_AID_CSP_REPORT_ONLY` | Trial CSP without enforcement; leave false for normal production protection. |
| `HSTS_MAX_AGE` | Production HSTS lifetime in seconds. |
| `HSTS_INCLUDE_SUBDOMAINS` / `HSTS_PRELOAD` | Optional HSTS directives; enable only when the whole domain is ready. |
| `GIFT_AID_BACKUP_DIR` | Private directory used by `backup.py create`. |
| `GIFT_AID_AUDIT_EXPORT_KEY` | Independent key used to sign audit exports. |
| `GIFT_AID_WEB_ROOTS` | Additional paths that protected data and exports must not use. |

Do not commit any of these secrets. Encryption, session signing, blind indexing and audit signing require separate keys because they protect different security properties.

### PythonAnywhere client addresses

PythonAnywhere places the public client address in `X-Real-IP`; `request.remote_addr` contains an internal load-balancer address. Set `CLUB_GIFT_AID_PROXY_MODE=pythonanywhere` in the private WSGI configuration so login throttling separates clients correctly. In this mode the application validates and uses only `X-Real-IP`. It deliberately ignores `X-Forwarded-For`, whose earlier entries may be supplied by untrusted clients, and rejects login processing if the trusted header is absent or malformed.

Do not enable this mode when running Flask directly or behind a different proxy. Direct deployments use the socket peer address and ignore forwarding headers. A different hosting topology needs its own reviewed trust boundary rather than a generic configurable header name.

## Run tests

```bash
python -m pip install --require-hashes -r requirements.txt
python -m pytest -q
```

The automated suite uses an isolated temporary SQLite database per test. It exercises business calculations, persistence and relationships, authorization and CSRF enforcement, CSV imports, privacy-notice upload, anonymization, and page rendering. Measure line and branch coverage with:

```bash
python -m pytest --cov=app --cov=club_gift_aid_manager --cov-branch --cov-report=term-missing -q
```

The report lists untested lines and branches; a passing test suite does not imply 100% code coverage. CI syntax-checks every tracked first-party JavaScript file with `node --check`.

CI enforces Ruff static and formatting checks, mypy on the typed domain/DTO/model/security boundaries, at least 95% first-party docstring coverage, syntax checks for every tracked JavaScript file, and at least 80% Python branch coverage. Run the same quality checks locally with:

```bash
ruff check club_gift_aid_manager app.py backup.py audit_export.py manage.py monitoring.py retention.py tests
ruff format --check club_gift_aid_manager app.py backup.py audit_export.py manage.py monitoring.py retention.py tests
mypy club_gift_aid_manager/domain club_gift_aid_manager/dtos club_gift_aid_manager/models club_gift_aid_manager/security club_gift_aid_manager/mfa
interrogate club_gift_aid_manager app.py backup.py audit_export.py manage.py monitoring.py retention.py
git ls-files -z '*.js' | xargs -0 -n1 node --check
python -m pytest --cov=app --cov=club_gift_aid_manager --cov-branch --cov-fail-under=80 -q
```

Hypothesis property tests complement example-based tests with generated monetary values, UK tax-year dates, malformed and extreme validation inputs, and quoted CSV data.

### Source documentation standard

First-party Python modules, classes, and callables have docstrings describing their
purpose and, where it is not obvious from the signature, their authorization rules,
side effects, or failure behaviour. The `interrogate` check above enforces complete
callable documentation outside migrations and tests. Complex business rules and
security decisions also have focused inline comments at the decision point.

Browser JavaScript is split by feature and uses descriptive function names. Comments
are reserved for behaviour that the code alone cannot explain, such as security
boundaries, compatibility constraints, and intentional browser workarounds. HTML,
CSS, configuration, migrations, generated lock files, and vendored assets are
documented at the section or file level where useful; they are not annotated line by
line. This keeps comments accurate and makes reviewable intent more visible than
mechanical narration.

Browser integration tests use Playwright with a disposable, seeded SQLite database. They cover CSRF transmission, modal forms, role-specific navigation, CSP enforcement, escaped/filterable tables, CSV downloads, and route-distance requests:

```bash
npm install
npx playwright install chromium
npm run test:browser
```

The Playwright server listens only on `127.0.0.1:5173`; its database and test artifacts are ignored by Git. Use `npm run test:browser:headed` when debugging interactions locally.

## Create the first administrator

There are no built-in credentials. Initialize the database, then create the first administrator interactively:

```bash
python app.py create-admin
```

The password is entered interactively and must be at least 15 characters with an
uppercase letter, lowercase letter, number, and special character. Additional
accounts are created by an administrator through `POST /admin/users`.

## Database migrations

Alembic revisions in `migrations/versions` are the authoritative schema history. Fresh development and test databases are upgraded automatically for convenience. Production startup never creates, alters, stamps, or upgrades schema objects: it reads `alembic_version` and refuses to start unless the database is exactly at the packaged migration head.

Back up the database before every migration. Set `CLUB_GIFT_AID_DB` and the encryption settings, then migrate independently of the web process:

```powershell
python manage.py db-current
python manage.py db-upgrade
python manage.py db-verify
```

Each revision contains reviewable `upgrade()` and `downgrade()` operations. Production rollback should normally restore the pre-migration backup because a schema downgrade cannot guarantee recovery of discarded or transformed data.

An existing pre-Alembic database has application tables but no `alembic_version`. Test a restored copy first. After approval, back up the live database and adopt it once with `python manage.py db-stamp-head`, followed by `python manage.py db-verify`. Stamping executes no migration DDL and refuses schemas missing a modeled table or column. Never stamp an unknown or unreviewed database merely to bypass startup checks.

## Run the app

Set a stable, secret session key before starting the application. On PowerShell:

```powershell
$env:CLUB_GIFT_AID_SECRET = "a-long-random-secret-value"
$env:CLUB_GIFT_AID_COOKIE_SECURE = "true"
$env:GIFT_AID_ENC_KEY = "a-generated-fernet-key"
$env:GIFT_AID_BLIND_INDEX_KEY = "a-separate-base64-encoded-32-byte-key"
python app.py
```

Use HTTPS when `CLUB_GIFT_AID_COOKIE_SECURE=true`. The application enables database-backed authentication by default, stores password hashes (never plaintext passwords), and protects session-authenticated state-changing requests with CSRF tokens.

### Staff multi-factor authentication

MFA is mandatory for administrator and treasurer accounts and is not currently
required for volunteers. Staff can use any standards-compatible TOTP authenticator,
including common authenticator apps and password managers. The enrollment QR code is
generated locally; its secret is encrypted in the database using `GIFT_AID_ENC_KEY`.
Recovery codes are stored only as keyed hashes and are displayed once.

If a staff member loses both their authenticator and recovery codes, an administrator
must verify their identity using the club's approved offline process, open **User
accounts**, and select **Reset MFA**. The reset is recent-password protected, audited,
and signs out every session belonging to that account. The user enrolls again after
their next password login. Administrators cannot retrieve an existing secret or
recovery code.

Administrators can also perform an assisted password reset from **User accounts**
after verifying the account holder offline. The administrator assigns a policy-
compliant temporary password; the reset invalidates every target session and is
recorded without the password value. The temporary credential opens only the password
replacement page. The user must choose a different password before receiving normal
access or proceeding to staff MFA. Password creation, reset, and replacement all use
the same 15-character, uppercase, lowercase, number, and special-character policy.

For production, set `CLUB_GIFT_AID_ENV=production`, supply a stable secret of at least 32 characters in `CLUB_GIFT_AID_SECRET`, set `CLUB_GIFT_AID_DB` to an absolute persistent path outside the source checkout, enable HTTPS, and set `CLUB_GIFT_AID_COOKIE_SECURE=true`. Upgrade and verify the database before starting the web process. The application refuses to start in production without these settings or at an outdated/unversioned database revision; it also refuses to start its development server in production. State-changing requests and their audit records are committed in one SQLite transaction. Login failures are throttled with short-lived counters keyed by HMACs of the source address and username; the raw values are not stored. SQLite files receive owner-only permissions on POSIX hosts.

Sensitive SQLite fields are encrypted using keys supplied outside the repository. Key custody, provider controls, backup access, restoration testing, incident response, and an approved retention schedule remain the club/operator's responsibility. Existing plaintext development databases are not migrated and must be deleted and recreated. Review [SECURITY-OPERATIONS.md](./SECURITY-OPERATIONS.md) before deployment. The provided privacy notice uses a policy-led retention statement, but still needs the club's controller/contact, lawful basis, and other marked details completed and reviewed before use.

## Roles

- **Admin:** all application data, plus account creation and role/activation management.
- **Treasurer:** people, trips, declarations, donations, payments, and payment reconciliation; no account management or club-wide financial report.
- **Volunteer:** their own linked person record and trips only; cannot view financial records or other volunteers' trips.

Volunteer accounts must be linked to an existing person. Role checks and volunteer record scoping are enforced by the server, not just the UI.

The authenticated application includes a **How it works** guide with separate administrator and user workflows, including a treasurer-specific explanation of access.

The dashboard gives administrators and treasurers reminders for trips without a linked in-app reimbursement, active Gift Aid-eligible donations without an active unwithdrawn declaration link, and a missing active home ground. These are record checks, not approval or tax determinations. Administrators also see recent audit activity; volunteers see only their own trip count and recent trips.

Administrators can maintain complete mileage rate schedules from the Mileage rates page. Each schedule has an effective date and whole-pence-per-mile rates for cars (first 10,000 and over), motorcycles, bicycles, and passenger allowance. The car mileage band accumulates chronologically per volunteer and UK tax year; motorcycle and bicycle trips do not consume the car band. The previous schedule applies through the day before the next schedule begins. Calculations select a schedule using the trip occurrence date, including when a trip is entered after a rate change. The original built-in rates are retained as the baseline before the first configured change.

## API examples

- GET /people
- POST /people
- GET /trips
- POST /trips
- POST /trips/import (multipart CSV upload; admins may select any person, other roles are assigned to their linked person)
- GET /trips/import/template (download CSV headers; includes person_id for admins and omits assignment columns for other roles)
- POST /api/route-distance (CSRF-protected road miles lookup for the trip form)
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

The Privacy / GDPR button on each page displays the contents of `privacy-notice.txt` in the application folder. The supplied file is a template and must be completed and reviewed by the club before use. An administrator can edit and save the notice directly in the privacy-statement dialog, or optionally upload a UTF-8 `.txt` file, up to 64 KB. The application account must be able to write to its installation folder. Content is displayed as plain text, and successful replacements are recorded in the audit log without recording the statement text.

Every visible input, selector, and text area receives a keyboard-focusable `?` help button. The help text is keyed by field ID or name in `static/js/help.js`; table search inputs receive search-specific guidance. When adding a form field, add a concise purpose description to that map. Hidden state fields and action buttons intentionally do not receive field popovers.

The trip form requests one-way driving distance from the route API when both locations are selected. It geocodes a location once and caches its coordinates in the database; a location address edit clears that cache. If a location name/address lookup does not match but a postcode exists, the geocoder retries using the postcode alone, which may be less precise than the venue entrance. The default services are the public OpenStreetMap Nominatim geocoder and OSRM demo router, which require no API key but are community services with usage limits and no availability guarantee. The application sends only location names and addresses, not volunteer details. OpenStreetMap attribution is shown on the trip form. For production or higher usage, provide a compatible hosted service using `CLUB_GIFT_AID_GEOCODER_URL` and `CLUB_GIFT_AID_ROUTER_URL`. If a route cannot be found, mileage stays blank and the form reports the problem rather than silently accepting a guessed distance.

On the Donations page, one donation can be linked to one or more existing reimbursement payments belonging to the same person. These relationships are references only: amounts are not allocated or offset. A donation linked to multiple payments appears against each payment for reference, but counts once in person-level reconciliation to avoid double counting. Donations can be edited, made dormant, and restored; dormant donations are excluded from financial totals and reconciliation.

Payments can be linked to one or more of the selected person's trips. The payment amount is calculated from, and must equal, the full reimbursement total for the selected trips. Each trip can belong to at most one payment, and linked trips cannot be deleted or reassigned to another person. Partial allocations are not supported.

Trips can be imported from the Trips page using a UTF-8 CSV; use Download CSV template to get headers appropriate to your role. Required columns are `origin_id`, `destination_id`, `trip_date` (YYYY-MM-DD), `vehicle_type` (`car`, `motorcycle`, or `bicycle`), and `declaration_accepted` (`yes`). Optional columns are `return_trip` (`yes`/`no`) and `passengers` (default `0`). Road distance is calculated from each active origin/destination pair during import, so the CSV does not need a mileage column; any legacy `miles_one_way` column is ignored. Administrators receive a `person_id` column and can assign trips to any person; treasurers and volunteers receive a template without person-assignment columns and can only create trips for their own linked person. The trip form and CSV import enforce this rule on the server. All row and route errors are listed in the upload dialog, and no trips are saved unless the complete file validates and all distances can be calculated.

The admin-only Audit log records the actor, action, record type and ID, timestamp, and changed field names for successful record operations. It does not store changed values or passwords. Existing records created before auditing was enabled have no reliable creator history and are shown as unknown; the audit page retains the latest 500 events.

Removing a person from the People page anonymizes their record rather than deleting related history. The person’s name is replaced with a non-identifying placeholder, their email, phone, notes, and linked declaration name/address/postcode/evidence reference are cleared, and any linked login is deactivated. Trips, distances, payment/donation amounts, and audit events are retained. Apply this process consistently with the club’s legal record-retention obligations and documented privacy policy.

Manage opponent teams and club grounds on the Destinations page. Flag one active location as the club home ground; it becomes the default origin when entering trips. Trip origins and destinations are selected from active locations. Existing databases are migrated automatically; historical trips without an origin or destination remain visible as legacy records.

Locations can also be imported from CSV on the Locations page. Use Download CSV template for the supported headers: `name`, `address`, `town_city`, `postcode`, `notes`, and `home_ground`. The `name` column is required, and every non-empty row must include a location name. Optional `home_ground` values are yes/no. The entire upload is validated before any locations are saved. If a location name is repeated within the CSV, the first row is imported and later duplicate rows are skipped and listed in the result. A name already present in the database is identified in an error and prevents the upload from being saved.
