from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MSK_OFFSET_HOURS = 3
DEFAULT_HABIT_TIMEZONE = "Europe/Moscow"
HABIT_TIMEZONE_OPTIONS = (
    ("Europe/Moscow", "Москва"),
    ("Asia/Almaty", "Алматы"),
    ("Europe/Berlin", "Берлин"),
    ("Europe/London", "Лондон"),
    ("America/New_York", "Нью-Йорк"),
    ("America/Los_Angeles", "Лос-Анджелес"),
    ("Asia/Dubai", "Дубай"),
    ("Asia/Tokyo", "Токио"),
    ("UTC", "UTC"),
)
HABIT_TIMEZONE_LABELS = dict(HABIT_TIMEZONE_OPTIONS)


def msk_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=MSK_OFFSET_HOURS)


def format_msk(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M", fallback: str = "-") -> str:
    if not dt:
        return fallback
    return dt.strftime(fmt)


def normalize_habit_timezone(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value


def habit_timezone_name(value: str | None) -> str:
    return normalize_habit_timezone(value) or DEFAULT_HABIT_TIMEZONE


def resolve_habit_timezone(value: str | None) -> ZoneInfo:
    return ZoneInfo(habit_timezone_name(value))


def format_habit_timezone(value: str | None) -> str:
    name = habit_timezone_name(value)
    label = HABIT_TIMEZONE_LABELS.get(name, name)
    suffix = " (по умолчанию)" if not normalize_habit_timezone(value) else ""
    return f"{label} — {name}{suffix}"


def as_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_naive(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


def habit_local_now(timezone_name: str | None = None, now: datetime | None = None) -> datetime:
    return as_utc(now).astimezone(resolve_habit_timezone(timezone_name))


def normalize_habit_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        hour, minute = (int(part) for part in value.strip().split(":"))
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _valid_local_candidates(local_value: datetime, zone: ZoneInfo) -> list[datetime]:
    candidates = []
    seen = set()
    for fold in (0, 1):
        candidate = local_value.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip != local_value:
            continue
        candidate_utc = candidate.astimezone(timezone.utc)
        if candidate_utc not in seen:
            seen.add(candidate_utc)
            candidates.append(candidate)
    return sorted(candidates, key=lambda item: item.astimezone(timezone.utc))


def calculate_next_habit_reminder(
    time_target: str,
    timezone_name: str | None = None,
    now: datetime | None = None,
    last_reminder_at: datetime | None = None,
) -> datetime:
    normalized_time = normalize_habit_time(time_target)
    if not normalized_time:
        raise ValueError(f"Invalid habit time: {time_target!r}")

    hour, minute = map(int, normalized_time.split(":"))
    now_utc = as_utc(now)
    zone = resolve_habit_timezone(timezone_name)
    local_now = now_utc.astimezone(zone)
    first_date = local_now.date()
    if last_reminder_at is not None:
        last_local_date = as_utc(last_reminder_at).astimezone(zone).date()
        if last_local_date >= first_date:
            first_date = last_local_date + timedelta(days=1)

    for day_offset in range(370):
        local_date = first_date + timedelta(days=day_offset)
        local_value = datetime.combine(local_date, time(hour=hour, minute=minute))
        candidates = _valid_local_candidates(local_value, zone)
        if candidates:
            for candidate in candidates:
                candidate_utc = candidate.astimezone(timezone.utc)
                if candidate_utc > now_utc:
                    return candidate_utc
            continue

        # A target inside the spring-forward gap does not exist. ZoneInfo's
        # fold=0 mapping lands on the first valid wall-clock time after the gap.
        candidate = local_value.replace(tzinfo=zone, fold=0)
        candidate_utc = candidate.astimezone(timezone.utc)
        round_trip = candidate_utc.astimezone(zone).replace(tzinfo=None)
        if round_trip > local_value and candidate_utc > now_utc:
            return candidate_utc

    raise RuntimeError("Could not calculate the next habit reminder")
