# Security review

Last reviewed: 16 November 2026

## Scope and standard

This review covers first-party Python, browser JavaScript, templates, migrations,
dependency locks, CI configuration, and automated security tests. It uses the OWASP
Top 10:2025 as its risk taxonomy and OWASP ASVS 5.0.0 Level 2 as the target for
application controls. It is a source-assisted review, not OWASP certification or a
substitute for an independent production penetration test.

References: https://top10.owasp.org/ and https://asvs.owasp.org/.

## Automated evidence

Run these checks before release:

```bash
python -m bandit -r club_gift_aid_manager app.py audit_export.py backup.py manage.py monitoring.py retention.py -c .bandit
python -m pip_audit -r requirements.txt --disable-pip
npm audit
python -m pytest tests/security tests/auth tests/operations -q
python -m pytest -q
```

CI runs the static scans, dependency audits, linting, type checks, documentation
checks, JavaScript syntax checks, and complete test suite. Advisory results are
time-sensitive and must be checked again for every release.

## OWASP Top 10:2025 disposition

| Risk | Reviewed controls and disposition |
| --- | --- |
| A01 Broken Access Control | Deny-by-default authentication, role decorators, volunteer scoping, object-level policy, CSRF, protected proxy parsing, SSRF validation, and access-control tests are present. |
| A02 Security Misconfiguration | Production fail-closed configuration, trusted hosts, secure cookies, HSTS, CSP, framing protection, cross-origin isolation, no-store responses, bounded requests, opaque errors, and private storage checks are present. Hosting TLS and environment settings require operator verification. |
| A03 Software Supply Chain Failures | Hash-locked Python dependencies, npm lockfile, commit-pinned CI actions, vendored checksums, and advisory scans are present. No known dependency vulnerabilities were reported at review time. |
| A04 Cryptographic Failures | Fernet field encryption, separate keys, password hashing, secure cookies, encrypted backups, key rotation, and HTTPS/HSTS requirements are present. Key custody remains an operator responsibility. |
| A05 Injection | Parameterized SQL, allowlisted dynamic fields, DTO validation, Jinja autoescaping, browser HTML escaping, and restrictive CSP are present. Bandit reported no issue. |
| A06 Insecure Design | Transactional and immutable auditing, payment invariants, bounded imports, fail-closed routing, anonymization, recent-authentication gates, and migration verification are present. Club policies still require approval. |
| A07 Authentication Failures | Constant-work checks, password hashing, a 15-character multi-class password policy, administrator-assisted reset with mandatory replacement, session rotation, persistent throttling, generic errors, session invalidation, recent-login checks, and mandatory encrypted TOTP MFA with replay protection and single-use recovery codes for administrators and treasurers are present. TOTP is not phishing-resistant; passkeys remain a future strengthening option. |
| A08 Software or Data Integrity Failures | Migration verification, signed exports, backup verification, hash locks, checksums, and pinned actions are present. Deployment provenance remains an operator control. |
| A09 Security Logging and Alerting Failures | Structured safe logs, request IDs, security events, immutable audit metadata, monitoring support, and incident procedures are present. Operators must connect logs to alerts. |
| A10 Mishandling of Exceptional Conditions | Generic errors, rollback, bounded parsing, finite-number checks, timeouts, redirect refusal, response validation, and fail-closed startup are present and tested. |

## Changes made by this review

- Repaired the Bandit configuration so static security scanning runs.
- Added Bandit, pip-audit, and npm audit gates to CI.
- Added same-origin opener and resource policies with regression tests.
- Updated the production migration test for the required trusted-host setting.
- Generalized the ignore rule so SQLite runtime databases cannot be committed by
  filename accident.

## Residual and operational work

Before public use, complete `SECURITY-OPERATIONS.md`, configure HTTPS and trusted
hosts, protect and back up keys, enable hosting-account MFA, connect logs to alerts,
approve retention and privacy content, and commission a penetration test of the
deployed site using test accounts for every role. Supplement application login
throttling at the hosting edge when that service is available.
