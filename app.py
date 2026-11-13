"""WSGI entry point and command-line administration.

The application itself is assembled by :func:`create_app`.  Keeping this file
small makes imports predictable while preserving the historic ``app:app``
deployment target.
"""

import os
import sqlite3
from getpass import getpass

from club_gift_aid_manager.db import create_user, list_users
from club_gift_aid_manager.factory import (
    create_app,
    parse_hsts_max_age,
    validate_deployment_config,
    validate_production_database_path,
)
from club_gift_aid_manager.roles import Role
from club_gift_aid_manager.views.shared import HTML_FORM
from club_gift_aid_manager.views.shared import calculate_route_distance, record_audit_event


# WSGI servers import this object.  Tests may instead call create_app() to make
# another fully configured instance.
app = create_app()
DEPLOYMENT_ENV = app.config["DEPLOYMENT_ENV"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        if list_users():
            raise SystemExit("Administrator accounts already exist; manage accounts as an administrator.")
        username = input("Admin username: ").strip()
        password = getpass("Admin password (12+ characters): ")
        try:
            user = create_user(
                username=username,
                password=password,
                role=Role.ADMIN.value,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise SystemExit("Username is already in use.") from exc
        print(f"Created administrator account {user['username']!r}.")
    else:
        if DEPLOYMENT_ENV == "production":
            raise SystemExit("Production deployments must use the configured WSGI server.")
        app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"})
