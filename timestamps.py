"""UTC timestamps as written to logs, ledgers, and review files."""

from datetime import datetime, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_stamp(moment: datetime | None = None) -> str:
    """Now, or the given moment, in UTC. A naive moment is read as UTC."""

    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(STAMP_FORMAT)
