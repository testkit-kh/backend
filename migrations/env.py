"""
Alembic environment — async engine + PostGIS-aware autogenerate.

The URL comes from app.config.settings, so migrations and the application
always talk to the same database.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the models module registers every table on Base.metadata.
from app import models  # noqa: F401
from app.config import settings
from app.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# geoalchemy2's helpers keep autogenerate from fighting PostGIS:
#   * skip the internal spatial_ref_sys table and PostGIS-managed indexes
#   * render Geometry/Geography types with the right import in new revisions
COMMON_KWARGS = dict(
    target_metadata=target_metadata,
    include_object=alembic_helpers.include_object,
    render_item=alembic_helpers.render_item,
    process_revision_directives=alembic_helpers.writer,
    compare_type=True,
    compare_server_default=True,
)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMMON_KWARGS,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, **COMMON_KWARGS)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
