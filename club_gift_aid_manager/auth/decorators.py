"""Reusable authorization decorators.

Decorators wrap a view function before Flask calls it.  This lets every
protected route use the same permission check instead of duplicating a subtle
security rule in many files.
"""

from functools import wraps

import time

from flask import g, jsonify, request, session

from club_gift_aid_manager.db import record_audit_event
from club_gift_aid_manager.roles import Role


def roles_required(*roles):
    """Allow the decorated route only for signed-in users in ``roles``.

    The two nested functions are normal Python decorator structure.  ``view``
    is the original route and ``wrapped`` is what Flask actually invokes.
    ``wraps`` preserves the original function name, which Flask uses as the
    endpoint name.
    """

    # Convert enum members once when Python imports the route.  The database
    # continues to return plain strings, so authorization compares strings.
    allowed_roles = {
        role.value if isinstance(role, Role) else role
        for role in roles
    }

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            # ``g`` stores data for this request only.  The before-request hook
            # places the database user here after resolving the session cookie.
            user = getattr(g, "current_user", None)
            if user is None:
                return jsonify({"error": "Authentication required."}), 401
            if user["role"] not in allowed_roles:
                # Record denied access without recording submitted personal data.
                # Import at call time to keep the compatibility seam used by
                # older integrations which patch the root ``app`` module.
                from club_gift_aid_manager.views.shared import record_audit_event as audit
                audit(
                    actor_user_id=user["id"],
                    actor_username=user["username"],
                    action="permission_denied",
                    entity_type="authorization",
                    entity_id=(request.endpoint or "unknown").rsplit(".", 1)[-1],
                    changed_fields=(request.method,),
                )
                return jsonify({"error": "You do not have permission to perform this action."}), 403
            return view(*args, **kwargs)

        # Expose policy metadata for automated access-control matrix tests.
        # Flask still invokes the wrapper normally; this attribute has no
        # effect on production authorization behavior.
        wrapped.required_roles = frozenset(allowed_roles)
        return wrapped

    return decorator


def recent_authentication_required(max_age_seconds: int = 15 * 60):
    """Require a recent password login before a privileged operation."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            authenticated_at = session.get("authenticated_at")
            if not isinstance(authenticated_at, int) or (
                int(time.time()) - authenticated_at > max_age_seconds
            ):
                return jsonify({
                    "error": "Recent authentication is required. Sign out and sign in again."
                }), 401
            return view(*args, **kwargs)

        return wrapped

    return decorator

