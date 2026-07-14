"""Bitunix 采集器：真实公告中心跑在 Zendesk 上（support.bitunix.com），
不是主站 www.bitunix.com（那条路径已在 Phase 1 确认死路）。逻辑见 zendesk_base.py。
"""

from __future__ import annotations

from src.collectors.zendesk_base import ZendeskCollector


class BitunixCollector(ZendeskCollector):
    source_name = "Bitunix"
    group_id_prefix = "bitunix"
