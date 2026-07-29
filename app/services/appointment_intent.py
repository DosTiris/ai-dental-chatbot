# app/services/appointment_intent.py
#
# OWNER OF: interpreting patient scheduling language.
#   - preferred date  ("thursday", "tomorrow", "july 16", "7/16")
#   - time preference ("morning", "afternoon", "after work")
#   - slot choice     ("the second one", "1:30", "10am works")
#   - yes / no        (final confirmation answer)
#
# Rule 3: date parsing lives here and ONLY here. Neither chat.py nor the
# booking services may re-implement any of this.
#
# Everything in this file is PURE: no database, no network, no client object.
# That keeps it fully unit-testable (see calendar_tests/test_appointment_intent.py).

import re
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

# Weekday names -> Python weekday numbers (Monday=0 ... Sunday=6).
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Time-preference buckets. "any" means the patient does not care.
PREF_MORNING = "morning"     # slot starts before 12:00 local
PREF_AFTERNOON = "afternoon" # slot starts 12:00-16:59 local
PREF_EVENING = "evening"     # slot starts 17:00+ local
PREF_ANY = "any"


def _norm(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9:/\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_preferred_date(user_text: str, today: date) -> Optional[date]:
    """
    Purpose: Extract the appointment day the patient asked for.
    Inputs:
        user_text: raw patient message.
        today:     current date IN THE CLIENT'S TIMEZONE (callers must convert
                   first — passing server-local "today" causes off-by-one days
                   near midnight, which is exactly the timezone-boundary bug
                   Rule 9 requires us to think about).
    Returns: a date, or None when no day could be understood.
    Failures: returns None; never raises on weird input.

    Interpretation rules (documented because language is ambiguous):
      - "today" / "tomorrow" / "day after tomorrow": literal.
      - Bare weekday ("thursday"): the next occurrence, today included.
      - "next thursday": the upcoming Thursday strictly AFTER today. English
        speakers disagree about "next"; the risk is acceptable because Mia
        always echoes the full resolved date ("Thursday, July 16") before
        showing slots AND again at confirmation, so a misread is caught by
        the patient before anything is booked.
      - "july 16" / "7/16": if that date already passed this year, assume the
        patient means next year.
      - Past explicit dates with a year ("7/16/2020") return None.
    """
    t = _norm(user_text)
    if not t:
        return None

    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if re.search(r"\btomorrow\b|\btmrw\b|\btmr\b", t):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b", t):
        return today

    explicit = _parse_explicit_date(t, today)
    if explicit is not None:
        return explicit

    return _parse_weekday(t, today)


def _parse_weekday(t: str, today: date) -> Optional[date]:
    """Resolve 'thursday' / 'next thursday' relative to `today` (see rules above)."""
    for name, weekday_num in _WEEKDAYS.items():
        if not re.search(rf"\b{name}\b", t):
            continue
        days_ahead = (weekday_num - today.weekday()) % 7
        wants_next = bool(re.search(rf"\bnext\s+{name}\b", t))
        if wants_next and days_ahead == 0:
            # "next thursday" said ON a Thursday -> the following Thursday.
            days_ahead = 7
        elif not wants_next and days_ahead == 0:
            # Bare "thursday" said on a Thursday -> today (min-notice filtering
            # will hide slots that are already too soon).
            days_ahead = 0
        elif wants_next and days_ahead > 0:
            # "next" means strictly after today; the modular result already is.
            pass
        return today + timedelta(days=days_ahead)
    return None


def _parse_explicit_date(t: str, today: date) -> Optional[date]:
    """Resolve 'july 16', '7/16', '7/16/2026' style dates (see rules above)."""
    # Month-name form: "july 16" or "16 july".
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})\b", t) or re.search(r"\b(\d{1,2})\s+([a-z]+)\b", t)
    if m:
        a, b = m.group(1), m.group(2)
        month = _MONTHS.get(a) or _MONTHS.get(b)
        day_str = b if a in _MONTHS else a
        if month:
            resolved = _safe_date(today.year, month, int(day_str))
            if resolved is None:
                return None
            if resolved < today:  # "July 2" asked on July 11 -> next year.
                resolved = _safe_date(today.year + 1, month, int(day_str))
            return resolved

    # Numeric form: 7/16 or 7/16/2026 (US month-first order).
    m = re.search(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            resolved = _safe_date(year, month, day)
            # Explicit past year is a mistake, not a rollover candidate.
            return resolved if resolved and resolved >= today else None
        resolved = _safe_date(today.year, month, day)
        if resolved is None:
            return None
        if resolved < today:
            resolved = _safe_date(today.year + 1, month, day)
        return resolved

    return None


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    """date() that returns None instead of raising on impossible dates (2/30)."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time_preference(user_text: str) -> Optional[str]:
    """
    Purpose: Classify the patient's time-of-day preference.
    Inputs:  raw patient message.
    Returns: PREF_MORNING / PREF_AFTERNOON / PREF_EVENING / PREF_ANY, or
             None when the message expressed no preference at all (so the
             caller knows to keep asking rather than silently assuming "any" —
             Rule 4: no silent fallbacks).
    Failures: returns None; never raises.
    """
    t = _norm(user_text)
    if not t:
        return None
    if re.search(r"\bmornings?\b|\bam\b|\bearly\b|before noon|before lunch", t):
        return PREF_MORNING
    if re.search(r"\bevenings?\b|after work|after 5|\blate\b|end of (the )?day", t):
        return PREF_EVENING
    if re.search(r"\bafternoons?\b|\bpm\b|after noon|after lunch|midday|mid day", t):
        return PREF_AFTERNOON
    if re.search(r"\bany\b|\banytime\b|\beither\b|whatever|no preference|doesn t matter|dont care|don t care|flexible|open", t):
        return PREF_ANY
    return None


def slot_matches_preference(start_local_hour: int, preference: str) -> bool:
    """
    Purpose: Decide if a slot's LOCAL start hour matches a preference bucket.
    Inputs:  start hour 0-23 (client timezone!), preference string.
    Returns: True/False. Unknown preference values behave as PREF_ANY on purpose:
             an unexpected stored value must never hide all availability.
    """
    if preference == PREF_MORNING:
        return start_local_hour < 12
    if preference == PREF_AFTERNOON:
        return 12 <= start_local_hour < 17
    if preference == PREF_EVENING:
        return start_local_hour >= 17
    return True  # PREF_ANY or anything unrecognized.


def match_slot_selection(
    user_text: str,
    offered: Sequence[Tuple[str, datetime]],
) -> Optional[str]:
    """
    Purpose: Figure out which OFFERED slot the patient picked.
    Inputs:
        user_text: raw patient message ("the second one", "1:30 works", "2").
        offered:   list of (slot_id, start_datetime_in_CLIENT_timezone) in the
                   exact order they were shown to the patient.
    Returns: the chosen slot_id, or None when the message doesn't clearly
             match exactly one option (caller re-asks — one question).
    Failures: returns None; never raises.

    Safety rule: only slots that were actually offered can match. The patient
    cannot type an arbitrary time and book an un-offered slot.
    """
    t = _norm(user_text)
    if not t or not offered:
        return None

    ordinals = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
    for word, idx in ordinals.items():
        if re.search(rf"\b{word}\b", t) and idx < len(offered):
            return offered[idx][0]

    # A bare small number is an index ("2") — but ONLY if it can't be confused
    # with a clock time that was offered (e.g. offered 2:00 PM and patient says
    # "2"). Time matching below wins in that case, so check times first.
    time_match = _match_by_time(t, offered)
    if time_match is not None:
        return time_match

    m = re.fullmatch(r"(?:option\s*)?([1-9])", t)
    if m:
        idx = int(m.group(1)) - 1
        return offered[idx][0] if idx < len(offered) else None

    return None


def _match_by_time(t: str, offered: Sequence[Tuple[str, datetime]]) -> Optional[str]:
    """Match '10', '10am', '1:30', '1 30 pm' against offered start times."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    meridiem = m.group(3)
    if hour > 23 or minute > 59:
        return None

    matches = []
    for slot_id, start_local in offered:
        for candidate_hour in _candidate_hours(hour, meridiem):
            if start_local.hour == candidate_hour and start_local.minute == minute:
                matches.append(slot_id)
                break
    # Exactly one match required; "10" when both 10 AM and 10 PM were offered
    # would be ambiguous, so we re-ask instead of guessing (Rule 4).
    return matches[0] if len(matches) == 1 else None


def _candidate_hours(hour: int, meridiem: Optional[str]) -> List[int]:
    """Expand '1' with no am/pm into both 1 and 13; respect explicit am/pm."""
    if meridiem == "am":
        return [0 if hour == 12 else hour]
    if meridiem == "pm":
        return [12 if hour == 12 else hour + 12] if hour <= 12 else [hour]
    if 1 <= hour <= 12:
        return [hour % 12, (hour % 12) + 12]  # e.g. 1 -> [1, 13]; 12 -> [0, 12]
    return [hour]


def parse_yes_no(user_text: str) -> Optional[bool]:
    """
    Purpose: Interpret the confirmation answer.
    Returns: True (yes), False (no), or None (ambiguous -> caller re-asks).
    Failures: returns None; never raises.
    """
    t = _norm(user_text)
    if re.search(r"\b(yes|yeah|yep|yup|correct|confirm|confirmed|right|sure|ok|okay|sounds good|that works|book it|perfect)\b", t):
        # Mixed signals are NOT a yes. "ok but can we do 2pm" must not book.
        # (Found during the Rule-10 "patient changes direction" mental test.)
        if re.search(r"\bbut\b", t):
            return None
        # "no that's not right ... sure" — any negative alongside an
        # affirmative is ambiguous; re-ask instead of guessing.
        if re.search(r"\b(no|nope|not|wrong|incorrect|cancel)\b", t):
            return None
        return True
    if re.search(r"\b(no|nope|nah|not right|wrong|incorrect|cancel|change)\b", t):
        return False
    return None
