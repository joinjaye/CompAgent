"""飞书群日报：由应用机器人发送一张“Campaign/Product 摘要、Overview 图片、
业务入口”顺序的交互卡片。

业务日报只发送到 EN 群。卡片正文只推送当天已缓存的 Campaign、Product 两份 LLM
Summary（Overview 综合变化不在文本区重复展示），并提供三张多维表和公开看板入口。截图前由
`capture_overview()` 显式点击顶部“最新批次”，再由应用上传取得 `image_key` 并嵌入
同一张卡片。

该流程需要 FEISHU_APP_ID/SECRET、机器人能力以及 im:chat/im:message 权限，不使用
业务群 webhook。本模块不逐条判断公告是否推送，也不修改
`announcements.push_status`。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.analysis.daily_digest import peek_cached_digest
from src.collectors.http import HttpError, fetch
from src.dashboard.screenshot import capture_overview
from src.db.connection import DEFAULT_DB_PATH, connect
from src.db.operations import utcnow_iso

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
PUSH_TARGETS_PATH = PROJECT_ROOT / "config" / "push_targets.yaml"

API_BASE = "https://open.larksuite.com/open-apis"
MAX_RETRIES = 3
TOKEN_EXPIRE_SAFETY_S = 60

# 业务群推送只发送 Overview 全局截图到 EN 群。
PUSH_LOCALES = ["EN"]


class FeishuBotError(Exception):
    """推送流程失败（上传图片 / 查群列表 / 发消息最终失败），不是"这个 locale 没
    配置群""机器人还没被邀请进这个群"那种预期内的跳过。"""


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


def load_push_targets(path: Path | str = PUSH_TARGETS_PATH) -> dict[str, dict]:
    """读 config/push_targets.yaml，返回应用机器人目标群名和展示名称。

    `chat_name` 是
    群在飞书里显示的名字（机器人已加入该群时，`list_bot_chats()` 才能按名字解析出
    `chat_id`）；没配置 `chat_name` 的 locale，调用方据此判断"该不该跳过"，不是报错
    （不是每个 locale 从第一天起就一定有群，如实反映配置状态）。群名不是需要保密的
    密钥，直接保存在配置中。
    """
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
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
# 图片上传 + 应用机器人主动发消息
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


def list_bot_chats(credentials: BotCredentials) -> dict[str, str]:
    """列出应用机器人当前已加入的全部群聊，返回 {群名: chat_id}。分页遍历
    `page_token`（当前真实群数量很少，一页就能拿完，但不假设以后一直如此）。
    需要应用具备 `im:chat`/`im:chat:readonly` 类 scope，机器人本身也必须已经被
    邀请进对应的群——这两者缺一都会导致目标群"查不到"（`chat_id` 解析不出来），
    调用方按"群不存在"处理（skip），不是报错。同名群理论上可能撞车（飞书不保证
    群名唯一），后出现的会覆盖先出现的，本项目目前只有个位数测试群，未做去重
    保护，如果以后群数量变多、真的撞名，需要改成让 `chat_name` 换成更精确的
    `chat_id` 直接配置。"""
    token = get_tenant_access_token(credentials.app_id, credentials.app_secret)
    chats: dict[str, str] = {}
    page_token = ""
    while True:
        url = f"{API_BASE}/im/v1/chats?page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        try:
            raw = fetch(url, method="GET", headers={"Authorization": f"Bearer {token}"})
        except HttpError as e:
            raise FeishuBotError(f"获取群聊列表请求失败：{e}") from e
        resp = json.loads(raw)
        if resp.get("code") != 0:
            raise FeishuBotError(f"获取群聊列表失败：code={resp.get('code')} msg={resp.get('msg')}")
        data = resp.get("data", {})
        for item in data.get("items", []):
            chats[item["name"]] = item["chat_id"]
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return chats


def send_image_via_bot(chat_id: str, image_key: str, credentials: BotCredentials, *, max_retries: int = MAX_RETRIES) -> None:
    """用应用机器人身份把图片消息主动发到指定群（`POST im/v1/messages
    ?receive_id_type=chat_id`），取代此前的自定义机器人 webhook 路径（见模块顶部
    2026-07-15 架构变更说明）。跟 `upload_image()` 同样的 token 失效重试逻辑——
    这里也会因为 token 过期而收到业务错误码，值得重试；4xx 传输层错误不重试。"""
    body = json.dumps(
        {"receive_id": chat_id, "msg_type": "image", "content": json.dumps({"image_key": image_key})}
    ).encode("utf-8")

    last_error: Optional[str] = None
    force_refresh = False
    for attempt in range(max_retries):
        token = get_tenant_access_token(credentials.app_id, credentials.app_secret, force_refresh=force_refresh)
        try:
            raw = fetch(
                f"{API_BASE}/im/v1/messages?receive_id_type=chat_id",
                method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                body=body,
            )
        except HttpError as e:
            raise FeishuBotError(f"发送消息请求失败：{e}") from e
        resp = json.loads(raw)
        if resp.get("code") == 0:
            return
        if resp.get("code") in (99991661, 99991663, 99991664):
            force_refresh = True
            last_error = f"token 失效，code={resp.get('code')}"
            continue
        raise FeishuBotError(f"发送消息失败：code={resp.get('code')} msg={resp.get('msg')}")

    raise FeishuBotError(f"发送消息重试 {max_retries} 次后仍失败：{last_error}")


def send_card_via_bot(
    chat_id: str, card: dict, credentials: BotCredentials, *, max_retries: int = MAX_RETRIES
) -> None:
    """使用应用机器人向群聊发送一张 interactive 卡片。"""
    body = json.dumps(
        {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        ensure_ascii=False,
    ).encode("utf-8")
    last_error: Optional[str] = None
    force_refresh = False
    for _ in range(max_retries):
        token = get_tenant_access_token(credentials.app_id, credentials.app_secret, force_refresh=force_refresh)
        try:
            raw = fetch(
                f"{API_BASE}/im/v1/messages?receive_id_type=chat_id",
                method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                body=body,
            )
        except HttpError as e:
            raise FeishuBotError(f"发送日报卡片请求失败：{e}") from e
        resp = json.loads(raw)
        if resp.get("code") == 0:
            return
        if resp.get("code") in (99991661, 99991663, 99991664):
            force_refresh = True
            last_error = f"token 失效，code={resp.get('code')}"
            continue
        raise FeishuBotError(f"发送日报卡片失败：code={resp.get('code')} msg={resp.get('msg')}")
    raise FeishuBotError(f"发送日报卡片重试 {max_retries} 次后仍失败：{last_error}")


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
    cards_pushed: int = 0
    images_pushed: int = 0
    details: list[str] = field(default_factory=list)


def _table_url(base_url: str, app_token: Optional[str], table_id: Optional[str]) -> str:
    if not app_token or not table_id:
        return ""
    return f"{base_url.rstrip('/')}/{app_token}?table={table_id}"


def build_daily_card(
    batch_date: str,
    digest,
    env: dict[str, str],
    dashboard_url: str,
    *,
    overview_image_key: Optional[str] = None,
) -> dict:
    """构建移动端优先的飞书交互卡片。只读取已经生成并缓存的 Campaign、Product
    Summary，推送阶段不重新调用 LLM。按钮统一置于 Overview 图片之后。"""
    base_url = env.get("FEISHU_BITABLE_BASE_URL", "https://zoomex.larksuite.com/base")
    links = {
        "Campaign 表": env.get("FEISHU_CAMPAIGN_TABLE_URL") or _table_url(
            base_url, env.get("FEISHU_CAMPAIGN_APP_TOKEN"), env.get("FEISHU_CAMPAIGN_TABLE_ID")
        ),
        "Product 表": env.get("FEISHU_PRODUCT_TABLE_URL") or _table_url(
            base_url, env.get("FEISHU_PRODUCT_APP_TOKEN"), env.get("FEISHU_PRODUCT_TABLE_ID")
        ),
        "Listing & Delisting 表": env.get("FEISHU_LISTING_TABLE_URL") or _table_url(
            base_url, env.get("FEISHU_LISTING_APP_TOKEN"), env.get("FEISHU_LISTING_TABLE_ID")
        ),
        "打开可视化看板": env.get("DASHBOARD_PUBLIC_URL") or dashboard_url,
    }
    summaries = [
        ("CAMPAIGN", "活动策略观察", "blue", getattr(digest, "campaign_summary", None)),
        ("PRODUCT", "产品能力观察", "green", getattr(digest, "product_summary", None)),
    ]
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**最新批次 · {batch_date}**  |  UTC\n以下为本批次 Campaign 与 Product 的 AI 汇总观察。",
            },
        }
    ]
    for kicker, title, color, summary in summaries:
        elements.extend([
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [{
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [{
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"<font color='{color}'>**{kicker}**</font>  |  **{title}**\n"
                                f"{summary or 'No Significant Changes Today'}"
                            ),
                        },
                    }],
                }],
            },
        ])
    footer_buttons = [
        {"tag": "button", "text": {"tag": "plain_text", "content": label}, "url": url, "type": button_type}
        for label, url, button_type in (
            ("Campaign", links["Campaign 表"], "default"),
            ("Product", links["Product 表"], "default"),
            ("Listing", links["Listing & Delisting 表"], "default"),
            ("Dashboard", links["打开可视化看板"], "primary"),
        )
        if url
    ]
    if overview_image_key:
        elements.extend([
            {"tag": "hr"},
            {
                "tag": "img",
                "img_key": overview_image_key,
                "alt": {"tag": "plain_text", "content": f"{batch_date} 最新批次 Overview 看板截图"},
                "mode": "fit_horizontal",
                "preview": True,
            },
        ])
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "Overview 截图锁定“最新批次”；详情与原文请通过下方入口查看。"}],
    })
    if footer_buttons:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "layout": "flow", "actions": footer_buttons})
    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "竞品情报日报"},
            "subtitle": {"tag": "plain_text", "content": "Competitive Intelligence Daily Brief"},
        },
        "elements": elements,
    }


def send_card_via_webhook(webhook_url: str, card: dict) -> None:
    """兼容保留的 webhook 发送器；业务日报主流程不调用。"""
    try:
        raw = fetch(
            webhook_url,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False).encode("utf-8"),
        )
    except HttpError as e:
        raise FeishuBotError(f"发送摘要卡片请求失败：{e}") from e
    resp = json.loads(raw)
    if resp.get("code") == 0 or resp.get("StatusCode") == 0:
        return
    raise FeishuBotError(f"发送摘要卡片失败：code={resp.get('code', resp.get('StatusCode'))} msg={resp.get('msg', resp.get('StatusMessage'))}")


def push_dashboard_screenshots(
    dashboard_url: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    batch_date: Optional[str] = None,
    screenshot_dir: str | Path = "/tmp/dashboard_screenshots",
    dry_run: bool = True,
) -> PushReport:
    """应用机器人合并卡片推送编排。dry_run=True（默认）：正常截图（本地操作，无副作用），
    但不调用飞书任何接口，只打印“如果真推会发生什么”。
    """
    env = load_env()
    report = PushReport()
    batch_date = batch_date or time.strftime("%Y-%m-%d", time.gmtime())

    digest = None
    digest_conn = connect(str(db_path))
    try:
        digest = peek_cached_digest(digest_conn, "ALL", batch_date)
    finally:
        digest_conn.close()

    try:
        screenshots = {"EN": capture_overview(dashboard_url, screenshot_dir)}
    except Exception as e:
        logger.warning("Overview 截图失败，跳过推送：%s", e)
        screenshots = {}

    targets = load_push_targets(PUSH_TARGETS_PATH)
    credentials = load_bot_credentials(env) if not dry_run else None
    if credentials is not None:
        credentials.validate()

    conn: Optional[sqlite3.Connection] = None
    if not dry_run:
        conn = connect(str(db_path))

    try:
        chats = list_bot_chats(credentials) if not dry_run else {}
        for locale in PUSH_LOCALES:
            target = targets.get(locale, {})
            chat_name = target.get("chat_name") or ""
            record_id = f"{locale}_{batch_date}"

            if dry_run:
                build_daily_card(
                    batch_date, digest, env, dashboard_url,
                    overview_image_key="dry_run_overview_image_key" if locale in screenshots else None,
                )
                if not chat_name:
                    report.skipped += 1
                msg = (
                    f"{locale}: [dry-run] 会上传 {screenshots.get(locale, '截图未生成')}，"
                    f"并由应用机器人向 {chat_name or '未配置群'} 发送一张包含 Campaign/Product Summary、Overview 图片和四个入口的卡片"
                )
                logger.info(msg)
                report.details.append(msg)
                continue

            if not chat_name:
                report.skipped += 1
                msg = f"{locale}: 未配置 chat_name，跳过整张日报卡片"
                logger.warning(msg)
                report.details.append(msg)
                _log_sync(conn, f"bot_card_{locale}", record_id, "skip", "success", "no_chat_name_configured")
                continue
            chat_id = chats.get(chat_name)
            if not chat_id:
                report.skipped += 1
                msg = f"{locale}: 应用机器人未加入群或群名不匹配：{chat_name}"
                logger.warning(msg)
                report.details.append(msg)
                _log_sync(conn, f"bot_card_{locale}", record_id, "skip", "success", "chat_not_found")
                continue

            image_key = None
            image_error = None
            if locale in screenshots:
                try:
                    image_key = upload_image(screenshots[locale], credentials)
                    report.images_pushed += 1
                except FeishuBotError as e:
                    image_error = str(e)
                    logger.error("%s: Overview 图片上传失败，将降级发送纯文本卡片 - %s", locale, e)
            else:
                image_error = "screenshot_failed"

            try:
                card = build_daily_card(
                    batch_date, digest, env, dashboard_url, overview_image_key=image_key,
                )
                send_card_via_bot(chat_id, card, credentials)
                report.cards_pushed += 1
                report.pushed += 1
                msg = (
                    f"{locale}: 文本 + Overview 图片合并卡片推送成功 -> {chat_name}"
                    if image_key else
                    f"{locale}: 纯文本降级卡片推送成功 -> {chat_name}"
                )
                report.details.append(msg)
                logger.info(msg)
                _log_sync(conn, f"bot_card_{locale}", record_id, "create", "success", image_error)
            except FeishuBotError as e:
                report.failed += 1
                msg = f"{locale}: 合并日报卡片推送失败 - {e}"
                report.details.append(msg)
                logger.error(msg)
                _log_sync(conn, f"bot_card_{locale}", record_id, "create", "failed", str(e))
    finally:
        if conn is not None:
            conn.commit()
            conn.close()

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="由应用机器人把 Campaign/Product Summary 与 Overview 截图合并推送到 EN 飞书群")
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
    print(
        f"pushed={report.pushed} cards_pushed={report.cards_pushed} "
        f"images_pushed={report.images_pushed} skipped={report.skipped} failed={report.failed}"
    )
    for line in report.details:
        print(" -", line)


if __name__ == "__main__":
    main()
