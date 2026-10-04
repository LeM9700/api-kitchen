from datetime import UTC, datetime

from worker.tasks.haccp_opening_reminders import opening_due_times


def test_opening_reminders_are_quarter_hourly_and_include_h0():
    opening = datetime(2026, 10, 5, 9, 10, tzinfo=UTC)
    assert opening_due_times(opening) == (
        datetime(2026, 10, 5, 8, 10, tzinfo=UTC),
        datetime(2026, 10, 5, 8, 25, tzinfo=UTC),
        datetime(2026, 10, 5, 8, 40, tzinfo=UTC),
        datetime(2026, 10, 5, 8, 55, tzinfo=UTC),
        opening,
    )


def test_reminder_schedule_preserves_local_wall_clock_before_dst_change():
    opening = datetime(2026, 10, 25, 9, 10, tzinfo=UTC)
    due = opening_due_times(opening)
    assert len(due) == 5
    assert (due[-1] - due[-2]).total_seconds() == 15 * 60
