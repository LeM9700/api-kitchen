from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.modules.delivery import pricing as p

PARIS = ZoneInfo("Europe/Paris")
# 2026-10-02 est un vendredi (weekday() == 4).
FRIDAY_20H = datetime(2026, 10, 2, 20, 0, tzinfo=PARIS)


@dataclass
class Rule:
    id: int = 1
    label: str = "regle"
    kind: str = "free"
    fee: float | None = None
    min_subtotal: float | None = None
    days_of_week: list | None = None
    start_time: time | None = None
    end_time: time | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    priority: int = 0
    is_active: bool = True


def price(rules, subtotal=20, now=FRIDAY_20H, base=3.5, **kwargs):
    return p.evaluate_pricing(base, rules, subtotal, now, **kwargs)


def test_base_fee_when_no_rule():
    result = price([])
    assert (result.fee, result.base_fee, result.free_delivery, result.applied) == (3.5, 3.5, False, None)


def test_free_rule_with_threshold_unlocks_at_the_threshold_and_nudges_below():
    rule = Rule(label="Offerte des 25 EUR", min_subtotal=25)

    below = price([rule], subtotal=20)
    assert below.fee == 3.5 and not below.free_delivery
    assert below.remaining_for_free == 5.0

    exact = price([rule], subtotal=25)
    assert exact.fee == 0 and exact.free_delivery
    assert (exact.applied, exact.applied_label) == ("rule", "Offerte des 25 EUR")
    assert exact.remaining_for_free is None


def test_inactive_rule_is_ignored():
    assert price([Rule(is_active=False)]).fee == 3.5


def test_day_of_week_filter():
    friday_only = Rule(days_of_week=[4])
    monday_only = Rule(days_of_week=[0])
    assert price([friday_only]).free_delivery
    assert not price([monday_only]).free_delivery


def test_time_window_same_day_is_half_open():
    rule = Rule(start_time=time(18), end_time=time(21))
    assert price([rule], now=datetime(2026, 10, 2, 18, 0, tzinfo=PARIS)).free_delivery
    assert price([rule], now=datetime(2026, 10, 2, 20, 59, tzinfo=PARIS)).free_delivery
    assert not price([rule], now=datetime(2026, 10, 2, 21, 0, tzinfo=PARIS)).free_delivery
    assert not price([rule], now=datetime(2026, 10, 2, 17, 59, tzinfo=PARIS)).free_delivery


def test_time_window_crossing_midnight_belongs_to_the_starting_day():
    rule = Rule(days_of_week=[4], start_time=time(22), end_time=time(2))  # vendredi 22h -> samedi 2h
    assert price([rule], now=datetime(2026, 10, 2, 23, 0, tzinfo=PARIS)).free_delivery  # ven 23h
    assert price([rule], now=datetime(2026, 10, 3, 1, 0, tzinfo=PARIS)).free_delivery  # sam 1h
    assert not price([rule], now=datetime(2026, 10, 3, 3, 0, tzinfo=PARIS)).free_delivery  # sam 3h
    assert not price([rule], now=datetime(2026, 10, 2, 1, 0, tzinfo=PARIS)).free_delivery  # ven 1h : c'est la nuit de jeudi
    assert not price([rule], now=datetime(2026, 10, 4, 1, 0, tzinfo=PARIS)).free_delivery  # dim 1h : nuit de samedi


def test_date_range_is_inclusive():
    rule = Rule(starts_on=date(2026, 10, 2), ends_on=date(2026, 10, 2))
    assert price([rule]).free_delivery
    assert not price([rule], now=datetime(2026, 10, 3, 12, 0, tzinfo=PARIS)).free_delivery
    assert not price([rule], now=datetime(2026, 10, 1, 12, 0, tzinfo=PARIS)).free_delivery


def test_fee_rules_highest_priority_then_lowest_fee():
    low_priority_cheap = Rule(id=1, kind="fee", fee=1, priority=0)
    high_priority = Rule(id=2, kind="fee", fee=6, priority=5)
    result = price([low_priority_cheap, high_priority])
    assert result.fee == 6 and result.applied == "rule"

    tie_a = Rule(id=3, kind="fee", fee=4, priority=1)
    tie_b = Rule(id=4, kind="fee", fee=2, priority=1)
    assert price([tie_a, tie_b]).fee == 2


def test_free_rule_beats_fee_rule():
    result = price([Rule(id=1, kind="fee", fee=1, priority=99), Rule(id=2, kind="free")])
    assert result.fee == 0 and result.free_delivery


def test_promotion_and_loyalty_force_free_delivery_over_everything():
    assert price([Rule(kind="fee", fee=9)], promo_free=True).applied == "promotion"
    assert price([Rule(kind="fee", fee=9)], loyalty_free=True).applied == "loyalty"
    both = price([], promo_free=True, loyalty_free=True)
    assert both.fee == 0 and both.applied == "promotion"


def test_zero_base_fee_is_reported_as_free():
    assert price([], base=0).free_delivery


def test_window_is_evaluated_in_the_establishment_timezone():
    # 20h a Belgrade = 20h a Paris ; mais 2026-10-02T18:30Z = 20h30 a Paris et 20h30 a Belgrade
    # (meme fuseau en octobre). On verifie surtout qu'un instant UTC est converti.
    rule = Rule(start_time=time(20), end_time=time(21))
    utc_instant = datetime(2026, 10, 2, 18, 30, tzinfo=ZoneInfo("UTC"))
    assert price([rule], now=p.local_now_for("Europe/Paris", utc_instant)).free_delivery
    assert not price([rule], now=p.local_now_for("America/New_York", utc_instant)).free_delivery


def test_unknown_timezone_falls_back_to_paris():
    assert p.resolve_timezone("Mars/Olympus").key == "Europe/Paris"
    assert p.resolve_timezone(None).key == "Europe/Paris"


def test_local_now_accepts_naive_datetimes_as_utc():
    local = p.local_now_for("Europe/Paris", datetime(2026, 10, 2, 12, 0))
    assert local.hour == 14  # UTC+2 en octobre
