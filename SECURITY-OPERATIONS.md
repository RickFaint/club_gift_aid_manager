# Security and deployment operations

This application contains contact details, mileage records, donation and payment records, and Gift Aid declaration information. These controls support safer operation; they do not establish legal compliance. The club, as data controller, should review its purposes, lawful bases, retention schedule, hosting terms, and response arrangements before entering real data.

## Production settings

Use the hosting provider's WSGI deployment, not `python app.py` or Flask's development server. Configure these values in a private host configuration, not in Git:

- `CLUB_GIFT_AID_ENV=production`
- `CLUB_GIFT_AID_SECRET`: a stable, randomly generated secret with at least 32 characters
- `CLUB_GIFT_AID_COOKIE_SECURE=true`
- `CLUB_GIFT_AID_DB`: an absolute path in a private persistent data directory, outside the source checkout and any static/public directory
- `GIFT_AID_ENC_KEY`: comma-separated Fernet keys, newest first
- `GIFT_AID_BLIND_INDEX_KEY`: a separate URL-safe base64 key containing at least 32 random bytes
- `GIFT_AID_BACKUP_DIR`: an absolute private backup directory outside repository and web/static paths
- `HSTS_MAX_AGE=31536000`: HSTS lifetime in seconds
- `HSTS_INCLUDE_SUBDOMAINS=false` and `HSTS_PRELOAD=false`: enable only after confirming control of every subdomain and preload suitability

For example, generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store it in the provider's private application configuration. If the provider has no environment-variable facility, put it only in a host-private WSGI configuration with restricted access; never paste it into this repository, a public issue, or a shared log.

On PythonAnywhere, create a **manual Flask web app** using the same Python version as the virtual environment, point the Web tab at the virtual environment, and edit the WSGI configuration file to import `app` from this project as `application`. Configure HTTPS in the Web tab and verify that the browser uses HTTPS before entering real data. A suitable layout is `/home/<username>/club-gift-aid-data/club.sqlite3` for the database and `/home/<username>/club-gift-aid-backups/` for backups. Neither may be under the repository, a static mapping, OneDrive, or Dropbox. List any additional web-accessible roots as comma-separated absolute paths in `GIFT_AID_WEB_ROOTS`.

Only the PythonAnywhere account owner and explicitly authorised operators should have shell, database, backup, WSGI configuration, or key access. The application sets data and backup directories to mode `0700` and database/backup files to `0600` on POSIX. Production is refused on Windows because POSIX modes do not harden Windows ACLs. Windows is development-only and emits a warning; use fake data there.

The secure-cookie flag protects session cookies only when the site is served over HTTPS. Production startup will fail if the stable secret or secure-cookie setting is missing, but it cannot prove that the hosting provider's TLS or proxy configuration is correct. Confirm HTTPS and certificate renewal in the hosting dashboard.

HSTS is emitted only in production. It defaults to `max-age=31536000`; `includeSubDomains` and `preload` are deliberately absent unless their environment variables are explicitly enabled because a PythonAnywhere app may not control its parent domain.

The centralized `finalize_request_transaction` response hook in `app.py` overwrites cache policy on every non-static response with `Cache-Control: no-store, private, max-age=0`, `Pragma: no-cache`, and `Expires: 0`. This covers authenticated HTML and JSON, login and error pages, redirects, and downloads; versioned CSS, JavaScript, and image responses retain normal static caching. Authenticated pages also reload when restored from the browser back/forward cache. Back-button logout check last completed: **2026-11-13** (see the verification result recorded with the corresponding security change).

Bootstrap **5.3.3** is vendored under `static/vendor/bootstrap/5.3.3/` with its MIT licence; templates load no CDN assets. The centralized `finalize_request_transaction` hook also sets the nonce-based Content Security Policy, Permissions Policy, `X-Content-Type-Options`, and `Referrer-Policy`. Set `CLUB_GIFT_AID_CSP_REPORT_ONLY=true` only while trialling a policy; the default is enforcing. To update Bootstrap, review the upstream release and licence, download the exact CSS and bundled JavaScript into a new versioned vendor directory, verify their published hashes, update all three template references, run the template/CSP tests, and manually exercise every Bootstrap component in Chrome with the console open before removing the old version.

## Field encryption and key storage

Sensitive values are encrypted before SQLite receives them using authenticated Fernet encryption. Ciphertext is marked `enc:v1:`; unmarked plaintext or corrupt ciphertext in an encrypted column raises an error. `MultiFernet` accepts the comma-separated `GIFT_AID_ENC_KEY` list, newest first. A separate HMAC-SHA256 key in `GIFT_AID_BLIND_INDEX_KEY` creates exact-match blind indexes for usernames and location names. Blind indexes reveal equality patterns but not the source text.

Encrypted fields are: person name, email, phone and notes; username; Gift Aid name, address, postcode, scope and evidence reference; location name, address, town/city, postcode, notes and coordinates; trip vehicle, mileage, passenger count and declaration wording; donation amount, method, references and receipt ID; payment batch, amount, method and reference; and audit actor username. IDs, relationships, dates, roles and status flags remain visible metadata. Passwords remain one-way password hashes.

Generate independent values with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and `python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`. Store them in private PythonAnywhere WSGI environment assignments, or load them from a mode `0600` shell environment file outside the repository. The web app and scheduled backup task must load the same protected settings; scheduled tasks do not inherit assignments made only inside the WSGI process. Never commit keys, put a key file inside the repository, paste keys into logs/issues, or keep them with the database or backups. Maintain a separately protected recovery copy. Losing every configured Fernet key makes the encrypted records unrecoverable.

Encryption-key rotation procedure:

1. Generate a new Fernet key and prepend it to `GIFT_AID_ENC_KEY`, retaining old keys after it.
2. Reload the web app and confirm existing records can be read.
3. Create and verify a protected backup using the combined key list.
4. Stop application writes and run `python backup.py rotate-keys --database /home/<username>/club-gift-aid-data/club.sqlite3`.
5. Create another backup, run `python backup.py verify <backup-file>`, and test the application.
6. Remove retired keys only after the live database and every retained backup that must remain readable has been rotated or expired.

The blind-index key is stable. Changing it requires an offline rebuild of both blind-index columns and is not part of routine Fernet rotation.

## Backups and restore verification

`backup.py` uses SQLite's online backup API rather than copying a live database file. Sensitive columns remain ciphertext in backups, while metadata remains visible. Configure a daily PythonAnywhere scheduled task similar to:

```bash
set -a && . /home/<username>/.club-gift-aid-secrets && set +a && cd /home/<username>/Club_Gift_Aid_Manager && /home/<username>/.virtualenvs/club/bin/python backup.py create --database /home/<username>/club-gift-aid-data/club.sqlite3 --directory /home/<username>/club-gift-aid-backups --retention-days 30
```

The default retention is 30 days. A successful run verifies the new backup, then deletes only recognized `club-gift-aid-*.sqlite3.backup` files older than the configured period. Restrict scheduled-task logs because they contain operational paths. Keys must be stored separately from backups.

At least quarterly, copy a selected backup to a separate private test directory, run `python backup.py verify /private/path/to/file`, and start an isolated application instance against the restored file with the correct keys. Confirm representative records can be read and securely delete the test copy. Record the exercise without personal values:

- Last restore-test date: ____________________
- Tested by: ____________________
- Backup date/reference: ____________________
- Integrity and decryption result: ____________________
- Restored application result: ____________________
- Test copy deleted date: ____________________

## Login and audit controls

The login flow limits repeated failures for a source address and for a source-address/username pair, and stores HMAC-derived keys rather than raw addresses or usernames. The account-pair limit is five failures in 15 minutes; the source-address limit is 30 failures in 15 minutes. A threshold starts a 15-minute lockout. These are basic protections, not a substitute for MFA or provider-level abuse monitoring. The app does not trust a caller-supplied `X-Forwarded-For` header; confirm that the hosting platform provides a reliable client address to the WSGI request.

Creating or changing accounts, publishing the privacy notice, and changing mileage rates require a password login within the previous 15 minutes. If the window expires, sign out and sign in again before retrying. Role changes and deactivation invalidate existing sessions. Enable MFA on every hosting/operator account; application-user MFA still requires an approved identity-provider or enrollment/recovery design and must not be represented as enabled until that is deployed and tested.

Successful login/logout, failed logins, login lockouts, permission denials, and audited record changes are stored in the same SQLite database. Login identifiers are represented by HMAC-derived keys in security events. Write requests and their audit records are committed together. Database triggers reject application-level update and deletion of audit rows. A database owner can still remove those triggers, so this is not a substitute for a separately controlled copy.

Set a dedicated `GIFT_AID_AUDIT_EXPORT_KEY` of at least 32 random characters in the protected operator environment, then regularly run `python audit_export.py create /private/audit/club-audit.json`. The destination must be outside the repository, web roots, and synchronized folders. Transfer it to a separately controlled log/evidence store. Verify a received copy with `python audit_export.py verify /private/audit/club-audit.json`; verification fails if signed metadata or events changed. Restrict export contents because they include usernames and record identifiers. Review the admin audit page or signed export for repeated login throttling, permission denials, account changes, and unexpected bulk activity. Alert delivery and immutable external retention must be configured in the chosen hosting/logging service.

## Retention and privacy notice

The supplied notice says records should be kept only as long as needed under a club-approved retention schedule. This application does not calculate retention deadlines or automatically delete records and backups. Before use, the club should approve periods or criteria for accounts, contact details, Gift Aid declarations/evidence, trips, donations, payments, audit records, and backups; assign an owner; and define how anonymisation or erasure requests are assessed against legal recordkeeping duties. Then publish the actual schedule/contact details and update the notice. Do not invent a single period for all record types.

Privacy notices are stored in the `privacy_notice_versions` table with a stable version ID, effective and published dates, the complete text, and its SHA-256. Database triggers prohibit update and deletion of published rows. Admin publication creates an audit event; the current notice is the latest version, and `/privacy-notice/<id>` serves retained history. Each new Gift Aid declaration stores the notice version ID and digest that applied when it was created. The repository's `privacy-notice.txt` is only the seed for a newly created database.

Administrators can retrieve `/people/<person_id>/export` to assemble that person's contact, trip, declaration, donation, and payment records for DSAR review. Treat the export as sensitive, verify the requester's identity and legal scope outside the application, review exemptions/third-party information, and transfer any approved response through an authorised secure channel. The endpoint supports review; it does not automate the controller's legal decision.

Trip and destination CSV uploads are limited both to Flask's configured 1 MiB request size and to 1,000 data rows. Split larger batches. Imports remain synchronous and atomic; monitor worker time and external routing availability during planned bulk work.

## Incident response

Before launch, name a primary and backup incident contact, decide who can disable accounts and restrict the site, and keep provider support/contact details accessible offline. For a suspected compromise or personal data breach:

1. Contain it: restrict access or take the site offline, disable affected accounts, and rotate exposed credentials/secrets using the provider's private settings.
2. Preserve evidence: record the timeline and actions, secure relevant logs, and make a protected copy of the database before attempting recovery. Limit who can access evidence.
3. Assess what data and people may be affected, the likely consequences, and whether records or backups were accessed or altered. Do not overwrite evidence during restoration.
4. The club's data controller should decide, with appropriate advice, whether individuals or the ICO must be notified. Follow the ICO's current [personal data breach guidance](https://ico.org.uk/for-organisations/report-a-breach/personal-data-breach/); where notification is required, the ICO says to report without undue delay and within 72 hours of becoming aware.
5. Recover from a verified clean backup, rotate affected credentials, patch the cause, and confirm access controls and auditability before reopening.
6. Document decisions, notifications, and lessons learned; update this response plan and test it periodically.

## Dependency maintenance

Generate the hash-locked dependency file with `python -m piptools compile --generate-hashes --output-file requirements.txt requirements.in`, and install it with `python -m pip install --require-hashes -r requirements.txt`. The weekly dependency workflow audits that lock, verifies vendored Bootstrap hashes, scans Python plus the explicitly inventoried Bootstrap release with OSV-Scanner, and publishes a CycloneDX SBOM artifact. Dependabot checks Python dependencies and GitHub Actions weekly; workflow actions are pinned to reviewed commit SHAs. When a vendored front-end release changes, update `VENDORED-ASSETS.md`, its checksum file, and `osv-scanner-custom.json` together.

Critical and high-severity dependency vulnerabilities must be assessed and patched within 7 days, medium-severity issues within 30 days, and other supported updates at the next monthly dependency review. Test and document exceptions with an owner and review date. A clean dependency scan does not assess application logic or the hosting platform.

## Pre-launch checklist

- [ ] PythonAnywhere two-factor authentication is enabled for every operator account.
- [ ] The repository is private and collaborator access has been reviewed.
- [ ] The production database is outside the repository and all static/web mappings.
- [ ] Production encryption and blind-index keys are set privately and differ from development keys.
- [ ] Data/backup directories are `0700`; database and backup files are `0600`.
- [ ] The backup scheduled task is enabled with an approved retention period.
- [ ] A trial backup and restore has succeeded with fake data and is recorded above.
- [ ] Git history has been checked for committed `.db`, `.sqlite`, `.sqlite3`, backup, `.env`, and key files.
- [ ] Development databases contain fake data only.
- [ ] Keys have a separate protected recovery copy and are not stored with backups.
- [ ] A dedicated audit-export signing key and separately controlled export destination are configured.
- [ ] Audit export verification and security-event review are scheduled and assigned to an owner.
- [ ] PythonAnywhere account isolation and provider encryption-at-rest controls have been confirmed and recorded in the club's processor/security assessment.
- [ ] HTTPS redirect, certificate renewal, accepted host-header behaviour, and the real client IP seen behind PythonAnywhere's proxy have been tested and recorded.

## References

- [ICO: Security](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/security/)
- [ICO: Personal data breach reporting](https://ico.org.uk/for-organisations/report-a-breach/personal-data-breach/)
- [PythonAnywhere: Flask deployment](https://help.pythonanywhere.com/pages/Flask/)
- [PythonAnywhere: plans and limits](https://www.pythonanywhere.com/pricing/)
