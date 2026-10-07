from datetime import date, datetime, time
from unittest import mock

from django.test import SimpleTestCase

from organizer.core import notifications, timetable_sync as ts
from organizer.models import IntegrationConnection, Notification, TimetableEntry

from .helpers import SandboxedPathsTestCase

# Trimmed to the structural shape parse_teaching_table/parse_exam_table
# actually look for -- the real site wraps this in a lot more AdminLTE
# chrome that the parser ignores anyway (it only looks inside the nested
# <table border="border">).
TEACHING_HTML = """
<div id="content-wrapper">
<table class="table table-striped table-bordered" id="timetable">
<thead></thead>
<tbody>
<tr><td>
<table border="border" class="table">
<tr><td colspan='7'>&nbsp;</td></tr>
<tr><td colspan='7' id='SE-2'><center><h4>SE-2</h4></center></td></tr>
<tr><td></td><td>Monday</td><td>Tuesday</td><td>Wednesday</td><td>Thursday</td><td>Friday</td><td>Saturday</td></tr>
<tr><td width='100px'>08:00 - 09:00</td><td>SE-2<br>BSE 2107 Object Oriented Programming II<br>LLT 6A<br>Joab Agaba<br><br></td><td>---</td><td>---</td><td>---</td><td>SE-2<br>CSC 2114 Artificial Intelligence<br>LLT 1A<br>Proposed Lecturer = Lillian Muyama<br><br></td><td>---</td></tr>
<tr><td width='100px'>09:00 - 10:00</td><td>---</td><td>---</td><td>---</td><td>---</td><td>---</td><td>---</td></tr>
</table>
</td></tr>
</tbody>
</table>
</div>
"""

EXAM_HTML = """
<div id="content-wrapper">
<table class="table table-striped table-bordered" id="timetable">
<thead></thead>
<tbody>
<tr><td>
<table border="border" class="table">
<tr><td colspan='7'>&nbsp;</td></tr>
<tr><td colspan='7' id='SE-2'><center><h4>SE-2</h4></center></td></tr>
<tr><td></td><td>Monday</td><td>Tuesday</td><td>Wednesday</td><td>Thursday</td><td>Friday</td></tr>
<tr><td width='100px'>Week 1 (17th - 21st November)</td><td>---</td><td>---</td><td>---</td><td>---</td><td>---</td></tr>
<tr><td width='100px'>WK-1 8:00AM - 11:00AM</td><td>---</td><td>---</td><td>---</td><td>---</td><td>---</td></tr>
<tr><td width='100px'>WK-1 12:00PM - 03:00PM</td><td>---</td><td>---</td><td>---</td><td>---</td><td>SE-2<br>BSE 2105 Formal Methods<br><br><br><br></td></tr>
</table>
</td></tr>
</tbody>
</table>
</div>
"""


class ParseTeachingTableTests(SimpleTestCase):
    def test_parses_weekday_time_and_course_from_each_occupied_cell(self):
        rows = ts.parse_teaching_table(TEACHING_HTML, "SE-2")

        self.assertEqual(len(rows), 2)
        monday = next(r for r in rows if r["weekday"] == 0)
        self.assertEqual(monday["start_time"], "08:00")
        self.assertEqual(monday["end_time"], "09:00")
        self.assertEqual(monday["course_code"], "BSE2107")
        self.assertEqual(monday["course_name"], "Object Oriented Programming II")
        self.assertEqual(monday["room"], "LLT 6A")
        self.assertEqual(monday["lecturer"], "Joab Agaba")

    def test_strips_the_proposed_lecturer_prefix(self):
        rows = ts.parse_teaching_table(TEACHING_HTML, "SE-2")

        friday = next(r for r in rows if r["weekday"] == 4)
        self.assertEqual(friday["lecturer"], "Lillian Muyama")

    def test_dash_cells_are_skipped_entirely(self):
        rows = ts.parse_teaching_table(TEACHING_HTML, "SE-2")

        # Only 2 of the 12 weekday cells across both time rows are occupied;
        # the rest are "---" and must not become empty/garbage entries.
        self.assertEqual(len(rows), 2)

    def test_missing_timetable_table_returns_no_rows(self):
        self.assertEqual(ts.parse_teaching_table("<html></html>", "SE-2"), [])


class ParseExamTableTests(SimpleTestCase):
    def test_resolves_a_real_calendar_date_from_the_week_header(self):
        rows = ts.parse_exam_table(EXAM_HTML, "SE-2", "2025/2026")

        self.assertEqual(len(rows), 1)
        entry = rows[0]
        # "Week 1 (17th - 21st November)", Friday column -> Nov 21, 2025.
        self.assertEqual(entry["specific_date"], date(2025, 11, 21))
        self.assertEqual(entry["course_code"], "BSE2105")
        self.assertEqual(entry["course_name"], "Formal Methods")

    def test_converts_12_hour_am_pm_slots_to_24_hour(self):
        rows = ts.parse_exam_table(EXAM_HTML, "SE-2", "2025/2026")

        self.assertEqual(rows[0]["start_time"], "12:00")
        self.assertEqual(rows[0]["end_time"], "15:00")

    def test_unparseable_academic_year_label_keeps_the_row_but_no_date(self):
        rows = ts.parse_exam_table(EXAM_HTML, "SE-2", "")

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["specific_date"])
        self.assertIn("Week 1", rows[0]["date_label"])


class TimetableNewItemNotificationTests(SandboxedPathsTestCase):
    @mock.patch.object(ts, "fetch_timetable_html", return_value=(TEACHING_HTML, None))
    def test_full_replace_sync_notifies_only_for_new_sessions(self, _fetch):
        profile = self.make_profile()
        connection = IntegrationConnection.objects.create(
            profile=profile,
            provider="mak_timetable",
            display_name="Makerere Timetable",
            config={
                "academic_year_id": "1",
                "academic_year_label": "2025/2026",
                "semester_id": "1",
                "college": "COCIS",
                "group": "SE-2",
            },
        )

        first_total, first_error = ts.sync_group_timetable(
            profile, connection, kinds=("teaching",)
        )
        second_total, second_error = ts.sync_group_timetable(
            profile, connection, kinds=("teaching",)
        )

        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first_total, 2)
        self.assertEqual(second_total, 2)
        self.assertEqual(
            Notification.objects.filter(
                profile=profile, title="New timetable sessions"
            ).count(),
            1,
        )


class SplitCourseTests(SimpleTestCase):
    def test_splits_code_and_name(self):
        self.assertEqual(
            ts._split_course("BSE 2107 Object Oriented Programming II"),
            ("BSE2107", "Object Oriented Programming II"),
        )

    def test_falls_back_to_bare_text_when_unrecognized(self):
        self.assertEqual(ts._split_course("Supervision"), ("Supervision", ""))


class CheckUpcomingClassesTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()
        self.connection = IntegrationConnection.objects.create(
            profile=self.profile, provider="mak_timetable", display_name="Makerere Timetable",
        )

    def _fake_now(self, wall_clock):
        patcher = mock.patch("organizer.core.notifications.datetime")
        mock_datetime = patcher.start()
        mock_datetime.now.return_value = wall_clock
        self.addCleanup(patcher.stop)
        return mock_datetime

    def test_notifies_for_a_lecture_starting_within_the_window(self):
        # A Monday at 09:52; the 10:00 lecture is 8 minutes out, inside the
        # default 15-minute window.
        self._fake_now(datetime(2026, 7, 20, 9, 52))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="teaching",
            weekday=0, start_time=time(10, 0), end_time=time(11, 0),
            course_code="BSE2107", course_name="Object Oriented Programming II",
            room="LLT 6A", raw_group="SE-2",
        )

        count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(count, 1)
        notification = Notification.objects.get()
        self.assertIn("BSE2107", notification.title)

    def test_does_not_notify_twice_for_the_same_occurrence(self):
        self._fake_now(datetime(2026, 7, 20, 9, 52))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="teaching",
            weekday=0, start_time=time(10, 0), raw_group="SE-2",
        )

        notifications.check_upcoming_classes(self.profile)
        second_count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(second_count, 0)
        self.assertEqual(Notification.objects.count(), 1)

    def test_ignores_lectures_outside_the_window(self):
        self._fake_now(datetime(2026, 7, 20, 9, 0))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="teaching",
            weekday=0, start_time=time(14, 0), raw_group="SE-2",
        )

        count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(count, 0)

    def test_ignores_lectures_on_a_different_weekday(self):
        # July 20 2026 is a Monday (weekday 0); this entry is Tuesday (1).
        self._fake_now(datetime(2026, 7, 20, 9, 52))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="teaching",
            weekday=1, start_time=time(10, 0), raw_group="SE-2",
        )

        count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(count, 0)

    def test_notifies_for_an_exam_on_the_exact_matching_date(self):
        self._fake_now(datetime(2026, 7, 20, 11, 55))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="examination",
            specific_date=date(2026, 7, 20), start_time=time(12, 0),
            course_code="BSE2105", course_name="Formal Methods", raw_group="SE-2",
        )

        count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(count, 1)
        self.assertIn("critical", Notification.objects.get().urgency)

    def test_exam_on_a_different_date_is_not_notified(self):
        self._fake_now(datetime(2026, 7, 20, 11, 55))
        TimetableEntry.objects.create(
            profile=self.profile, connection=self.connection, kind="examination",
            specific_date=date(2026, 7, 21), start_time=time(12, 0), raw_group="SE-2",
        )

        count = notifications.check_upcoming_classes(self.profile)

        self.assertEqual(count, 0)
