"""通用 HTTP 客户端：超时重试（指数退避）+ certifi CA 证书链。

所有 collector 的网络请求都应该走这里，不要各自 urlopen 一遍——重试策略统一在这里改。
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import certifi

logger = logging.getLogger(__name__)

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 15
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0


class HttpError(Exception):
    """请求最终失败（重试耗尽，或 4xx 客户端错误不重试直接失败）。"""


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_retries: int = MAX_RETRIES,
) -> str:
    """发起一次 HTTP 请求，返回响应正文（str）。

    网络错误 / 5xx 按指数退避重试（最多 max_retries 次）；4xx 判定为客户端错误，
    重试无意义，直接抛出。
    """
    req_headers = {"User-Agent": DEFAULT_USER_AGENT, **(headers or {})}
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise HttpError(f"{method} {url} -> HTTP {e.code}（客户端错误，不重试）") from e
            last_error = e
        except urllib.error.URLError as e:
            last_error = e

        if attempt < max_retries - 1:
            delay = BACKOFF_BASE_S * (2**attempt)
            logger.warning(
                "请求失败（第 %d/%d 次），%.1fs 后重试：%s %s",
                attempt + 1, max_retries, delay, url, last_error,
            )
            time.sleep(delay)

    raise HttpError(f"{method} {url} 重试 {max_retries} 次后仍失败：{last_error}") from last_error


def fetch_json(url: str, **kwargs: Any) -> Any:
    return json.loads(fetch(url, **kwargs))


DEFAULT_RATE_LIMIT_MS = 500


def rate_limit_seconds(config: dict[str, Any]) -> float:
    """从 sources.yaml 的配置块里取 rate_limit_ms，换算成 time.sleep() 用的秒数。

    不要写成 `cfg.get("rate_limit_ms") or 500`——0 是合法值（比如测试里想关掉限速），
    但 `or` 会把它当假值吞掉、错误地换成默认的 500ms。
    """
    rate_limit_ms = config.get("rate_limit_ms")
    if rate_limit_ms is None:
        rate_limit_ms = DEFAULT_RATE_LIMIT_MS
    return rate_limit_ms / 1000
