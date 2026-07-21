# CLAUDE.md

本文件是每次 Claude Code session 开始时必读的项目上下文。每个 Phase 结束后必须更新本文件，
让下一个 session 能自然接续，不需要重新解释背景。

## 项目背景

竞品情报平台（Python）。自动采集 6 家加密交易所公告中心的内容 → 清洗去重 → 分类 → LLM 分析 →
同步飞书多维表 → 按区域分群推送飞书群日报。为运营和产品团队提供持续的竞品情报支持。

### 竞品与语言范围

| 交易所 | 语言 | 角色 |
|---|---|---|
| Bitunix | EN, FR, ID | 竞品 |
| Weex | EN, FR | 竞品 |
| BingX | EN, VN | 竞品 |
| Phemex | EN, FR | 竞品 |
| Lbank | EN, VN, ID | 竞品 |
| Zoomex | EN, FR, EN-Asia, VN, ID | 我方，对比基线，不是竞品 |

### 数据流

```
竞品公告 API / HTML
       ↓
  采集器（Collectors）── watermark 模式 / full_scan 模式 + 清洗（HTML/富文本 → 纯文本）
       ↓
  SQLite（唯一真相源）
       ↓
  归组 & 打标（Pipeline）── 归组 / 分类 / 地区独占标记
       ↓
  LLM 分析（Analysis）── summary / ZMX 差异
       ↓
  飞书多维表（业务视图） + 可视化看板（静态 HTML）
       ↓
  飞书群日报（按 locale 分群推送）
```

## 核心设计约束（必须遵守，任何 Phase 都不得违反）

1. **SQLite 是唯一真相源**。飞书多维表只是同步出去的业务视图。所有重跑、补数、改分类只操作
   SQLite，不直接改飞书表。
2. **两种增量采集策略，按源选择**（记录在 `crawl_state.strategy` 与 `sources.yaml`）：
   - **watermark 模式（默认）**：按源端 `update_time` 做水位线，只拉 `update_time > high_watermark`
     的内容。适用于 API 返回可靠 `update_time` 的源。
   - **full_scan 模式（fallback）**：拉前 N 页，用 `content_hash` 检测变更。适用于无 `update_time`
     或该字段不可靠（如恒等于 `post_time`）的源。
   - 两种模式都保留 `content_hash` 二次校验：同一 URL 的正文若 hash 变了，识别为「变更」，旧版本
     存入 `content_history`，不覆盖丢失。`content_hash` 的语义是**清洗后正文**的 SHA256
     （清洗在采集层完成，见下方 schema 表格与「Phase 2.5 完成情况」），不是原始 HTML 的 hash。
   - 【2026-07-14 政策调整，详见「水位逻辑策略调整」】`--force-full`（全量回填/全量核查）
     默认关闭（`force_full=False`），**只有 Zoomex 保留全量回填能力**（我方基线，需要定期
     全量核查兜底）。其余全部 full_scan 策略的源（Weex/BingX/Phemex/Lbank）翻页/列表机制
     本身大多没有可靠的"多翻几页"空间，`fetch_list()` 固定只拉一个有限窗口（Weex 受
     `pagination.max_pages` 限制；BingX/Phemex/Lbank 连真正的分页接口都没有，只有固定的
     一屏/一页，`force_full` 对这三个源是 no-op，如实记录，不假装支持全量历史回填）。
     Bitunix 的 watermark 早停机制不受这次调整影响，仍然正常工作。
3. **跨语言归组（`group_id`）**：同一竞品同一条公告的多语言版本归为一组。归组用于分析层
   （跨区域对比、地区独占识别），**不用于推送去重**。
4. **推送按 locale 分群**：每个 locale 对应一个独立的飞书群，各群独立推送，不做跨语言去重。
   因为 `announcements` 表本身按 `(source, locale, article_id)` 分行，`push_status` 天然就是
   per-locale 的，不需要额外的多值结构。
5. **合规**：遵守 robots.txt、控制请求频率（`sources.yaml` 里的 `rate_limit_ms`）、不绕过登录墙、
   不抓非公开内容。
6. **不允许猜测数据**：任何 API endpoint、字段映射都必须来自实测（curl 请求 + 真实响应），
   不允许凭记忆编造。Phase 1 及之后如遇到不确定的字段，如实记录为待验证，不要假装已验证。

## 目录结构

```
├── CLAUDE.md                  # 本文件
├── README.md                   # 项目简介（业务背景，人读）
├── phasePrompts.md             # Phase 0-8 的完整任务 prompt 存档
├── pytest.ini
├── requirements.txt            # 运行时依赖（PyYAML, certifi）
├── requirements-dev.txt        # + pytest
├── config/
│   ├── sources.yaml            # 数据源配置（Phase 1 填充：endpoint / 策略 / 字段映射）
│   ├── category_mapping.yaml   # 各源 raw_category → 我方 category 映射（Phase 2.5 起草、Phase 2.6
│   │                            #   订正为按 raw_category 原始值做 key，Phase 3 消费）
│   ├── push_targets.yaml       # locale → 飞书群 webhook 映射
│   ├── push_rules.yaml         # 推送规则（配置化，Phase 6 消费）
│   ├── analysis.yaml           # Phase 4 新增：LLM 非敏感参数（temperature/max_tokens/
│   │                            #   prompt_versions/zmx_index/content_truncation）
│   └── .env.example            # 飞书 / LLM 凭证模板
├── src/
│   ├── db/                     # SQLite schema & 操作层（Phase 0，已完成）
│   │   ├── schema.sql
│   │   ├── connection.py       # connect / get_connection / init_db
│   │   ├── operations.py       # upsert_announcement / crawl_state 读写
│   │   └── __main__.py         # `python -m src.db init`
│   ├── probe/                   # Phase 1 数据源验活 CLI（不是采集器）
│   ├── collectors/              # 每个交易所一个 adapter（Phase 2，✅ 批次 4/4 全部完成）
│   │   ├── http.py             # 通用 HTTP 客户端（超时重试 + certifi + rate_limit_seconds()）
│   │   ├── timeutil.py         # 时间格式转换（unix ms/带偏移 ISO <-> UTC ISO8601）
│   │   ├── base.py             # BaseCollector：fetch_list/fetch_detail/normalize/needs_detail 契约 + run() 编排
│   │   ├── zendesk_base.py     # Zendesk Help Center 通用采集逻辑（Bitunix 用；Weex 已于
│   │   │                        #   2026-07-14 迁移出去，见下方 weex.py）
│   │   ├── bitunix.py          # ✅ 批次 1（Zendesk）
│   │   ├── weex.py             # ✅ 批次 1；2026-07-14 起改为独立实现，解析 www.weex.com
│   │   │                        #   前台页面（原 Zendesk API 已过期，见「Weex 数据源迁移」）
│   │   ├── zoomex.py           # ✅ 批次 2（我方基线，多分类 menu_id，full 全量回填保留）
│   │   ├── bingx.py            # ✅ 批次 4（首屏 NUXT_DATA 聚合视图，force_full no-op）
│   │   ├── phemex.py           # ✅ 批次 4（3 分类 news/activities/newsletter，force_full no-op）
│   │   ├── lbank.py            # ✅ 批次 4（真实 JSON API notice/latestList，2026-07-14 重写，
│   │   │                        #   已替代早期 RSC flight 流方案，旧版 lbank_web.py 已删除）
│   │   ├── bitunix_activity.py # ✅ 2026-07-20 新增：Bitunix 活动中心端口（www.bitunix.com/
│   │   │                        #   activity/act-center），跟常规 bitunix.py 并集写入同一 source
│   │   ├── lbank_events.py     # ✅ 2026-07-20 新增：Lbank 精选活动端口（new-popular-events）
│   │   ├── weex_rewards.py     # ✅ 2026-07-20 新增：Weex 活动奖励端口（/rewards）
│   │   ├── bingx_events.py     # ✅ 2026-07-20 新增：BingX 活动中心端口（/events），本项目
│   │   │                        #   唯一浏览器驱动（Playwright）的采集器，见「补充活动类内容
│   │   │                        #   采集端口」一节
│   │   └── __main__.py         # `python -m src.collectors --source <x> --locale <y> [--category <c>] [--force-full]`
│   ├── parsers/                  # 每种响应格式一个 parser，离线可单测
│   │   ├── zendesk.py          # ✅ Bitunix 用（标准 Zendesk articles.json，cursor 分页）
│   │   ├── zoomex.py           # ✅ getArticleListByMenuId / getArticleById 响应解析（按 lang 匹配 contents[]）
│   │   ├── slate_json.py       # ✅ Zoomex 详情 content 字段：Slate.js 富文本 JSON → 纯文本（保留表格结构）
│   │   ├── html_text.py        # ✅ HTML → 纯文本（保留表格结构，跟 slate_json.py 同一套
│   │   │                        #   表格表示法），Bitunix/BingX/Phemex/Lbank 共用
│   │   ├── weex_web.py         # ✅ www.weex.com 前台页面解析（RSC flight 流 + zendesk-html div）
│   │   ├── bingx_web.py        # ✅ 批次 4：BingX __NUXT_DATA__ devalue 格式解析（手写最小
│   │   │                        #   解引用器，非第三方 devalue 库）
│   │   ├── phemex_web.py       # ✅ 批次 4：Phemex window.preloadedData 宽松 JS 对象字面量
│   │   │                        #   解析（手写字符级解析器，不是正则替换）
│   │   ├── lbank.py            # ✅ Lbank notice/latestList 响应解析（2026-07-14 重写）
│   │   ├── bitunix_activity.py # ✅ 2026-07-20 新增：devalue（EN）+ 明文 __custom__nuxt__payload
│   │   │                        #   （FR/ID）双格式解析，同一份代码
│   │   ├── lbank_events.py     # ✅ 2026-07-20 新增：RSC flight 流提取，复用 weex_web.py 思路
│   │   └── weex_rewards.py     # ✅ 2026-07-20 新增：标准 __NEXT_DATA__（列表+详情）
│   ├── pipeline/                # 跨语言归组、分类打标（Phase 3；清洗已在 Phase 2.5 前移到采集层）
│   ├── analysis/                 # ✅ Phase 4：批次级 LLM summary & ZMX 差异
│   │   ├── config.py           # analysis.yaml + .env 加载（LlmCredentials）
│   │   ├── zmx_index.py        # Zoomex 基线 TF-IDF 检索（纯 Python，不依赖 sklearn）
│   │   ├── batch.py            # 批次 PK + locale 复用判断（can_derive_from_en）
│   │   ├── prompts.py          # campaign/product/listing/delisting 四套 prompt 模板
│   │   ├── llm.py              # OpenAI 兼容 HTTP 调用 + 入库前校验 + llm_cache 缓存
│   │   ├── run.py              # 批次编排 + CLI（含 main()）
│   │   ├── daily_digest.py     # ✅ Phase 7：跨类目当日 Summary 机制（prompt 已实现，
│   │   │                        #   本次未真实调用 LLM，见「Phase 7 完成情况」）
│   │   └── __main__.py         # `python -m src.analysis`
│   ├── sinks/
│   │   ├── feishu_bitable.py   # 多维表同步（Phase 5）
│   │   └── feishu_bot.py       # ✅ 区域 tab 截图 -> 飞书群推送（见「Phase 7 之后：飞书群截图推送」，
│   │                            #   不是原计划 Phase 6 的逐条规则推送引擎，那个仍待开始）
│   └── dashboard/               # ✅ Phase 7：SQLite → 静态 JSON 导出（可视化本体是 docs/index.html）
│       ├── export_data.py      # build_dashboard_data() / export()，所有查询与聚合逻辑
│       ├── screenshot.py       # Playwright 截图：区域 tab -> PNG，供 feishu_bot.py 推送用
│       └── __main__.py         # `python -m src.dashboard --db-path <db> --out docs/data/dashboard.json`
├── tests/
│   ├── fixtures/                # 每个源的真实响应快照（Phase 1 起填充，供离线单测）
│   ├── parsers/                  # parser 离线单测（Phase 2 起）
│   ├── collectors/               # collector 离线单测，mock HTTP（Phase 2 起）
│   ├── analysis/                  # Phase 4 单测（离线 mock LLM，见「Phase 4 完成情况」）
│   ├── test_db.py               # db 层单测（Phase 0）
│   ├── test_migrate_v2.py        # migrate_v2.py 单测（Phase 2.5）
│   └── test_migrate_v3.py        # migrate_v3.py 单测（Phase 4）
├── data/
│   ├── competitor_intel.db      # SQLite 数据库文件（不入版本控制）
│   └── logs/                    # 每日跑批日志（Phase 8）
├── docs/                        # ✅ Phase 7：GitHub Pages 站点本体（未配置 Pages 发布，见「Phase 7 完成情况」）
│   ├── index.html               # 单文件静态看板，fetch('./data/dashboard.json') 纯前端渲染
│   └── data/dashboard.json      # python -m src.dashboard 的导出产物（需要重新生成，不手改）
└── scripts/
    ├── migrate_v2.py             # schema v1 -> v2 迁移（Phase 2.5，见下文）
    ├── migrate_v3.py             # schema v2 -> v3 迁移：insights 表批次化 + llm_cache（Phase 4）
    ├── build_dashboard_demo_db.py  # Phase 7 专用：合并三个真实库生成 demo 数据（非生产链路）
    ├── generate_mock_insights.py   # Phase 7 专用：为 demo 数据缺口生成模拟 insights（非生产链路）
    ├── run_daily.sh              # 每日跑批入口（Phase 8）
    └── backfill.sh               # 补数脚本（Phase 8）
```

## SQLite Schema

所有时间字段统一存 **UTC ISO8601** 字符串（如 `2026-07-13T02:30:00Z`），不使用 SQLite 原生
`DATETIME` 类型。完整 DDL 见 `src/db/schema.sql`。

### announcements（原始层）

一行 = 一个 `source × locale × article_id` 的公告。同一公告的多语言版本各占一行，用 `group_id` 归组。

| 字段 | 类型 | 说明 |
|---|---|---|
| uid | TEXT PK | `SHA256({source}_{locale}_{article_id})` |
| group_id | TEXT | 跨语言归组，Phase 3 填充 |
| source | TEXT | Bitunix / Weex / BingX / Phemex / Lbank / Zoomex |
| locale | TEXT | EN / FR / ID / VN / EN-Asia |
| article_id | TEXT | 源站原生文章 ID |
| url | TEXT | 原文链接 |
| title | TEXT | |
| content | TEXT | 清洗后正文（纯文本，Phase 2.5 起在采集层清洗，见「Phase 2.5 完成情况」） |
| raw_category | TEXT | 源站原生分类的原始值，不做任何映射转换，数值型转字符串存（Phase 2.5 新增）。Bitunix/Weex 是 Zendesk section_id，Zoomex 是 menu_id，BingX 是 sectionId，Phemex 是抓取子源名（news/activities/newsletter，**不是** `category.name`，locale 相关不稳定，见「Phase 2.6」）；Lbank 恒 NULL。`upsert_announcement` 的 `unchanged` 分支也会更新这一列（Phase 2.6：源端分类归属变了但正文没变时，不算内容变更，不触发 `content_history`/`push_status`，但要跟着更新，否则永远停在第一次抓到的旧值） |
| content_hash | TEXT | `SHA256(content)`，变更检测用；**清洗后正文**的 hash（不是原始 HTML 的 hash） |
| post_time | TEXT | 发布时间，UTC ISO8601 |
| update_time | TEXT | 源端更新时间（如有），UTC |
| fetched_at | TEXT | 本次抓取时间 |
| status | TEXT | new / changed / unchanged |
| category | TEXT | campaign / product / listing / delisting / other，可为 NULL（Phase 3 之前未分类） |
| is_region_exclusive | BOOLEAN | 是否地区独占，默认 false |
| push_status | TEXT | pending / pushed / skipped，默认 pending。**按行天然是 per-locale 的** |
| source_endpoint | TEXT | 来源 API endpoint，便于溯源排障 |

### content_history（变更历史）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| uid | TEXT FK → announcements.uid | ON DELETE CASCADE |
| content_hash | TEXT | 被归档时的旧 hash |
| content | TEXT | 旧版本正文 |
| captured_at | TEXT | 归档时间（= 旧版本的 fetched_at） |

### insights（分析层 / 批次级汇总分析表，schema v3，Phase 4 起）

一行 = 一次「批次」分析结论：同一天同一 `(source, category, locale)` 的全部
`status IN (new, changed)` 公告合并成一次 LLM 调用的产出，**不是逐条公告一行**
（v1/v2 时代的设计已废弃）。PK：`id = SHA256(source || "_" || category || "_" ||
locale || "_" || batch_date)`。详见「Phase 4 完成情况」。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 批次 PK，见上 |
| batch_date | TEXT | UTC date，YYYY-MM-DD |
| source | TEXT | 竞品名 |
| category | TEXT | campaign / product / listing / delisting（`other` 不产出 insights，见 run()） |
| locale | TEXT | |
| article_count | INTEGER | 本批次公告数 |
| related_uids | TEXT | JSON 数组，回链 `announcements.uid`（本 locale 自己的 uid，即使是复用 EN 分析） |
| is_locale_derived | BOOLEAN | true = 复用同日 EN 批次分析结果，未真正调用 LLM |
| derived_from_id | TEXT | is_locale_derived=true 时指向 EN 批次的 id |
| summary | TEXT | LLM 输出的 batch_summary |
| articles_analysis | TEXT | JSON 数组，每篇公告的结构化分析（字段随 category 不同） |
| zmx_diff | TEXT | zmx_comparison.analysis 的文字部分，末尾附一行「优先级依据：...」（来自 priority_reason，schema 未单开列） |
| diff_type | TEXT | ZMX已有 / ZMX缺失 / ZMX玩法不同 / 混合 / 不适用（listing 不含"ZMX玩法不同"；delisting 恒"不适用"） |
| priority | TEXT | 高 / 中 / 低 |
| zmx_evidence_uids | TEXT | JSON 数组，evidence_indices 映射回的 Zoomex uid |
| prompt_version | TEXT | 如 "campaign-v1"，改 prompt 正文必须递增 |
| llm_tokens_used | INTEGER | 复用 EN 分析或命中缓存时为 0 |
| created_at / updated_at | TEXT | 同批次重跑更新 updated_at，created_at 保留首次写入时间 |

### llm_cache（Phase 4 新增，LLM 响应缓存）

| 字段 | 类型 | 说明 |
|---|---|---|
| cache_key | TEXT PK | `SHA256(SHA256(排序后的 content_hash 拼接) \|\| prompt_version)` |
| response | TEXT | 原始 LLM 响应 JSON 字符串 |
| created_at | TEXT | |

### crawl_state（采集水位线）

| 字段 | 类型 | 说明 |
|---|---|---|
| source | TEXT | PK (source, locale, category) |
| locale | TEXT | |
| category | TEXT | PK 第三列，Phase 2 批次 2 新增。多分类源（同一 locale 下有多个互相独立翻页的子分类，如 Zoomex 的 menu_id）各分类独立维护水位线；单分类源恒为 `''`（不是 NULL） |
| high_watermark | TEXT | 上轮最大 update_time，UTC ISO8601，full_scan 模式下可为 NULL |
| strategy | TEXT | watermark / full_scan |
| updated_at | TEXT | |

### sync_log（飞书同步日志）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| target | TEXT | bitable / bot_EN / bot_FR / bot_VN / bot_ID / bot_EN-Asia |
| record_id | TEXT | uid 或 insight_id |
| action | TEXT | create / update / skip |
| status | TEXT | success / failed |
| error | TEXT | |
| synced_at | TEXT | |

### db 层使用方式

```python
from src.db.connection import get_connection, init_db
from src.db.operations import upsert_announcement, set_crawl_state, get_crawl_state

init_db("data/competitor_intel.db")

with get_connection("data/competitor_intel.db") as conn:
    result = upsert_announcement(
        conn, source="Bitunix", locale="EN", article_id="1001",
        title="...", content="...", post_time="2026-07-10T00:00:00Z",
    )
    # result.status: new / changed / unchanged
```

`upsert_announcement` 封装了去重、变更检测、历史归档三件事，任何 collector 落库都应该调用它，
不要在各 collector 里各自实现一遍判断逻辑。

CLI：`python -m src.db init` 建库（幂等，`CREATE TABLE IF NOT EXISTS`，可重复执行）。

## 推送规则（业务口径，实现见 `config/push_rules.yaml` + Phase 6）

| 场景 | 动作 | 备注 |
|---|---|---|
| 新增活动 | 推送 | status=new & category=campaign |
| 活动规则/奖励变化 | 推送 | status=changed & diff 涉及规则或奖励 |
| 新玩法 | 推送 | diff_type=ZMX缺失 & priority=高 |
| 地区独占公告 | 推送 | is_region_exclusive=true |
| 新增/变更下架公告 | 推送 | category=delisting & (status=new or status=changed)，2026-07-13 确定：下架信息对运营/产品有情报价值，不能当噪音过滤掉 |
| 与 Zoomex 一致 | 不推送 | diff_type=ZMX已有 |
| category=other | 不推送 | 维护、风控等噪音（delisting 已独立为单独分类，不再落入 other） |
| 已推送过 | 不推送 | push_status=pushed |

## Phase 规划摘要

| Phase | 内容 | 交付物 | 状态 |
|---|---|---|---|
| 0 | 项目骨架 + 数据模型 | CLAUDE.md、目录结构、SQLite schema、配置模板、db 层单测 | ✅ 已完成 |
| 1 | 数据源侦察 | 填满的 sources.yaml + 每个源一份真实响应 fixture | ✅ 已完成 |
| 2 | 采集器 + 增量/变更检测 | src/collectors/*.py + src/parsers/*.py | ✅ 已完成（批次 4/4：Bitunix+Weex+Zoomex+BingX+Phemex+Lbank 全部完成） |
| 2.5 | schema 收口 + 清洗前移 | raw_category 列、category CHECK 约束加 delisting、src/parsers/html_text.py、scripts/migrate_v2.py | ✅ 已完成 |
| 2.6 | category_mapping.yaml 修复 + 真实数据核验 | config/category_mapping.yaml 改按 raw_category 原始值做 key（原 key 是猜测/不稳定的人类可读名称，Phase 3 会全线 miss）、raw_category 的 unchanged 分支更新逻辑 | ✅ 已完成 |
| 2.7 | Weex Listings/Delistings + P2P Announcement 分类补采、多语言数据补齐 | Zendesk 分类覆盖主动核查、Weex 三分类改造（含 section 级采集）、Bitunix/Weex 全部 locale 入库、ZendeskCollector 改 cursor 分页（修复 Zendesk offset 分页 page=100 硬限制）、html_text.py 表格单元格嵌套 `<p>` 的 bug 修复 | ✅ 已完成 |
| 3 | 跨语言归组、分类打标 | src/pipeline/（清洗已在 Phase 2.5 前移到采集层，不再是 Phase 3 的事） | ✅ 已完成（范围：Bitunix+Weex+Zoomex；Phemex/BingX/Lbank 的映射/归组留白，等批次 3/4 采集器落地后回来接） |
| 4 | LLM 分析（summary + ZMX 差异，批次级） | src/analysis/，写入 insights 表（schema v3） | ✅ 已完成，多次真实调用验证（cursor_agent 后端；.env 里 OpenAI 兼容 LLM_API_KEY/LLM_API_BASE 仍未配置，真实 OpenAI 兼容响应未验证过） |
| 5 | 飞书多维表同步 | src/sinks/feishu_bitable.py | ✅ 已完成（真实网络验收，见「Phase 5 完成情况」） |
| 6 | 推送规则引擎 + 飞书群日报 | src/sinks/feishu_bot.py + src/pipeline/push_rules.py | 逐条规则推送引擎未做；已有整页截图推送（screenshot.py + feishu_bot.py），目前正从按 locale 分群重构为单一总群（进行中，见「当前状态与已知问题」） |
| 7 | 可视化看板 | src/dashboard/，静态 HTML | ✅ 已完成并持续迭代（当前 v2，category-first：Overview/Campaign/Product/Listing/Markets/Search），GitHub Pages 已启用 |
| 8 | 调度与监控 | scripts/run_daily.sh + 告警 | 待开始 |

行业热点模块（Phase 2 规划，与上表 Phase 编号无关）待业务明确定义后启动，不在当前 Roadmap 范围内。

详细的每个 Phase 任务 prompt 见 `phasePrompts.md`（每个 Phase 独立开一个 session，session 开始时
先读本文件同步项目状态）。

## 当前状态与已知问题（2026-07-21，速览）

详细的逐次真实验收记录、每个 Phase 的完整实现细节和调试过程，已整理进
`docs/history.md`（按时间顺序的完整日志，本文件不再重复）。本节只给出"现在能不能用、
卡在哪"的速览，新开 session 时先看这里，需要背景细节再翻 `docs/history.md`。

### 各源采集状态

| 源 | 主库状态 | 备注 |
|---|---|---|
| Zoomex（我方基线） | ✅ 全量基线（2018+ 条） | 唯一保留全量回填能力的源，定期 `--force-full` 核查 |
| Bitunix | ✅ 有数据，daily 增量 | |
| BingX | ✅ 有数据，daily 增量 | 活动中心端口只能拿到第 1 页（签名保护，见「BingX 签名保护」） |
| Phemex | ✅ 有数据，daily 增量 | 跨语言归组已修复（2026-07-21，group_id 改用 URL slug） |
| Lbank | ✅ 有数据，daily 增量 | |
| Weex | ✅ 已恢复，daily 增量（2026-07-21 用户确认） | 此前"Weex 路径问题"（2026-07-15 搁置）已解决，主库不再是 0 行（截至
  2026-07-21 共 59 条，EN/FR，当日新增 9 条）；2026-07-20 新增的 `latest_updates` section
  端点已随本次恢复一并生效 |

**长期政策（2026-07-21 拍板）**：除 Zoomex 外，任何源都不再对主库做历史回填，只做
`--lookback-days` 限定的每日增量。Weex 恢复采集同样遵守此政策——不做全量建仓补数。

### 已知未解决问题

- ~~Weex 采集暂停~~：已解决（2026-07-21 用户确认恢复），见上表。
- **飞书群推送正在重构（进行中）**：已从"按 locale 分群推送"决定改为"整页 Overview
  截图推送到单一总群"，卡在用户尚未提供总群真实群名（机器人当前只加入了
  `CompAgent_EN/FR/VN/ID/KR`，目标总群不在其中）。
- **Zoomex `zmx_baseline.mechanism_type` 标签碎片化：已解决（2026-07-21）**——
  `zmx_baseline.py`/`zmx_baseline` 表整体退休，替换为
  `src/analysis/zmx_catalog.py` + `zmx_summary`/`zmx_catalog_entry` 两张新表，
  `mechanism_type` 改用 `config/zmx_mechanism_taxonomy.yaml` 定义的封闭/半封闭
  枚举，且提取/查询不再有 90 天 lookback 窗口（详见「Zoomex Capability Catalog +
  staged.py 竞品分析管线 + Dashboard 事件级 Detail」小节）。**主库尚未迁移到新
  schema**——所有真实验证都在 scratch db 上做的，`data/competitor_intel.db` 目前
  仍是旧的 `zmx_baseline` 表，还没跑过 `python -m src.analysis.zmx_catalog`。
- **`run.py` 已改为 staged.py 三段式管线（Stage1 事实抽取 → Stage2 确定性召回 →
  Stage3 业务判断），AI 不再产出 priority/action_type/owner/follow_up**——同一次
  改动。`insights.articles_analysis` 的字段形状变了（`mechanism`/`feature`/
  `start_at`/`end_at`/`diff_detail`/`zmx_counterpart_uids` 取代旧的
  `mechanics`/`feature_description`/`time_window`/`priority_reason`），
  `export_data.py`/`docs/index.html` 已同步更新读取新字段（含优雅降级读旧数据）。
  主库现存的 insights 仍是旧字段形状，需要重新跑 `python -m src.analysis` 才会
  换成新形状。
- **没有真实的 OpenAI 兼容 LLM 后端**：`.env` 的 `LLM_API_KEY`/`LLM_API_BASE` 仍为空，
  目前全部真实 LLM 调用都走 `cursor_agent`（Cursor Background Agent）这个替代后端，
  真实 OpenAI 兼容响应格式/行为从未验证过。
- **Phemex 遗留 insights 未重算**：`group_id` 修复后，4 条基于旧 group_id 产出的
  Phemex insights（campaign/delisting 各 EN+FR）尚未重新计算，需要真实跑一次
  `python -m src.analysis --source Phemex` 才能确认是否会因此改判为 EN→FR 复用
  （大概率零新增真实 token 消耗，但会改写 insights 表内容，需要用户先确认再做）。
- **GitHub Pages**：已启用（2026-07-21 用户确认），不再是待办事项。

### 推送/看板现状

- 可视化看板：`docs/index.html` 是当前 v2（category-first：Overview/Campaign/
  Product/Listing/Markets/Search），`docs/data/dashboard.json` 由
  `python -m src.dashboard` 导出，GitHub Pages 已启用。Campaign/Product 现在支持
  点击展开的 Detail 抽屉（Basic Info/Rule-Reward-Timeline/AI Summary/Zoomex
  Comparison 两栏对照）+ Type/Status/Diff/Keyword 筛选（2026-07-21）。Overview
  的"Today's Insight"现在优先展示 `src/analysis/daily_digest.py` 产出的真实 LLM
  综述（命中缓存时，"LLM 生成"绿色徽标），否则回退占位统计文案（"占位符"徽标）——
  但导出层从不主动触发 LLM 调用，需要有人先真实跑过
  `generate_daily_digest(..., dry_run=False)` 并写入缓存才会显示真实内容。
- 飞书多维表同步（`src/sinks/feishu_bitable.py`）：已验证可用，主库已同步过一次。
- 飞书群推送：Phase 6 规划的"逐条规则推送引擎"从未实现；现有的是"整页截图推送"
  （`src/dashboard/screenshot.py` + `src/sinks/feishu_bot.py`），且这条链路本身正在
  从"按 locale 分群"重构为"单一总群"，见上方"已知未解决问题"。
