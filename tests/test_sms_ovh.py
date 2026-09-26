import hashlib
import json

import pytest

from worker.tasks import sms


class _FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload or {"ids": [123], "totalCreditsRemoved": 1}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, capture, *args, **kwargs):
        self.capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, content, headers):
        self.capture["url"] = url
        self.capture["content"] = content
        self.capture["headers"] = headers
        return _FakeResponse()


@pytest.mark.asyncio
async def test_send_ovh_sms_signs_request(monkeypatch):
    capture = {}
    monkeypatch.setattr(sms.settings, "ovh_endpoint", "ovh-eu")
    monkeypatch.setattr(sms.settings, "ovh_sms_service_name", "sms-test-1")
    monkeypatch.setattr(sms.settings, "ovh_application_key", "ak")
    monkeypatch.setattr(sms.settings, "ovh_application_secret", "as")
    monkeypatch.setattr(sms.settings, "ovh_consumer_key", "ck")
    monkeypatch.setattr(sms.settings, "ovh_sms_sender", "PIZZA")
    monkeypatch.setattr(sms.settings, "ovh_sms_no_stop_clause", True)
    monkeypatch.setattr(sms.time, "time", lambda: 1_700_000_000)
    monkeypatch.setattr(sms.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(capture, *a, **kw))

    result = await sms._send_ovh_sms(to_phone_e164="+33612345678", body="Votre code est 123456")

    assert result["ids"] == [123]
    assert capture["url"] == "https://eu.api.ovh.com/1.0/sms/sms-test-1/jobs"
    body_json = capture["content"].decode("utf-8")
    payload = json.loads(body_json)
    assert payload["receivers"] == ["+33612345678"]
    assert payload["message"] == "Votre code est 123456"
    assert payload["sender"] == "PIZZA"
    assert payload["noStopClause"] is True

    expected_signature = "$1$" + hashlib.sha1(
        ("as+ck+POST+" + capture["url"] + "+" + body_json + "+1700000000").encode("utf-8")
    ).hexdigest()
    assert capture["headers"]["X-Ovh-Application"] == "ak"
    assert capture["headers"]["X-Ovh-Consumer"] == "ck"
    assert capture["headers"]["X-Ovh-Timestamp"] == "1700000000"
    assert capture["headers"]["X-Ovh-Signature"] == expected_signature
