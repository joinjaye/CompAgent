"""四套按 category 分发的 LLM prompt 模板（原始版本 -v1 是 phasePrompts.md Phase 4
给定的完整文本，逐字实现；-v2，2026-07-20，在每个模板的 articles[] 里逐条新增
diff_type/priority/follow_up 三个通用字段 + evidence_indices（campaign/product/
listing）+ change_kind（仅 campaign）+ listing_kind（仅 listing），供 Phase 7 看板
逐条排序/展示用，取代原来只有批次级 zmx_comparison 的粗粒度）。改任何一套 prompt 的
正文必须递增 config/analysis.yaml 的 prompt_versions[category]。这 5 个新字段是否
合法由 src/analysis/llm.py 的 validate_and_normalize() 程序性强制，不是单纯信任
LLM 输出遵守本文件里写的规则。

变量替换只认形如 {ALL_CAPS_NAME} 的占位符（正则 `\\{[A-Z][A-Z0-9_]*\\}`），公告
正文/标题这些不受信任的自由文本先各自拼进 ARTICLES_BLOCK / ZMX_BLOCK 字符串，再作为
整体值填入模板——不用 str.format()，避免正文里偶然出现的 "{" "}" 破坏格式化或触发
KeyError（爬来的公告原文不可控，JSON 示例里的花括号也不能被误当成占位符）。
"""

from __future__ import annotations

import json
import re
from difflib import ndiff
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # zmx_baseline.py 反过来要 import 本模块的 build_extraction_prompt（提取 prompt
    # 复用这里的 render()/BuiltPrompt），运行时互相 import 会循环，这里只在类型检查时
    # 导入（配合文件顶部 `from __future__ import annotations`，注解本身在运行时是
    # 字符串，不需要真的把类拿到）。
    from src.analysis.zmx_baseline import ZmxBaselineEntry

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def render(template: str, variables: dict[str, str]) -> str:
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return variables[key] if key in variables else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template)


# ============================================================
# campaign-v2（2026-07-20：articles[] 逐条新增 diff_type/evidence_indices/priority/
# follow_up/change_kind，供 Phase 7 看板逐条分析 + 优先级排序用；change_kind 仅
# campaign 独有，其它三个 category 没有这个字段）
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

【Zoomex 基线（同类目、同地区，近 90 天玩法类型总览，共 {ZMX_COUNT} 条，覆盖各已知
玩法类型，不是按相关度排序的检索结果——缺席某个玩法类型不代表判断置信度低，判断的
是"这些类型里有没有跟本批次雷同/缺失的"）】
{ZMX_BLOCK}
（每条格式：[Zindex] 类型：mechanism_type | UID: zmx_uid | 标题：title | 机制：
key_mechanics | 奖励：reward_range | 目标用户：target_users | 时间：
start_date~end_date）
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
      "change_summary": "（仅 status=changed 时填写，其他情况填 null）具体变更内容，如「奖池从 10,000 USDT 增加至 50,000 USDT，活动截止日期延长 7 天」。",
      "diff_type": "本条与 Zoomex 的差异类型（逐条判断，独立于下面 zmx_comparison 的批次级判断），从以下选项选一个：ZMX已有 / ZMX缺失 / ZMX玩法不同 / 混合 / 不适用。本条 evidence_indices 为空数组时必须填「不适用」。",
      "evidence_indices": "[整数数组，本条引用了哪几条 [Zindex]，未引用任何基线时必须为空数组 []]",
      "priority": "本条优先级：高 / 中 / 低，判断标准同 zmx_comparison.priority，但只针对本条。",
      "follow_up": "一句话可执行的中文跟进建议，如「建议评估是否上线同类交易赛」。priority=低 或 diff_type=不适用 时可填空字符串或「无需跟进」。",
      "change_kind": "仅当本条 status=changed 时可能有值：reward（奖励规模/形式变化）/ rule（规则或门槛变化）/ other（其他变化）；status≠changed 时必须填 null，不得猜测。"
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
3. evidence_indices 为空数组时，diff_type 只能是「不适用」（zmx_comparison 顶层和每条 articles 内部各自独立判断，互不影响）
4. 整个输出必须是合法 JSON，不加任何注释（// 或 /* */ 均不允许）
5. 每条 articles 的 change_kind 只在该条自己 status=changed 时才可能有值，其余一律 null；不得因为批次里有别的条目 changed 就误填
"""

# ============================================================
# product-v2（2026-07-20：articles[] 逐条新增 diff_type/evidence_indices/priority/
# follow_up，供 Phase 7 看板逐条分析 + 优先级排序用。change_kind 是 campaign 独有
# 字段，product 不产出这个字段。）
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

【Zoomex 基线（同类目、同地区，近 90 天玩法类型总览，共 {ZMX_COUNT} 条，覆盖各已知
功能类型，不是按相关度排序的检索结果）】
{ZMX_BLOCK}
（每条格式：[Zindex] 类型：mechanism_type | UID: zmx_uid | 标题：title | 机制：
key_mechanics | 目标用户：target_users）
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
      "change_summary": "（仅 status=changed 时填写，其他情况填 null）具体改了什么，如「手续费返还比例从 20% 提高至 30%，适用范围从 VIP3+ 扩展至 VIP1+」。",
      "diff_type": "本条与 Zoomex 的差异类型（逐条判断，独立于下面 zmx_comparison 的批次级判断）：ZMX已有 / ZMX缺失 / ZMX玩法不同 / 混合 / 不适用。本条 evidence_indices 为空数组时必须填「不适用」。",
      "evidence_indices": "[整数数组，本条引用了哪几条 [Zindex]，未引用任何基线时必须为空数组 []]",
      "priority": "本条优先级：高 / 中 / 低，判断标准同 zmx_comparison.priority，但只针对本条。",
      "follow_up": "一句话可执行的中文跟进建议，如「建议评估该功能是否列入下季度 roadmap」。priority=低 或 diff_type=不适用 时可填空字符串或「无需跟进」。"
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
3. evidence_indices 为空时 diff_type 只能是「不适用」（zmx_comparison 顶层和每条 articles 内部各自独立判断）
4. 整个输出必须是合法 JSON
5. articles 内不产出 change_kind 字段（该字段仅 campaign 类目有意义）
"""

# ============================================================
# listing-v2（2026-07-20：articles[] 逐条新增 diff_type/evidence_indices/priority/
# follow_up/listing_kind。listing_kind 是 listing 独有字段，spot/perp 从
# market_type 归约而来；market_type 本身是「现货/合约/两者均有/不明」四选一，
# 「两者均有」「不明」时 listing_kind 必须填 null，不强行二选一猜测。）
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

【Zoomex 基线（近 90 天上币类型总览，共 {ZMX_COUNT} 条，不是按相关度排序的检索结果）】
{ZMX_BLOCK}
（每条格式：[Zindex] 类型：mechanism_type | UID: zmx_uid | 标题：title | 机制：
key_mechanics | 时间：start_date~end_date）
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
      "project_brief": "项目一句话简介，从正文提取。正文无介绍填 null，禁止自行补充 LLM 知识库里的项目信息。",
      "diff_type": "本条与 Zoomex 的差异类型（逐条判断，独立于下面 zmx_comparison 的批次级判断）：ZMX已有 / ZMX缺失 / 混合 / 不适用（不含「ZMX玩法不同」，同批次级约束）。本条 evidence_indices 为空数组时必须填「不适用」。",
      "evidence_indices": "[整数数组，本条引用了哪几条 [Zindex]，未引用任何基线时必须为空数组 []]",
      "priority": "本条优先级：高 / 中 / 低，判断标准同 zmx_comparison.priority，但只针对本条。",
      "follow_up": "一句话可执行的中文跟进建议，如「建议关注该代币是否值得纳入 ZMX 上币评估」。priority=低 或 diff_type=不适用 时可填空字符串或「无需跟进」。",
      "listing_kind": "spot（现货）/ perp（合约/期货）。从 market_type 归约：market_type=现货→spot；market_type=合约→perp；market_type=两者均有 或 不明→null，不强行二选一猜测。"
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
3. evidence_indices 为空时 diff_type 只能是「不适用」（zmx_comparison 顶层和每条 articles 内部各自独立判断）
4. listing 批次的 diff_type（zmx_comparison 与每条 articles 内部均不含）不含「ZMX玩法不同」选项
5. listing_kind 无法从 market_type 明确归约时（两者均有/不明）必须填 null，不得猜测
6. articles 内不产出 change_kind 字段（该字段仅 campaign 类目有意义）
"""

# ============================================================
# delisting-v2（2026-07-20：articles[] 逐条新增 priority/follow_up。delisting 没有
# ZMX 基线对比（无 {ZMX_BLOCK}），所以不产出 evidence_indices；diff_type 逐条同样
# 恒为「不适用」（与批次级一致），不产出 listing_kind/change_kind。）
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
      "reason": "下架原因，从正文提取。常见值：「流动性不足」「项目方要求」「合规原因」「维护升级」。正文未说明填 null，禁止推断。",
      "diff_type": "固定为「不适用」，不得修改（delisting 不做 ZMX 差异分析）。",
      "priority": "本条优先级：高 / 中 / 低，判断标准同 zmx_comparison.priority，但只针对本条。",
      "follow_up": "一句话可执行的中文跟进建议，如「建议检查 ZMX 是否有该代币的对应仓位/挂单风险提示」。priority=低 时可填空字符串或「无需跟进」。"
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
3. diff_type（批次级和每条 articles 内部均）固定为「不适用」，不得修改
4. articles 内不产出 evidence_indices/listing_kind/change_kind 字段（delisting 无 ZMX 对比，这些字段对 delisting 均无意义）
"""

SYSTEM_CAMPAIGN_V3 = """\
你是服务于交易所运营团队的竞品情报分析师。只使用输入证据，不使用外部知识。
区分“Zoomex 基线未发现记录”和“已确认缺失”，不得把前者写成后者。
输出必须是合法 JSON，不包含 markdown 或解释文字。"""

USER_CAMPAIGN_V3 = """\
context: source={SOURCE}; locale={LOCALE}; date={BATCH_DATE}; items={ARTICLE_COUNT}

articles:
{ARTICLES_BLOCK}

zmx_baseline_candidates:
{ZMX_BLOCK}
{ZMX_NOTE}

返回：
{
  "batch_summary": "最多2句；说明共同机制、奖励或变化，必须保留关键数字",
  "articles": [{
    "uid": "原样返回",
    "mechanics": "一句话，含门槛/奖励/机制；无证据填原文信息不足",
    "time_window": "YYYY-MM-DD ~ YYYY-MM-DD 或 null",
    "target_users": "明确用户群或 null",
    "change_summary": "仅changed；明确before→after，否则null",
    "change_kind": "仅changed：reward/rule/other，否则null",
    "diff_type": "ZMX已有/ZMX缺失/ZMX玩法不同/混合/不适用",
    "evidence_indices": [],
    "priority": "高/中/低",
    "priority_reason": "一句，必须含输入中的数字或事实",
    "action_type": "no_action/monitor/manual_verify/benchmark/campaign_design",
    "owner": "campaign_ops/regional_ops/product",
    "follow_up": "可直接执行的动作；写明对象和交付物，禁止只写关注/评估"
  }],
  "zmx_comparison": {
    "diff_type": "ZMX已有/ZMX缺失/ZMX玩法不同/混合/不适用",
    "analysis": "最多2句，引用[Zindex]；无证据写基线未发现记录、建议人工复核",
    "evidence_indices": [],
    "priority": "高/中/低",
    "priority_reason": "一句具体事实"
  }
}

规则：
1. articles 每个输入 UID 恰好返回一次，不返回 title。
2. evidence_indices 为空时 diff_type 必须为“不适用”。
3. “ZMX缺失”只能在输入证据可确认时使用；仅未召回候选时用“不适用”。
4. priority：高=明确缺口且直接影响获客/收入；中=机制或规模有事实差异；低=已覆盖或信息不足。
5. 不重复正文，不输出未要求字段。"""

SYSTEM_PRODUCT_V3 = """\
你是服务于交易所产品团队的竞品情报分析师。只使用输入证据，不使用外部知识。
聚焦功能差距和可执行产品动作；不得把“基线未发现记录”断言为产品缺失。
输出必须是合法 JSON，不包含 markdown 或解释文字。"""

USER_PRODUCT_V3 = """\
context: source={SOURCE}; locale={LOCALE}; date={BATCH_DATE}; items={ARTICLE_COUNT}

articles:
{ARTICLES_BLOCK}

zmx_baseline_candidates:
{ZMX_BLOCK}
{ZMX_NOTE}

返回：
{
  "batch_summary": "最多2句；指出具体产品领域和功能变化",
  "articles": [{
    "uid": "原样返回",
    "feature_description": "一句话说明做了什么，禁止体验优化等空话",
    "affected_users": "明确用户群或null",
    "change_summary": "仅changed；明确before→after，否则null",
    "diff_type": "ZMX已有/ZMX缺失/ZMX玩法不同/混合/不适用",
    "evidence_indices": [],
    "priority": "高/中/低",
    "priority_reason": "一句具体事实",
    "action_type": "no_action/monitor/manual_verify/benchmark/product_evaluation",
    "owner": "product/regional_ops",
    "follow_up": "可直接执行的动作；写明对象和交付物，禁止只写关注/评估"
  }],
  "zmx_comparison": {
    "diff_type": "ZMX已有/ZMX缺失/ZMX玩法不同/混合/不适用",
    "analysis": "最多2句，引用[Zindex]；未召回候选只能写基线未发现、需人工复核",
    "evidence_indices": [],
    "priority": "高/中/低",
    "priority_reason": "一句具体事实"
  }
}

规则：
1. articles 每个输入 UID 恰好返回一次，不返回 title。
2. evidence_indices 为空时 diff_type 必须为“不适用”。
3. priority：高=证据确认关键能力缺口；中=实现范围/机制有差异；低=常规维护、已覆盖或信息不足。
4. 不重复正文，不输出未要求字段。"""

_TEMPLATES: dict[str, tuple[str, str]] = {
    "campaign": (SYSTEM_CAMPAIGN_V3, USER_CAMPAIGN_V3),
    "product": (SYSTEM_PRODUCT_V3, USER_PRODUCT_V3),
}

# category -> zmx_count==0 时的提示文案。原来还有一档"命中数 < min_hits → 置信度
# 可能较低"，那是针对 TF-IDF 检索"可能没搜全"设计的；改成结构化基线注入
# （get_baseline_digest 尽力覆盖全部已知 mechanism_type）后不再有"搜索没搜全"这层
# 不确定性，只保留"完全没有基线数据"这一档。
_ZMX_ZERO_NOTE = {
    "campaign": "注意：当前 Zoomex 基线数据不足，无法进行差异判断，zmx_comparison 的 diff_type 必须填「不适用」。",
    "product": "注意：当前 Zoomex 基线数据不足，zmx_comparison 的 diff_type 必须填「不适用」。",
    "listing": "注意：Zoomex 上币基线数据不足，diff_type 必须填「不适用」。",
}


def build_zmx_note(category: str, zmx_count: int) -> str:
    """category=delisting 不带 ZMX 部分，不应该调用本函数。"""
    if zmx_count == 0:
        return _ZMX_ZERO_NOTE[category]
    return ""


_HIGH_VALUE_RE = re.compile(
    r"\d|reward|prize|bonus|pool|start|end|extend|eligible|require|rule|fee|"
    r"launch|support|available|用户|奖励|奖池|规则|资格|开始|结束|延期|手续费",
    re.IGNORECASE,
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|\n+", text or "") if s.strip()]


def _compress_content(text: str, max_chars: int) -> str:
    """保留首尾和含数字/时间/奖励/规则的高价值句，避免简单截断丢掉活动条款。"""
    sentences = _sentences(text)
    if not sentences:
        return ""
    picked: list[str] = []
    candidate_indices = [0, *[i for i, s in enumerate(sentences) if _HIGH_VALUE_RE.search(s)], len(sentences) - 1]
    for i in candidate_indices:
        sentence = sentences[i]
        if sentence not in picked:
            picked.append(sentence)
        if len("\n".join(picked)) >= max_chars:
            break
    return "\n".join(picked)[:max_chars]


def _compact_diff(old: str, new: str, max_chars: int = 1000) -> str:
    old_lines = _sentences(old)
    new_lines = _sentences(new)
    changes = [line for line in ndiff(old_lines, new_lines) if line.startswith(("+ ", "- "))]
    return "\n".join(changes)[:max_chars]


def build_articles_block(rows: list, old_content_by_uid: dict[str, Optional[str]], max_chars: int = 2400) -> str:
    """紧凑证据块：正文按高价值句压缩；changed 仅附句子级 diff，不重复完整旧正文。"""
    parts: list[str] = []
    for i, row in enumerate(rows, start=1):
        content = _compress_content(row["content"] or "", max_chars)
        lines = [
            f"[{i}] uid={row['uid']} | status={row['status']}",
            f"title={row['title']}",
            f"evidence={content}",
        ]
        if row["status"] == "changed":
            old_content = old_content_by_uid.get(row["uid"])
            if old_content:
                lines.append(f"diff(-before/+after)={_compact_diff(old_content, row['content'] or '')}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "（本批次无公告，不应该发生）"


def build_zmx_block(entries: list["ZmxBaselineEntry"]) -> str:
    if not entries:
        return "（无 Zoomex 基线记录）"
    lines = [
        f"[Z{i}] 类型：{e.mechanism_type} | UID: {e.uid} | 标题：{e.title} | "
        f"机制：{e.key_mechanics or '无'} | 奖励：{e.reward_range or '无'} | "
        f"目标用户：{e.target_users or '无'} | 时间：{e.start_date or '?'}~{e.end_date or '?'}"
        for i, e in enumerate(entries, start=1)
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
    zmx_hits: Optional[list["ZmxBaselineEntry"]] = None,
    article_content_chars: int = 2400,
) -> BuiltPrompt:
    if category not in _TEMPLATES:
        raise ValueError(f"category {category!r} 不使用 LLM；仅 campaign/product 支持分析")

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

    hits = zmx_hits or []
    variables["ZMX_COUNT"] = str(len(hits))
    variables["ZMX_BLOCK"] = build_zmx_block(hits)
    variables["ZMX_NOTE"] = build_zmx_note(category, len(hits))

    user = render(user_template, variables)
    return BuiltPrompt(system=system, user=user)


# ============================================================
# staged-v1：事实抽取与业务判断分离
# ============================================================

SYSTEM_FACT_EXTRACTION = """\
你是交易所公告事实抽取器。只根据输入证据提取事实，不评价商业价值，不与 Zoomex
比较，不使用外部知识。没有明确证据的字段返回 null，不得推测。输出必须是合法 JSON。"""

SYSTEM_BUSINESS_JUDGMENT = """\
你是 Zoomex 竞品情报分析师。article_facts 已经过事实抽取，不要重新解释原文或引入
外部事实。只判断业务影响、与给定候选基线的差异，并给出结构化可执行行动。
baseline_not_found 只表示 90 天公告基线未发现记录，不得表述为已确认产品缺失。
输出必须是合法 JSON。"""


def build_fact_extraction_prompt(
    *,
    index: int,
    category: str,
    status: str,
    title: str,
    preprocessed: dict,
) -> BuiltPrompt:
    """每篇独立短 Prompt；只返回批次内整数 i，不回传 UID/title。"""
    payload = {
        "i": index,
        "category": category,
        "status": status,
        "title": title,
        "current_content": preprocessed["content"],
        "content_diff": preprocessed["diff"],
        "pre_extracted_candidates": preprocessed["candidates"],
    }
    schema = {
        "i": index,
        "event_type": (
            "created/reward_changed/rule_changed/extended/ended/cancelled/"
            "other_updated/unknown"
        ),
        "mechanism": "string|null",
        "feature": "string|null",
        "start_at": "ISO8601|null",
        "end_at": "ISO8601|null",
        "reward": {"amount": "number|null", "currency": "string|null", "type": "string|null"},
        "eligibility": "string|null",
        "target_users": ["string"],
        "changes": [{"field": "string", "before": "any|null", "after": "any|null"}],
        "evidence": ["输入中的短原句"],
        "confidence": "0..1",
    }
    user = (
        "input:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\nrequired_schema:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        + "\n规则：i 原样返回；evidence 最多5条；没有证据填 null/[]；不得输出额外字段。"
    )
    return BuiltPrompt(system=SYSTEM_FACT_EXTRACTION, user=user)


def build_business_judgment_prompt(
    *,
    batch_date: str,
    locale: str,
    source: str,
    category: str,
    facts: list[dict],
    candidates_by_index: dict[int, list["ZmxBaselineEntry"]],
) -> BuiltPrompt:
    """比较阶段不再读取公告原文，每篇最多注入自己的 Top 4 候选。"""
    items = []
    for fact in facts:
        index = int(fact["i"])
        candidates = [
            {
                "z": pos,
                "mechanism_type": entry.mechanism_type,
                "mechanics": entry.key_mechanics,
                "reward": entry.reward_range,
                "target_users": entry.target_users,
            }
            for pos, entry in enumerate(candidates_by_index.get(index, []), start=1)
        ]
        items.append({"i": index, "facts": fact, "zmx_candidates": candidates})
    payload = {
        "date": batch_date,
        "locale": locale,
        "source": source,
        "category": category,
        "items": items,
    }
    output = {
        "items": [{
            "i": "integer",
            "gap_type": (
                "confirmed_gap/baseline_not_found/different_mechanism/covered/not_applicable"
            ),
            "business_impact": "high/medium/low",
            "novelty": "0..3",
            "urgency": "0..3",
            "zmx_evidence": ["candidate z integer"],
            "reason": "一句，必须含输入事实",
            "action_type": (
                "no_action/monitor/manual_verify/benchmark/campaign_design/product_evaluation"
            ),
            "owner": "campaign_ops/product/regional_ops",
            "action": "具体动作|null",
            "deliverable": "具体交付物|null",
            "deadline": "within_2_business_days/within_1_week/ongoing|null",
            "needs_human_review": "boolean",
        }]
    }
    user = (
        "input:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\noutput_schema:\n" + json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        + "\n每个 i 恰好返回一次。无候选时 gap_type=baseline_not_found；"
        "不得直接断言 confirmed_gap。action 必须包含对象和动作，禁止只写关注/评估。"
    )
    return BuiltPrompt(system=SYSTEM_BUSINESS_JUDGMENT, user=user)


# ============================================================
# zmx-extract-v1
#
# 跟上面四套（竞品公告 -> 分析）不同，这一套的输入是 Zoomex（我方）自己的公告，
# 任务是把公告结构化提取成 mechanism_type/key_mechanics/reward_range/target_users/
# start_date/end_date 六个字段，不做任何跟竞品的比较判断——比较判断留给上面四套
# prompt 在拿到 zmx_baseline 结构化结果后再做。三个 category（campaign/product/
# listing）共用同一套模板，字段形状相同，category 只是上下文变量。见
# src/analysis/zmx_baseline.py。
# ============================================================

SYSTEM_ZMX_EXTRACT = (
    "你是负责梳理 Zoomex（我方）自身活动/产品/上币公告的助手，任务是把公告结构化"
    "提取成简短字段，供后续跟竞品做对比参考，不做任何竞品比较判断。输出必须是合法 "
    "JSON，不包含任何 markdown 标记或解释文字。"
)

USER_ZMX_EXTRACT_TEMPLATE = """\
【类目】{CATEGORY}
【地区/语言】{LOCALE}

【已使用过的玩法类型标签】
{EXISTING_LABELS}
（如果本批次里有语义相同的玩法，请直接复用上面列出的同一个标签；仅措辞不同、玩法
本质相同时不要新建近义标签；确实是新玩法时才新建标签。）

【待提取公告列表】
{ARTICLES_BLOCK}
（每条格式：
[index] UID: uid
标题：title
正文：content）

【提取任务】
请输出以下结构的 JSON：

{
  "articles": [
    {
      "uid": "（原样照抄，不得修改）",
      "mechanism_type": "玩法类型标签，简短（4-8 个汉字），优先复用上面列出的已有标签；无法判断类型时填「其他」",
      "key_mechanics": "玩法机制一句话，门槛/规则从正文提取，不可编造",
      "reward_range": "奖励范围，如「5-50 USDT」「最高 5000 USDT 奖池」，正文未提供填 null",
      "target_users": "目标用户群，如「所有用户」「新注册用户」，正文未提供填 null",
      "start_date": "起始日期，格式 YYYY-MM-DD，正文未提供填 null",
      "end_date": "结束日期，格式 YYYY-MM-DD，正文未提供填 null"
    }
  ]
}

【强制规则】
1. uid 字段原样照抄，不得改动任何字符
2. mechanism_type 不得为空字符串，无法判断时填「其他」
3. 不得使用 LLM 自身知识补充正文未提及的信息
4. 整个输出必须是合法 JSON，不加任何注释（// 或 /* */ 均不允许）
"""


def build_extraction_articles_block(rows: list, max_chars: int = 4000) -> str:
    """rows 是 announcements 的行（需要 uid/title/content 列）。"""
    parts: list[str] = []
    for i, row in enumerate(rows, start=1):
        content = (row["content"] or "")[:max_chars]
        parts.append(f"[{i}] UID: {row['uid']}\n标题：{row['title']}\n正文：{content}")
    return "\n\n".join(parts) if parts else "（本批次无公告，不应该发生）"


def build_existing_labels_block(labels: list[str]) -> str:
    if not labels:
        return "（暂无已使用过的标签，这是第一批提取）"
    return "、".join(labels)


def build_extraction_prompt(
    *,
    category: str,
    locale: str,
    rows: list,
    existing_labels: list[str],
    article_content_chars: int = 4000,
) -> BuiltPrompt:
    variables = {
        "CATEGORY": category,
        "LOCALE": locale,
        "EXISTING_LABELS": build_existing_labels_block(existing_labels),
        "ARTICLES_BLOCK": build_extraction_articles_block(rows, max_chars=article_content_chars),
    }
    user = render(USER_ZMX_EXTRACT_TEMPLATE, variables)
    return BuiltPrompt(system=SYSTEM_ZMX_EXTRACT, user=user)


# ============================================================
# daily-digest-v1
#
# 不同于上面四套（每个 category×locale 一批公告 → 一次分析），这一套的输入不是
# 公告原文，而是「当天这个 locale 已经产出的全部批次分析结果」（batch_summary +
# zmx_diff），任务是综合归纳出一段跨类目/跨来源的当日简报，不是重新分析公告。
# 见 src/analysis/daily_digest.py。
# ============================================================

SYSTEM_DAILY_DIGEST = (
    "你是竞品情报平台的日报编辑，负责把当天各个类目已经产出的批次分析结果综合成一段"
    "给运营/产品团队看的每日简报。你不会看到原始公告正文，只看到每个批次已经写好的"
    "summary 和 ZMX 差异结论，你的任务是提炼、串联、突出重点，不是重新分析公告本身。"
    "输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。"
)

USER_DAILY_DIGEST_TEMPLATE = """\
【日期】{BATCH_DATE}
【地区】{LOCALE}
【本日已产出批次数】{BATCH_COUNT}

【本日批次列表】
{BATCHES_BLOCK}
（每条格式：
[index] 来源：source | 类目：category | 公告数：n | diff_type | priority
摘要：batch_summary
ZMX 对比：zmx_diff（如有）
）

【分析任务】
请输出以下结构的 JSON：
{
  "daily_summary": "3-5 句话的当日综述，串联今天各类目/来源里最重要的信号，指出运营/产品团队今天最该关注什么。禁止逐条复述每个批次，要综合归纳出跨批次的规律或对比（例如「今天多个竞品集中冲刺活动，Bitunix/BingX 均以 USDT 奖池为主，但只有 Lbank 在下架侧有动作」这类归纳性判断，不是把每条 batch_summary 抄一遍）。",
  "priority_focus": "一句话点出今天最优先应该看的 1-2 条，引用具体来源和类目。"
}

【强制规则】
1. 只能基于【本日批次列表】里提供的信息做归纳，不能编造列表之外的公告或结论
2. daily_summary 必须综合多条批次，不是简单罗列或直接复制某一条 batch_summary
3. 整个输出必须是合法 JSON
"""


def build_batches_block(batches: list[dict]) -> str:
    """batches 每项需要 source/category/article_count/diff_type/priority/summary/
    zmx_diff 字段（来自 insights 表已产出的批次，不是 announcements 原文）。"""
    if not batches:
        return "（本日无任何批次，不应该发生）"
    parts = []
    for i, b in enumerate(batches, start=1):
        lines = [
            f"[{i}] 来源：{b['source']} | 类目：{b['category']} | 公告数：{b['article_count']} | "
            f"diff_type：{b.get('diff_type') or '（无）'} | priority：{b.get('priority') or '（无）'}",
            f"摘要：{b.get('summary') or '（无）'}",
        ]
        if b.get("zmx_diff"):
            lines.append(f"ZMX 对比：{b['zmx_diff']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_daily_digest_prompt(locale: str, batch_date: str, batches: list[dict]) -> BuiltPrompt:
    variables = {
        "LOCALE": locale,
        "BATCH_DATE": batch_date,
        "BATCH_COUNT": str(len(batches)),
        "BATCHES_BLOCK": build_batches_block(batches),
    }
    user = render(USER_DAILY_DIGEST_TEMPLATE, variables)
    return BuiltPrompt(system=SYSTEM_DAILY_DIGEST, user=user)
