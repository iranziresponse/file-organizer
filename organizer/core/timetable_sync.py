"""Makerere Timetable (https://timetable.mak.ac.ug) client and parser.

The site is a plain server-rendered PHP app (no login required to view a
timetable) with an AngularJS front end for its cascading Academic Year ->
Semester -> Type -> College -> Group picker. There is no JSON/iCal export,
only a client-side "Download as PDF" button, so this scrapes the same
endpoints that page's own JavaScript calls:

  getSemester.php   POST {academicYearId}                      -> semesters
  getUnits.php      POST {academicYearId, semesterId, typeId}   -> colleges
                    (named "Unit" on the site; it actually returns colleges)
  getGroupsQa.php   POST {..., unit}                            -> groups
  index.php         POST {..., GroupName, Submit=Search}        -> the
                    timetable itself, rendered server-side into the
                    response HTML's #timetable table -- not a further AJAX
                    call, so no JS execution is needed to read it.

All public functions return (result, error_message) tuples -- never throw.
Confirmed working with plain `requests` (no special CA bundle needed here,
unlike muele.mak.ac.ug -- see muele_api.py for why that one needs one).
"""

import calendar
import logging
import re
from datetime import date, datetime, timedelta

import requests
from django.utils.html import strip_tags

BASE_URL = "https://timetable.mak.ac.ug"
_DEFAULT_TIMEOUT = 20

logger = logging.getLogger("organizer.timetable")

TYPE_IDS = {
    "teaching": 1,
    "examination": 2,
    "recess": 3,
    "test": 4,
}

_WEEKDAY_COLUMNS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _get(path, **kwargs):
    try:
        resp = requests.get(f"{BASE_URL}/{path}", timeout=_DEFAULT_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp, None
    except requests.RequestException as exc:
        logger.warning("Makerere Timetable GET %s failed: %s", path, exc)
        return None, f"Could not reach the Makerere Timetable site: {exc}"


def _post_json(path, payload):
    try:
        resp = requests.post(f"{BASE_URL}/{path}", json=payload, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as exc:
        logger.warning("Makerere Timetable POST %s failed: %s", path, exc)
        return None, f"Could not reach the Makerere Timetable site: {exc}"
    except ValueError as exc:
        logger.warning("Makerere Timetable POST %s returned bad JSON: %s", path, exc)
        return None, "The Makerere Timetable site returned something Orch didn't expect."


def fetch_academic_years():
    """Scrapes the live <select name="academicYearId"> options instead of
    hardcoding a year list that would go stale every August."""
    resp, error = _get("")
    if error:
        return [], error

    options = re.findall(
        r'<option value="(\d+)">\s*([^<]+?)\s*</option>', resp.text
    )
    years = [{"id": value, "name": label.strip()} for value, label in options if value]
    if not years:
        return [], "Could not find any academic years on the Makerere Timetable site."
    return years, None


def fetch_semesters(academic_year_id):
    data, error = _post_json("getSemester.php", {"academicYearId": int(academic_year_id)})
    return data or [], error


def fetch_colleges(academic_year_id, semester_id, kind):
    """Confusingly named "Unit" on the site itself; what it actually
    returns is the list of colleges offering that Type of timetable."""
    data, error = _post_json("getUnits.php", {
        "academicYearId": int(academic_year_id),
        "semesterId": int(semester_id),
        "typeId": TYPE_IDS[kind],
    })
    return data or [], error


def fetch_groups(academic_year_id, semester_id, kind, college):
    data, error = _post_json("getGroupsQa.php", {
        "academicYearId": int(academic_year_id),
        "semesterId": int(semester_id),
        "typeId": TYPE_IDS[kind],
        "unit": college,
    })
    return data or [], error


def fetch_timetable_html(academic_year_id, semester_id, kind, college, group):
    try:
        resp = requests.post(
            f"{BASE_URL}/index.php",
            data={
                "academicYearId": int(academic_year_id),
                "semesterId": int(semester_id),
                "typeId": TYPE_IDS[kind],
                "unit": college,
                "GroupName": group,
                "Submit": "Search",
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text, None
    except requests.RequestException as exc:
        logger.warning("Makerere Timetable index.php POST failed: %s", exc)
        return None, f"Could not reach the Makerere Timetable site: {exc}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")


def _extract_result_table(html):
    """The real results live in the second, unstyled <table border="border">
    nested inside #timetable -- the outer one is just AdminLTE chrome."""
    marker = html.find('id="timetable"')
    if marker == -1:
        return None
    inner_start = html.find('<table border="border"', marker)
    if inner_start == -1:
        return None
    inner_end = html.find("</table>", inner_start)
    if inner_end == -1:
        return None
    return html[inner_start:inner_end]


def _split_cell(cell_html):
    """Cells look like "SE-2<br>BSE 2107 Object Oriented Programming II<br>
    LLT 6A<br>Joab Agaba<br><br>" -- split on <br> and strip tags/whitespace
    from each part, dropping empties."""
    parts = re.split(r"<br\s*/?>", cell_html)
    return [strip_tags(p).strip() for p in parts if strip_tags(p).strip()]


def _split_course(text):
    """"BSE 2107 Object Oriented Programming II" -> ("BSE2107", "Object
    Oriented Programming II"). Falls back to (text, "") if it doesn't start
    with a recognizable "LETTERS DIGITS" code."""
    match = re.match(r"^([A-Z]{2,6})\s?(\d{3,4})\s+(.+)$", text)
    if not match:
        return text, ""
    letters, digits, name = match.groups()
    return f"{letters}{digits}", name.strip()


def parse_teaching_table(html, raw_group):
    """Parses a Teaching or Recess Term result: a weekly grid, one row per
    time slot, one column per weekday (Monday-Saturday). Recurring every
    week of the semester -- no calendar date is published, so entries are
    matched against today's real weekday at notification time instead of a
    stored date. Returns a list of dicts ready for TimetableEntry(**dict)
    minus profile/connection/kind."""
    table_html = _extract_result_table(html)
    if not table_html:
        return []

    entries = []
    for row_html in _ROW_RE.findall(table_html):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 2:
            continue
        time_match = _TIME_RANGE_RE.match(strip_tags(cells[0]).strip())
        if not time_match:
            continue
        start_time, end_time = time_match.groups()

        for weekday_index, cell_html in enumerate(cells[1:1 + len(_WEEKDAY_COLUMNS)]):
            parts = _split_cell(cell_html)
            if not parts or parts == ["---"]:
                continue
            # parts[0] is the group name (e.g. "SE-2", sometimes
            # "SE-2+CS-2" for a joint/combined session); parts[1] is the
            # "CODE Name" course line; parts[2:] are room/lecturer, in
            # whatever order the site happened to include them.
            course_text = parts[1] if len(parts) > 1 else ""
            code, name = _split_course(course_text)
            room = parts[2] if len(parts) > 2 else ""
            lecturer = parts[3] if len(parts) > 3 else ""
            lecturer = lecturer.replace("Proposed Lecturer = ", "").strip()
            entries.append({
                "weekday": weekday_index,
                "start_time": start_time,
                "end_time": end_time,
                "course_code": code,
                "course_name": name,
                "room": room,
                "lecturer": lecturer,
                "raw_group": raw_group,
            })
    return entries


_WEEK_HEADER_RE = re.compile(
    r"Week\s+\d+\s*\((\d{1,2})\w{0,2}\s*-\s*(\d{1,2})\w{0,2}\s+([A-Za-z]+)\)"
)
_WEEK_SLOT_RE = re.compile(r"WK-\d+\s+(.+)")


def parse_exam_table(html, raw_group, academic_year_label):
    """Parses a Test or Examination result: a "Week N (day - day Month)"
    header row followed by time-slot rows for that week, repeated per week.
    Each week's date range is parsed into a real start_date -- Semester I
    falls in the first calendar year of "2025/2026", Semester II in the
    second -- and combined with the row's weekday column to get an exact
    date. If a week's header can't be parsed with confidence, its slots are
    still kept (so nothing is silently dropped) but with specific_date left
    null and date_label holding the raw text, so Orch never guesses at a
    reminder time it isn't sure of."""
    table_html = _extract_result_table(html)
    if not table_html:
        return []

    first_year_str = academic_year_label.split("/")[0].strip() if academic_year_label else ""
    first_year = int(first_year_str) if first_year_str.isdigit() else None

    entries = []
    week_start_date = None
    week_label = ""

    for row_html in _ROW_RE.findall(table_html):
        cells = _CELL_RE.findall(row_html)
        if not cells:
            continue
        first_cell_text = strip_tags(cells[0]).strip()

        header_match = _WEEK_HEADER_RE.search(first_cell_text)
        if header_match:
            week_label = first_cell_text
            week_start_date = _resolve_week_start(header_match, first_year)
            continue

        slot_match = _WEEK_SLOT_RE.match(first_cell_text)
        if not slot_match:
            continue
        time_match = _AMPM_TIME_RANGE_RE.search(slot_match.group(1))
        if not time_match:
            continue
        start_time = _to_24h(time_match.group(1), time_match.group(2))
        end_time = _to_24h(time_match.group(3), time_match.group(4))

        for weekday_index, cell_html in enumerate(cells[1:1 + len(_WEEKDAY_COLUMNS)]):
            parts = _split_cell(cell_html)
            if not parts or parts == ["---"]:
                continue
            course_text = parts[1] if len(parts) > 1 else ""
            code, name = _split_course(course_text)
            specific_date = None
            if week_start_date is not None:
                specific_date = week_start_date + timedelta(days=weekday_index)
            entries.append({
                "specific_date": specific_date,
                "date_label": week_label,
                "start_time": start_time,
                "end_time": end_time,
                "course_code": code,
                "course_name": name,
                "room": "",
                "lecturer": "",
                "raw_group": raw_group,
            })
    return entries


def _resolve_week_start(header_match, first_year):
    """header_match groups: (start_day, end_day, month_name). Exam weeks
    only ever appear within a single semester, so the month name alone
    (plus knowing which calendar year that semester falls in) is enough to
    resolve a real date -- no year is printed on the site itself."""
    if first_year is None:
        return None
    start_day, _end_day, month_name = header_match.groups()
    try:
        month_number = list(calendar.month_name).index(month_name.capitalize())
    except ValueError:
        try:
            month_number = list(calendar.month_abbr).index(month_name.capitalize()[:3])
        except ValueError:
            return None
    if month_number == 0:
        return None
    # Semester I (Aug-Dec) sits in the academic year's first calendar year;
    # Semester II (Jan-Jun) spills into its second. Months Jan-Jul are
    # assumed to belong to the following calendar year from `first_year`.
    year = first_year if month_number >= 8 else first_year + 1
    try:
        return date(year, month_number, int(start_day))
    except ValueError:
        return None


_AMPM_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*([AaPp][Mm])\s*-\s*(\d{1,2}:\d{2})\s*([AaPp][Mm])"
)


def _to_24h(time_str, ampm):
    """"8:00" + "AM" -> "08:00"; test/exam slots use 12-hour clock, unlike
    the teaching grid's plain 24-hour "08:00 - 09:00" labels."""
    try:
        parsed = datetime.strptime(f"{time_str}{ampm.upper()}", "%I:%M%p")
        return parsed.strftime("%H:%M")
    except ValueError:
        return time_str


def sync_group_timetable(profile, connection, kinds=("teaching", "recess", "test", "examination")):
    """Full-replace sync: fetches each requested Type for the connection's
    saved academic year/semester/college/group, and swaps in fresh rows for
    each kind -- never merges, so a class dropped from the official
    timetable disappears from Orch too instead of lingering forever."""
    from organizer.models import TimetableEntry

    config = connection.config or {}
    academic_year_id = config.get("academic_year_id")
    academic_year_label = config.get("academic_year_label", "")
    semester_id = config.get("semester_id")
    college = config.get("college")
    group = config.get("group")

    if not all([academic_year_id, semester_id, college, group]):
        return 0, "This connection is missing its academic year, semester, college, or group."

    total = 0
    new_entries = 0
    errors = []
    signature_fields = (
        "weekday", "specific_date", "start_time", "end_time", "course_code",
        "course_name", "room", "lecturer", "raw_group",
    )

    def signature(row):
        values = []
        for field in signature_fields:
            value = row.get(field)
            text = str(value) if value is not None else ""
            if field in {"start_time", "end_time"} and text:
                text = text[:5]
            elif field == "specific_date" and text:
                text = text[:10]
            values.append(text)
        return tuple(values)

    for kind in kinds:
        html, error = fetch_timetable_html(academic_year_id, semester_id, kind, college, group)
        if error:
            errors.append(f"{kind}: {error}")
            continue

        if kind in ("teaching", "recess"):
            rows = parse_teaching_table(html, group)
        else:
            rows = parse_exam_table(html, group, academic_year_label)

        existing = {
            signature(row)
            for row in TimetableEntry.objects.filter(
                profile=profile, connection=connection, kind=kind
            ).values(*signature_fields)
        }
        new_entries += sum(1 for row in rows if signature(row) not in existing)
        TimetableEntry.objects.filter(profile=profile, connection=connection, kind=kind).delete()
        TimetableEntry.objects.bulk_create([
            TimetableEntry(profile=profile, connection=connection, kind=kind, **row)
            for row in rows
        ])
        total += len(rows)

    connection.status = "connected" if total or not errors else "error"
    # last_sync_at means "last time this genuinely worked" -- a hard
    # failure (status=="error": nothing came back and something actually
    # went wrong) leaves the previous known-good timestamp alone instead
    # of overwriting it with "just now", so the UI can honestly show when
    # the last real sync was even while today's attempt is failing.
    if connection.status != "error":
        from django.utils import timezone
        connection.last_sync_at = timezone.now()
    connection.save(update_fields=["status", "last_sync_at"])

    if new_entries:
        from . import notifications

        notifications.notify_new_items(
            "New timetable sessions",
            "timetable",
            new_entries,
            profile=profile,
        )

    return total, "; ".join(errors) if errors else None
