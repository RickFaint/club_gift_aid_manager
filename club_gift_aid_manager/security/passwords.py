"""One authoritative password policy for creation, reset, and replacement."""

import re


MINIMUM_PASSWORD_LENGTH = 15
MAXIMUM_PASSWORD_LENGTH = 1024


def validate_password(password: str, *, label: str = "Password") -> str:
    """Require length and multiple character classes without arbitrary exclusions.

    Long passphrases remain supported up to the denial-of-service bound. The policy
    requires uppercase and lowercase letters, a number, and a non-alphanumeric
    character, while allowing spaces and every Unicode character.
    """
    if not MINIMUM_PASSWORD_LENGTH <= len(password) <= MAXIMUM_PASSWORD_LENGTH:
        raise ValueError(
            f"{label} must be between {MINIMUM_PASSWORD_LENGTH} and "
            f"{MAXIMUM_PASSWORD_LENGTH} characters."
        )
    requirements = (
        (r"[a-z]", "a lowercase letter"),
        (r"[A-Z]", "an uppercase letter"),
        (r"[0-9]", "a number"),
        (r"[^A-Za-z0-9]", "a special character"),
    )
    missing = [
        description for pattern, description in requirements if not re.search(pattern, password)
    ]
    if missing:
        raise ValueError(f"{label} must include " + ", ".join(missing) + ".")
    return password
