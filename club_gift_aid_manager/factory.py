"""Flask application factory and request-lifecycle setup.

An application factory creates the Flask object inside a function instead of
at import time.  That makes configuration explicit and allows tests or WSGI
servers to create an application in a predictable way.
"""

from __future__ import annotations

import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, session, url_for

from .crypto import validate_encryption_config
from .db import (
    begin_request_transaction,
    create_app_database,
    finish_request_transaction,
    get_current_privacy_notice,
    get_user_by_id,
)
from .storage import validate_private_path
from .validation import ValidationError
from .roles import Role


def validate_deployment_config(environment, secret, cookie_secure):
    """Reject production settings that could expose or invalidate sessions."""
    if environment not in {"development", "production"}:
        raise RuntimeError("CLUB_GIFT_AID_ENV must be 'development' or 'production'.")
    if environment == "production" and (not secret or len(secret) < 32 or not cookie_secure):
        raise RuntimeError(
            "Production requires a stable CLUB_GIFT_AID_SECRET of at least 32 characters "
            "and CLUB_GIFT_AID_COOKIE_SECURE=true."
        )


def parse_hsts_max_age(value):
    """Convert the HSTS setting to a safe, non-negative integer."""
    try:
        max_age = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("HSTS_MAX_AGE must be a non-negative integer.") from exc
    if max_age < 0:
        raise RuntimeError("HSTS_MAX_AGE must be a non-negative integer.")
    return max_age


def validate_production_database_path(database_path, project_directory):
    """Require production data to use an absolute path outside the checkout."""
    path = Path(database_path)
    if not path.is_absolute():
        raise RuntimeError("Production database path must be absolute.")
    try:
        path.resolve().relative_to(Path(project_directory).resolve())
    except ValueError:
        return
    raise RuntimeError("Production database path must be outside the source checkout.")


def register_blueprints(app):
    """Attach every route collection to ``app``.

    Imports are local to avoid importing view modules before configuration has
    been validated.  No URL prefix is supplied, so all existing paths remain
    unchanged.
    """
    from .views import admin, auth, destinations, finance, main, people, reports, trips

    for module in (auth, people, trips, destinations, finance, admin, reports, main):
        app.register_blueprint(module.blueprint)


def register_hooks(app):
    """Register functions that run around routes and supply template values."""

    @app.errorhandler(ValidationError)
    def handle_validation_error(error):
        """Return every reusable validation failure in the established format."""
        return jsonify({"error": error.message}), error.status_code

    @app.before_request
    def enforce_security():
        """Resolve the user, protect writes with CSRF, and begin transactions."""
        g.csp_nonce = secrets.token_urlsafe(24)
        endpoint = request.endpoint or ""
        short_endpoint = endpoint.rsplit(".", 1)[-1]
        html_endpoints = {
            "index", "how_it_works_page", "mileage_rates_page", "people_page",
            "trips_page", "destinations_page", "declarations_page", "donations_page",
            "payments_page", "audit_page", "users_page", "claim_page",
        }
        if endpoint in {"", "static", "auth.login_page", "main.privacy_notice_current", "main.privacy_notice_historical"}:
            g.current_user = None
            return None
        if endpoint == "auth.login":
            g.current_user = None
            expected = session.get("_csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return jsonify({"error": "CSRF token missing or invalid."}), 400
            begin_request_transaction()
            return None
        user_id = session.get("user_id")
        g.current_user = get_user_by_id(user_id) if isinstance(user_id, int) else None
        session_is_stale = (
            g.current_user is not None
            and session.get("session_version") != g.current_user["session_version"]
        )
        if g.current_user is None or not g.current_user["is_active"] or session_is_stale:
            session.clear()
            if request.method == "GET" and short_endpoint in html_endpoints:
                return redirect(url_for("auth.login_page"))
            if short_endpoint not in {"index", "claim_page"}:
                return jsonify({"error": "Authentication required."}), 401
            return redirect(url_for("auth.login_page"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("_csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                return jsonify({"error": "CSRF token missing or invalid."}), 400
            begin_request_transaction()
        return None

    @app.after_request
    def finalize_request_transaction(response):
        """Finish database work and add browser security headers."""
        finish_request_transaction(commit=(response.status_code < 400 or response.status_code == 403 or (request.endpoint == "auth.login" and response.status_code in {401, 429})))
        if app.config["DEPLOYMENT_ENV"] == "production":
            directives = [f"max-age={app.config['HSTS_MAX_AGE']}"]
            if app.config["HSTS_INCLUDE_SUBDOMAINS"]:
                directives.append("includeSubDomains")
            if app.config["HSTS_PRELOAD"]:
                directives.append("preload")
            response.headers["Strict-Transport-Security"] = "; ".join(directives)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("X-Frame-Options", "DENY")
        nonce = getattr(g, "csp_nonce", secrets.token_urlsafe(24))
        policy = ("default-src 'self'; " f"script-src 'self' 'nonce-{nonce}'; " f"style-src 'self' 'nonce-{nonce}'; " "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; " "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        header = "Content-Security-Policy-Report-Only" if app.config["CSP_REPORT_ONLY"] else "Content-Security-Policy"
        response.headers[header] = policy
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), accelerometer=(), gyroscope=(), magnetometer=()"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store, private, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.teardown_request
    def rollback_unfinished_transaction(_error):
        """Roll back anything left open when a request or exception finishes."""
        finish_request_transaction(commit=False)

    @app.context_processor
    def inject_security_context():
        """Expose request-specific values to every Jinja template."""
        token = session.setdefault("_csrf_token", secrets.token_urlsafe(32))
        notice = get_current_privacy_notice()
        return {"csrf_token": token, "current_user": getattr(g, "current_user", None), "privacy_notice": notice["notice_text"], "privacy_notice_version": notice, "csp_nonce": getattr(g, "csp_nonce", ""), "Role": Role}


def create_app():
    """Validate configuration, create the Flask app, and initialise storage."""
    environment = os.environ.get("CLUB_GIFT_AID_ENV", "development").strip().lower()
    secret = os.environ.get("CLUB_GIFT_AID_SECRET")
    secure_cookie = os.environ.get("CLUB_GIFT_AID_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
    validate_encryption_config()
    validate_deployment_config(environment, secret, secure_cookie)
    if environment == "production":
        database_path = os.environ.get("CLUB_GIFT_AID_DB")
        if not database_path:
            raise RuntimeError("Production requires CLUB_GIFT_AID_DB to be an absolute path in persistent storage.")
        validate_production_database_path(database_path, Path(__file__).resolve().parent.parent)
        validate_private_path(Path(database_path), purpose="Production database")
        if os.name != "posix":
            raise RuntimeError("Production database hosting requires POSIX permission enforcement.")
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent.parent / "templates"), static_folder=str(Path(__file__).resolve().parent.parent / "static"))
    app.config.update(SECRET_KEY=secret or secrets.token_hex(32), DEPLOYMENT_ENV=environment, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=secure_cookie, PERMANENT_SESSION_LIFETIME=timedelta(hours=8), MAX_CONTENT_LENGTH=1024 * 1024, CSP_REPORT_ONLY=os.environ.get("CLUB_GIFT_AID_CSP_REPORT_ONLY", "false").lower() in {"1", "true", "yes"}, HSTS_MAX_AGE=parse_hsts_max_age(os.environ.get("HSTS_MAX_AGE", "31536000")), HSTS_INCLUDE_SUBDOMAINS=os.environ.get("HSTS_INCLUDE_SUBDOMAINS", "false").lower() in {"1", "true", "yes"}, HSTS_PRELOAD=os.environ.get("HSTS_PRELOAD", "false").lower() in {"1", "true", "yes"})
    register_blueprints(app)
    register_hooks(app)
    # Startup must always ensure the schema exists, including factory-created apps.
    create_app_database()
    return app
