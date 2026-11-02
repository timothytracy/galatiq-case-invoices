"""Create the local inventory database: python3 setup_db.py [path]"""

import argparse
from pathlib import Path

from db import SEED, ensure_inventory_db, load_stock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the README inventory database.")
    parser.add_argument("path", nargs="?", default="inventory.db")
    args = parser.parse_args(argv)

    db_path = ensure_inventory_db(args.path)
    rows = load_stock(db_path)

    print(f"database\t{Path(db_path)}")
    for item, stock in rows:
        print(f"{item}\t{stock}")
    print(f"{len(rows)} rows")
    missing = [item for item, _stock in SEED if item not in {name for name, _stock in rows}]
    if missing:
        print(f"missing seed items: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
