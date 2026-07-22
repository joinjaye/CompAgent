"""src/sinks/feishu_bot.py 单测：全部离线（mock fetch/fetch_json + mock
capture_push_views，不启动真实 Playwright/浏览器，不发真实网络请求）。

真实网络验收记录（2026-07-15，用真实 FEISHU_APP_ID/SECRET）：
- 截图（src/dashboard/screenshot.py）对本地 http.server 跑通，5 个 locale 全部
  产出非空 PNG。
- 2026-07-15 架构变更：从"自定义机器人 webhook"改成"应用机器人 im/v1/messages"
  （见 src/sinks/feishu_bot.py 顶部说明）。真实验收：`upload_image()` 已确认可用
  （应用的"机器人"能力已开通）；`list_bot_chats()` 真实查到机器人已加入的群
  （CompAgent_EN/FR/VN/ID 等），按群名解析出真实 chat_id；`send_image_via_bot()`
  对 CompAgent_EN/FR/VN/ID 四个真实群推送成功，EN-Asia 因为没有匹配的
  chat_name（"CompAgent_EN-Asia"）在机器人已加入的群列表里查不到，被正确跳过
  （不是失败）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db.connection import SCHEMA_PATH, connect
from src.sinks import feishu_bot as bot


@pytest.fixture(autouse=True)
def _clear_token_cache():
    bot._token_cache.clear()
    yield
    bot._token_cache.clear()


# ============================================================
# multipart body + push_targets 加载
# ============================================================


def test_build_multipart_body_contains_boundary_and_image_bytes():
    body, content_type = bot._build_multipart_body(b"\x89PNG-fake-bytes", "test.png")
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    assert boundary.encode() in body
    assert b"\x89PNG-fake-bytes" in body
    assert b'name="image_type"' in body
    assert b'name="image"; filename="test.png"' in body


def test_load_push_targets_reads_chat_name_directly(tmp_path):
    path = tmp_path / "push_targets.yaml"
    path.write_text(
        "targets:\n"
        "  EN:\n"
        '    chat_name: "CompAgent_EN"\n'
        '    name: "竞品情报-EN"\n'
        "  FR:\n"
        '    name: "竞品情报-FR"\n',  # FR 没配置 chat_name
        encoding="utf-8",
    )
    targets = bot.load_push_targets(path)
    assert targets["EN"]["chat_name"] == "CompAgent_EN"
    assert targets["FR"].get("chat_name") is None
    assert set(targets.keys()) == {"EN", "FR"}


# ============================================================
# token 获取/缓存
# ============================================================


def test_get_tenant_access_token_caches_and_reuses(monkeypatch):
    calls = []

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        calls.append(url)
        return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    t1 = bot.get_tenant_access_token("app-cache-test", "secret1")
    t2 = bot.get_tenant_access_token("app-cache-test", "secret1")
    assert t1 == t2 == "tok-1"
    assert len(calls) == 1


# ============================================================
# upload_image
# ============================================================


def test_upload_image_success(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"fake-png-bytes")

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        assert url.endswith("/im/v1/images")
        assert headers["Content-Type"].startswith("multipart/form-data")
        return json.dumps({"code": 0, "data": {"image_key": "img_v2_fake"}})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-upload-1", app_secret="s")
    key = bot.upload_image(img, creds)
    assert key == "img_v2_fake"


def test_upload_image_refreshes_token_on_invalid_code(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"fake-png-bytes")
    token_calls = {"n": 0}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            token_calls["n"] += 1
            return json.dumps({"code": 0, "tenant_access_token": f"tok-{token_calls['n']}", "expire": 7200})
        if headers["Authorization"] == "Bearer tok-1":
            return json.dumps({"code": 99991663, "msg": "token invalid"})
        return json.dumps({"code": 0, "data": {"image_key": "img_v2_after_refresh"}})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-upload-2", app_secret="s")
    key = bot.upload_image(img, creds)
    assert key == "img_v2_after_refresh"
    assert token_calls["n"] == 2


def test_upload_image_business_error_raises(tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"fake-png-bytes")

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        return json.dumps({"code": 234007, "msg": "App does not enable bot feature."})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-upload-3", app_secret="s")
    with pytest.raises(bot.FeishuBotError, match="234007"):
        bot.upload_image(img, creds)


def test_upload_image_http_error_does_not_retry(tmp_path, monkeypatch):
    """4xx（HttpError）在这一层重试没有意义——fetch() 内部已经决定不重试，验证
    upload_image 不会画蛇添足再包一层重试（只应该请求一次 token + 一次上传）。"""
    img = tmp_path / "shot.png"
    img.write_bytes(b"fake-png-bytes")
    call_count = {"n": 0}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        call_count["n"] += 1
        raise bot.HttpError("POST ... -> HTTP 400（客户端错误，不重试）")

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-upload-4", app_secret="s")
    with pytest.raises(bot.FeishuBotError):
        bot.upload_image(img, creds)
    assert call_count["n"] == 1


# ============================================================
# list_bot_chats
# ============================================================


def test_list_bot_chats_returns_name_to_chat_id_mapping(monkeypatch):
    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        assert url.endswith("/im/v1/chats?page_size=100")
        return json.dumps({
            "code": 0,
            "data": {
                "has_more": False,
                "page_token": "",
                "items": [
                    {"chat_id": "oc_en", "name": "CompAgent_EN"},
                    {"chat_id": "oc_fr", "name": "CompAgent_FR"},
                ],
            },
        })

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-chats-1", app_secret="s")
    chats = bot.list_bot_chats(creds)
    assert chats == {"CompAgent_EN": "oc_en", "CompAgent_FR": "oc_fr"}


def test_list_bot_chats_paginates_until_has_more_false(monkeypatch):
    pages = {"n": 0}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        pages["n"] += 1
        if "page_token=cursor-2" in url:
            return json.dumps({
                "code": 0,
                "data": {"has_more": False, "page_token": "", "items": [{"chat_id": "oc_2", "name": "Group2"}]},
            })
        return json.dumps({
            "code": 0,
            "data": {"has_more": True, "page_token": "cursor-2", "items": [{"chat_id": "oc_1", "name": "Group1"}]},
        })

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-chats-2", app_secret="s")
    chats = bot.list_bot_chats(creds)
    assert chats == {"Group1": "oc_1", "Group2": "oc_2"}
    assert pages["n"] == 2


def test_list_bot_chats_business_error_raises(monkeypatch):
    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        return json.dumps({"code": 99991672, "msg": "Access denied. scope required"})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-chats-3", app_secret="s")
    with pytest.raises(bot.FeishuBotError, match="99991672"):
        bot.list_bot_chats(creds)


# ============================================================
# send_image_via_bot
# ============================================================


def test_send_image_via_bot_success(monkeypatch):
    captured = {}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        captured["url"] = url
        captured["body"] = json.loads(body.decode())
        return json.dumps({"code": 0})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-send-1", app_secret="s")
    bot.send_image_via_bot("oc_target", "img_1", creds)  # 不抛异常即通过
    assert captured["url"].endswith("/im/v1/messages?receive_id_type=chat_id")
    assert captured["body"]["receive_id"] == "oc_target"
    assert captured["body"]["msg_type"] == "image"
    assert json.loads(captured["body"]["content"]) == {"image_key": "img_1"}


def test_send_image_via_bot_refreshes_token_on_invalid_code(monkeypatch):
    token_calls = {"n": 0}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            token_calls["n"] += 1
            return json.dumps({"code": 0, "tenant_access_token": f"tok-{token_calls['n']}", "expire": 7200})
        if headers["Authorization"] == "Bearer tok-1":
            return json.dumps({"code": 99991664, "msg": "token invalid"})
        return json.dumps({"code": 0})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-send-2", app_secret="s")
    bot.send_image_via_bot("oc_target", "img_1", creds)
    assert token_calls["n"] == 2


def test_send_image_via_bot_failure_raises(monkeypatch):
    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        return json.dumps({"code": 19021, "msg": "invalid image_key"})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    creds = bot.BotCredentials(app_id="app-send-3", app_secret="s")
    with pytest.raises(bot.FeishuBotError, match="19021"):
        bot.send_image_via_bot("oc_target", "img_1", creds)


# ============================================================
# 日报摘要卡片
# ============================================================


def test_build_daily_card_contains_three_summaries_and_four_links():
    digest = SimpleNamespace(
        daily_summary="综合总结两句话。整体变化清晰。",
        campaign_summary="活动总结两句话。奖励保持稳定。",
        product_summary="产品总结两句话。能力变化有限。",
    )
    env = {
        "FEISHU_BITABLE_BASE_URL": "https://tenant.larksuite.com/base",
        "FEISHU_CAMPAIGN_APP_TOKEN": "base_campaign", "FEISHU_CAMPAIGN_TABLE_ID": "tbl_campaign",
        "FEISHU_PRODUCT_APP_TOKEN": "base_product", "FEISHU_PRODUCT_TABLE_ID": "tbl_product",
        "FEISHU_LISTING_APP_TOKEN": "base_listing", "FEISHU_LISTING_TABLE_ID": "tbl_listing",
        "DASHBOARD_PUBLIC_URL": "https://example.com/dashboard/",
    }
    card = bot.build_daily_card(
        "2026-07-22", digest, env, "http://127.0.0.1:8765", overview_image_key="img_overview",
    )
    payload = json.dumps(card, ensure_ascii=False)
    assert "综合总结两句话" in payload
    assert "活动总结两句话" in payload
    assert "产品总结两句话" in payload
    assert "base_campaign?table=tbl_campaign" in payload
    assert "base_product?table=tbl_product" in payload
    assert "base_listing?table=tbl_listing" in payload
    assert "https://example.com/dashboard/" in payload
    assert "img_overview" in payload
    image_index = next(i for i, element in enumerate(card["elements"]) if element.get("tag") == "img")
    action_indexes = [i for i, element in enumerate(card["elements"]) if element.get("tag") == "action"]
    assert image_index > max(action_indexes)
    assert card["header"]["template"] == "blue"


def test_send_card_via_webhook_uses_interactive_payload(monkeypatch):
    captured = {}

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        captured["url"] = url
        captured["payload"] = json.loads(body.decode())
        return json.dumps({"code": 0, "msg": "success"})

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    bot.send_card_via_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/test", {"elements": []})
    assert captured["payload"]["msg_type"] == "interactive"
    assert captured["payload"]["card"] == {"elements": []}


# ============================================================
# push_dashboard_screenshots：编排层
# ============================================================


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = connect(str(path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return path


def _fake_screenshots(tmp_path, locales):
    paths = {}
    for locale in locales:
        p = tmp_path / f"{locale}.png"
        p.write_bytes(b"fake")
        paths[locale] = p
    return paths


def _fake_push_targets(tmp_path, mapping: dict[str, str]) -> Path:
    """写一份临时 push_targets.yaml，`mapping` 是 {locale: chat_name}（没配置的
    locale 不写 chat_name，跟真实"没配群"场景一致）。"""
    lines = ["targets:"]
    for locale in bot.PUSH_LOCALES:
        lines.append(f"  {locale}:")
        if locale in mapping:
            lines.append(f'    chat_name: "{mapping[locale]}"')
        lines.append(f'    name: "竞品情报-{locale}"')
    path = tmp_path / "push_targets.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_dry_run_captures_screenshots_but_calls_no_feishu_api(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(bot, "capture_overview", lambda url, out_dir, **kw: _fake_screenshots(tmp_path, ["EN"])["EN"])

    def _boom(*a, **k):
        raise AssertionError("dry_run 不应该调用任何飞书 API")

    monkeypatch.setattr(bot, "fetch", _boom)
    monkeypatch.setattr(bot, "PUSH_TARGETS_PATH", _fake_push_targets(tmp_path, {"EN": "CompAgent_EN"}))

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=True)
    assert report.pushed == 0
    assert report.failed == 0
    assert report.skipped == 0
    assert any("EN" in d and "会上传" in d for d in report.details)


def test_missing_chat_name_is_skipped_not_failed(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(bot, "capture_overview", lambda url, out_dir, **kw: _fake_screenshots(tmp_path, ["EN"])["EN"])
    monkeypatch.setattr(bot, "PUSH_TARGETS_PATH", _fake_push_targets(tmp_path, {}))

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=True)
    assert report.skipped == 1
    assert report.failed == 0


def test_real_run_pushes_one_merged_card_via_app_and_logs_sync_log(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(bot, "capture_overview", lambda url, out_dir, **kw: _fake_screenshots(tmp_path, ["EN"])["EN"])
    monkeypatch.setattr(bot, "load_env", lambda: {
        "FEISHU_APP_ID": "app-x", "FEISHU_APP_SECRET": "secret-x",
    })
    monkeypatch.setattr(bot, "PUSH_TARGETS_PATH", _fake_push_targets(tmp_path, {"EN": "CompAgent_EN"}))

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        if url.endswith("/im/v1/images"):
            return json.dumps({"code": 0, "data": {"image_key": "img_key_1"}})
        if "/im/v1/chats" in url:
            return json.dumps({
                "code": 0,
                "data": {"has_more": False, "page_token": "", "items": [{"chat_id": "oc_en", "name": "CompAgent_EN"}]},
            })
        assert url.endswith("/im/v1/messages?receive_id_type=chat_id")
        payload = json.loads(body.decode())
        assert payload["receive_id"] == "oc_en"
        assert payload["msg_type"] == "interactive"
        card = json.loads(payload["content"])
        image = next(element for element in card["elements"] if element.get("tag") == "img")
        assert image["img_key"] == "img_key_1"
        return json.dumps({"code": 0})

    monkeypatch.setattr(bot, "fetch", fake_fetch)

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, batch_date="2026-07-15", dry_run=False)
    assert report.pushed == 1
    assert report.cards_pushed == 1
    assert report.images_pushed == 1
    assert report.skipped == 0
    assert report.failed == 0

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT target, record_id, action, status FROM sync_log ORDER BY target").fetchall()
    conn.close()
    targets = {r[0]: (r[2], r[3]) for r in rows}
    assert targets["bot_card_EN"] == ("create", "success")
    assert rows[0][1].endswith("2026-07-15")  # record_id 带上了 batch_date


def test_chat_not_found_is_skipped_not_failed(tmp_path, monkeypatch, db_path):
    """配了 chat_name，但机器人没有加入这个群（或群名不匹配）——`list_bot_chats()`
    查不到对应 chat_id，应该 skip，不是 failed（跟 EN-Asia 目前的真实状态一致）。"""
    monkeypatch.setattr(bot, "capture_overview", lambda url, out_dir, **kw: _fake_screenshots(tmp_path, ["EN"])["EN"])
    monkeypatch.setattr(bot, "load_env", lambda: {
        "FEISHU_APP_ID": "app-x", "FEISHU_APP_SECRET": "secret-x",
    })
    monkeypatch.setattr(bot, "PUSH_TARGETS_PATH", _fake_push_targets(tmp_path, {"EN": "CompAgent_EN"}))

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        if "/im/v1/chats" in url:
            return json.dumps({
                "code": 0,
                "data": {"has_more": False, "page_token": "", "items": [{"chat_id": "oc_kr", "name": "CompAgent_KR"}]},
            })
        raise AssertionError("chat_id 解析不到，不应该走到上传图片这一步")

    monkeypatch.setattr(bot, "fetch", fake_fetch)

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=False)
    assert report.pushed == 0
    assert report.failed == 0
    assert report.skipped == 1
    assert any("EN" in d and "未加入" in d for d in report.details)

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status, error FROM sync_log WHERE target='bot_card_EN'"
    ).fetchone()
    conn.close()
    assert row == ("success", "chat_not_found")  # skip 动作本身是 success，error 字段记录跳过原因


def test_screenshot_failure_for_one_locale_is_skipped(tmp_path, monkeypatch, db_path):
    def _failed_overview(*args, **kwargs):
        raise RuntimeError("overview screenshot failed")

    monkeypatch.setattr(bot, "capture_overview", _failed_overview)
    monkeypatch.setattr(bot, "load_env", lambda: {
        "FEISHU_APP_ID": "app-x", "FEISHU_APP_SECRET": "secret-x",
    })
    monkeypatch.setattr(
        bot, "PUSH_TARGETS_PATH",
        _fake_push_targets(tmp_path, {"EN": "CompAgent_EN"}),
    )

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        if "/im/v1/chats" in url:
            return json.dumps({
                "code": 0,
                "data": {
                    "has_more": False, "page_token": "",
                    "items": [
                        {"chat_id": "oc_en", "name": "CompAgent_EN"},
                        {"chat_id": "oc_fr", "name": "CompAgent_FR"},
                    ],
                },
            })
        assert url.endswith("/im/v1/messages?receive_id_type=chat_id")
        payload = json.loads(body.decode())
        assert payload["msg_type"] == "interactive"
        card = json.loads(payload["content"])
        assert not any(element.get("tag") == "img" for element in card["elements"])
        return json.dumps({"code": 0})

    monkeypatch.setattr(bot, "fetch", fake_fetch)

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=False)
    assert report.pushed == 1
    assert report.cards_pushed == 1
    assert report.images_pushed == 0
    assert report.skipped == 0
    assert any("EN" in d and "纯文本降级" in d for d in report.details)
