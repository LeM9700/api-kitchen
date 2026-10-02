"""Moteur de frais de livraison, cote serveur uniquement.

Ordre d'application, du plus fort au plus faible :

1. code promo ou recompense fidelite « livraison offerte » -> frais a 0 ;
2. regle de zone « livraison offerte » applicable (jour, horaire, periode, seuil de panier) ;
3. regle de zone tarifaire applicable (la plus haute priorite, puis le tarif le plus bas) ;
4. tarif de base de la zone.

Une regle est « applicable » quand le moment present (heure locale de l'etablissement) tombe
dans sa fenetre de jours / horaires / dates et que le sous-total atteint son seuil.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Paris"


class RuleLike(Protocol):
    id: int
    label: str
    kind: str
    fee: float | None
    min_subtotal: float | None
    days_of_week: list | None
    start_time: time | None
    end_time: time | None
    starts_on: date | None
    ends_on: date | None
    priority: int
    is_active: bool


@dataclass(frozen=True)
class PricingResult:
    base_fee: float
    fee: float
    free_delivery: bool
    # 'promotion' | 'loyalty' | 'rule' | None
    applied: str | None
    applied_label: str | None
    # Montant qu'il manque au panier pour debloquer une livraison offerte (None si rien a
    # debloquer ou si la livraison est deja offerte).
    remaining_for_free: float | None


def _money(value: float | None) -> float:
    return round(float(value or 0), 2)


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _date_ok(rule: RuleLike, day: date) -> bool:
    if rule.starts_on is not None and day < rule.starts_on:
        return False
    if rule.ends_on is not None and day > rule.ends_on:
        return False
    return True


def _day_ok(rule: RuleLike, day: date) -> bool:
    days = rule.days_of_week
    return not days or day.weekday() in days


def window_matches(rule: RuleLike, local_now: datetime) -> bool:
    """Vrai si ``local_now`` (datetime avec fuseau de l'etablissement) tombe dans les jours,
    horaires et dates de la regle. Ne regarde pas le seuil de panier."""
    day = local_now.date()
    now_time = local_now.time().replace(tzinfo=None)
    start, end = rule.start_time, rule.end_time

    if start is None or end is None or start == end:
        return _date_ok(rule, day) and _day_ok(rule, day)
    if start < end:
        return start <= now_time < end and _date_ok(rule, day) and _day_ok(rule, day)

    # Fenetre qui passe minuit (ex. 22:00 -> 02:00) : la nuit appartient au jour de debut.
    if now_time >= start:
        return _date_ok(rule, day) and _day_ok(rule, day)
    if now_time < end:
        previous = day - timedelta(days=1)
        return _date_ok(rule, previous) and _day_ok(rule, previous)
    return False


def evaluate_pricing(
    base_fee: float,
    rules: Iterable[RuleLike],
    subtotal: float,
    local_now: datetime,
    *,
    promo_free: bool = False,
    loyalty_free: bool = False,
) -> PricingResult:
    base = _money(base_fee)
    subtotal = _money(subtotal)
    candidates = [rule for rule in rules if rule.is_active and window_matches(rule, local_now)]
    eligible = [rule for rule in candidates if subtotal >= _money(rule.min_subtotal)]

    def best(items: list[RuleLike]) -> RuleLike:
        return sorted(items, key=lambda r: (-int(r.priority or 0), _money(r.fee), r.id))[0]

    if promo_free:
        return PricingResult(base, 0.0, True, "promotion", None, None)
    if loyalty_free:
        return PricingResult(base, 0.0, True, "loyalty", None, None)

    free_rules = [rule for rule in eligible if rule.kind == "free"]
    if free_rules:
        chosen = best(free_rules)
        return PricingResult(base, 0.0, True, "rule", chosen.label, None)

    fee_rules = [rule for rule in eligible if rule.kind == "fee"]
    fee = base
    applied: str | None = None
    label: str | None = None
    if fee_rules:
        chosen = best(fee_rules)
        fee = _money(chosen.fee)
        applied, label = "rule", chosen.label

    remaining: float | None = None
    if fee > 0:
        # Pas encore eligibles mais dans la bonne fenetre : on indique combien il manque.
        upcoming = [
            _money(rule.min_subtotal) - subtotal
            for rule in candidates
            if rule.kind == "free" and subtotal < _money(rule.min_subtotal)
        ]
        if upcoming:
            remaining = _money(min(upcoming))

    return PricingResult(base, fee, fee == 0, applied, label, remaining)


def local_now_for(timezone_name: str | None, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(resolve_timezone(timezone_name))
