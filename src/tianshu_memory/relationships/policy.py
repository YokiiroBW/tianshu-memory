"""Deterministic, configurable affinity policy; no identity or transport imports."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..domain import fingerprint

STAGES = (
    "deeply_distant",
    "strongly_distant",
    "distant",
    "acquaintance",
    "familiar",
    "close",
    "intimate",
    "deeply_intimate",
)


@dataclass(frozen=True)
class Policy:
    minimum: int = -1200
    maximum: int = 1200
    boundaries: tuple[int, ...] = (-1200, -800, -400, 0, 200, 600, 900, 1200)
    positive_limit: int = 4
    negative_limit: int = 12
    daily_positive_limit: int = 12
    grace_days: int = 3
    decay_rates: tuple[int, int, int] = (2, 5, 8)
    hysteresis: int = 20
    timezone: str = "UTC"
    group_growth: bool = False

    def __post_init__(self):
        integers = (
            self.minimum,
            self.maximum,
            self.positive_limit,
            self.negative_limit,
            self.daily_positive_limit,
            self.grace_days,
            self.hysteresis,
            *self.boundaries,
            *self.decay_rates,
        )
        if any(type(v) is not int for v in integers):
            raise ValueError("Policy values must be integers")
        if not -1_000_000 <= self.minimum < 0 < self.maximum <= 1_000_000:
            raise ValueError("Invalid affinity bounds")
        if len(self.boundaries) != 8 or tuple(sorted(set(self.boundaries))) != self.boundaries:
            raise ValueError("Eight ordered unique stage boundaries required")
        if self.boundaries[0] != self.minimum or self.boundaries[-1] > self.maximum:
            raise ValueError("Stage coverage mismatch")
        if not 0 <= self.hysteresis <= self.maximum - self.minimum:
            raise ValueError("Invalid hysteresis")
        if not (1 <= self.positive_limit <= 100 and 1 <= self.negative_limit <= 100):
            raise ValueError("Invalid per-event limits")
        if not 0 <= self.daily_positive_limit <= 10000 or not 0 <= self.grace_days <= 365:
            raise ValueError("Invalid daily policy")
        if len(self.decay_rates) != 3 or any(not 0 <= v <= 100 for v in self.decay_rates):
            raise ValueError("Invalid decay rates")
        if type(self.group_growth) is not bool:
            raise ValueError("group_growth must be boolean")
        if self.timezone != "UTC":
            ZoneInfo(self.timezone)

    @property
    def version(self):
        return fingerprint(asdict(self))

    def clamp(self, value):
        return max(self.minimum, min(self.maximum, value))

    def day(self, moment: datetime):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Aware clock required")
        return (
            moment.astimezone(UTC if self.timezone == "UTC" else ZoneInfo(self.timezone))
            .date()
            .isoformat()
        )

    def stage(self, score, previous=None):
        index = max(i for i, boundary in enumerate(self.boundaries) if score >= boundary)
        if previous is None:
            return STAGES[index]
        old = STAGES.index(previous)
        if index > old:
            while old < index and score >= min(
                self.maximum, self.boundaries[old + 1] + self.hysteresis
            ):
                old += 1
            return STAGES[old]
        if index < old:
            while old > index and score < self.boundaries[old] - self.hysteresis:
                old -= 1
            return STAGES[old]
        return previous

    def decay(self, completed_days, already_days):
        """Closed form: bounded work even after a long offline interval."""
        first = max(self.grace_days + 1, already_days + 1)
        total = 0
        for lower, upper, rate in (
            (1, 7, self.decay_rates[0]),
            (8, 14, self.decay_rates[1]),
            (15, completed_days, self.decay_rates[2]),
        ):
            count = max(0, min(completed_days, upper) - max(first, lower) + 1)
            total += count * rate
        return total
