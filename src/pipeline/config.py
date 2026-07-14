"""Phase 3 pipeline 共用的配置加载：category_mapping.yaml + sources.yaml 里每个源的
locale 集合。纯读取 + 解析，不发请求、不碰 DB。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CATEGORY_MAPPING_PATH = PROJECT_ROOT / "config" / "category_mapping.yaml"
SOURCES_YAML_PATH = PROJECT_ROOT / "config" / "sources.yaml"

_LOCALE_KEY_RE = re.compile(r"^[A-Z]{2}(-[A-Za-z]+)?$")


def load_category_mapping(path: Path | str = CATEGORY_MAPPING_PATH) -> dict[str, Optional[dict[str, str]]]:
    """key 是小写 source 名（bitunix/weex/...），value 是 {raw_category_str: category}
    或 None（如 lbank，无 per-item raw_category）。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_source_locales(path: Path | str = SOURCES_YAML_PATH) -> dict[str, set[str]]:
    """每个源实际配置了哪些 locale，从 sources.yaml 的真实结构解析（不是猜的/写死的），
    因为 Phase 2 各批次都可能新增 locale，这里读的是当前配置的真相。locale key 的写法
    统一是大写字母开头（EN/FR/ID/VN/EN-Asia），跟 endpoint/method 等小写配置 key 用
    正则区分。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = data.get("sources", {})
    result: dict[str, set[str]] = {}
    for source_name, source_cfg in sources.items():
        if not isinstance(source_cfg, dict):
            continue
        locales = {key for key in source_cfg.keys() if _LOCALE_KEY_RE.match(key)}
        result[source_name] = locales
    return result
