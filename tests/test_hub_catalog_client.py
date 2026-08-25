import pytest


def test_is_configured_false_when_catalog_url_empty(monkeypatch):
    from app.core.config import settings
    from app.modules.catalog import hub_client

    monkeypatch.setattr(settings, "pos_hub_catalog_url", "")
    assert hub_client.is_configured() is False


def test_is_configured_true_when_catalog_url_set(monkeypatch):
    from app.core.config import settings
    from app.modules.catalog import hub_client

    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    assert hub_client.is_configured() is True


async def test_fetch_catalog_raises_when_not_configured(monkeypatch):
    from app.core.config import settings
    from app.modules.catalog import hub_client

    monkeypatch.setattr(settings, "pos_hub_catalog_url", "")
    client = hub_client.HttpHubCatalogClient()

    with pytest.raises(hub_client.HubCatalogClientNotConfigured):
        await client.fetch_catalog({"access_token_encrypted": "irrelevant"})


class _FakeResponse:
    def __init__(self, json_data):
        self._json_data = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.last_call = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, timeout=None):
        self.last_call = {"url": url, "headers": headers}
        return self._response


async def test_fetch_catalog_calls_configured_url_with_access_token_header(monkeypatch):
    """HubRise authentifie via X-Access-Token, pas Authorization: Bearer --
    voir https://www.hubrise.com/developers/api/authentication."""
    from app.core.config import settings
    from app.core.services import crypto
    from app.modules.catalog import hub_client

    monkeypatch.setattr(settings, "pos_hub_catalog_url", "https://hub.example.com/catalog")
    monkeypatch.setattr(crypto, "decrypt_secret", lambda ciphertext: "plain-token")

    fake_response = _FakeResponse({"products": []})
    fake_client = _FakeAsyncClient(fake_response)
    monkeypatch.setattr(hub_client.httpx, "AsyncClient", lambda: fake_client)

    client = hub_client.HttpHubCatalogClient()
    result = await client.fetch_catalog({"access_token_encrypted": "cipher"})

    assert result == {"products": []}
    assert fake_client.last_call["url"] == "https://hub.example.com/catalog"
    assert fake_client.last_call["headers"]["X-Access-Token"] == "plain-token"
    assert "Authorization" not in fake_client.last_call["headers"]
