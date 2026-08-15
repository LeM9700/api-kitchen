import inspect

from app import main
from app.main import _is_valid_sentry_dsn


def test_invalid_sentry_dsn_is_rejected() -> None:
    assert _is_valid_sentry_dsn("://:") is False


def test_http_sentry_dsn_is_accepted() -> None:
    assert _is_valid_sentry_dsn("https://public@example.ingest.sentry.io/1") is True


def test_lifespan_starts_redis_notification_subscriber() -> None:
    source = inspect.getsource(main.lifespan)
    assert "_redis_subscriber" in source
    assert "redis_subscriber_task" in source
