"""Model-level tests for the HR module (no DB required)."""

from app.modules.hr.models import (
    EmployeeProfile,
    Establishment,
    EstablishmentHrConfig,
    HrAlert,
    Shift,
    TimeClockCorrection,
    TimeClockEntry,
)


def test_establishment_tablename():
    assert Establishment.__tablename__ == "establishments"


def test_employee_profile_tablename_and_columns():
    assert EmployeeProfile.__tablename__ == "employee_profiles"
    cols = EmployeeProfile.__table__.columns
    assert "user_id" in cols
    assert "hourly_rate_cents" in cols
    assert "weekly_hours_contract" in cols


def test_shift_tablename_and_columns():
    assert Shift.__tablename__ == "shifts"
    cols = Shift.__table__.columns
    assert {"employee_id", "starts_at", "ends_at", "status"} <= set(cols.keys())


def test_time_clock_entry_tablename_and_columns():
    assert TimeClockEntry.__tablename__ == "time_clock_entries"
    cols = TimeClockEntry.__table__.columns
    assert {"employee_id", "clock_in_at", "clock_out_at", "method", "status"} <= set(cols.keys())


def test_time_clock_correction_tablename():
    assert TimeClockCorrection.__tablename__ == "time_clock_corrections"


def test_hr_alert_tablename_and_columns():
    assert HrAlert.__tablename__ == "hr_alerts"
    cols = HrAlert.__table__.columns
    assert {
        "employee_id",
        "establishment_id",
        "type",
        "severity",
        "payload",
        "last_alert_sent_at",
    } <= set(cols.keys())
    assert cols["employee_id"].nullable is True


def test_establishment_hr_config_defaults():
    col = EstablishmentHrConfig.__table__.columns["weekly_hours_legal_threshold"]
    assert col.default.arg == 35
