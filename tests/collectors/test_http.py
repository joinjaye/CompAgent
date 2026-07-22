from email.message import Message
from urllib.error import HTTPError

import pytest

from src.collectors import http


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"ok"


def _http_error(code: int, *, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://example.com", code, "error", headers, None)


def test_fetch_retries_429_and_honors_retry_after(monkeypatch):
    responses = iter([_http_error(429, retry_after="3"), _Response()])
    sleeps = []

    def fake_urlopen(*args, **kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(http.time, "sleep", sleeps.append)

    assert http.fetch("https://example.com") == "ok"
    assert sleeps == [3.0]


def test_fetch_does_not_retry_other_4xx(monkeypatch):
    calls = 0

    def fake_urlopen(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise _http_error(403)

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(http.HttpError, match="不重试"):
        http.fetch("https://example.com")
    assert calls == 1
