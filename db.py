"""Inventory database. The four seed rows match the README and are not extended."""

import sqlite3
from pathlib import Path

# Same statement and rows as the README. Do not add columns to this table.
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

    A database that already has rows is left alone, so a second run does not
    duplicate the seed and does not overwrite stock.
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


def inventory_rows(path: str | Path) -> list[tuple[str, int]]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute("SELECT item, stock FROM inventory ORDER BY item"))
    finally:
        connection.close()
