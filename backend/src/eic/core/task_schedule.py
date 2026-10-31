"""严格五字段 cron；候选时间做 UTC 往返校验，拒绝未选择的 DST 策略。"""
from datetime import UTC, datetime, timedelta
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("invalid_time_zone") from exc


def field_values(field, minimum, maximum):
    values = set()
    for item in field.split(","):
        if not re.fullmatch(r"(?:\*|\d+(?:-\d+)?)(?:/\d+)?", item):
            raise ValueError("invalid_cron_field")
        base, _, step_text = item.partition("/")
        step = int(step_text or "1")
        if step < 1 or step > maximum - minimum + 1:
            raise ValueError("invalid_cron_step")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start, end = map(int, base.split("-"))
        else:
            start = int(base)
            end = maximum if step_text else start
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron_out_of_range")
        values.update(range(start, end + 1, step))
    return values


def parse(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron_requires_five_fields")
    values = [field_values(f, low, high) for f, (low, high) in
              zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)), strict=True)]
    values[4] = {v % 7 for v in values[4]}
    return fields, values


def day_matches(day, fields, values):
    dom, dow = day.day in values[2], (day.weekday() + 1) % 7 in values[4]
    # Unix cron 的两个日字段均受限时使用 OR；星号含步长仍属于通配字段。
    if fields[2].startswith("*") or fields[4].startswith("*"):
        return dom and dow
    return dom or dow


def matches(expression, moment):
    fields, values = parse(expression)
    return (moment.minute in values[0] and moment.hour in values[1]
            and moment.month in values[3] and day_matches(moment, fields, values))


def next_time(expression, time_zone, after, policy="hold"):
    fields, values = parse(expression)
    tz = zone(time_zone)
    after = utc(after)
    day = after.astimezone(tz).date()
    for _ in range(366 * 8):
        if day.month in values[3] and day_matches(day, fields, values):
            candidates = []
            barriers = []
            for hour in sorted(values[1]):
                for minute in sorted(values[0]):
                    local = datetime(day.year, day.month, day.day, hour, minute)
                    instants = sorted({
                        local.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1)
                        if local.replace(tzinfo=tz, fold=fold).astimezone(UTC).astimezone(tz).replace(tzinfo=None) == local
                    })
                    future = [instant for instant in instants if instant > after]
                    if not future:
                        # DST 跳过的当地时刻也需要明确策略，不默默移到下一小时。
                        if not instants and policy == "hold":
                            guessed = local.replace(tzinfo=tz).astimezone(UTC)
                            if guessed > after:
                                barriers.append(guessed)
                        continue
                    if len(instants) == 2 and policy == "hold":
                        barriers.append(future[0])
                        continue
                    # skip/coalesce 均选择重叠时刻的第一次；跳过不存在的当地分钟。
                    if instants[0] > after:
                        candidates.append(instants[0])
            if barriers and (not candidates or min(barriers) <= min(candidates)):
                raise ValueError("dst_policy_required")
            if candidates:
                return min(candidates)
        day += timedelta(days=1)
    raise ValueError("cron_has_no_occurrence_within_eight_years")


def normalize_once(value, time_zone):
    if value is None:
        raise ValueError("once_requires_run_at")
    if value.tzinfo is not None:
        return utc(value)
    tz = zone(time_zone)
    candidates = {
        value.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1)
        if value.replace(tzinfo=tz, fold=fold).astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value
    }
    if len(candidates) != 1:
        raise ValueError("once_requires_offset_for_dst")
    return candidates.pop()
