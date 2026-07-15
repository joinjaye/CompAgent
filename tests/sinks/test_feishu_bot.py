"""src/sinks/feishu_bot.py 单测：全部离线（mock fetch/fetch_json + mock
capture_locale_tabs，不启动真实 Playwright/浏览器，不发真实网络请求）。

真实网络验收记录（2026-07-15，用真实 FEISHU_APP_ID/SECRET）：
- 截图（src/dashboard/screenshot.py）对本地 http.server 跑通，5 个 locale 全部
  产出非空 PNG。
- upload_image() 的 multipart 请求格式本身正确（拿到了 Feishu 的业务级 JSON 错误
  响应，不是"请求格式不对"的错误）：`{"code":234007,"msg":"App does not enable
  bot feature."}`——当前 FEISHU_APP_ID 对应的应用还没有在飞书开发者后台开通"机器人"
  能力，这是应用配置问题，不是代码问题，真正推送前需要用户去开通。
- push_image_to_webhook() 未做真实调用：`.env` 里没有配置任何 WEBHOOK_* 真实值
  （config/push_targets.yaml 的占位符全部替换成 None），没有真实群可以测试。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db.connection import SCHEMA_PATH, connect
from src.sinks import feishu_bot as bot


@pytest.fixture(autouse=True)
def _clear_token_cache():
    bot._token_cache.clear()
    yield
    bot._token_cache.clear()


# ============================================================
# multipart body + push_targets 替换
# ============================================================


def test_build_multipart_body_contains_boundary_and_image_bytes():
    body, content_type = bot._build_multipart_body(b"\x89PNG-fake-bytes", "test.png")
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    assert boundary.encode() in body
    assert b"\x89PNG-fake-bytes" in body
    assert b'name="image_type"' in body
    assert b'name="image"; filename="test.png"' in body


def test_load_push_targets_substitutes_env_vars():
    env = {"WEBHOOK_EN": "https://open.larksuite.com/open-apis/bot/v2/hook/real-en"}
    targets = bot.load_push_targets(env)
    assert targets["EN"]["webhook"] == "https://open.larksuite.com/open-apis/bot/v2/hook/real-en"
    assert targets["FR"]["webhook"] is None  # 没配置的 locale，占位符替换成空字符串 -> YAML 解析成 None
    assert set(targets.keys()) == {"EN", "FR", "VN", "ID", "EN-Asia"}


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
# push_image_to_webhook
# ============================================================


def test_push_image_to_webhook_success_with_code(monkeypatch):
    monkeypatch.setattr(bot, "fetch", lambda *a, **k: json.dumps({"code": 0, "msg": "ok"}))
    bot.push_image_to_webhook("https://hook.example/x", "img_1")  # 不抛异常即通过


def test_push_image_to_webhook_success_with_status_code(monkeypatch):
    monkeypatch.setattr(bot, "fetch", lambda *a, **k: json.dumps({"StatusCode": 0}))
    bot.push_image_to_webhook("https://hook.example/x", "img_1")


def test_push_image_to_webhook_failure_raises(monkeypatch):
    monkeypatch.setattr(bot, "fetch", lambda *a, **k: json.dumps({"code": 19021, "msg": "invalid image_key"}))
    with pytest.raises(bot.FeishuBotError, match="19021"):
        bot.push_image_to_webhook("https://hook.example/x", "img_1")


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


def test_dry_run_captures_screenshots_but_calls_no_feishu_api(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(
        bot, "capture_locale_tabs",
        lambda url, locales, out_dir, **kw: _fake_screenshots(tmp_path, locales),
    )

    def _boom(*a, **k):
        raise AssertionError("dry_run 不应该调用任何飞书 API")

    monkeypatch.setattr(bot, "fetch", _boom)
    monkeypatch.setattr(bot, "load_env", lambda: {"WEBHOOK_EN": "https://hook.example/en"})

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=True)
    assert report.pushed == 0
    assert report.failed == 0
    # EN 配置了 webhook -> dry-run 里应该出现"会推送"的详情；其余 4 个没配置 webhook -> skipped
    assert report.skipped == 4
    assert any("EN" in d and "会上传" in d for d in report.details)


def test_missing_webhook_is_skipped_not_failed(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(
        bot, "capture_locale_tabs",
        lambda url, locales, out_dir, **kw: _fake_screenshots(tmp_path, locales),
    )
    monkeypatch.setattr(bot, "load_env", lambda: {})

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=True)
    assert report.skipped == 5
    assert report.failed == 0


def test_real_run_pushes_and_logs_sync_log(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(
        bot, "capture_locale_tabs",
        lambda url, locales, out_dir, **kw: _fake_screenshots(tmp_path, locales),
    )
    monkeypatch.setattr(bot, "load_env", lambda: {
        "FEISHU_APP_ID": "app-x", "FEISHU_APP_SECRET": "secret-x",
        "WEBHOOK_EN": "https://hook.example/en",
    })

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        if url.endswith("/im/v1/images"):
            return json.dumps({"code": 0, "data": {"image_key": "img_key_1"}})
        return json.dumps({"code": 0})  # webhook post

    monkeypatch.setattr(bot, "fetch", fake_fetch)

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, batch_date="2026-07-15", dry_run=False)
    assert report.pushed == 1  # 只有 EN 配置了 webhook
    assert report.skipped == 4
    assert report.failed == 0

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT target, record_id, action, status FROM sync_log ORDER BY target").fetchall()
    conn.close()
    targets = {r[0]: (r[2], r[3]) for r in rows}
    assert targets["bot_EN"] == ("create", "success")
    assert targets["bot_FR"] == ("skip", "success")
    assert rows[0][1].endswith("2026-07-15")  # record_id 带上了 batch_date


def test_screenshot_failure_for_one_locale_is_skipped(tmp_path, monkeypatch, db_path):
    def _partial_screenshots(url, locales, out_dir, **kw):
        result = _fake_screenshots(tmp_path, locales)
        del result["FR"]  # 模拟 FR 截图失败
        return result

    monkeypatch.setattr(bot, "capture_locale_tabs", _partial_screenshots)
    monkeypatch.setattr(bot, "load_env", lambda: {
        "FEISHU_APP_ID": "app-x", "FEISHU_APP_SECRET": "secret-x",
        "WEBHOOK_EN": "https://hook.example/en", "WEBHOOK_FR": "https://hook.example/fr",
    })

    def fake_fetch(url, *, method="GET", headers=None, body=None, timeout=None, max_retries=None):
        if "tenant_access_token" in url:
            return json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200})
        if url.endswith("/im/v1/images"):
            return json.dumps({"code": 0, "data": {"image_key": "img_key_1"}})
        return json.dumps({"code": 0})

    monkeypatch.setattr(bot, "fetch", fake_fetch)

    report = bot.push_dashboard_screenshots("http://fake", db_path=db_path, dry_run=False)
    assert report.pushed == 1  # EN 成功
    assert report.skipped == 4  # FR（截图失败）+ VN/ID/EN-Asia（无 webhook）
    assert any("FR" in d and "截图失败" in d for d in report.details)
