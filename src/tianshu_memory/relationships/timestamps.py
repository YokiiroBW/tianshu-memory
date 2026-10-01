"""One sortable UTC representation, including subsecond and offset inputs."""

from datetime import UTC


def stamp(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
