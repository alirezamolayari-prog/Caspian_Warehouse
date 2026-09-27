"""Minimal 5-field cron: "minute hour day-of-month month day-of-week".

Supports `*`, numbers, lists (`1,15`), ranges (`1-5`) and steps (`*/15`, `8-18/2`).
Day-of-week: 0 or 7 = Sunday, 1 = Monday ... 6 = Saturday (standard cron). When both
day fields are restricted, a day matches if either does (standard cron semantics).
"""

import datetime as dt

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("دقیقه", "ساعت", "روز ماه", "ماه", "روز هفته")


class CronError(ValueError):
    pass


def _field(text: str, low: int, high: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"گام نامعتبر در {name}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"بازه نامعتبر در {name}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"مقدار نامعتبر در {name}: {part!r}")
        if not (low <= start <= end <= high):
            raise CronError(f"{name} باید بین {low} و {high} باشد")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expression: str) -> None:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError("عبارت زمان‌بندی باید ۵ بخش داشته باشد (دقیقه ساعت روز ماه روزهفته).")
        self.expression = " ".join(parts)
        fields = [_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, _RANGES, _NAMES, strict=True)]
        self.minutes, self.hours, self.days, self.months, dows = fields
        self.dows = {d % 7 for d in dows}
        self._dom_any = parts[2] == "*"
        self._dow_any = parts[4] == "*"

    def _day_matches(self, day: dt.date) -> bool:
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.dows  # isoweekday: Mon=1..Sun=7 -> Sun=0
        if self._dom_any and self._dow_any:
            return True
        if self._dom_any:
            return dow
        if self._dow_any:
            return dom
        return dom or dow

    def next_after(self, after: dt.datetime) -> dt.datetime:
        """First matching minute strictly after `after`."""
        t = (after + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = t + dt.timedelta(days=366 * 5)
        while t < limit:
            if t.month not in self.months or not self._day_matches(t.date()):
                t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + dt.timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += dt.timedelta(minutes=1)
                continue
            return t
        raise CronError("این زمان‌بندی هیچ‌وقت اجرا نمی‌شود.")


PERSIAN_WEEKDAYS = {6: "شنبه", 0: "یکشنبه", 1: "دوشنبه", 2: "سه‌شنبه", 3: "چهارشنبه",
                    4: "پنجشنبه", 5: "جمعه"}


def describe(expression: str) -> str:
    """Human Persian description for the common shapes; falls back to the expression."""
    try:
        cron = Cron(expression)
    except CronError:
        return expression
    m, h, dom, mon, dow = expression.split()
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        at = f"ساعت {int(h):02d}:{int(m):02d}"
        if dow == "*":
            return f"هر روز {at}"
        if len(cron.dows) < 7:
            days = "، ".join(PERSIAN_WEEKDAYS[d] for d in (6, 0, 1, 2, 3, 4, 5) if d in cron.dows)
            return f"هر هفته {days} {at}"
    if m.isdigit() and h.isdigit() and dom.isdigit() and mon == "*" and dow == "*":
        return f"روز {dom} هر ماه (میلادی) ساعت {int(h):02d}:{int(m):02d}"
    return expression
