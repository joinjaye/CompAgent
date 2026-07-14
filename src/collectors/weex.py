"""Weex 采集器：公告中心跑在 Zendesk 上（weexsupport.zendesk.com）。逻辑见 zendesk_base.py。"""

from __future__ import annotations

from src.collectors.zendesk_base import ZendeskCollector


class WeexCollector(ZendeskCollector):
    source_name = "Weex"
    group_id_prefix = "weex"
