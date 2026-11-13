# Security and deployment operations

This application contains contact details, mileage records, donation and payment records, and Gift Aid declaration information. These controls support safer operation; they do not establish legal compliance. The club, as data controller, should review its purposes, lawful bases, retention schedule, hosting terms, and response arrangements before entering real data.

## Production settings

Use the hosting provider's WSGI deployment, not `python app.py` or Flask's development server. Configure these values in a private host configuration, not in Git:

- `CLUB_GIFT_AID_ENV=production`
- `CLUB_GIFT_AID_SECRET`: a stable, randomly generated secret with at least 32 characters
- `CLUB_GIFT_AID_COOKIE_SECURE=true`
- `CLUB_GIFT_AID_DB`: an absolute path in a private persistent data directory, outside the source checkout and any static/public directory

For example, generate a secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store it in the provider's private application configuration. If the provider has no environment-variable facility, put it only in a host-private WSGI configuration with restricted access; never paste it into this repository, a public issue, or a shared log.

On PythonAnywhere, create a **manual Flask web app** using the same Python version as the virtual environment, point the Web tab at the virtual environment, and edit the WSGI configuration file to import `app` from this project as `application`. Configure HTTPS in the Web tab and verify that the browser uses HTTPS before entering real data. Set the database path to a private location under the account, not the checkout. The app creates missing parent directories and sets a newly created POSIX SQLite file to owner read/write; the operator must still ensure the directory is private and writable.

The secure-cookie flag protects session cookies only when the site is served over HTTPS. Production startup will fail if the stable secret or secure-cookie setting is missing, but it cannot prove that the hosting provider's TLS or proxy configuration is correct. Confirm HTTPS and certificate renewal in the hosting dashboard.

## Database and backups

SQLite files are plaintext unless the host or storage layer encrypts them. The application does not implement database encryption. Confirm the hosting provider's at-rest encryption, account isolation, and access controls; if these are insufficient for the club's risk assessment, do not store live personal data there.

Restrict the database directory and all backup locations to authorised operators. Do not place backups in the project checkout, a static directory, or another web-accessible path. Use an SQLite-consistent backup (SQLite's online backup API or `.backup` command), encrypt it before copying it off the host, restrict access to the encryption key separately, and define an expiry/deletion schedule. Do not assume a free hosting plan provides automated scheduled backups.

At least periodically, restore a protected backup to a separate, non-public test location and verify that the application can read the restored records. Record when the backup and restore checks were completed, without including personal data in the record.

## Login and audit controls

The login flow limits repeated failures for a source address and for a source-address/username pair, and stores HMAC-derived keys rather than raw addresses or usernames. The account-pair limit is five failures in 15 minutes; the source-address limit is 30 failures in 15 minutes. A threshold starts a 15-minute lockout. These are basic protections, not a substitute for MFA or provider-level abuse monitoring. The app does not trust a caller-supplied `X-Forwarded-For` header; confirm that the hosting platform provides a reliable client address to the WSGI request.

Successful login/logout, failed logins, login lockouts, permission denials, and audited record changes are stored in the same SQLite database. Login identifiers are represented by HMAC-derived keys in security events. Write requests and their audit records are committed together. Database administrators can still alter or delete these local records; export security logs to a separately controlled destination if tamper resistance is required. Review the admin audit page for unusual activity and restrict database access.

## Retention and privacy notice

The supplied notice says records should be kept only as long as needed under a club-approved retention schedule. This application does not calculate retention deadlines or automatically delete records and backups. Before use, the club should approve periods or criteria for accounts, contact details, Gift Aid declarations/evidence, trips, donations, payments, audit records, and backups; assign an owner; and define how anonymisation or erasure requests are assessed against legal recordkeeping duties. Then publish the actual schedule/contact details and update the notice. Do not invent a single period for all record types.

## Incident response

Before launch, name a primary and backup incident contact, decide who can disable accounts and restrict the site, and keep provider support/contact details accessible offline. For a suspected compromise or personal data breach:

1. Contain it: restrict access or take the site offline, disable affected accounts, and rotate exposed credentials/secrets using the provider's private settings.
2. Preserve evidence: record the timeline and actions, secure relevant logs, and make a protected copy of the database before attempting recovery. Limit who can access evidence.
3. Assess what data and people may be affected, the likely consequences, and whether records or backups were accessed or altered. Do not overwrite evidence during restoration.
4. The club's data controller should decide, with appropriate advice, whether individuals or the ICO must be notified. Follow the ICO's current [personal data breach guidance](https://ico.org.uk/for-organisations/report-a-breach/personal-data-breach/); where notification is required, the ICO says to report without undue delay and within 72 hours of becoming aware.
5. Recover from a verified clean backup, rotate affected credentials, patch the cause, and confirm access controls and auditability before reopening.
6. Document decisions, notifications, and lessons learned; update this response plan and test it periodically.

## Dependency maintenance

The repository's dependency-audit workflow checks the pinned `requirements.txt` on changes and weekly. Review and resolve findings promptly, test upgrades, and keep the supported Python runtime and hosting configuration current. A clean dependency scan does not assess application logic or the hosting platform.

## References

- [ICO: Security](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/security/)
- [ICO: Personal data breach reporting](https://ico.org.uk/for-organisations/report-a-breach/personal-data-breach/)
- [PythonAnywhere: Flask deployment](https://help.pythonanywhere.com/pages/Flask/)
- [PythonAnywhere: plans and limits](https://www.pythonanywhere.com/pricing/)
