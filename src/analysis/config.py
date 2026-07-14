"""Phase 4 分析层共用配置加载：config/analysis.yaml（非敏感参数）+ .env（LLM 凭证）。

不引入 python-dotenv 依赖（项目目前只有 PyYAML/certifi 两个运行时依赖，见
requirements.txt）：.env 格式本身极简单（KEY=VALUE，# 开头注释），手写十几行解析
足够，没必要为此新增依赖。真实环境变量（如部署时已经 export 过）优先于 .env
文件内容，跟大多数 dotenv 实现的默认行为一致。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ANALYSIS_YAML_PATH = PROJECT_ROOT / "config" / "analysis.yaml"
ENV_PATH = PROJECT_ROOT / ".env"


def load_analysis_config(path: Path | str = ANALYSIS_YAML_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def load_env(path: Path | str = ENV_PATH) -> dict[str, str]:
    """.env 文件内容 + 真实环境变量的合并结果，真实环境变量优先。"""
    file_values = _parse_env_file(Path(path))
    merged = dict(file_values)
    for key in file_values:
        if key in os.environ:
            merged[key] = os.environ[key]
    return merged


class LlmCredentials:
    def __init__(self, api_key: Optional[str], api_base: Optional[str], model: Optional[str]):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model

    def validate(self) -> None:
        missing = [name for name, val in (("LLM_API_KEY", self.api_key),
                                           ("LLM_API_BASE", self.api_base),
                                           ("LLM_MODEL", self.model)) if not val]
        if missing:
            raise RuntimeError(
                f"缺少 LLM 凭证环境变量：{', '.join(missing)}（见 config/.env.example）"
            )


def load_llm_credentials(env_path: Path | str = ENV_PATH) -> LlmCredentials:
    env = load_env(env_path)
    return LlmCredentials(
        api_key=env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY"),
        api_base=env.get("LLM_API_BASE") or os.environ.get("LLM_API_BASE"),
        model=env.get("LLM_MODEL") or os.environ.get("LLM_MODEL"),
    )
