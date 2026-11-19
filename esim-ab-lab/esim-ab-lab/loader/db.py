"""Подключение к базе и общие утилиты загрузчика."""
import io
import os
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "postgresql://esim:esim@localhost:5433/esim"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(retries: int = 30) -> psycopg.Connection:
    """Подключиться, подождав, пока Postgres стартует (важно в Docker)."""
    last = None
    for _ in range(retries):
        try:
            return psycopg.connect(database_url())
        except psycopg.OperationalError as err:
            last = err
            time.sleep(2)
    raise SystemExit(f"Не удалось подключиться к {database_url()}: {last}")


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def copy_df(conn: psycopg.Connection, table: str, df) -> None:
    """Быстрая загрузка DataFrame через COPY ... FROM STDIN (CSV)."""
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False, na_rep="")
    buf.seek(0)
    cols = ", ".join(df.columns)
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')") as cp:
            while chunk := buf.read(1 << 20):
                cp.write(chunk)
