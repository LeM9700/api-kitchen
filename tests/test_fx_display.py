"""Tests affichage indicatif multi-devise (app.core.services.fx_rates,
catalog.router._apply_display_currency, worker.tasks.fx_rates_sync).

Purement indicatif : ces tests ne couvrent jamais la devise reellement
facturee (voir tests/test_tenant_currency.py, tests/test_payments.py pour
la devise verrouillee TenantConfig.currency)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.services import fx_rates


# ---------------------------------------------------------------------------
# fx_rates.fetch_latest_rates
# ---------------------------------------------------------------------------


async def test_fetch_latest_rates_parses_response():
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "result": "success",
        "base_code": "EUR",
        "rates": {"USD": 1.08, "GBP": 0.86, "CHF": 0.95},
    }
    fake_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)) as mock_get:
        rates = await fx_rates.fetch_latest_rates("EUR", symbols=["USD", "GBP"])

    # Filtrage cote client : l'API ne supporte pas de parametre "symbols",
    # elle renvoie toutes les devises (CHF ici) -- seules USD/GBP sont gardees.
    assert rates == {"USD": 1.08, "GBP": 0.86}
    args, _ = mock_get.call_args
    assert args[0] == "https://open.er-api.com/v6/latest/EUR"


async def test_fetch_latest_rates_raises_on_api_failure_result():
    fake_response = MagicMock()
    fake_response.json.return_value = {"result": "error", "error-type": "unsupported-code"}
    fake_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(ValueError):
            await fx_rates.fetch_latest_rates("RSD")


async def test_fetch_latest_rates_raises_on_http_error():
    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.ConnectTimeout("timeout")),
    ):
        with pytest.raises(httpx.ConnectTimeout):
            await fx_rates.fetch_latest_rates("EUR")


# ---------------------------------------------------------------------------
# fx_rates.refresh_and_cache_rates — ne leve jamais, degrade gracieusement
# ---------------------------------------------------------------------------


async def test_refresh_and_cache_rates_success_writes_cache():
    redis = AsyncMock()
    with patch.object(fx_rates, "fetch_latest_rates", new=AsyncMock(return_value={"USD": 1.08})):
        with patch.object(fx_rates, "set_cached_json", new=AsyncMock()) as mock_set:
            ok = await fx_rates.refresh_and_cache_rates(redis, "EUR", symbols=["USD"])

    assert ok is True
    mock_set.assert_awaited_once()
    args, kwargs = mock_set.call_args
    assert args[1] == "fx:rates:EUR"
    assert args[2] == {"USD": 1.08}
    assert kwargs["ttl_seconds"] == fx_rates._CACHE_TTL_SECONDS


async def test_refresh_and_cache_rates_failure_returns_false_without_raising():
    redis = AsyncMock()
    with patch.object(
        fx_rates, "fetch_latest_rates", new=AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    ):
        with patch.object(fx_rates, "set_cached_json", new=AsyncMock()) as mock_set:
            ok = await fx_rates.refresh_and_cache_rates(redis, "EUR")

    assert ok is False
    mock_set.assert_not_called()


# ---------------------------------------------------------------------------
# fx_rates.get_cached_rate
# ---------------------------------------------------------------------------


async def test_get_cached_rate_same_currency_returns_one_without_cache_read():
    redis = AsyncMock()
    with patch.object(fx_rates, "get_cached_json", new=AsyncMock()) as mock_get_json:
        rate = await fx_rates.get_cached_rate(redis, "EUR", "eur")

    assert rate == 1.0
    mock_get_json.assert_not_called()


async def test_get_cached_rate_reads_from_cache():
    redis = AsyncMock()
    with patch.object(fx_rates, "get_cached_json", new=AsyncMock(return_value={"USD": 1.08})):
        rate = await fx_rates.get_cached_rate(redis, "EUR", "USD")

    assert rate == 1.08


async def test_get_cached_rate_returns_none_when_cache_empty():
    redis = AsyncMock()
    with patch.object(fx_rates, "get_cached_json", new=AsyncMock(return_value=None)):
        rate = await fx_rates.get_cached_rate(redis, "EUR", "USD")

    assert rate is None


async def test_get_cached_rate_returns_none_when_target_missing_from_rates():
    redis = AsyncMock()
    with patch.object(fx_rates, "get_cached_json", new=AsyncMock(return_value={"GBP": 0.86})):
        rate = await fx_rates.get_cached_rate(redis, "EUR", "USD")

    assert rate is None


# ---------------------------------------------------------------------------
# catalog.router._apply_display_currency — no-op silencieux sur tout echec
# ---------------------------------------------------------------------------


async def test_apply_display_currency_mutates_items_when_rate_available():
    from app.modules.admin.tenants.models import TenantConfig
    from app.modules.catalog import router

    items = [{"base_price": 10.0}, {"base_price": 5.5}]
    session = AsyncMock()
    config = TenantConfig(currency="EUR")

    with (
        patch.object(router, "get_or_create_config", new=AsyncMock(return_value=config)),
        patch.object(router, "get_cached_rate", new=AsyncMock(return_value=1.08)),
    ):
        await router._apply_display_currency(items, "usd", session, redis=AsyncMock())

    assert items[0]["display_price"] == 10.8
    assert items[0]["display_currency"] == "USD"
    assert items[1]["display_price"] == 5.94
    assert items[1]["display_currency"] == "USD"


async def test_apply_display_currency_noop_when_currency_not_requested():
    from app.modules.catalog import router

    items = [{"base_price": 10.0}]
    with patch.object(router, "get_or_create_config", new=AsyncMock()) as mock_config:
        await router._apply_display_currency(items, None, session=AsyncMock(), redis=AsyncMock())

    assert "display_price" not in items[0]
    mock_config.assert_not_called()


async def test_apply_display_currency_noop_when_currency_unsupported():
    from app.modules.catalog import router

    items = [{"base_price": 10.0}]
    with patch.object(router, "get_or_create_config", new=AsyncMock()) as mock_config:
        await router._apply_display_currency(items, "JPY", session=AsyncMock(), redis=AsyncMock())

    assert "display_price" not in items[0]
    mock_config.assert_not_called()


async def test_apply_display_currency_noop_when_rate_unavailable():
    from app.modules.admin.tenants.models import TenantConfig
    from app.modules.catalog import router

    items = [{"base_price": 10.0}]
    config = TenantConfig(currency="EUR")

    with (
        patch.object(router, "get_or_create_config", new=AsyncMock(return_value=config)),
        patch.object(router, "get_cached_rate", new=AsyncMock(return_value=None)),
    ):
        await router._apply_display_currency(items, "USD", session=AsyncMock(), redis=AsyncMock())

    assert "display_price" not in items[0]


# ---------------------------------------------------------------------------
# worker.tasks.fx_rates_sync.refresh_fx_rates
# ---------------------------------------------------------------------------


async def test_refresh_fx_rates_refreshes_every_supported_currency():
    from app.modules.admin.tenants.schemas import SUPPORTED_CURRENCIES
    from worker.tasks import fx_rates_sync

    ctx = {"redis": AsyncMock()}
    with patch.object(
        fx_rates_sync, "refresh_and_cache_rates", new=AsyncMock(return_value=True)
    ) as mock_refresh:
        await fx_rates_sync.refresh_fx_rates(ctx)

    assert mock_refresh.await_count == len(SUPPORTED_CURRENCIES)
    called_bases = {call.args[1] for call in mock_refresh.await_args_list}
    assert called_bases == SUPPORTED_CURRENCIES
