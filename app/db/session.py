from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_foreign_keys(dbapi_connection, _connection_record):
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def initialize_database() -> None:
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    migration_config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    migration_config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(migration_config, "head")
    # PostGIS geometry columns are maintained alongside portable lat/lon fields.
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("""
                CREATE OR REPLACE FUNCTION cignal_update_location_geom() RETURNS trigger AS $$
                BEGIN
                  IF NEW.latitude IS NULL OR NEW.longitude IS NULL THEN
                    NEW.location_geom := NULL;
                  ELSE
                    NEW.location_geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
                  END IF;
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
            """))
            for table in ("reports", "incidents"):
                connection.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS location_geom geometry(Point,4326)"
                ))
                connection.execute(text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_location_geom ON {table} USING GIST(location_geom)"
                ))
                connection.execute(text(
                    f"UPDATE {table} SET location_geom = ST_SetSRID(ST_MakePoint(longitude, latitude),4326) "
                    "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND location_geom IS NULL"
                ))
                connection.execute(text(f"DROP TRIGGER IF EXISTS trg_{table}_location_geom ON {table}"))
                connection.execute(text(
                    f"CREATE TRIGGER trg_{table}_location_geom BEFORE INSERT OR UPDATE OF latitude, longitude "
                    f"ON {table} FOR EACH ROW EXECUTE FUNCTION cignal_update_location_geom()"
                ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
