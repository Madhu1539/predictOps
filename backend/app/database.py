from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from app.config import get_settings
import logging
from typing import Any, Dict, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

settings = get_settings()


def normalise_database_url(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Return `(url, connect_args)` usable by an async SQLAlchemy engine.

    Hosted Postgres providers hand out URLs that SQLAlchemy and asyncpg both
    reject, and each failure mode is opaque at startup:

    * Render (and Heroku) emit the legacy `postgres://` scheme. SQLAlchemy 2.0
      removed that alias, so it raises `Can't load plugin: sqlalchemy.dialects:postgres`.
    * A bare `postgresql://` selects psycopg2, which is synchronous and cannot be
      driven by `create_async_engine`.
    * Render's external URL carries `?sslmode=require`. That is a libpq parameter;
      asyncpg does not accept it and fails with `connect() got an unexpected
      keyword argument 'sslmode'`. asyncpg expresses the same thing as `ssl`.

    Normalising here means `DATABASE_URL` can be pasted verbatim from the Render
    dashboard, which is what anyone deploying this will actually do.
    """
    url = (raw or "").strip()
    connect_args: Dict[str, Any] = {}

    if url.startswith("sqlite:///") and "aiosqlite" not in url:
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1), connect_args
    if url.startswith("sqlite"):
        return url, connect_args

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    if not url.startswith("postgresql+asyncpg://"):
        return url, connect_args

    # Strip libpq-only query parameters and translate the ones that carry meaning.
    split = urlsplit(url)
    kept = []
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered == "sslmode":
            # require/verify-ca/verify-full all mean "use TLS". asyncpg validates
            # certificates through an SSLContext; `True` uses the system trust store,
            # which is what Render's managed certificates chain to.
            if value.lower() not in ("disable", "allow", "prefer"):
                connect_args["ssl"] = True
            continue
        if lowered in ("channel_binding", "target_session_attrs", "gssencmode"):
            continue
        kept.append((key, value))

    url = urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(kept), split.fragment)
    )
    return url, connect_args


db_url, _connect_args = normalise_database_url(settings.database_url)
_is_sqlite = db_url.startswith("sqlite")


def sync_database_url(raw: str = None) -> str:
    """The same database addressed by a *synchronous* driver.

    The data generators and the model trainer are synchronous SQLAlchemy and are
    run as standalone scripts, so they cannot use the async engine. Previously
    `train.py` derived this with a bare `.replace("+aiosqlite", "")`, which silently
    produced an unusable URL for anything that was not SQLite.
    """
    url, connect_args = normalise_database_url(
        raw if raw is not None else settings.database_url
    )
    if url.startswith("sqlite+aiosqlite:///"):
        return url.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        # psycopg2 wants libpq's `sslmode`, the spelling asyncpg rejected.
        if connect_args.get("ssl"):
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url
    return url


# `pool_pre_ping` matters on hosted Postgres: Render recycles idle connections and
# without it the first request after a quiet spell fails on a dead socket rather
# than transparently reconnecting. SQLite has no server to disconnect from.
_engine_kwargs: Dict[str, Any] = {"echo": False}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=280,
    )
if _connect_args:
    _engine_kwargs["connect_args"] = _connect_args

engine = create_async_engine(db_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# Composite indexes required by the specification. Declared on the models as
# well, but repeated here so they can be applied to pre-existing databases.
REQUIRED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_sensor_readings_machine_timestamp "
    "ON sensor_readings (machine_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_alerts_machine_status "
    "ON alerts (machine_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_oee_snapshots_machine_timestamp "
    "ON oee_snapshots (machine_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_production_orders_machine_start "
    "ON production_orders (machine_id, scheduled_start)",
    "CREATE INDEX IF NOT EXISTS ix_machines_dataset_origin "
    "ON machines (dataset_id, data_origin)",
)


async def init_db():
    """Create all tables, then ensure required indexes exist.

    `create_all` skips existing tables entirely, so it will not retrofit
    indexes onto an already-seeded database. The explicit statements below
    are idempotent and guarantee the spec-required composite indexes exist
    regardless of when the database was first created.

    Everything after the index block is a SQLite-only migration shim. It exists to
    retrofit databases created by earlier versions of this app, and it is written in
    SQLite's own dialect (`PRAGMA`, `sqlite_master`, table rebuild). A fresh
    Postgres database has nothing to retrofit: `create_all` builds the current
    schema straight from the models, which already declare these columns and their
    nullability. Running the shim there would fail on the first `PRAGMA`.
    """
    async with engine.begin() as conn:
        # Importing the package registers every model on Base.metadata.
        from app import models  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)

        if conn.dialect.name != "sqlite":
            # Every indexed column exists by definition here, because the schema was
            # just built from the models.
            for statement in REQUIRED_INDEXES:
                await conn.execute(text(statement))
            logger.info(
                f"{conn.dialect.name}: schema created from models; "
                "skipping SQLite migration shim."
            )
            return

        # Additive columns on pre-existing tables; create_all will not add them.
        # Must run before the index block: ix_machines_dataset_origin indexes
        # dataset_id and data_origin, which a legacy database does not yet have.
        for table, column, ddl in (
            ("sensor_readings", "risk_score", "ALTER TABLE sensor_readings ADD COLUMN risk_score FLOAT"),
            ("alerts", "degraded_mode", "ALTER TABLE alerts ADD COLUMN degraded_mode BOOLEAN DEFAULT 0 NOT NULL"),
            ("machines", "cost_center_code", "ALTER TABLE machines ADD COLUMN cost_center_code VARCHAR"),
            ("machines", "ideal_units_per_hour", "ALTER TABLE machines ADD COLUMN ideal_units_per_hour FLOAT"),
            ("sensor_readings", "source", "ALTER TABLE sensor_readings ADD COLUMN source VARCHAR DEFAULT 'simulator' NOT NULL"),
            ("alerts", "estimated_downtime_cost", "ALTER TABLE alerts ADD COLUMN estimated_downtime_cost FLOAT"),
            ("alerts", "estimated_loss_avoided", "ALTER TABLE alerts ADD COLUMN estimated_loss_avoided FLOAT"),
            ("alerts", "attribution", "ALTER TABLE alerts ADD COLUMN attribution VARCHAR"),
            # Provenance. `data_origin` defaults to 'simulated' so existing rows
            # keep behaving exactly as before.
            ("machines", "data_origin", "ALTER TABLE machines ADD COLUMN data_origin VARCHAR DEFAULT 'simulated' NOT NULL"),
            ("machines", "dataset_id", "ALTER TABLE machines ADD COLUMN dataset_id INTEGER"),
            ("machines", "display_name", "ALTER TABLE machines ADD COLUMN display_name VARCHAR"),
            # Observed baselines, derived per machine.
            ("machines", "baseline_vibration", "ALTER TABLE machines ADD COLUMN baseline_vibration FLOAT"),
            ("machines", "baseline_temperature", "ALTER TABLE machines ADD COLUMN baseline_temperature FLOAT"),
            ("machines", "baseline_rpm", "ALTER TABLE machines ADD COLUMN baseline_rpm FLOAT"),
            ("machines", "baseline_method", "ALTER TABLE machines ADD COLUMN baseline_method VARCHAR"),
            ("machines", "baseline_sample_count", "ALTER TABLE machines ADD COLUMN baseline_sample_count INTEGER"),
            ("machines", "has_design_spec", "ALTER TABLE machines ADD COLUMN has_design_spec INTEGER DEFAULT 1 NOT NULL"),
            # Model-free deviation signal and ML confidence.
            ("alerts", "deviation_score", "ALTER TABLE alerts ADD COLUMN deviation_score FLOAT"),
            ("alerts", "deviation_detail", "ALTER TABLE alerts ADD COLUMN deviation_detail VARCHAR"),
            ("alerts", "confidence", "ALTER TABLE alerts ADD COLUMN confidence VARCHAR"),
            ("alerts", "confidence_detail", "ALTER TABLE alerts ADD COLUMN confidence_detail VARCHAR"),
        ):
            existing = await conn.execute(text(f"PRAGMA table_info({table})"))
            columns = {row[1] for row in existing.fetchall()}
            if columns and column not in columns:
                await conn.execute(text(ddl))

        for statement in REQUIRED_INDEXES:
            await conn.execute(text(statement))

        # Real machines are rarely fully instrumented, and a machine with no ML
        # score must be representable. Both require dropping NOT NULL, which SQLite
        # cannot do with ALTER.
        await _drop_not_null(conn, "sensor_readings", ("vibration", "temperature", "rpm"))
        await _drop_not_null(conn, "alerts", ("risk_score",))


async def _drop_not_null(conn, table: str, columns: tuple) -> None:
    """Remove NOT NULL from the named columns of an existing SQLite table.

    SQLite has no `ALTER COLUMN`, so the table is rebuilt. The new definition is
    derived from `PRAGMA table_info` rather than written by hand, so it cannot drift
    from the live schema as columns are added over time, and indexes are read back
    from `sqlite_master` and recreated.

    Guarded: a no-op when the constraint is already absent, so it runs at most once
    and does nothing on a freshly created database.
    """
    info = await conn.execute(text(f"PRAGMA table_info({table})"))
    # (cid, name, type, notnull, dflt_value, pk)
    rows = info.fetchall()
    if not rows:
        return  # table not created yet; create_all will use the model definition

    targets = {c for c in columns if any(r[1] == c and r[3] == 1 for r in rows)}
    if not targets:
        return  # already nullable

    logger.info(f"Rebuilding {table} to allow NULL in {', '.join(sorted(targets))}…")

    # Preserve index definitions; dropping the table drops them too.
    index_rows = await conn.execute(text(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:t "
        "AND sql IS NOT NULL"
    ), {"t": table})
    index_sql = [r[0] for r in index_rows.fetchall()]

    definitions = []
    for _, name, coltype, notnull, default, pk in rows:
        parts = [f'"{name}"', coltype or "BLOB"]
        if pk:
            parts.append("PRIMARY KEY")
        if notnull and name not in targets:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")
        definitions.append(" ".join(parts))

    column_list = ", ".join(f'"{r[1]}"' for r in rows)
    staging = f"{table}__rebuild"

    await conn.execute(text("PRAGMA foreign_keys=OFF"))
    await conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    await conn.execute(text(f"CREATE TABLE {staging} ({', '.join(definitions)})"))
    await conn.execute(text(
        f"INSERT INTO {staging} ({column_list}) SELECT {column_list} FROM {table}"
    ))
    await conn.execute(text(f"DROP TABLE {table}"))
    await conn.execute(text(f"ALTER TABLE {staging} RENAME TO {table}"))
    for statement in index_sql:
        # Recreate as IF NOT EXISTS so a name clash cannot abort startup.
        await conn.execute(text(statement.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)))
    await conn.execute(text("PRAGMA foreign_keys=ON"))
    logger.info(f"{table} rebuilt.")
