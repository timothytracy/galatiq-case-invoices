"""Inventory database.

setup_db.py is the only writer. The workflow opens the file read-only.
The four seed rows match the README and are not extended. There is no other table.
"""

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
    """Create inventory.db if needed and seed it when the table is empty.

    A database that already has rows is left alone. main.py calls this only
    when the file is missing. The invoice workflow does not call it.
    """

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
    """Open the inventory snapshot without writing it.

    The default path uses the read-only URI form. Writes raise OperationalError.
    """

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


def inventory_rows(path: str | Path) -> list[tuple[str, int]]:
    return load_stock(path)
