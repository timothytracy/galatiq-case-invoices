"""Inventory database. setup_db.py is the only writer. The workflow opens it read-only."""

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


def connect_readonly(path: str | Path = "inventory.db") -> sqlite3.Connection:
    """Read-only URI. Writes raise OperationalError."""

    name = Path(path).as_posix()
    if name == "inventory.db":
        return sqlite3.connect("file:inventory.db?mode=ro", uri=True)
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
