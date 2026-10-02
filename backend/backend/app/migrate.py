"""Create or print the Postgres schema.

    python -m app.migrate --print    # SQL for an administrator to review and run once
    python -m app.migrate --apply    # apply it using DATABASE_URL (the role needs CREATE on its schema)

With ``DB_AUTO_MIGRATE=true`` (the default) the service also applies this on startup, so the explicit
command is mainly for locked-down production roles and for release commands.
"""

import argparse
import sys

from app.config import get_settings
from app.services.db import PostgresDatabase, postgres_schema_sql


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print", dest="print_sql", action="store_true", help="print the DDL")
    group.add_argument("--apply", action="store_true", help="apply the DDL to DATABASE_URL")
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.print_sql:
        sys.stdout.write(postgres_schema_sql(settings.db_schema))
        return 0
    if not settings.database_url:
        sys.stderr.write("DATABASE_URL is not set\n")
        return 2
    database = PostgresDatabase(settings.database_url, schema=settings.db_schema, pool_size=1)
    try:
        database.ensure_schema()
    finally:
        database.close()
    sys.stdout.write(f"schema '{settings.db_schema}' is up to date\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
