"""四套按 category 分发的 LLM prompt 模板（phasePrompts.md Phase 4 给定的完整文本，
逐字实现）。改任何一套 prompt 的正文必须递增 config/analysis.yaml 的
prompt_versions[category]。

变量替换只认形如 {ALL_CAPS_NAME} 的占位符（正则 `\\{[A-Z][A-Z0-9_]*\\}`），公告
正文/标题这些不受信任的自由文本先各自拼进 ARTICLES_BLOCK / ZMX_BLOCK 字符串，再作为
整体值填入模板——不用 str.format()，避免正文里偶然出现的 "{" "}" 破坏格式化或触发
KeyError（爬来的公告原文不可控，JSON 示例里的花括号也不能被误当成占位符）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src.analysis.zmx_index import ZmxArticle

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def render(template: str, variables: dict[str, str]) -> str:
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return variables[key] if key in variables else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template)


# ============================================================
# campaign-v1
# ============================================================

SYSTEM_CAMPAIGN = (
    "你是一名加密交易所竞品分析师，服务对象是运营团队。你的职责是分析竞品的活动公告，"
    "提炼可操作的情报。输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。"
)

USER_CAMPAIGN_TEMPLATE = """\
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条（新增 {NEW_COUNT} 条，变更 {CHANGED_COUNT} 条）

【活动公告列表】
{ARTICLES_BLOCK}
（每条格式：
[index] UID: uid
标题：title
状态：status
正文：content
仅 status=changed 时追加一行：变更前正文：old_content）

【Zoomex 基线（同类目、同地区，近 90 天相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
（每条格式：[Zindex] UID: zmx_uid | post_time | 标题：title | 摘要：content_preview）
{ZMX_NOTE}

【分析任务】
请输出以下结构的 JSON：

{
  "batch_summary": "2-3 句话，描述本批次活动的整体方向和共同规律。必须具体：如「本日 Bitunix 集中发布 3 场交易竞赛，均以交易量排名为分配依据，奖励形式以 USDT 为主」。禁止使用「丰富」「多样」「显著」等模糊形容词。",
  "articles": [
    {
      "uid": "（原样照抄，不得修改）",
      "title": "（原样照抄）",
      "mechanics": "玩法机制一句话。门槛、奖励形式、奖励规模必须从正文数字中提取，不可估算或替换为模糊表达。正文信息不足时填「原文信息不足」。",
      "time_window": "活动起止时间，格式 YYYY-MM-DD ~ YYYY-MM-DD。正文未提供时填 null。",
      "target_users": "目标用户群，如「所有用户」「新注册用户」「合约交易用户」「大户（持仓 > X USDT）」。",
      "change_summary": "（仅 status=changed 时填写，其他情况填 null）具体变更内容，如「奖池从 10,000 USDT 增加至 50,000 USDT，活动截止日期延长 7 天」。"
    }
  ],
  "zmx_comparison": {
    "diff_type": "从以下选项选一个：ZMX已有 / ZMX缺失 / ZMX玩法不同 / 混合 / 不适用。「混合」表示本批次内同时存在 ZMX 已有和 ZMX 缺失的情况。没有充分基线依据时只能填「不适用」。",
    "analysis": "具体叙述与 Zoomex 的差异。必须引用 [Zindex] 编号（如「[Z2] 所示，Zoomex 在 2026-05 也举办过类似交易竞赛，但奖池规模（5,000 USDT）明显低于本批次」）。diff_type=「混合」时，逐条标注各篇公告的具体情况。无充分依据时填「基线数据不足，无法判断」，diff_type 同时改为「不适用」。",
    "evidence_indices": "[整数数组，引用了哪几条 [Zindex]，未引用任何基线时必须为空数组 []]",
    "priority": "高 / 中 / 低。高：ZMX 缺失的高价值玩法或奖池规模显著高于 ZMX 同类活动；中：ZMX 已有类似玩法但规模/机制有差异；低：与 ZMX 高度雷同或信息量不足。",
    "priority_reason": "一句话说明定级依据，必须包含具体数字或事实，不接受「因为差异较大」这类空话。"
  }
}

【强制规则，违反时输出视为无效】
1. uid 字段原样照抄，不得改动任何字符
2. mechanics 里的数字必须来自正文，禁止出现「大量」「丰厚」「一定数量」等模糊词
3. evidence_indices 为空数组时，diff_type 只能是「不适用」
4. 整个输出必须是合法 JSON，不加任何注释（// 或 /* */ 均不允许）
"""

# ============================================================
# product-v1
# ============================================================

SYSTEM_PRODUCT = (
    "你是一名加密交易所竞品分析师，服务对象是产品团队。你的职责是分析竞品的产品更新公告，"
    "识别功能差距和迭代方向。输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。"
)

USER_PRODUCT_TEMPLATE = """\
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条（新增 {NEW_COUNT} 条，变更 {CHANGED_COUNT} 条）

【产品更新公告列表】
{ARTICLES_BLOCK}

【Zoomex 基线（同类目、同地区，近 90 天相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
{ZMX_NOTE}

【分析任务】
{
  "batch_summary": "2-3 句话描述本批次产品更新的整体方向。必须具体说明功能领域，如「集中在合约风控规则调整（强平机制优化）和 API 接口扩展」，禁止使用「提升用户体验」「全面优化」等空话。",
  "articles": [
    {
      "uid": "（原样照抄）",
      "title": "（原样照抄）",
      "feature_description": "新功能或变更的一句话描述。必须说清楚「做了什么」，如「新增跟单交易止损止盈功能，支持跟单者自定义最大跟单金额上限」。禁止使用「优化了体验」「提升了性能」等无实质内容的表述。",
      "affected_users": "影响哪类用户，如「所有合约交易用户」「使用 API 接入的机构用户」「跟单交易跟随者」。",
      "change_summary": "（仅 status=changed 时填写，其他情况填 null）具体改了什么，如「手续费返还比例从 20% 提高至 30%，适用范围从 VIP3+ 扩展至 VIP1+」。"
    }
  ],
  "zmx_comparison": {
    "diff_type": "ZMX已有 / ZMX缺失 / ZMX玩法不同 / 混合 / 不适用",
    "analysis": "Zoomex 是否有同类功能，功能成熟度和覆盖范围的对比。必须引用 [Zindex] 编号。对于「ZMX缺失」的判断要保守：基线里没有搜到不等于 ZMX 真的没有，应表述为「基线中未见相关记录，建议人工复核」。",
    "evidence_indices": [],
    "priority": "高 / 中 / 低。高：基线确认 ZMX 缺失且该功能对用户留存或获客有直接影响；中：ZMX 有类似功能但实现细节有差距；低：功能高度雷同或属于常规维护类更新。",
    "priority_reason": "一句话定级依据。"
  }
}

【强制规则】
1. uid 字段原样照抄
2. feature_description 必须包含「做了什么」的实质内容，不接受仅描述影响而不描述功能的表述
3. evidence_indices 为空时 diff_type 只能是「不适用」
4. 整个输出必须是合法 JSON
"""

# ============================================================
# listing-v1
# ============================================================

SYSTEM_LISTING = (
    "你是一名加密交易所竞品分析师，服务对象是运营和产品团队。你的职责是分析竞品的上币公告，"
    "识别竞品的上币策略和潜在的 ZMX 上币机会。输出必须是合法 JSON。"
)

USER_LISTING_TEMPLATE = """\
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条

【上币公告列表】
{ARTICLES_BLOCK}

【Zoomex 基线（近 90 天上币记录，相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
{ZMX_NOTE}

【分析任务】
{
  "batch_summary": "2-3 句话描述本批次上币特征，如「本日 Bitunix 新增 5 个现货交易对，以 Layer2 生态项目为主，其中 2 个为 meme 类代币，无明显头部项目」。",
  "articles": [
    {
      "uid": "（原样照抄）",
      "title": "（原样照抄）",
      "token_symbol": "代币符号，如「BTCUSDT」。从标题或正文提取，提取不到填 null。",
      "market_type": "现货 / 合约 / 两者均有 / 不明",
      "launch_time": "上线时间，格式 YYYY-MM-DD HH:MM UTC。正文未提供填 null。",
      "project_brief": "项目一句话简介，从正文提取。正文无介绍填 null，禁止自行补充 LLM 知识库里的项目信息。"
    }
  ],
  "zmx_comparison": {
    "diff_type": "ZMX已有 / ZMX缺失 / 混合 / 不适用",
    "analysis": "逐一说明本批次各代币在 Zoomex 基线中的情况。对每个代币：基线中有记录则标注 [Zindex] 引用；基线中无记录则表述为「基线中未见 {token_symbol} 上币记录」（不得直接断言 ZMX 没有上线，因为 Zoomex 全量数据尚未入库）。",
    "evidence_indices": [],
    "priority": "高 / 中 / 低。高：至少 1 个代币基线确认 ZMX 缺失且属于有一定市值的主流项目；中：基线未见但项目知名度有限；低：全部代币已在基线中找到对应记录。",
    "priority_reason": "一句话定级依据。"
  }
}

【强制规则】
1. uid 字段原样照抄
2. project_brief 只能来自正文，禁止使用 LLM 训练数据中的项目知识
3. evidence_indices 为空时 diff_type 只能是「不适用」
4. listing 批次的 diff_type 不含「ZMX玩法不同」选项
"""

# ============================================================
# delisting-v1
# ============================================================

SYSTEM_DELISTING = (
    "你是一名加密交易所竞品分析师。你的职责是分析竞品的下架公告，提取关键信息供运营团队"
    "参考和风险预警。输出必须是合法 JSON。"
)

USER_DELISTING_TEMPLATE = """\
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条

【下架公告列表】
{ARTICLES_BLOCK}

【分析任务】
下架公告不做 ZMX 差异分析（diff_type 固定为「不适用」）。
请聚焦在信息提取准确性上。

{
  "batch_summary": "一句话总结本批次下架概况，如「Bitunix 今日下架 3 个现货交易对，涉及 2 个 meme 类代币和 1 个流动性不足的小市值项目，下架时间集中在本周末」。",
  "articles": [
    {
      "uid": "（原样照抄）",
      "title": "（原样照抄）",
      "token_symbol": "代币符号，提取不到填 null",
      "market_type": "现货 / 合约 / 两者均有 / 不明",
      "delist_time": "下架时间，格式 YYYY-MM-DD HH:MM UTC。正文未提供填 null。",
      "reason": "下架原因，从正文提取。常见值：「流动性不足」「项目方要求」「合规原因」「维护升级」。正文未说明填 null，禁止推断。"
    }
  ],
  "zmx_comparison": {
    "diff_type": "不适用",
    "analysis": null,
    "evidence_indices": [],
    "priority": "高 / 中 / 低。高：涉及主流代币或下架时间紧迫（72 小时内）；中：小市值代币但时间充裕；低：纯合约维护类下架。",
    "priority_reason": "一句话定级依据。"
  }
}

【强制规则】
1. uid 字段原样照抄
2. reason 只能来自正文，禁止推断
3. diff_type 固定为「不适用」，不得修改
"""

_TEMPLATES: dict[str, tuple[str, str]] = {
    "campaign": (SYSTEM_CAMPAIGN, USER_CAMPAIGN_TEMPLATE),
    "product": (SYSTEM_PRODUCT, USER_PRODUCT_TEMPLATE),
    "listing": (SYSTEM_LISTING, USER_LISTING_TEMPLATE),
    "delisting": (SYSTEM_DELISTING, USER_DELISTING_TEMPLATE),
}

# category -> (zmx_count==0 时的提示文案, 0<命中数<min_hits 时的提示文案模板)
_ZMX_ZERO_NOTE = {
    "campaign": "注意：当前 Zoomex 基线数据不足，无法进行差异判断，zmx_comparison 的 diff_type 必须填「不适用」。",
    "product": "注意：当前 Zoomex 基线数据不足，zmx_comparison 的 diff_type 必须填「不适用」。",
    "listing": "注意：Zoomex 上币基线数据不足，diff_type 必须填「不适用」。",
}
_ZMX_LIMITED_NOTE_TMPL = "注意：当前 Zoomex 基线数据有限（仅 {n} 条），差异判断的置信度可能较低。"


def build_zmx_note(category: str, zmx_count: int, min_hits: int) -> str:
    """category=delisting 不带 ZMX 部分，不应该调用本函数。"""
    if zmx_count == 0:
        return _ZMX_ZERO_NOTE[category]
    if zmx_count < min_hits:
        return _ZMX_LIMITED_NOTE_TMPL.format(n=zmx_count)
    return ""


def build_articles_block(rows: list, old_content_by_uid: dict[str, Optional[str]], max_chars: int = 4000) -> str:
    """rows 是 announcements 的行（需要 uid/title/status/content 列）。"""
    parts: list[str] = []
    for i, row in enumerate(rows, start=1):
        content = (row["content"] or "")[:max_chars]
        lines = [
            f"[{i}] UID: {row['uid']}",
            f"标题：{row['title']}",
            f"状态：{row['status']}",
            f"正文：{content}",
        ]
        if row["status"] == "changed":
            old_content = old_content_by_uid.get(row["uid"])
            if old_content:
                lines.append(f"变更前正文：{old_content[:max_chars]}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "（本批次无公告，不应该发生）"


def build_zmx_block(hits: list[ZmxArticle]) -> str:
    if not hits:
        return "（无匹配的 Zoomex 基线记录）"
    lines = [
        f"[Z{i}] UID: {hit.uid} | {hit.post_time} | 标题：{hit.title} | 摘要：{hit.content_preview}"
        for i, hit in enumerate(hits, start=1)
    ]
    return "\n".join(lines)


@dataclass
class BuiltPrompt:
    system: str
    user: str


def build_prompt(
    category: str,
    *,
    source: str,
    locale: str,
    batch_date: str,
    rows: list,
    old_content_by_uid: dict[str, Optional[str]],
    zmx_hits: Optional[list[ZmxArticle]] = None,
    min_hits_for_full_confidence: int = 3,
    article_content_chars: int = 4000,
) -> BuiltPrompt:
    if category not in _TEMPLATES:
        raise ValueError(f"未知 category：{category!r}")

    system, user_template = _TEMPLATES[category]

    new_count = sum(1 for r in rows if r["status"] == "new")
    changed_count = sum(1 for r in rows if r["status"] == "changed")
    articles_block = build_articles_block(rows, old_content_by_uid, max_chars=article_content_chars)

    variables = {
        "SOURCE": source,
        "LOCALE": locale,
        "BATCH_DATE": batch_date,
        "ARTICLE_COUNT": str(len(rows)),
        "NEW_COUNT": str(new_count),
        "CHANGED_COUNT": str(changed_count),
        "ARTICLES_BLOCK": articles_block,
    }

    if category != "delisting":
        hits = zmx_hits or []
        variables["ZMX_COUNT"] = str(len(hits))
        variables["ZMX_BLOCK"] = build_zmx_block(hits)
        variables["ZMX_NOTE"] = build_zmx_note(category, len(hits), min_hits_for_full_confidence)

    user = render(user_template, variables)
    return BuiltPrompt(system=system, user=user)
