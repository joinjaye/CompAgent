"""LLM prompt 构建：staged-v1 竞品分析（事实抽取 + 业务判断分离，见 run.py）+
zmx-catalog-extract-v1（Zoomex 能力目录提取，见 zmx_catalog.py）+ daily-digest-v1
（当日综述，见 daily_digest.py）。

变量替换只认形如 {ALL_CAPS_NAME} 的占位符（正则 `\\{[A-Z][A-Z0-9_]*\\}`），公告
正文/标题这些不受信任的自由文本先各自拼进 ARTICLES_BLOCK 字符串，再作为整体值填入
模板——不用 str.format()，避免正文里偶然出现的 "{" "}" 破坏格式化或触发 KeyError
（爬来的公告原文不可控，JSON 示例里的花括号也不能被误当成占位符）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # zmx_catalog.py 反过来要 import 本模块的 build_catalog_extraction_prompt（提取
    # prompt 复用这里的 render()/BuiltPrompt），运行时互相 import 会循环，这里只在
    # 类型检查时导入（配合文件顶部 `from __future__ import annotations`，注解本身在
    # 运行时是字符串，不需要真的把类拿到）。
    from src.analysis.zmx_catalog import ZmxCatalogEntry

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def render(template: str, variables: dict[str, str]) -> str:
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        return variables[key] if key in variables else match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template)


@dataclass
class BuiltPrompt:
    system: str
    user: str


# ============================================================
# staged-v1：事实抽取与业务判断分离
# ============================================================

SYSTEM_FACT_EXTRACTION = """\
你是交易所公告事实抽取器。只根据输入证据提取事实，不评价商业价值，不与 Zoomex
比较，不使用外部知识。没有明确证据的字段返回 null，不得推测。输出必须是合法 JSON。"""

SYSTEM_BUSINESS_JUDGMENT = """\
你是 Zoomex 竞品情报分析师。article_facts 已经过事实抽取，不要重新解释原文或引入
外部事实。只判断业务影响、以及与给定候选目录条目的差异。
baseline_not_found 只表示没有找到匹配的 Zoomex 能力目录候选，不得表述为已确认产品
缺失（目录本身覆盖 Zoomex 全量历史，但候选是词项重叠召回的，召回不到不等于目录里
真的没有）。gap_type 与 reason 必须自洽：reason 中一旦点名具体候选（如"候选 z2"）
并描述其与本文的异同，就说明确实在跟某个候选做比较，gap_type 不得再判
baseline_not_found/not_applicable，必须从 confirmed_gap/different_mechanism/covered
里选一个，并把该候选序号写进 zmx_evidence；反过来，若 gap_type 判定为
baseline_not_found/not_applicable，reason 只能泛述"候选类型均不匹配"，不得点名
任何具体候选序号。不产出任何行动建议/负责人/时限——这些由下游规则程序化派生，
不是你的职责。输出必须是合法 JSON。"""


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
    candidates_by_index: dict[int, list["ZmxCatalogEntry"]],
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
        }]
    }
    user = (
        "input:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\noutput_schema:\n" + json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        + "\n每个 i 恰好返回一次。无候选时 gap_type=baseline_not_found；"
        "不得直接断言 confirmed_gap。reason 点名某个候选序号时，zmx_evidence 必须"
        "包含该序号且 gap_type 不得为 baseline_not_found/not_applicable。不产出"
        "output_schema 之外的任何字段（尤其不产出行动建议/负责人/时限，这些由下游"
        "规则派生）。"
    )
    return BuiltPrompt(system=SYSTEM_BUSINESS_JUDGMENT, user=user)


# ============================================================
# zmx-catalog-extract-v1（Phase①，取代 zmx-extract-v1）
#
# 跟上面四套（竞品公告 -> 分析）不同，这一套的输入是 Zoomex（我方）自己的公告，
# 任务是把公告结构化提取成能力目录条目，不做任何跟竞品的比较判断——比较判断留给
# 竞品分析阶段在拿到 zmx_catalog 结构化结果后再做。关键变化（相对旧的
# zmx-extract-v1）：mechanism_type 不再是 LLM 自由生成的中文标签，而是
# config/zmx_mechanism_taxonomy.yaml 定义的封闭/半封闭枚举 key——遇到不匹配的
# 一律落 "other" + raw_mechanism_label 记录原始描述，不允许自造新类型。campaign/
# product 字段形状不同（product 没有奖励/时间字段，多了功能覆盖范围字段），
# 由 category 参数决定渲染哪一套 schema。见 src/analysis/zmx_catalog.py。
# ============================================================

SYSTEM_ZMX_CATALOG_EXTRACT = (
    "你是负责梳理 Zoomex（我方）自身活动/产品公告的助手，任务是把公告结构化提取成"
    "能力目录条目，供后续跟竞品做对比参考，不做任何竞品比较判断。mechanism_type 必须"
    "从给定的封闭枚举里选择，不得自造新类型；确实不匹配任何枚举时才填 other，并在"
    "raw_mechanism_label 里如实描述这是什么。输出必须是合法 JSON，不包含任何 markdown "
    "标记或解释文字。"
)

USER_ZMX_CATALOG_EXTRACT_TEMPLATE = """\
【类目】{CATEGORY}
【地区/语言】{LOCALE}

【可选的机制/功能类型枚举】
{TAXONOMY_BLOCK}
（必须从上面列出的 key 中选择一个填入 mechanism_type；如果确实没有任何一个匹配，
填 "other" 并在 raw_mechanism_label 里用几个字描述这到底是什么，不允许自己发明一个
新的 key。）

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
      "mechanism_type": "上面枚举里的 key（英文），或 other",
      "raw_mechanism_label": "仅 mechanism_type=other 时填，简短描述这是什么类型，否则填 null",
      "core_summary": "核心内容一句话，从正文提取，不可编造",
      "key_mechanics": "玩法/功能机制一句话，门槛/规则从正文提取，不可编造",
{ARTICLE_FIELDS}
    }
  ]
}

【强制规则】
1. uid 字段原样照抄，不得改动任何字符
2. mechanism_type 只能是枚举里的 key 或 other，不得自造新值
3. mechanism_type=other 时 raw_mechanism_label 不得为空
4. 不得使用 LLM 自身知识补充正文未提及的信息，提取不到的字段填 null
5. 整个输出必须是合法 JSON，不加任何注释（// 或 /* */ 均不允许）
"""

_CAMPAIGN_ARTICLE_FIELDS = """\
      "reward_form": "奖励形式，如「USDT 奖池」「代币空投」「加息券」，提取不到填 null",
      "reward_amount": "奖励数额，如「1500」，提取不到填 null",
      "reward_token": "奖励币种，如「USDT」，提取不到填 null",
      "target_users": "目标用户群，如「所有用户」「新注册用户」，提取不到填 null",
      "entry_threshold": "参与门槛，如「充值满 100 USDT」，提取不到填 null",
      "start_date": "起始日期，格式 YYYY-MM-DD，提取不到填 null",
      "end_date": "结束日期，格式 YYYY-MM-DD，提取不到填 null"\
"""

_PRODUCT_ARTICLE_FIELDS = """\
      "main_feature": "核心功能点一句话，提取不到填 null",
      "target_users": "目标用户群，如「合约交易用户」「API 接入用户」，提取不到填 null",
      "entry_threshold": "使用门槛，如「VIP3 以上」，无门槛或提取不到填 null",
      "supported_market": "适用市场，字符串数组，如 [\\"spot\\", \\"perp\\"]，提取不到填 []",
      "supported_token": "适用币种，字符串数组，提取不到填 []",
      "supported_platform": "适用平台，字符串数组，如 [\\"web\\", \\"app\\", \\"api\\"]，提取不到填 []",
      "supported_user_tier": "适用用户等级，字符串数组，提取不到填 []"\
"""

_CATALOG_ARTICLE_FIELDS_BY_CATEGORY = {
    "campaign": _CAMPAIGN_ARTICLE_FIELDS,
    "product": _PRODUCT_ARTICLE_FIELDS,
}


@dataclass
class TaxonomyCategory:
    key: str
    name: str
    definition: str
    examples: list[str]


@dataclass
class TaxonomySpec:
    category: str
    method: str  # semi_closed | fixed
    entries: list[TaxonomyCategory]


def build_taxonomy_block(taxonomy: TaxonomySpec) -> str:
    lines = []
    for entry in taxonomy.entries:
        example_text = "；".join(entry.examples[:2]) if entry.examples else "（无示例）"
        lines.append(f"- {entry.key}（{entry.name}）：{entry.definition}。示例：{example_text}")
    return "\n".join(lines)


def build_catalog_extraction_articles_block(rows: list, max_chars: int = 4000) -> str:
    """rows 是 announcements 的行（需要 uid/title/content 列）。"""
    parts: list[str] = []
    for i, row in enumerate(rows, start=1):
        content = (row["content"] or "")[:max_chars]
        parts.append(f"[{i}] UID: {row['uid']}\n标题：{row['title']}\n正文：{content}")
    return "\n\n".join(parts) if parts else "（本批次无公告，不应该发生）"


def build_catalog_extraction_prompt(
    *,
    category: str,
    locale: str,
    rows: list,
    taxonomy: TaxonomySpec,
    article_content_chars: int = 4000,
) -> BuiltPrompt:
    if category not in _CATALOG_ARTICLE_FIELDS_BY_CATEGORY:
        raise ValueError(f"category {category!r} 不支持能力目录提取；仅 campaign/product")
    variables = {
        "CATEGORY": category,
        "LOCALE": locale,
        "TAXONOMY_BLOCK": build_taxonomy_block(taxonomy),
        "ARTICLE_FIELDS": _CATALOG_ARTICLE_FIELDS_BY_CATEGORY[category],
        "ARTICLES_BLOCK": build_catalog_extraction_articles_block(rows, max_chars=article_content_chars),
    }
    user = render(USER_ZMX_CATALOG_EXTRACT_TEMPLATE, variables)
    return BuiltPrompt(system=SYSTEM_ZMX_CATALOG_EXTRACT, user=user)


# ============================================================
# daily-digest-v4
#
# 不同于上面四套（每个 category×locale 一批公告 → 一次分析），这一套的输入不是
# 公告原文，而是「当天这个 locale 已经产出的全部批次分析结果」（batch_summary +
# zmx_diff），任务是综合归纳出一段跨类目/跨来源的当日简报，不是重新分析公告。
# 见 src/analysis/daily_digest.py。
# ============================================================

SYSTEM_DAILY_DIGEST = (
    "你是加密交易所竞品情报平台的日报编辑，负责把当前批次各平台、类目和市场的分析结果"
    "综合成给运营/产品团队看的极简 Insight。你不会看到完整原始公告，只能使用批次摘要和"
    "结构化信号；你的任务是归纳共同特征与差异，不是逐条复述，也不能把公告数当成用户数。"
    "输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。"
)

USER_DAILY_DIGEST_TEMPLATE = """\
【日期】{BATCH_DATE}
【市场范围】{LOCALE}
【本日已产出批次数】{BATCH_COUNT}

【本日批次列表】
{BATCHES_BLOCK}
（每条格式：
[index] 来源：source | 市场：locale | 类目：category | 公告数：n | diff_type | priority
摘要：batch_summary
结构化信号：signals
ZMX 对比：zmx_diff（如有）
）

【分析任务】
请输出以下结构的 JSON：
{
  "daily_summary": "严格 2-4 句话的整体综述。按有信息才写的原则覆盖：活动侧的主要玩法/奖励特点；产品侧的主要能力或更新特点；市场侧的活跃区域或区域差异；最后概括整体以新增、内容调整还是上下币为主。不要重复页面上已经单独展示的领先平台数字。",
  "campaign_summary": "严格 2-4 句话的 Campaign 专项总结，综合说明活动类型、重点市场、主要玩法、奖励趋势和整体特点。没有可靠奖励变化时如实说明，不得编造金额或趋势。",
  "product_summary": "严格 2-4 句话的产品核心能力与内容总结，综合说明主要产品能力、重点市场、新产品与功能/规则更新的结构，以及整体产品变化特点。",
  "priority_focus": null
}

【强制规则】
1. 只能基于【本日批次列表】里提供的信息做归纳，不能编造列表之外的公告或结论
2. 三个 summary 都必须各自是 2-4 个完整句子，综合多条批次，不能逐条列平台流水账
3. 活动、产品、市场某一维度没有可靠信号时直接省略，禁止写“暂无”来凑句数
4. 同一业务事件可能有多个语言版本；只做定性归纳，不把跨语言 article_count 相加后声称为全局唯一事件数
5. 不输出行动建议、优先级建议或 Follow-up
6. “未检索到同类”时只概括没有直接可比能力，不复述被判定为不相关的候选名称、类型或内容
7. 整个输出必须是合法 JSON
"""


def build_batches_block(batches: list[dict]) -> str:
    """batches 每项需要 source/category/article_count/diff_type/priority/summary/
    zmx_diff 字段（来自 insights 表已产出的批次，不是 announcements 原文）。"""
    if not batches:
        return "（本日无任何批次，不应该发生）"
    parts = []
    for i, b in enumerate(batches, start=1):
        lines = [
            f"[{i}] 来源：{b['source']} | 市场：{b.get('locale') or 'ALL'} | 类目：{b['category']} | 公告数：{b['article_count']} | "
            f"diff_type：{b.get('diff_type') or '（无）'} | priority：{b.get('priority') or '（无）'}",
            f"摘要：{b.get('summary') or '（无）'}",
            f"结构化信号：{b.get('signals') or '（无）'}",
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
