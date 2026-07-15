"""飞书群机器人推送（看板截图版）：把每个区域 tab（EN/FR/VN/ID/EN-Asia）当前渲染的
截图，推送到该 locale 在 `config/push_targets.yaml` 里配置的独立飞书群。

跟 CLAUDE.md 原先规划的「Phase 6 推送规则引擎」（逐条公告按 push_rules.yaml 匹配、
推送文字消息）是两条不同的路径——本模块是"每日一张图"的看板快照推送，不逐条判断
单篇公告要不要推，也不touch `announcements.push_status`（那一列的语义是"这条公告
有没有被单独推送过"，跟"今天有没有把整个 locale tab 的截图发过群"是两回事）。
Phase 6 的规则引擎如果以后要做，是另一条独立路径，不依赖本模块。

「全量」「全局视角」两个 tab 不推送——业务决定，见调用方 `push_dashboard_screenshots`
的 `locales` 参数（固定传 `EN/FR/VN/ID/EN-Asia`，不包含 `archive`/`global`）。

飞书自定义机器人 webhook 本身只能发消息，不能直接带图片二进制——发图片前必须先用
应用凭证（FEISHU_APP_ID/SECRET，同 `feishu_bitable.py` 用的那对，需要 im:resource
上传权限，跟 Bitable 读写权限是否已经在飞书开发者后台开通是两回事，本模块没有验证过
这个权限是否已经打开）把图片上传到 `im/v1/images` 换一个 image_key，再拿这个
image_key 发到 webhook。这是飞书协议本身的限制，不是本模块的设计选择。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.collectors.http import HttpError, fetch
from src.dashboard.screenshot import capture_locale_tabs
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
PUSH_TARGETS_PATH = PROJECT_ROOT / "config" / "push_targets.yaml"

API_BASE = "https://open.larksuite.com/open-apis"
MAX_RETRIES = 3
TOKEN_EXPIRE_SAFETY_S = 60

# 只推区域 tab，「全量」「全局视角」不在推送范围内（业务决定，见模块顶部说明）
PUSH_LOCALES = ["EN", "FR", "VN", "ID", "EN-Asia"]


class FeishuBotError(Exception):
    """推送流程失败（上传图片 / 调用 webhook 最终失败），不是"这个 locale 没配置群"
    那种预期内的跳过。"""


# ============================================================
# 凭证 + push_targets.yaml 加载
# ============================================================


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
    file_values = _parse_env_file(Path(path))
    merged = dict(file_values)
    for key in file_values:
        if key in os.environ:
            merged[key] = os.environ[key]
    return merged


_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def load_push_targets(env: dict[str, str], path: Path | str = PUSH_TARGETS_PATH) -> dict[str, dict]:
    """读 config/push_targets.yaml，把 `${WEBHOOK_EN}` 这类占位符替换成 .env 里的真实值。
    没配置真实 webhook 的 locale，值就是空字符串——调用方据此判断"这个 locale 该不该
    跳过"，不是报错（不是每个 locale 从第一天起就一定有群，如实反映配置状态）。
    """
    raw = Path(path).read_text(encoding="utf-8")

    def _sub(match: re.Match) -> str:
        return env.get(match.group(1), "")

    substituted = _ENV_VAR_RE.sub(_sub, raw)
    data = yaml.safe_load(substituted)
    return data.get("targets", {})


@dataclass
class BotCredentials:
    app_id: Optional[str]
    app_secret: Optional[str]

    def validate(self) -> None:
        missing = [n for n, v in (("FEISHU_APP_ID", self.app_id), ("FEISHU_APP_SECRET", self.app_secret)) if not v]
        if missing:
            raise RuntimeError(f"缺少飞书凭证环境变量：{', '.join(missing)}（见 config/.env.example）")


def load_bot_credentials(env: dict[str, str]) -> BotCredentials:
    return BotCredentials(app_id=env.get("FEISHU_APP_ID"), app_secret=env.get("FEISHU_APP_SECRET"))


# ============================================================
# tenant_access_token（跟 feishu_bitable.py 同样的获取/缓存逻辑，未做跨模块共享——
# 两个模块各自独立、体量不大，见本文件顶部关于 Phase 6 路径独立性的说明）
# ============================================================

_token_cache: dict[str, tuple[str, float]] = {}


def get_tenant_access_token(app_id: str, app_secret: str, *, force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh:
        cached = _token_cache.get(app_id)
        if cached and cached[1] > now + TOKEN_EXPIRE_SAFETY_S:
            return cached[0]

    raw = fetch(
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
    )
    resp = json.loads(raw)
    if resp.get("code") != 0:
        raise FeishuBotError(f"获取 tenant_access_token 失败：code={resp.get('code')} msg={resp.get('msg')}")
    token = resp["tenant_access_token"]
    expire_s = resp.get("expire", 7200)
    _token_cache[app_id] = (token, now + expire_s)
    return token


# ============================================================
# 图片上传 + webhook 推送
# ============================================================


def _build_multipart_body(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
    boundary = f"----FeishuBotBoundary{uuid.uuid4().hex}"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'.encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + image_bytes
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_image(image_path: Path, credentials: BotCredentials, *, max_retries: int = MAX_RETRIES) -> str:
    """上传图片换 image_key。飞书 image_key 有效期较短且一次性绑定消息使用，不做
    跨批次缓存——每次推送都是当天最新截图，缓存旧 image_key 也没有意义。"""
    image_bytes = Path(image_path).read_bytes()
    body, content_type = _build_multipart_body(image_bytes, Path(image_path).name)

    last_error: Optional[str] = None
    force_refresh = False
    for attempt in range(max_retries):
        token = get_tenant_access_token(credentials.app_id, credentials.app_secret, force_refresh=force_refresh)
        try:
            raw = fetch(
                f"{API_BASE}/im/v1/images",
                method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
                body=body,
            )
        except HttpError as e:
            # fetch() 内部已经对 5xx/网络错误做过指数退避重试、对 4xx 立即抛出——
            # 这两种情况在这一层再重试都没有意义（同样的请求只会得到同样的结果），
            # 不像下面 token 失效那样重试真的可能改变结果
            raise FeishuBotError(f"上传图片请求失败：{e}") from e
        resp = json.loads(raw)
        if resp.get("code") == 0:
            return resp["data"]["image_key"]
        if resp.get("code") in (99991661, 99991663, 99991664):
            force_refresh = True
            last_error = f"token 失效，code={resp.get('code')}"
            continue
        raise FeishuBotError(f"上传图片失败：code={resp.get('code')} msg={resp.get('msg')}")

    raise FeishuBotError(f"上传图片重试 {max_retries} 次后仍失败：{last_error}")


def push_image_to_webhook(webhook_url: str, image_key: str) -> None:
    """webhook 请求本身不需要外层重试——fetch() 内部已经处理了 5xx/网络错误的指数
    退避，4xx（比如 webhook URL 失效）重试没有意义；webhook 也没有 token 失效这种
    "重试可能改变结果"的场景（跟 upload_image 不一样，不需要那层循环）。"""
    body = json.dumps({"msg_type": "image", "content": {"image_key": image_key}}).encode("utf-8")
    try:
        raw = fetch(webhook_url, method="POST", headers={"Content-Type": "application/json"}, body=body)
    except HttpError as e:
        raise FeishuBotError(f"推送到 webhook 失败：{e}") from e
    resp = json.loads(raw)
    # 自定义机器人 webhook 的成功响应是 {"code":0,...} 或 StatusCode==0，
    # 也可能只回 {"StatusCode":0}（旧版协议），两种都当成功处理
    if resp.get("code", resp.get("StatusCode")) == 0:
        return
    raise FeishuBotError(f"推送到 webhook 失败：{resp}")


def _log_sync(conn: sqlite3.Connection, target: str, record_id: str, action: str,
              status: str, error: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO sync_log (target, record_id, action, status, error, synced_at) VALUES (?, ?, ?, ?, ?, ?)",
        (target, record_id, action, status, error, utcnow_iso()),
    )


@dataclass
class PushReport:
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


def push_dashboard_screenshots(
    dashboard_url: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    batch_date: Optional[str] = None,
    screenshot_dir: str | Path = "/tmp/dashboard_screenshots",
    dry_run: bool = True,
) -> PushReport:
    """截图 + 推送编排。dry_run=True（默认）：正常截图（本地操作，无副作用），但不
    调用飞书任何接口（不上传图片、不发 webhook），只打印"如果真推会发生什么"——跟
    `feishu_bitable.py --dry-run` 的语义一致，安全默认值，避免误发到真实群。
    """
    env = load_env()
    report = PushReport()
    batch_date = batch_date or time.strftime("%Y-%m-%d", time.gmtime())

    screenshots = capture_locale_tabs(dashboard_url, PUSH_LOCALES, screenshot_dir)

    targets = load_push_targets(env)
    credentials = load_bot_credentials(env) if not dry_run else None
    if credentials is not None:
        credentials.validate()

    conn: Optional[sqlite3.Connection] = None
    if not dry_run:
        conn = connect(str(db_path))

    try:
        for locale in PUSH_LOCALES:
            target = targets.get(locale, {})
            webhook_url = target.get("webhook") or ""
            record_id = f"{locale}_{batch_date}"

            if locale not in screenshots:
                msg = f"{locale}: 截图失败，跳过推送"
                logger.warning(msg)
                report.skipped += 1
                report.details.append(msg)
                if conn is not None:
                    _log_sync(conn, f"bot_{locale}", record_id, "skip", "success", "screenshot_failed")
                continue

            if not webhook_url:
                msg = f"{locale}: 未配置 webhook（config/push_targets.yaml + .env 的 WEBHOOK_* 变量），跳过"
                logger.warning(msg)
                report.skipped += 1
                report.details.append(msg)
                if conn is not None:
                    _log_sync(conn, f"bot_{locale}", record_id, "skip", "success", "no_webhook_configured")
                continue

            if dry_run:
                msg = f"{locale}: [dry-run] 会上传 {screenshots[locale]} 并推送到 {target.get('name', locale)} 群"
                logger.info(msg)
                report.details.append(msg)
                continue

            try:
                image_key = upload_image(screenshots[locale], credentials)
                push_image_to_webhook(webhook_url, image_key)
                report.pushed += 1
                msg = f"{locale}: 推送成功 -> {target.get('name', locale)}"
                report.details.append(msg)
                logger.info(msg)
                _log_sync(conn, f"bot_{locale}", record_id, "create", "success")
            except FeishuBotError as e:
                report.failed += 1
                msg = f"{locale}: 推送失败 - {e}"
                report.details.append(msg)
                logger.error(msg)
                _log_sync(conn, f"bot_{locale}", record_id, "create", "failed", str(e))
    finally:
        if conn is not None:
            conn.commit()
            conn.close()

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="把看板各区域 tab 的截图推送到对应飞书群")
    parser.add_argument("--dashboard-url", required=True, help="docs/index.html 的可访问 URL（本地 http.server 或线上 GitHub Pages）")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--batch-date", default=None, help="默认 UTC 今天，用于 sync_log 的 record_id")
    parser.add_argument("--screenshot-dir", default="/tmp/dashboard_screenshots")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", dest="dry_run", action="store_false", help="真正调用飞书 API 推送（默认是 dry-run）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    report = push_dashboard_screenshots(
        args.dashboard_url, db_path=args.db_path, batch_date=args.batch_date,
        screenshot_dir=args.screenshot_dir, dry_run=args.dry_run,
    )
    print(f"pushed={report.pushed} skipped={report.skipped} failed={report.failed}")
    for line in report.details:
        print(" -", line)


if __name__ == "__main__":
    main()
