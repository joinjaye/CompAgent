"""Playwright 无头浏览器截图：从渲染好的看板页面（`docs/index.html`，本地文件、本地
`http.server`、或线上 GitHub Pages URL 均可，只要能访问到同目录下的
`data/dashboard.json`）截取指定 locale tab 的当前渲染内容，供 src/sinks/feishu_bot.py
推送到对应飞书群。

只截取区域 tab（EN/FR/VN/ID/EN-Asia）——「全量」「全局视角」两个 tab 不在推送范围内，
是业务决定（这两个是给人主动去浏览的工具，不是"今天发生了什么"的简报），见
CLAUDE.md「Phase 7 之后：飞书群截图推送」。

单个浏览器实例、单次页面加载，之后逐个 tab 点击 + 截图——dashboard.json 只需要
fetch 一次，跟真实用户点 tab 切换的行为一致，比给每个 locale 各开一次浏览器/重新
加载页面快得多，也更接近"用户会看到的真实渲染结果"。
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1180
VIEWPORT_HEIGHT = 900
DEFAULT_TIMEOUT_MS = 20000


def capture_locale_tabs(url: str, locales: list[str], out_dir: Path | str,
                         *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Path]:
    """打开 url 一次，依次点击每个 locale tab 并截全页图，返回 {locale: 图片路径}。

    某个 locale 截图失败（如页面结构变了、选择器找不到）不影响其它 locale——记录
    警告并跳过，返回结果里就不会有这个 locale 的条目，调用方（feishu_bot.py）据此
    判断哪些 locale 这次没有产出截图，不会拿一张空白/半成品图片去推送。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_selector(".locale-tabs", timeout=timeout_ms)

            for locale in locales:
                try:
                    selector = f'.locale-tab[data-tab="{locale}"]'
                    page.wait_for_selector(selector, timeout=timeout_ms)
                    page.click(selector)
                    # renderActivePane() 是同步 DOM 操作，但给一点缓冲避开动画过渡帧
                    page.wait_for_timeout(300)
                    out_path = out_dir / f"{locale.replace('-', '_')}.png"
                    page.screenshot(path=str(out_path), full_page=True)
                    result[locale] = out_path
                    logger.info("截图完成：%s -> %s", locale, out_path)
                except Exception as e:
                    logger.warning("locale=%s 截图失败，跳过：%s", locale, e)
        finally:
            browser.close()

    return result
