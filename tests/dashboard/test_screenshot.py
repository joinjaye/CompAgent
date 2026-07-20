"""src/dashboard/screenshot.py 单测：capture_push_views() 对每个 locale 正确构造
`?view=push&locale=<X>` URL、等待对应的 [data-push-ready] 选择器、单个 locale 失败
不影响其它 locale。全部离线：mock 掉 playwright.sync_api.sync_playwright，不启动
真实浏览器。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.dashboard import screenshot as screenshot_module


class _FakePage:
    def __init__(self, fail_locales=()):
        self.fail_locales = set(fail_locales)
        self.goto_calls = []
        self.wait_for_selector_calls = []
        self.screenshot_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)

    def wait_for_selector(self, selector, **kwargs):
        self.wait_for_selector_calls.append(selector)
        for locale in self.fail_locales:
            if f'"{locale}"' in selector:
                raise TimeoutError(f"selector not found for {locale}")

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, *, path, full_page):
        self.screenshot_calls.append(path)


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_page(self, **kwargs):
        return self._page

    def close(self):
        pass


def _install_fake_playwright(monkeypatch, page):
    fake_chromium = MagicMock()
    fake_chromium.launch.return_value = _FakeBrowser(page)
    fake_pw_instance = MagicMock()
    fake_pw_instance.chromium = fake_chromium

    class _FakeSyncPlaywright:
        def __enter__(self):
            return fake_pw_instance

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(screenshot_module, "sync_playwright", lambda: _FakeSyncPlaywright())


def test_capture_push_views_navigates_to_push_url_per_locale(tmp_path, monkeypatch):
    page = _FakePage()
    _install_fake_playwright(monkeypatch, page)

    result = screenshot_module.capture_push_views(
        "http://localhost:8731/index.html", ["EN", "FR"], tmp_path
    )

    assert page.goto_calls == [
        "http://localhost:8731/index.html?view=push&locale=EN",
        "http://localhost:8731/index.html?view=push&locale=FR",
    ]
    assert page.wait_for_selector_calls == [
        '[data-push-ready="EN"]',
        '[data-push-ready="FR"]',
    ]
    assert set(result.keys()) == {"EN", "FR"}
    assert result["EN"].name == "EN.png"


def test_capture_push_views_url_encodes_locale_with_hyphen(tmp_path, monkeypatch):
    page = _FakePage()
    _install_fake_playwright(monkeypatch, page)

    result = screenshot_module.capture_push_views(
        "http://localhost:8731/index.html", ["EN-Asia"], tmp_path
    )

    assert page.goto_calls == ["http://localhost:8731/index.html?view=push&locale=EN-Asia"]
    # 输出文件名把连字符换成下划线，跟旧版 capture_locale_tabs 的既有约定一致
    assert result["EN-Asia"].name == "EN_Asia.png"


def test_capture_push_views_one_locale_failure_does_not_block_others(tmp_path, monkeypatch):
    page = _FakePage(fail_locales={"FR"})
    _install_fake_playwright(monkeypatch, page)

    result = screenshot_module.capture_push_views(
        "http://localhost:8731/index.html", ["EN", "FR", "VN"], tmp_path
    )

    assert set(result.keys()) == {"EN", "VN"}
    assert "FR" not in result
