"""Create a user from the CLI: python -m scripts.create_user NAME PASSWORD [--admin]"""

from __future__ import annotations

import argparse
import asyncio

from config.settings import get_settings
from database.engine import Database
from database.repositories.users import UsersRepository


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    # AUDIT S8: never take a password as a command-line argument — it lands in
    # shell history and `ps`. Optional positional kept for CI; interactive
    # entry is preferred.
    parser.add_argument("password", nargs="?", default=None)
    parser.add_argument("--admin", action="store_true")
    args = parser.parse_args()
    if args.password is None:
        import getpass

        password = getpass.getpass("Password: ")
        if not password:
            raise SystemExit("empty password")
    else:
        import warnings

        warnings.warn(
            "passing the password as an argument leaks it to shell history; prefer the prompt",
            stacklevel=2,
        )
        password = args.password

    settings = get_settings()
    db = Database(settings.database_url)
    await db.connect()
    try:
        async with db.session() as session:
            row = await UsersRepository(session).create(
                args.username, password, is_admin=args.admin
            )
            print(f"created user: {row.username} (admin={row.is_admin})")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
