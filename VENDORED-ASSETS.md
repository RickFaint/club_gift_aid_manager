# Vendored front-end assets

The application needs no third-party host at runtime. CI verifies these files against `static/vendor/VENDORED-ASSETS.sha256`.

| Asset | Version | Upstream source | SHA-256 |
| --- | --- | --- | --- |
| `bootstrap.min.css` | Bootstrap 5.3.3 | https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css | `3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8` |
| `bootstrap.bundle.min.js` | Bootstrap 5.3.3 | https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js | `0833b2e9c3a26c258476c46266e6877fc75218625162e0460be9a3a098a61c6c` |
| `LICENSE` | Bootstrap 5.3.3 | https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/LICENSE | `8c14611ae41ac6fd543c13349f22188eb12c69b3e59105c5eca3925a8e4eca3e` |

When updating, download an explicitly selected Bootstrap release, retain its licence, calculate each file's SHA-256 locally, update both this table and the checksum file, then run the full tests and CI integrity check.
