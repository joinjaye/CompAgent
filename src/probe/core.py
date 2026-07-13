"""Phase 1 数据源验活：对 config/sources.yaml 里已填的源发起一次真实请求，
确认能拿到 >=1 条真实公告；detail_mode=blocked 的源直接报受阻原因，不发请求。

这不是采集器（Phase 2 职责），只做最小化的"活不活、有没有数据"验证：
用 field_mapping.post_time 对应的 key 在原始响应文本里出现的次数作为“确认拿到
了真实条目”的信号，对 JSON 列表接口和 HTML 内嵌 JSON 页面都通用，不需要为每个
源写专门的解析器。
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
import yaml

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

DEFAULT_SOURCES_PATH = Path(__file__).resolve().parents[2] / "config" / "sources.yaml"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_S = 15
DEFAULT_RATE_LIMIT_MS = 500
PROBE_PAGE_SIZE = 3  # 只为验活，不需要拉全量


@dataclass
class ProbeResult:
    source: str
    locale: str
    status: str  # OK / FAIL / BLOCKED
    http_code: int | None
    count: int
    note: str


def load_sources(path: Path | str = DEFAULT_SOURCES_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["sources"]


def _build_url(cfg: dict[str, Any]) -> str:
    endpoint = cfg["endpoint"]
    pagination = cfg.get("pagination") or {}
    if pagination.get("type") == "offset" and pagination.get("page_size_param"):
        page_param = pagination.get("param", "page")
        size_param = pagination["page_size_param"]
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{page_param}=1&{size_param}={PROBE_PAGE_SIZE}"
    return endpoint


def _marker_key(field_mapping: dict[str, Any]) -> str | None:
    """用 post_time 对应的 key 作为"这是一条真实公告"的信号。

    有的字段是点号路径（如 phemex 的 i18n.updatedAt 用在 update_time 上，
    但 post_time 本项目全部是单层 key），这里只处理最后一段以防万一。
    """
    key = field_mapping.get("post_time")
    if not key:
        return None
    return key.rsplit(".", 1)[-1]


def _fetch(url: str, headers: dict[str, str]) -> tuple[int | None, str]:
    req_headers = {"User-Agent": DEFAULT_USER_AGENT, **headers}
    request = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(
            request, timeout=DEFAULT_TIMEOUT_S, context=_SSL_CONTEXT
        ) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        return None, str(e.reason)


def probe_one(source: str, locale: str, cfg: dict[str, Any]) -> ProbeResult:
    if cfg.get("detail_mode") == "blocked" or not cfg.get("endpoint"):
        reason = cfg.get("blocked_reason", "endpoint 未填，见 sources.yaml 注释")
        return ProbeResult(source, locale, "BLOCKED", None, 0, reason)

    url = _build_url(cfg)
    http_code, body = _fetch(url, cfg.get("headers") or {})

    if http_code is None or http_code >= 400:
        return ProbeResult(
            source, locale, "FAIL", http_code, 0, f"请求失败：{body or http_code}"
        )

    marker = _marker_key(cfg.get("field_mapping") or {})
    if marker is None:
        return ProbeResult(
            source, locale, "FAIL", http_code, 0, "field_mapping.post_time 未填，无法验证"
        )

    # 不同源的响应格式不一样：Zendesk/RSC 是带引号的 JSON key（"createdAt"），
    # 有的源（如 Phemex）是不带引号的 JS 对象字面量（{createdAt: ...}），
    # 所以只匹配裸 key 本身，两种格式都能命中。
    count = body.count(marker)
    if count >= 1:
        return ProbeResult(
            source, locale, "OK", http_code, count, f'响应中出现 "{marker}" {count} 次'
        )
    return ProbeResult(
        source, locale, "FAIL", http_code, 0, f'响应 200 但未找到 "{marker}" 字段，可能页面结构已变'
    )


def probe_all(
    sources: dict[str, Any],
    source_filter: str | None = None,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for source, locales in sources.items():
        if source_filter and source != source_filter:
            continue
        for locale, cfg in locales.items():
            result = probe_one(source, locale, cfg)
            results.append(result)
            if result.status != "BLOCKED":
                rate_limit_ms = cfg.get("rate_limit_ms") or DEFAULT_RATE_LIMIT_MS
                time.sleep(rate_limit_ms / 1000)
    return results
