"""Inventory database.

ensure_inventory_db creates the seed file when it is missing and does not open
an existing file. The pipeline reads the file and does not change it.
"""

import os
import sqlite3
from pathlib import Path

# Same statement and rows as the README. Do not add columns or tables.
CREATE_INVENTORY = (
    "CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER)"
)

SEED = (
    ("WidgetA", 15),
    ("WidgetB", 10),
    ("GadgetX", 5),
    ("FakeItem", 0),
)


ROOT = Path(__file__).resolve().parent


def data_dir() -> Path:
    """Folder for inventory.db, payments.db, and logs/. Not the working directory.

    INVOICE_DATA_DIR, when set, replaces the repository root. Read on each call.
    """

    override = os.environ.get("INVOICE_DATA_DIR", "").strip()
    return Path(override) if override else ROOT


def payments_db_path() -> Path:
    return data_dir() / "payments.db"


def log_dir() -> Path:
    return data_dir() / "logs"


def inventory_db_path(default: str | Path | None = None) -> Path:
    """Inventory path for this run.

    INVENTORY_DB, when set, replaces default. Tests set it to a temporary
    file so they do not open the repository inventory.db. With no default,
    the file is inventory.db in the data folder.
    """

    override = os.environ.get("INVENTORY_DB", "").strip()
    if override:
        return Path(override)
    return Path(default) if default is not None else data_dir() / "inventory.db"


def create_database(path: str | Path = "inventory.db") -> Path:
    """Create the file if needed and seed it when the table is empty."""

    db_path = Path(path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(CREATE_INVENTORY)
        count = connection.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
        if count == 0:
            connection.executemany(
                "INSERT INTO inventory (item, stock) VALUES (?, ?)",
                SEED,
            )
        connection.commit()
    finally:
        connection.close()
    return db_path


def ensure_inventory_db(path: str | Path = "inventory.db") -> Path:
    """Create the seed file when it is missing. An existing file is not opened."""

    db_path = Path(path)
    if db_path.exists():
        return db_path
    return create_database(db_path)


def reset_inventory_db(path: str | Path = "inventory.db") -> Path:
    """Delete the inventory file and build a new one from the seed."""

    db_path = Path(path)
    db_path.unlink(missing_ok=True)
    return create_database(db_path)


def connect_readonly(path: str | Path = "inventory.db") -> sqlite3.Connection:
    """Read-only URI. Writes raise OperationalError."""

    name = Path(path).as_posix()
    return sqlite3.connect(f"file:{name}?mode=ro", uri=True)


def load_stock(path: str | Path = "inventory.db") -> list[tuple[str, int]]:
    connection = connect_readonly(path)
    try:
        rows = connection.execute(
            "SELECT item, stock FROM inventory ORDER BY item"
        ).fetchall()
    finally:
        connection.close()
    return [(item, int(stock)) for item, stock in rows]
