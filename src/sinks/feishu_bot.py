"""飞书群机器人推送（看板截图版）：把每个 locale 的"推送视图"（`docs/index.html` 的
`renderPushView()`，URL 触发 `?view=push&locale=<X>`，不是六个顶层 tab 之一）当前
渲染的截图，推送到该 locale 在 `config/push_targets.yaml` 里配置的独立飞书群。

2026-07-20 起：看板从 locale-first（顶层 `.locale-tab`）改成 category-first（顶层
Overview/Campaign/Product/Listing/Markets/Search 六个 tab）之后，不再有能直接点出
"这个 locale 的一整页内容"的顶层入口，所以新增了这个专门给推送用的紧凑视图（也顺带
解决了旧版整页截图从未验证过的"图片在飞书里是不是太长"的问题），见
`src/dashboard/screenshot.py::capture_push_views()`。

跟 CLAUDE.md 原先规划的「Phase 6 推送规则引擎」（逐条公告按 push_rules.yaml 匹配、
推送文字消息）是两条不同的路径——本模块是"每日一张图"的看板快照推送，不逐条判断
单篇公告要不要推，也不touch `announcements.push_status`（那一列的语义是"这条公告
有没有被单独推送过"，跟"今天有没有把这个 locale 的推送视图截图发过群"是两回事）。
Phase 6 的规则引擎如果以后要做，是另一条独立路径，不依赖本模块。

EN-Asia 之外的其余四个真实竞品 locale 都会推送；「Markets」「Search」两个 tab
本身不对应任何单一 locale，不在推送范围内——业务决定，见调用方
`push_dashboard_screenshots` 的 `PUSH_LOCALES` 常量（固定 `EN/FR/VN/ID/EN-Asia`）。

**2026-07-15 架构变更：从"自定义机器人 webhook"改成"应用机器人 im/v1/messages"**，
不再是最初设计（见 git 历史）。原因：自定义机器人 webhook 虽然协议上支持
`msg_type=image`，但发图片前必须先用应用凭证把二进制传到 `im/v1/images` 换
`image_key`——这一步本身就需要应用在飞书开发者后台开通"机器人"能力，跟维不维护
webhook 无关。既然应用侧的机器人能力已经是硬依赖，直接换成应用机器人主动发消息
（`POST im/v1/messages?receive_id_type=chat_id`）反而更简单：不需要为每个 locale
单独申请/存一个 webhook 密钥，只需要机器人被邀请进对应的群（一次性操作），配置里
维护"群名"就行，`chat_id` 通过 `list_bot_chats()`（`GET im/v1/chats`，机器人自己
所在的群列表）按名字动态解析，比手工去飞书后台复制 `chat_id` 更不容易出错（改群名
不用改配置，只要 `chat_name` 还对得上）。upload_image() 上传图片这一步完全不变
（依然是应用凭证 FEISHU_APP_ID/SECRET），只是"图片上传完之后怎么发出去"这一步从
webhook 换成了应用机器人消息接口。除了"机器人"能力，这条路径还需要应用具备
`im:chat`（或 `im:chat:readonly`，列群列表用）和 `im:message`（发消息用）这两类
scope，在飞书开发者后台"权限管理"里添加。
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

from src.collectors.http import HttpError, fetch
from src.dashboard.screenshot import capture_push_views
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
    """读 config/push_targets.yaml，返回 {locale: {chat_name, name}}。`chat_name` 是
    群在飞书里显示的名字（机器人已加入该群时，`list_bot_chats()` 才能按名字解析出
    `chat_id`）；没配置 `chat_name` 的 locale，调用方据此判断"该不该跳过"，不是报错
    （不是每个 locale 从第一天起就一定有群，如实反映配置状态）。不再需要 `.env` 里的
    `${WEBHOOK_*}` 占位符替换——群名是配置本身，不是需要保密的密钥。
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
    调用飞书任何接口（不上传图片、不查群列表、不发消息），只打印"如果真推会发生
    什么"——跟 `feishu_bitable.py --dry-run` 的语义一致，安全默认值，避免误发到
    真实群。dry_run 模式下不解析 `chat_id`（那需要调 `im/v1/chats`），只按配置里的
    `chat_name` 打印目标群名，不保证这个群名真的能在机器人已加入的群列表里查到。
    """
    env = load_env()
    report = PushReport()
    batch_date = batch_date or time.strftime("%Y-%m-%d", time.gmtime())

    screenshots = capture_push_views(dashboard_url, PUSH_LOCALES, screenshot_dir)

    targets = load_push_targets(PUSH_TARGETS_PATH)
    credentials = load_bot_credentials(env) if not dry_run else None
    if credentials is not None:
        credentials.validate()

    chats_by_name: dict[str, str] = {}
    conn: Optional[sqlite3.Connection] = None
    if not dry_run:
        chats_by_name = list_bot_chats(credentials)
        conn = connect(str(db_path))

    try:
        for locale in PUSH_LOCALES:
            target = targets.get(locale, {})
            chat_name = target.get("chat_name") or ""
            record_id = f"{locale}_{batch_date}"

            if locale not in screenshots:
                msg = f"{locale}: 截图失败，跳过推送"
                logger.warning(msg)
                report.skipped += 1
                report.details.append(msg)
                if conn is not None:
                    _log_sync(conn, f"bot_{locale}", record_id, "skip", "success", "screenshot_failed")
                continue

            if not chat_name:
                msg = f"{locale}: 未配置群名（config/push_targets.yaml 的 chat_name），跳过"
                logger.warning(msg)
                report.skipped += 1
                report.details.append(msg)
                if conn is not None:
                    _log_sync(conn, f"bot_{locale}", record_id, "skip", "success", "no_chat_name_configured")
                continue

            if dry_run:
                msg = f"{locale}: [dry-run] 会上传 {screenshots[locale]} 并通过应用机器人发到群「{chat_name}」"
                logger.info(msg)
                report.details.append(msg)
                continue

            chat_id = chats_by_name.get(chat_name)
            if not chat_id:
                msg = f"{locale}: 应用机器人未加入群「{chat_name}」（或群名不匹配），跳过"
                logger.warning(msg)
                report.skipped += 1
                report.details.append(msg)
                _log_sync(conn, f"bot_{locale}", record_id, "skip", "success", "chat_not_found")
                continue

            try:
                image_key = upload_image(screenshots[locale], credentials)
                send_image_via_bot(chat_id, image_key, credentials)
                report.pushed += 1
                msg = f"{locale}: 推送成功 -> {chat_name}"
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
