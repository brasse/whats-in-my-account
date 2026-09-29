"""Generate the [auth] block for config.toml.

    python -m wima.passwd

Prompts twice without echoing, so the password never appears in a file, in an
argument list, or in your shell history.
"""

import getpass
import sys

from wima.auth import hash_password

MINIMUM_LENGTH = 8


def main() -> None:
    username = input("username: ").strip()
    if not username:
        sys.exit("username cannot be empty")

    password = getpass.getpass("password: ")
    if len(password) < MINIMUM_LENGTH:
        sys.exit(f"password must be at least {MINIMUM_LENGTH} characters")
    if password != getpass.getpass("password again: "):
        sys.exit("passwords do not match")

    print("\nPut this in config.toml:\n")
    print("[auth]")
    print(f'username = "{username}"')
    print(f'password_hash = "{hash_password(password)}"')


if __name__ == "__main__":
    main()
