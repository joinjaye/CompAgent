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
│   │   ├── lbank.py            # ✅ 批次 4（RSC flight 流，仅默认聚合视图 10 条，force_full no-op）
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
│   │   └── lbank_web.py        # ✅ 批次 4：Lbank RSC flight 流解析，含文本分段引用
│   │                            #   （"$N"）还原、高亮模板标记剥离
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
| 4 | LLM 分析（summary + ZMX 差异，批次级） | src/analysis/，写入 insights 表（schema v3） | 🔄 代码完成，未跑真实 LLM 验收（见「Phase 4 完成情况」） |
| 5 | 飞书多维表同步 | src/sinks/feishu_bitable.py | ✅ 已完成（真实网络验收，见「Phase 5 完成情况」） |
| 6 | 推送规则引擎 + 飞书群日报 | src/sinks/feishu_bot.py + src/pipeline/push_rules.py | 待开始（`src/sinks/feishu_bot.py` 已存在，但是「Phase 7 之后」新增的截图推送，不是这里规划的逐条规则引擎，见该节说明） |
| 7 | 可视化看板 | src/dashboard/，静态 HTML | 🔄 代码完成，用 demo 数据验收，未接入真实调度/GitHub Pages |
| 8 | 调度与监控 | scripts/run_daily.sh + 告警 | 待开始 |

行业热点模块（Phase 2 规划，与上表 Phase 编号无关）待业务明确定义后启动，不在当前 Roadmap 范围内。

详细的每个 Phase 任务 prompt 见 `phasePrompts.md`（每个 Phase 独立开一个 session，session 开始时
先读本文件同步项目状态）。

## Phase 0 完成情况

- [x] 目录结构：`src/{db,collectors,pipeline,analysis,sinks,dashboard}`、`tests/fixtures`、
      `config`、`data`、`scripts`
- [x] `src/db/schema.sql`：5 张表，含 CHECK 约束和常用查询索引
- [x] `src/db/connection.py` + `operations.py` + `__main__.py`：`python -m src.db init` 可用
- [x] `config/sources.yaml`：17 个 `exchange × locale` 条目，字段值均为占位，无任何猜测的 endpoint
- [x] `config/push_targets.yaml`、`config/push_rules.yaml`：模板已就绪
- [x] `config/.env.example`：凭证模板
- [x] `tests/test_db.py`：11 个用例，覆盖建库、插入、去重、变更检测（含手动 tamper 场景）、
      水位线读写、CHECK 约束
- 验收命令：
  ```bash
  python -m src.db init
  pytest   # 或 .venv/bin/python -m pytest，需先安装 requirements-dev.txt
  ```

未做（有意留给后续 Phase，本 session 未写任何爬虫逻辑、未猜测任何 API 地址）：
- collectors 里没有任何 HTTP 请求代码
- `sources.yaml` 里的 `endpoint` / `field_mapping` 等全部是 `null` 占位，等 Phase 1 实测填充

## Phase 1 完成情况：数据源现状表

32/32 个 source×locale×category 组合验证可用（6 家交易所，全部无阻塞）。Phemex EN/FR
拆成 3 个分类子源、Zoomex 5 个 locale 拆成 3-4 个分类子源，故总数比最初的 17 大幅增加。
详细侦察记录见 `config/sources.yaml` 对应条目上方注释，真实响应快照见 `tests/fixtures/`。

验收命令（对所有已填源发真实请求，确认 ≥1 条真实公告；受阻源打印明确原因）：
```bash
python -m src.probe --all
# 或探测单个源：python -m src.probe --source bitunix
```
`src/probe/` 是轻量验活工具，不是采集器（采集器是 Phase 2 的事）：用
`field_mapping.post_time` 对应的 key 在原始响应里出现的次数作为"确认拿到真实条目"
的信号，JSON 接口和 HTML 内嵌 JSON/JS 对象字面量页面通用。新增依赖 `certifi`
（修复本机 Python.org 发行版 urllib 默认没有 CA 证书链的问题）。

| 交易所 | locale | 状态 | 策略 | 备注 |
|---|---|---|---|---|
| Zoomex（我方基线） | EN / FR / EN-Asia / VN / ID | ✅ 通 | watermark（⚠️ 仅供观测，不参与采集决策，见 Phase 2 批次 2 记录） | 页面本身是纯客户端渲染 SPA（curl 拿不到），但用一次性 headless browser（Playwright）拦截运行时请求，找到了匿名公开、无需登录态的真实 API：`POST api2.zoomex.com/gw/pub/v1/helpCenter/getArticleListByMenuId`（真正支持服务端翻页，是本项目目前唯一原生分页可用的源）+ `getArticleById`（详情，正文是 Slate.js 风格富文本 JSON，不是 HTML）。3-4 个分类（Platform Announcement / New Product Announcement / Platform Events，EN-Asia 多一个 Exclusive Events）。gmtCreatedAt/gmtUpdatedAt 抽样 10/10 存在真实差异。详见 `config/sources.yaml` zoomex 块注释 |
| Bitunix | EN | ✅ 通 | watermark | 真实公告在 Zendesk（support.bitunix.com），非主站 SPA；主站 platformgateway.bitunix.com 已确认死路（403/Cloudflare）。29/30 抽样有真实 updated_at 差异 |
| Bitunix | FR | ✅ 通 | watermark | 同 EN 机制 |
| Bitunix | ID | ✅ 通 | watermark | 同 EN 机制 |
| Weex | EN | ✅ 通 | watermark | 跑在 Zendesk 上（weexsupport.zendesk.com），标准 Help Center API，匿名可访问，正文 inline。category 18540264809497="Latest Announcements" |
| Weex | FR | ✅ 通 | watermark | 同 EN 机制，locale=fr |
| BingX | EN | ✅ 通 | full_scan | Nuxt 3 SSR，`__NUXT_DATA__` 内嵌数据；首屏仅~20条，**已确认是跨 12 个分区的聚合视图（非单分区）**，完整历史用 sitemap（7829 URL，已确认扁平覆盖全部分区）替代；createTime==updateTime 恒等 |
| BingX | VN | ✅ 通 | full_scan | 同 EN 机制，article_id 跨 locale 一致可做 group_id；VN sitemap 不完整需借用 EN sitemap |
| Phemex | EN | ✅ 通 | full_scan | SSR，`window.preloadedData` 内嵌数据，detail 页 inline 正文；updatedAt 只是秒级发布噪音；**News/Activities/Newsletter 3 个分类均已确认可抓（拆成 3 个 categories.\* endpoint）**；sitemap_Announcement.xml 给全量文章（已验证覆盖全部 3 个分类）+跨语言映射 |
| Phemex | FR | ✅ 通 | full_scan | 同 EN 机制，3 个分类 |
| Lbank | EN | ✅ 通 | full_scan（⚠️ Phase 2.5 订正，原记录误写成 watermark，见下方说明） | Next.js SSR，正文内嵌在列表页 RSC JSON 里；updateTime 需另请求 detail 页（/support/articles/{code}）。仅默认聚合视图第 1 页（10条）可稳定拿到；**7 个页面级 tab 的分类代码树已找到（可用于 category 命名映射），但按 tab 单独抓取已确认不可行（curl 三种候选 URL 均只返回导航壳，0 条实际公告），翻页/按 tab 筛选均需 headless browser** |
| Lbank | VN | ✅ 通 | full_scan | 同 EN 机制，noticeId/code 跨 locale 一致，可直接做 group_id |
| Lbank | ID | ✅ 通 | full_scan | 同 EN 机制 |

> **Phase 2.5 订正（2026-07-14）**：本表 Lbank 三行的策略此前误记成 watermark，与
> README/phasePrompts.md 的原始设计、以及本表备注里已经写明的实际能力（翻页未逆向，
> 每轮只能拿固定 10 条，没有"只拉增量"的空间）相矛盾。改判 full_scan：detail 页的
> updateTime 字段本身可靠（抽样 8/9 有真实差异），但只落库供观测，不驱动翻页决策，
> 详见 `config/sources.yaml` lbank 块注释。

### Phase 1 补充侦察（同日，2026-07-13）：分类覆盖排查

首版 Phase 1 侦察中，Phemex/BingX/Lbank 三个源存在"分类覆盖是否完整"的疑问（首版只顺手
验证了默认列表页，没有系统性检查是否所有分类/分区/tab 都能拿到）。本次补充侦察逐个用真实
请求核实，结论：

- **Phemex：确认存在真实缺口，已修复。** 首版只配置了 News 分类的 endpoint，遗漏了
  Activities（campaign 标签的核心来源）和 Newsletter。实测两者均可正常抓取（EN
  total 476 / 12，FR total 158 / 4），已在 `sources.yaml` 里拆成 `categories.news` /
  `categories.activities` / `categories.newsletter` 三个子 endpoint。同时确认
  `sitemap_Announcement.xml` 本身是扁平的（`/announcements/{slug}`，不分类），已经
  天然覆盖这 3 个分类的全部历史文章（EN/FR 的 sitemap 去重条数与三个分类 total 之和
  高度吻合），全量回填不需要额外改动。
- **BingX：排查后确认不是问题。** 把 `__NUXT_DATA__` 当合法 JSON 解析（devalue 格式
  里整数就是同数组下标引用），还原出首屏 20 条各自的 sectionId，反查得到 5 个不同
  分区（Latest Promotions / Asset Maintenance / Product Updates / Delisting /
  Futures Listing），证实首屏本来就是跨分区聚合，不是只有 "Latest Announcements"
  单一分区。12 个分区的 sectionId 数值已全部反查确认，写入 `sources.yaml` 注释供
  Phase 3 分类映射用。sitemap 同理是扁平 URL，天然覆盖全部分区。
- **Lbank：排查后确认是已知限制，非新发现的缺口。** 找到了完整的 7-tab 分类代码树
  （含每个 tab 下的子分类），但实测三种候选 URL（父分类页 `/support/sections/{code}`、
  叶子子分类页、以及 `?categoryCode=` 查询参数）均不返回文章列表数据——只有默认聚合
  视图的 SSR 输出里有真实公告。这与 Phase 1 首版记录的"仅能稳定拿到默认 10 条"结论
  一致，补充侦察只是把"为什么拿不到"坐实了（不是没试对 URL，而是这些页面的公告列表
  确实是纯客户端 hydration 后请求的，暂时没有找到对应的 REST 调用）。分类代码树已保留
  在 `sources.yaml` 注释里，供以后接入 headless browser 时直接用。
- **Zoomex：BLOCKED 已解除。** 用 Playwright 一次性打开真实文章页/分类页，拦截运行时
  对 `api2.zoomex.com` 的请求，找到了 3 个匿名公开的真实 API：`getAllMenu`（分类树）、
  `getArticleListByMenuId`（真正支持服务端翻页的分页列表，本项目目前唯一一个翻页参数
  真实生效的源）、`getArticleById`（详情，正文是 Slate.js 风格富文本 JSON）。用纯
  Python `urllib`（无浏览器上下文、无 cookie）重放同样的请求同样返回 200，说明日常
  采集完全不需要 headless browser，headless 只用于"找到 endpoint"这一次性步骤。
  已把一次性侦察脚本删除，响应快照保存进 `tests/fixtures/zoomex_menu.json` /
  `zoomex_EN_platform_announcement.json` / `zoomex_article_detail.json`。同时把
  `src/probe/core.py` 扩展为支持 POST + JSON body 的探测（Zoomex 是本项目第一个
  POST 源），`python -m src.probe --all` 现在 32/32 全部 OK，0 BLOCKED。

详见 `config/sources.yaml` 对应源的"补充侦察"注释块。

## Phase 2 完成情况（进行中）：批次 1/4 — Bitunix + Weex（批次 2 见后文）

按 phasePrompts.md 的开发顺序分批推进，每批验收通过后再继续下一批。本节记录已完成批次；
后续批次（Zoomex → Phemex+BingX → Lbank）会在各自完成后追加到本节。

### 架构（后续批次沿用，不要重新设计）

- `src/collectors/base.py`：`BaseCollector` 抽象基类，契约是 `fetch_list(since) → list[RawItem]`
  / `fetch_detail(item) → RawItem`（默认原样返回，给 inline 源用）/ `normalize(item) →
  NormalizedAnnouncement`。`run(conn, force_full=False)` 做统一编排：按 `strategy` 决定要不要读
  `crawl_state.high_watermark` 当 `since`，调用子类三个方法，落库走 `upsert_announcement`
  （不自己判断 new/changed/unchanged，复用 Phase 0 的判断逻辑），最后按 `strategy=watermark`
  回写 `crawl_state`。`force_full=True` 会忽略已存水位线、从头全量拉取——不是过度设计，是
  验收 tamper-detection 的必要手段（见下方"验收记录"）。
- `src/collectors/http.py`：所有网络请求的唯一入口，指数退避重试（网络错误/5xx 最多 3 次，
  4xx 判定客户端错误直接不重试抛出），复用 Phase 1 probe 里验证过的 certifi CA 方案。
- `src/parsers/`：纯函数，不发请求，只把响应结构转成 dict list，方便离线单测。
  `zendesk.py` 是 Bitunix/Weex 共用的第一个实现。
- `src/collectors/zendesk_base.py`：`ZendeskCollector(BaseCollector)`，Bitunix/Weex 共用
  （字段格式、分页机制完全一致，只有 `source_name`/`group_id_prefix`/`sources.yaml` 里的
  endpoint 不同）。子类 `bitunix.py`/`weex.py` 各自 5 行。
- watermark 分页停止逻辑：URL 带 `sort_by=updated_at&sort_order=desc`，服务端已按
  update_time 降序排好，逐页解析，一旦遇到 `update_time <= since` 立刻停止翻页（不需要翻完）。
  `since=None`（crawl_state 里还没有水位线）时会翻到 `next_page` 耗尽为止——首次运行天然等价
  于一次全量回填，Bitunix/Weex 不需要像 BingX/Phemex 那样另外写 `--backfill` 模式。
- Phase 2 阶段 `announcements.category` 一律留 `NULL`（CLAUDE.md schema 文档里已经写明
  "Phase 3 之前未分类"）；`field_mapping.category`（如 Bitunix 的 `section_id`）在 Phase 2
  没有落库去处（schema 没有 raw_category 列，且 `category` 列的 CHECK 约束只接受
  campaign/product/listing/other），先不处理，留给 Phase 3 决定怎么用。

### 批次 1 验收记录（2026-07-14，真实网络请求，非离线测试）

```
python -m src.collectors --source bitunix --locale EN         # 首轮：new=1534 changed=0 unchanged=0 failed=0
python -m src.collectors --source bitunix --locale EN         # 第二轮：new=0    changed=0 unchanged=0 failed=0（水位线拦下，~2.6s 完成，未重新翻 1534 条）
# 手动 UPDATE announcements SET content_hash='tampered...' WHERE uid=<某条 Bitunix EN>
python -m src.collectors --source bitunix --locale EN --force-full   # changed=1 unchanged=1533 failed=0，content_history 里能查到被覆盖的 tampered hash
python -m src.collectors --source weex --locale EN             # 首轮：new=1038 changed=0 unchanged=0 failed=0
python -m src.collectors --source weex --locale EN             # 第二轮：new=0 changed=0 unchanged=0 failed=0
```

`pytest`：24 通过（Phase 0 的 11 个 + Phase 2 新增 13 个：`tests/parsers/test_zendesk.py` 8 个
离线解析单测 + `tests/collectors/test_zendesk_collectors.py` 5 个，覆盖 normalize 字段映射/
group_id 拼接、mock HTTP 后的幂等验证、以及 tamper→force_full 变更检测）。

### 未做 / 已知限制（有意留给后续批次或后续 Phase）

- `COLLECTOR_BUILDERS`（`src/collectors/__main__.py`，批次 2 起从 `COLLECTOR_REGISTRY` 重构为
  builder 函数字典以支持多分类源展开）目前登记了 bitunix/weex/zoomex；`sources.yaml` 里其余
  source 会被跳过不报错，下一批次实现后在这里补登记即可。
- ~~Bitunix/Weex 的 `field_mapping.category`（Zendesk `section_id`）没有落库~~ —— 已在
  Phase 2.5 解决：新增 `raw_category` 列落库原始值，映射规则交给 `config/category_mapping.yaml`
  + Phase 3。
- 未测试超时/5xx 重试路径的真实触发（`http.py` 的指数退避逻辑本身很直接，未强行 mock 网络异常
  单独测试；如果后续批次遇到不稳定源，建议在那批次里补一个针对 `fetch()` 重试行为的单测）。

## Phase 2 完成情况：批次 2/4 — Zoomex

### 与原计划的一处重要偏离（实测驱动，不是随意改设计）

phasePrompts.md 原计划是「Zoomex：POST getArticleListByMenuId，逐页拉取，按 gmtUpdatedAt
降序判断停止点」，假设列表接口按更新时间降序排列、可以早停。**实测证伪了这个假设**
（2026-07-14，对 menuId=26 EN 的 pageNum=1/2/3 各抽样 5 条真实数据）：同一页内
gmtUpdatedAt 完全不单调（例：page1 里 order=1067 的条目 updatedAt 比 order=1068 的更大），
order/gmtCreatedAt 也不是严格排序键。也就是说这个列表接口的默认排序既不是 update 时间、
也不是简单的创建顺序，无法安全依赖任何排序假设做提前退出翻页——按原计划实现会导致
「排在后面但被编辑过的旧文章」被漏采，是真实的正确性问题，不是吹毛求疵。

改为更稳健的实现（见 `src/collectors/zoomex.py` 顶部注释）：
1. `fetch_list(since)` 每轮翻完该 menu_id 下的全部页（列表请求很便宜，`since` 参数因此
   不参与翻页早停判断，仅保留在方法签名里满足基类契约）。
2. 新增 `BaseCollector.needs_detail(conn, item)` 钩子（默认恒 True，inline 源不受影响）：
   `ZoomexCollector` 覆写它，用 DB 里已存的 `update_time` 跟列表条目的 `update_time` 比对，
   只有新增或 update_time 变化的条目才会触发一次详情请求（`getArticleById`，正文只有这个
   接口才有）。`run()` 在调用 `needs_detail` 返回 False 时直接计入 `unchanged`，不落库、
   不发详情请求。
3. `force_full=True` 现在除了忽略 `high_watermark`，也会跳过 `needs_detail` 的判断
   （见 `base.py` run() 里 `if not force_full and not self.needs_detail(...)`），对拉到的
   每条都重新请求详情——这是 tamper-detection 人工复核依赖的机制，批次 1 只加了前半段
   （忽略 watermark），批次 2 发现只忽略 watermark 不够（Zoomex 的增量判断根本不看
   watermark），把 force_full 语义补全成「二者都跳过」。

### 架构新增（后续批次可复用）

- `src/db/schema.sql` 的 `crawl_state` 表加了 `category` 列，PK 从 `(source, locale)` 改成
  `(source, locale, category)`。单分类源（Bitunix/Weex）恒传 `category=''`，跟原来行为
  完全一致；多分类源（Zoomex 的每个 menu_id）各自独立维护水位线。`operations.py` 的
  `get_crawl_state`/`set_crawl_state` 加了 `category: str = ""` 关键字参数，向后兼容
  （旧调用不传该参数照常工作）。**本地 `data/competitor_intel.db` 是 gitignored 的开发态
  产物，`init_db` 是 `CREATE TABLE IF NOT EXISTS`，改表结构不会回溯迁移已存在的旧表**——
  验收/开发时如果本地库是批次 1 时代建的，需要删掉重建（`rm data/competitor_intel.db`），
  生产环境部署前需要正式的 migration，目前还没有（Phase 8 之前都是单人开发态，暂不需要）。
- `src/collectors/timeutil.py`：`ms_to_iso` / `iso_to_ms`，unix 毫秒 <-> UTC ISO8601 互转，
  Zoomex 和以后的 Lbank 都要用。
- `src/collectors/http.py` 新增 `rate_limit_seconds(config)`：**修了一个批次 1 就存在但没
  触发的 bug**——`(cfg.get("rate_limit_ms") or 500) / 1000` 会把合法的 `0`（测试里关闭限速）
  当假值吞掉、错误地换成默认 500ms。批次 1 的离线测试因为固定只用一页 fixture（`next_page`
  强制置 None）从没触发过这个分支，批次 2 的 Zoomex 测试因为要翻 2 页 + 3 次详情请求，
  测试跑到 10 秒才发现。`zendesk_base.py` 和 `zoomex.py` 都已改用这个共用函数。
- `RawItem` 的契约收紧了一处：`fetch_list` 返回的条目，时间字段必须已经是 UTC ISO8601
  字符串（不能留到 `normalize` 才转），因为 `needs_detail`/watermark 比较都要在
  `fetch_list`/`run` 阶段就能拿到可比较的时间值。Bitunix/Weex 一直就是这样（源端本来就是
  ISO 字符串），这次只是把隐含约定写进 `base.py` 的 docstring。
- Zoomex 的详情页 URL 只在 Phase 1 侦察时验证过一个真实样例（`help.zoomex.com/en/article/3858`），
  其它 locale 的 path segment 没有逐个验证，按项目"不允许猜测数据"的约束，`normalize()`
  里 `url` 字段先留 `None`，不是遗漏。

### 批次 2 验收记录（2026-07-14，真实网络请求，Zoomex EN new_product_announcement，40 篇）

```
python -m src.collectors --source zoomex --locale EN --category new_product_announcement
# 首轮：new=40 changed=0 unchanged=0 failed=0，耗时 ~46s（40 次详情请求 + 2 次列表请求）
python -m src.collectors --source zoomex --locale EN --category new_product_announcement
# 第二轮：new=0 changed=0 unchanged=40 failed=0，耗时 ~2.3s（只翻了 2 页列表，0 次详情请求——
# 证明 needs_detail() 正确跳过了全部详情请求）
# 手动 UPDATE announcements SET content_hash='tampered...' WHERE uid=<某条 Zoomex EN>
python -m src.collectors --source zoomex --locale EN --category new_product_announcement --force-full
# changed=1 unchanged=39 failed=0，content_history 里能查到被覆盖的 tampered hash
```

`pytest`：46 通过（批次 1 的 24 个 + 批次 2 新增 22 个：`tests/parsers/test_slate_json.py` 10 个、
`tests/parsers/test_zoomex.py` 7 个、`tests/collectors/test_zoomex_collector.py` 5 个，覆盖
Slate JSON 转纯文本+保留表格结构、按 lang 匹配 contents[]、全量翻页不依赖排序、
needs_detail 增量判断、force_full 变更检测）。`tests/test_db.py` 的 11 个用例在
`crawl_state` 加了 `category` 列后无需改动、原样通过（向后兼容验证）。

### 未做 / 已知限制（有意留给后续批次或后续 Phase）

- 只针对 EN 的 `new_product_announcement`（40 篇，最小的分类）做了真实网络验收，没有跑满
  全部 5 个 locale × 3-4 个 menu_id（共 ~2000+ 篇，详情请求预计 ~17 分钟），跟批次 1 只用
  Bitunix/Weex 各自的 EN 做真实验收是同样的取舍——核心路径（分页、增量判断、tamper 检测）
  已经用真实数据证明工作正常，其余 locale/menu_id 复用完全相同的代码路径，只是参数不同。
- `EN-Asia` 的 `exclusive_events`（menu_id=69，Phase 1 侦察记录是唯一有数据的 locale，其它
  4 个 locale 该分类 total=0）尚未跑过，理论上应该直接可用（跟其它 menu_id 走同一套代码），
  留给验收时顺手跑一下确认。
- ~~Zoomex 的 `field_mapping.category`（对应用的是哪个 menu_id）没有落库~~ —— 已在 Phase 2.5
  解决：`raw_category` 存 `menu_id` 本身（不是 categories.\* 的配置键名）。
- `crawl_state.category` 本身仍然没有正式的 SQLite migration 机制（Phase 2.5 的
  `scripts/migrate_v2.py` 只覆盖了这次改动的 `announcements`/`insights`）——现在只有开发态
  数据无所谓，但 Phase 8（调度与监控）上线前，如果 `crawl_state` 的 schema 还会再变，需要
  在 `migrate_v2.py` 沉淀的"建新表 -> 复制数据 -> drop 旧表 -> rename"流程基础上补一版。

## Phase 2.5 完成情况：schema 收口 + 清洗前移

批次 2/4（Bitunix+Weex+Zoomex）验收通过后、继续批次 3/4（Phemex+BingX）和批次 4/4（Lbank）
之前插入的一个补丁 session：三个在批次 1/2 里已经发现但先记成"留给后续 Phase"的设计缺陷，
如果放着不管，会在接下来两个批次里被复制三遍（每加一个新源都会再踩一次），所以提前收口。
**本 session 不实现任何新的源**，只改 schema、采集层的清洗时机、以及配套文档。

### 问题 1：raw_category 没有落库去处

Phase 2 批次 1/2 的 `normalize()` 拿到了各源的原生分类值（Bitunix/Weex 的 Zendesk
`section_id`、Zoomex 的 `menu_id`），但 schema 里没有对应列，只能丢弃——采集层本该原样保留
源端字段，映射转换是 Phase 3 pipeline 的事，不该在 Phase 2 就被迫决定"丢还是不丢"。

- `announcements` 新增 `raw_category TEXT` 列（可为 NULL），存源端原始值，不做任何映射（数值型
  转字符串）。`src/db/operations.py` 的 `upsert_announcement` 新增 `raw_category` 关键字参数，
  INSERT/UPDATE（changed 分支）都会写这一列；unchanged 分支不动它（没必要，值不会变）。
- `src/collectors/base.py` 的 `NormalizedAnnouncement` 新增 `raw_category` 字段，`run()` 里
  透传给 `upsert_announcement`。`RawItem.category_raw` 字段 Phase 2 批次 1 就已经存在（只是
  没被用上），这次只是把它接到底。
- `src/collectors/zendesk_base.py`：`raw_category = str(item.category_raw) if item.category_raw
  is not None else None`（Zendesk `section_id`，数值转字符串）。
- `src/collectors/zoomex.py`：`raw_category = str(self.menu_id)`（存 menu_id 本身，不是
  `sources.yaml` 里 `categories.*` 那个人类可读的配置键名，跟 CLAUDE.md schema 表格的措辞
  "源端原生分类的原始值"保持一致）。
- 新建 `config/category_mapping.yaml`：README/phasePrompts.md 早就规划了这个文件（Phase 0
  任务清单里的第 4 项），但一直没有创建（Phase 0 完成情况没有勾选它）。既然 raw_category
  现在有地方落库了，把这个 Phase 3 要用的映射表提前写好（内容取自 README 记录的初始值），
  顺带修掉问题 2 的 delisting 映射（见下）。

### 问题 2：category 的 CHECK 约束跟 delisting 决策不一致

2026-07-13 已经决定 delisting 独立成一类（CLAUDE.md 的 schema 表格、推送规则表当时就已经
按这个决定写了），但当时只改了文档，没有回头改 `schema.sql` 的 CHECK 约束（还是
`campaign/product/listing/other`）和 `config/category_mapping.yaml`（当时还不存在，是本
session 才创建的）——决定和实现出现了分叉。

- `src/db/schema.sql`：`announcements.category` 和 `insights.category` 的 CHECK 约束都改成
  `campaign/product/listing/delisting/other`（NULL 仍允许）。schema 版本记为 **v2**（v1 是
  Phase 0 建的初版，这是第一次改列/改约束）。
- `config/category_mapping.yaml`：`bitunix.Delisting` 和 `bingx.Delisting` 映射到
  `delisting`（不再落进 `listing`），其余映射照抄 README 初始值原样不变。
- `config/push_rules.yaml`：补上 `delisting_new_or_changed` 规则（`category=delisting AND
  status in (new, changed)` → 推送），跟推送规则表里已经写的口径对齐。`exclude_conditions`
  的 `noise_category`（`category=other`）不需要改——`delisting` 现在是独立取值，不会再被这条
  排除规则误伤。

### 问题 3：HTML 清洗前移到采集层（不再是 Phase 3 的事）

原计划（phasePrompts.md 里的 Phase 3）是"清洗后回写 content 并更新 content_hash"，但这样设计
有个后果：Phase 2 入库的 `content` 是原始 HTML，`content_hash` 是 HTML 的 hash；Phase 3 第一次
全库跑批清洗时，**全部**已入库的行都会因为 `content_hash` 变化被 `upsert_announcement` 判成
`status=changed`，一次性把几千条历史数据的旧版本（原始 HTML）灌进 `content_history`，还会把
`push_status` 重置成 `pending`、触发 Phase 6 的推送规则——这是纯粹的技术噪音，不是真实的公告
变更，但会被下游误判成大量"公告发生变化"。清洗本身是确定性纯函数、不依赖任何跨行状态，没有
理由不在采集层（第一次落库之前）就做完。

- 新建 `src/parsers/html_text.py`：基于标准库 `html.parser.HTMLParser` 写的 HTML → 纯文本
  转换器，不引入第三方 HTML 解析依赖。跳过 `script/style/nav/header/footer/noscript/iframe/
  form/button/svg` 等模板标签，以及 class/id 命中 `nav/footer/disclaimer/cookie/breadcrumb/
  sidebar` 等噪音关键词的元素；块级元素（`p/div/li/h1-h6/br` 等）触发换行；表格
  （`table/tr/td/th`）按行列结构转换，**格式故意跟 `slate_json.py` 的 `_render_table` 保持
  一致**（行 `\n` 分隔、列 `\t` 分隔），因为 Zoomex 走 Slate JSON、其它源走 HTML，两条链路
  应该产出同一种"表格转文本"的观感，不发明第二套表示法。畸形 HTML 不抛异常：`html.parser`
  本身对畸形标签容错，兜底再加一层正则去标签的降级路径。用真实 fixture（`bitunix_EN.json`
  的正文、`weex_EN.json` 里带 `<table>` 的那条 WXT 活动公告）验证过转换效果，不是纯靠手写
  样本猜测格式。
- `src/collectors/zendesk_base.py` 的 `normalize()` 调用 `html_to_text(item.content)`。Zoomex
  走 `slate_json.py`，详情接口返回的本来就不是 HTML，不受影响、不需要改。
- `content_hash` 的语义从此明确为「**清洗后**正文的 SHA256」，CLAUDE.md 的 schema 表格已同步
  这句话。
- Phase 3 职责相应收窄：只剩跨语言归组 + 分类打标（含新的 raw_category 映射第一层）+ 地区
  独占标记，不再包含清洗。`phasePrompts.md` 的 Phase 3 prompt 已删掉【清洗】整节并在开头加了
  指向本节的注明，分类打标那节也顺带把 raw_category 映射第一层和 delisting 关键词补全了。

### Migration：scripts/migrate_v2.py

本地 `data/competitor_intel.db` 是 gitignored 的开发态产物，`init_db` 是
`CREATE TABLE IF NOT EXISTS`，不会回溯迁移已存在的旧表结构，开发态直接删库重建最干净
（Bitunix/Weex 已入库的数据无论如何都要重刷才能拿到 `raw_category` 和清洗后的纯文本
`content`，删库重建反而更彻底）。但这是项目第一次需要"改列 + 改 CHECK 约束"，之前
`crawl_state` 加 `category` 列（批次 2）没有配套 migration，只记了"删库重建"，这次把标准
流程沉淀成一个真正可跑的脚本，给 Phase 8 上线前的正式 migration 需求起个头：

- 表重建走标准 SQLite 流程：`CREATE TABLE announcements_v2 (新 DDL)` → `INSERT INTO
  announcements_v2 SELECT 旧列..., NULL AS raw_category FROM announcements` → `DROP TABLE
  announcements` → `ALTER TABLE announcements_v2 RENAME TO announcements`。刻意不对原表做
  `RENAME`（比如先 `announcements` → `announcements_old` 再建新表），因为 SQLite 默认会在
  `RENAME TABLE` 时自动重写其它表里引用这个表名的外键声明（`content_history.uid REFERENCES
  announcements`）——先建全新表名（`_v2`）再重建成目标表名，规避了这个坑，`content_history`
  的外键定义全程没有被触碰过。
- `insights` 表同样的流程（CHECK 约束加 delisting，没有新增列）。
- 迁移期间 `PRAGMA foreign_keys = OFF`（迁移完成后恢复 `ON`），事务边界手动管理
  （`conn.isolation_level = None` + 显式 `BEGIN`/`COMMIT`/`ROLLBACK`），失败时回滚不留半成品表。
- 幂等：检测到 `announcements` 已有 `raw_category` 列就直接跳过，可重复执行；库/表不存在时
  也是安全 no-op（提示改用 `python -m src.db init`）。
- 用法：`python scripts/migrate_v2.py [db_path]`（不传参默认 `data/competitor_intel.db`）。
- `tests/test_migrate_v2.py`：对着手工建的 v1 结构临时库跑迁移，验证新列存在、旧数据不丢、
  `content_history` 外键级联删除仍然正常工作、CHECK 约束放开允许 `delisting`、重复调用幂等。

### 验收记录（2026-07-14，真实网络请求）

```
rm data/competitor_intel.db && python -m src.db init
python -m src.collectors --source bitunix --locale EN
# 全量重刷：new=1534 changed=0 unchanged=0 failed=0，~57s。抽查：1534/1534 行 raw_category
# 非 NULL，content 里搜不到 <div>/<p>/<strong> 等标签（HTML 已清洗成纯文本）。
python -m src.collectors --source bitunix --locale EN
# 第二轮：new=0 changed=0 unchanged=0 failed=0，~2.6s（水位线拦下，清洗前移没有破坏幂等）。
python -m src.collectors --source weex --locale EN
# new=1038 changed=0 unchanged=0 failed=0，~58s。抽查同上，1038/1038 行 raw_category 非
# NULL、0 行残留 HTML 标签；带 `<table>` 的真实公告（WXT 活动奖励表，article_id
# 57773585831833）转换后是 "WXT commitment amount\tReward multiplier\n300 ≤ X <
# 3,000\t1\n..." —— 行 \n 分隔、列 \t 分隔，跟 slate_json.py 的表格格式一致。
python -m src.collectors --source zoomex --locale EN --category new_product_announcement
# new=40 changed=0 unchanged=0 failed=0，~46s。40/40 行 raw_category = "123"
# （menu_id，不是 categories.* 的配置键名）。
pytest
# 68 通过：批次 2 的 46 个 + Phase 2.5 新增 22 个（tests/parsers/test_html_text.py 13 个、
# tests/collectors 里 4 个新增用例（Bitunix/Weex raw_category + 清洗校验、Zoomex
# raw_category=menu_id）、tests/test_migrate_v2.py 5 个）。
```

## Phase 2.6 完成情况：category_mapping.yaml 修复 + 真实数据核验

Phase 2.5 验收通过、报给用户 review 后，收到一份很扎实的复盘，指出 Phase 2.5 引入了一个
**会让 Phase 3 直接失效**的新缺陷，外加两个连带问题、两条留给批次 3 的设计笔记。本节记录
修复过程；**本 session 同样不实现任何新的源**，只改配置文件、`operations.py` 的一处分支逻辑，
以及配套文档/测试。

### 致命问题：category_mapping.yaml 的 key 类型选错了

Phase 2.5 创建 `config/category_mapping.yaml` 时，key 用的是人类可读 section name（照抄
README 早前的规划草案），但 `announcements.raw_category` 落库的是源端**原始值**（数值
`section_id`/`menu_id`）。Phase 3 第一层映射会拿 `"18540264809497"` 去查
`"Latest Announcements"`——**逐字节对不上，一条都命中不了**，而且这个失败是静默的：不报错，
只是第一层映射形同虚设，全部落到关键词层和 LLM 层，分类准确率和 LLM 调用成本都会跟着崩，
不会在跑批时报错，只会在人工抽查时才发现。

修复：`category_mapping.yaml` 全部改成以 `raw_category` 的原始值（字符串）做 key，人类可读
名称写成行内注释，仅供人工核对、不参与匹配。这样 `raw_category → category` 变成一次纯字典
查找，零运行时 HTTP、零字符串匹配脆弱性；YAML 里查不到某个 ID = 源站新增了分区，正好是一个
显眼的信号（该去补映射，而不是静默吞掉）。

### 连带问题：Phase 2.5 版本的映射值本身也没有实测校验

Phase 2.5 创建 `category_mapping.yaml` 时用的是 README 早前记录的初始值，README 自己也承认
这是"规划草案"，从未用真实请求核对过（Phase 0 完成情况的勾选列表里，`category_mapping.yaml`
甚至不存在——这个文件是 Phase 2.5 才创建的）。这直接踩了项目第 6 条铁律"不允许猜测数据"的线。

Bitunix/Weex 都是标准 Zendesk，`GET /api/v2/help_center/{locale}/sections.json` 一次真实请求
就能拿到全部 section 的 `id` + `name`，成本很低，本 session 补上了：

```
GET https://support.bitunix.com/api/v2/help_center/en-us/sections.json?per_page=100
GET https://weexsupport.zendesk.com/api/v2/help_center/en-us/sections.json?per_page=100
```

用返回结果反查 `category_id=13760946490649`（Bitunix "Announcements" 分类）下的全部 8 个
section，`category_id=18540264809497`（Weex "Latest Announcements" 分类）下的全部 4 个
section，再用当天真实采集到本地库的数据交叉验证——`data/competitor_intel.db` 里
Bitunix EN 1534 条、Weex EN 1038 条各自出现的 `raw_category` 值集合，跟 sections.json 反查
出来的 ID 集合**逐一对上**（Bitunix 8 个 ID 分别对应 746/228/216/115/111/108/8/2 条，
Weex 4 个 ID 对应 349/253/233/203 条，合计正好等于全量）。结论：README 当初记录的人类可读
名称本身碰巧是对的（Bitunix 8 个、Weex 4 个名字都跟真实 section name 逐字匹配），运气好，
但"没验证就用"这个过程本身是不对的，这次把验证补上，`category_mapping.yaml` 顶部注释里
留了完整的核对方法，供以后复查。

**顺带发现一个真实的覆盖缺口（超出本次映射修复范围，未擅自处理）**：核对 Weex 的 sections
时发现 `weexsupport.zendesk.com` 还有一个独立分类 `category_id=44507081559193`
「Listings/Delistings」，下辖 4 个 section（New spot listings / New futures listings /
Spot delisting/maintenance / Futures delisting/maintenance），当前 `sources.yaml` 的 weex
endpoint 只拉「Latest Announcements」这一个分类，完全没有覆盖这 4 个 section——也就是说
**Weex 的上架/下架公告目前根本没有被采集**，而推送规则里 `delisting`/`listing` 都是有专门
处理逻辑的类别。这跟 Phase 1 补充侦察当初给 Phemex 补 Activities/Newsletter 是同一类缺口，
但修复方式要新增一个采集 endpoint、改动已经"验收通过"的 Weex 批次 1 结果，改动范围明显
超出本次"修 category_mapping.yaml"的授权范围，所以只如实记录在
`config/category_mapping.yaml` 的 weex 块注释里，不擅自新增采集范围，留给用户决定是现在
补、还是并入批次 3/4 一起处理。

### 连带问题：Phemex 的 raw_category 设计从一开始就是错的（批次 3 尚未实现，提前订正）

核对 Bitunix/Weex 时顺带检查了 Phemex 的 `field_mapping.category: category.name`，发现这个
字段是 locale 相关的翻译文本，不是稳定标识——同一个逻辑分类，EN 抓到的响应里
`category:{id:432,name:'News',...}`，FR 抓到的是 `category:{id:438,name:'Nouvelles',...}`
（`tests/fixtures/phemex_EN.html` / `phemex_FR.html` 均可复现）。这个事实 Phase 1 侦察时其实
已经记录过（`sources.yaml` phemex FR 块注释早就写了 "category_id 438 对应 name:'Nouvelles'"），
只是当时没有连到"如果 raw_category 真存这个字段，`category_mapping.yaml` 就得按 locale 拆、
还要覆盖所有语言翻译"这个后果上——跟 Bitunix/Weex 用人类可读名称当 key 是同一类问题，会在
Phemex 批次重演。

Phemex 采集器还没实现（批次 3），趁现在改动成本最低时订正设计：`raw_category` 改存"抓取时
用的哪个 `categories.*` 子源 key"（`news`/`activities`/`newsletter`，locale 无关，本来就是
`sources.yaml` 配置里的字面量，采集时天然知道，不需要解析响应字段）。`sources.yaml` 的
Phemex EN/FR 块和 `category_mapping.yaml` 的 phemex 块都已经按这个设计更新注释/写好映射，
批次 3 实现 `PhemexCollector.normalize()` 时直接用这个 key，不要去解析 `category.name`。

### 问题：raw_category 在 unchanged 分支不会更新

`upsert_announcement` 原来的 `unchanged` 分支只更新 `fetched_at`，`raw_category` 一旦落库
就不再变了。但源端分类归属是会变的：Zendesk 后台把一篇公告从一个 section 挪到另一个（正文
一个字没改），`content_hash` 不变 → 判 `unchanged` → `raw_category` 就此永远停在第一次抓到
的旧值，分类错到底且没有任何信号提示。

修复：`src/db/operations.py` 的 `upsert_announcement`，`unchanged` 分支里如果传入的
`raw_category` 跟库里已存的不同，UPDATE 这一列——但**不**改 `status`（仍是 `unchanged`）、
**不**动 `push_status`、**不**写 `content_history`：分区变动不是内容变更，不该触发推送或
产生历史版本。`tests/test_db.py` 新增
`test_unchanged_content_but_raw_category_moved_updates_raw_category_only`，显式断言
`push_status`（用一个已经 `pushed` 的场景）和 `content_history` 都不受影响，只有
`raw_category` 被更新。

一篇文章同属多个分类（比如 Phemex 的 News/Activities 是否互斥）目前没有实测记录，`uid` 本身
不含 category，如果真的互斥关系不成立，第二次抓到时会用后一个分类覆盖前一个，静默丢失一个
归属——批次 3 实现 Phemex 时建议顺手确认一下 News/Activities/Newsletter 之间是否存在同一篇
文章跨子源出现的情况。

### 记录但不现在做：清洗器版本化

`content_hash` 现在是"清洗后正文的 hash"。`html_text.py` 的模板剔除规则目前只用 Bitunix/Weex
的真实样本调过，批次 3/4 拿到 Phemex/BingX/Lbank 的真实样本后大概率还要继续调整规则（比如
这几个源的原始页面是完整 SSR HTML，不是 Zendesk 那种已经剥离过页面壳的纯正文片段，会遇到
`html_text.py` 目前的 nav/footer/disclaimer 启发式没覆盖到的新模式）。`html_text.py` 每变一次
行为，同一份 HTML 就会产出不同的纯文本、不同的 hash——日常 watermark/needs_detail 增量不会
重抓旧条目所以不会立刻爆，但任何一次 `--force-full`（或未来的 `--backfill`）都会把存量数据
批量判成 `changed`，正是 Phase 2.5 想要消灭的技术噪音，只是触发方式从"一次性 HTML→纯文本
切换"变成了"清洗器每次迭代"。

现在上 `cleaner_version` 列这类版本化机制还太早（过度设计）。约定：**批次 3/4 里每次改动
`html_text.py` 的清洗规则，都在 CLAUDE.md 里显式记一行**，并在改动后主动对已入库的
Bitunix/Weex 跑一次 `--force-full`，把噪音 `changed` 一次性消化掉，而不是留到 Phase 6 上线后
在推送里炸出来。是否引入 `cleaner_version` 列，等 Phase 8 上线前再决定。

### 记录但不现在做：BingX full_scan 的首屏窗口盖不住新增

BingX 首屏 ~20 条是跨 12 个分区的聚合视图，且 `createTime == updateTime` 恒等。如果直接把
首屏列表当 `fetch_list()` 的数据源，发布密集的一天只要超过 20 条，多出来的公告会被挤出窗口、
**永久漏采**——不是"变更检测不到"，是新增条目从未进入过 `fetch_list()` 的返回值，无法靠任何
下游逻辑补救。

批次 3 实现 BingX collector 时的设计要点（已写入 `sources.yaml` bingx 块注释）：日常增量
`fetch_list()` 改用 sitemap diff——拉 `en/sitemap-support.xml`（已确认扁平覆盖全部 12 个
分区），跟 DB 里 `source='BingX'` 已有的 `article_id` 集合求差集，差集即新增文章，只对差集
请求详情页（省网络）。sitemap diff 能发现"新增"，发现不了"存量文章被编辑但 URL 不变"，如果
要覆盖这种场景，仍需要 full_scan 语义（定期对已入库文章重新请求详情页比对 `content_hash`），
可以用首屏 ~20 条兼顾"最近编辑"这个子集，不需要对全部历史文章都重新请求详情页。首屏/详情页
仍然要解析——它是唯一能拿到 `sectionId`（即 `raw_category`）的地方，sitemap 只有 URL。

### 验收记录（2026-07-14）

`category_mapping.yaml` 改动是配置 + 只读校验（对已有的 `data/competitor_intel.db` 跑
`SELECT DISTINCT raw_category`，不涉及新的采集请求），不需要重跑 collector 验证幂等；
`operations.py` 的改动通过单测覆盖：

```
python -m pytest
# 69 通过：Phase 2.5 的 68 个 + Phase 2.6 新增 1 个
# （test_unchanged_content_but_raw_category_moved_updates_raw_category_only）
```

交叉核验脚本（用真实采集到的数据校验 `category_mapping.yaml` 的 key 是否完全覆盖）：
```python
import yaml, sqlite3
c = yaml.safe_load(open('config/category_mapping.yaml'))
conn = sqlite3.connect('data/competitor_intel.db')
for source, key in (('Bitunix','bitunix'), ('Weex','weex'), ('Zoomex','zoomex')):
    db_ids = set(r[0] for r in conn.execute('SELECT DISTINCT raw_category FROM announcements WHERE source=?', (source,)))
    yaml_ids = set(c[key].keys())
    assert db_ids <= yaml_ids  # 库里出现的每个值，映射表里都必须能查到
```
结果：Bitunix/Weex/Zoomex 三个源，DB 里出现的 `raw_category` 值均是 `category_mapping.yaml`
对应 key 集合的子集，无遗漏。

## Phase 2.7 完成情况：Weex Listings/Delistings 分类补采 + 多语言数据补齐

进 Phase 3 前的最后一步补齐。**本 session 不实现任何新的源**（Phemex/BingX/Lbank 仍留给
批次 3/4）。背景：Phase 2.6 核对 category_mapping.yaml 时"撞见"了 Weex 有一个完全没被
采集的分类（Listings/Delistings），如实记录但没有擅自扩大采集范围。这个缺口是撞出来的、
不是查出来的——本 session 的任务 1 就是把"撞"变成"系统性查一遍"，避免同类缺口再次靠运气
发现。

### 任务 1：Zendesk 顶层 category 主动核查（真实请求，非抽样猜测）

用 `GET .../categories.json` 枚举 Bitunix / Weex 的全部顶层 category，逐个用
`GET .../categories/{id}/articles.json?per_page=1` 取 `count`，判断依据是「内容是不是
公告类（活动/上新/下架），还是静态客服文档（怎么用某功能、条款协议）」——用标题抽样
（而不是只看 category 名字）做判断，因为名字本身可能有歧义（如 Weex 的 "User Guide"
听起来纯 FAQ，但抽样发现混了几条功能上线通知）。

**Bitunix（13 个顶层 category，均已用真实请求确认）：**

| category_id | name | 文章数 | 判定 |
|---|---|---|---|
| 13760946490649 | Announcements | 1534 | 已覆盖 |
| 45187015952793 | Bitunix Video Zone | 45 | 不在范围内——教学视频合集 |
| 13645480136985 | Futures Trading | 29 | 不在范围内——抽样标题全是操作指南/FAQ（"Stock Perps Trading Guide"、"What is futures trading?"），无日期驱动的活动/公告 |
| 13645374757529 | Tutorials | 34 | 不在范围内——同上，操作教程 |
| 46015276048537 | Self-service | 15 | 不在范围内——账号自助操作指南 |
| 13762081511577 | About Bitunix | 15 | 不在范围内——公司介绍/联系方式 |
| 13645385807129 | Deposit/Withdrawal | 13 | 不在范围内——充提操作指南 |
| 17990160259225 | Buy/Sell Digital Assets | 12 | 不在范围内——买卖操作指南 |
| 15666981305497（KYC 等，实为 13645374757529 子集） | — | — | 已含在 Tutorials 内，不重复列 |
| 32133168302233 | Copy Trading | 5 | 不在范围内——抽样标题全是操作指南/协议（"Bitunix Copy Trading Operation Guide"），无活动公告 |
| 46013007195673 | Security Column | 3 | 不在范围内——安全白皮书/储备证明，静态文档 |
| 44381705229337 | Earn | 4 | 不在范围内——抽样标题是产品说明/用户协议（"What is Bitunix Earn"），不是活动公告 |
| 47133760146713 | VIP and Rewards | 1 | 不在范围内——单篇 "Introduction to VIP Benefits"，介绍性文档 |

结论：Bitunix 没有第二个缺口，"Announcements" 是唯一的公告类 category，跟 Phase 1/2.6
的判断一致，这次是从"顶层枚举"的角度重新确认了一遍（Phase 2.6 只反查过已知 category_id
下的 section，看不到平级的其它 category）。

**Weex（9 个顶层 category，均已用真实请求确认）：**

| category_id | name | 文章数 | 判定 |
|---|---|---|---|
| 18540264809497 | Latest Announcements | 1038 | 已覆盖 |
| 44507081559193 | Listings/Delistings | 3199 | **本次新增覆盖**（详见任务 2） |
| 49344046693529 | P2P Trading | 33 | **待定**，理由见下 |
| 4410757386393 | User Guide | 83 | 不在范围内（但信噪比低，见下） |
| 6659177619865 | Copy Trading | 48 | 不在范围内——跟单交易操作指南/FAQ |
| 4410857800345 | FAQ | 41 | 不在范围内——KYC/账号操作/API 报错等客服问答 |
| 4411285856153 | About WEEX | 39 | 不在范围内——抽样确认是隐私政策/各类服务条款（ToS），静态法律文档 |
| 4467195499673 | Quick Buy/OTC | 6 | 不在范围内——法币购买渠道教程 |
| 25845867857433 | App Download | 5 | 不在范围内——App 下载引导 |

**P2P Trading（33 篇）：待定项已由用户拍板，采纳「现在就补」，见任务 2c。** 这个
category 下有 4 个 section，其中 3 个（Start P2P trading / P2P merchant guide /
P2P Order Appeal）抽样确认是纯 FAQ（"How to post ads as a regular user"、申诉流程等），
但第 4 个 section **"P2P Announcement"**（id=49365180202777）抽样 20 条全部是区域专属
活动/新增法币支持这类内容（"[Vietnam Exclusive] P2P flash sale: Buy USDT at 20% off"、
"WEEX P2P now supports Nigerian Naira (NGN) — 0 Trading Fees"、"Russia-exclusive P2P
offer"），跟 Latest Announcements 的公告没有本质区别——只是被 Weex 归到了 P2P Trading
这个大分类下的一个子 section，不是独立的顶层 category。用户确认后，只拉了
"P2P Announcement" 这一个 section，不是整个 P2P Trading category（避免混入其余 3 个
section 的纯 FAQ 噪音）。

**User Guide（83 篇）判定为不在范围内，但记录信噪比问题**：绝大多数是交易术语词典
（"Limit Order"、"Maker and Taker"、"BTC"/"ETH" 等纯概念解释）和功能操作说明书，仅有
2 条标题带"Launches"/"is now available"字样的功能上线通知混在同一个 section
（"Futures trading features"）里。83 篇里只有 2 篇疑似公告内容，信噪比太低，不建议
整类收录；如果以后想要这类"功能上线"通知，需要比 category/section 更细的过滤（不是
本次能安全自动化的判断），暂不处理。

结论：Weex 的真实缺口是 Listings/Delistings（category 级）+ P2P Announcement
（section 级）两个，本次均已修复，见任务 2。

### 任务 2：Weex 多分类改造 + 两个连带的架构修复

#### 2a. 配置与 collector 改造（参照 Zoomex 的 categories.\* 模式）

`config/sources.yaml` 的 weex EN/FR 块从单一 `endpoint` 改成 `categories: {latest_
announcements: {endpoint: ...}, listings_delistings: {endpoint: ...}}`，两个 category
各自的 endpoint 是 `.../categories/{category_id}/articles.json`（18540264809497 /
44507081559193，均已用真实请求验证）。

`src/collectors/zendesk_base.py`：`ZendeskCollector.__init__` 新增 `category_key: str
= ""` 参数，设置 `self.category`（crawl_state 第三个 key）。默认空字符串保证向后兼容
——Bitunix（单分类，config 没有 `categories` 结构）、以及旧测试里 `BitunixCollector(
"EN", cfg)` 这种两参数调用方式完全不受影响，`category` 恒为 `''`，跟批次 1 行为一致。

`src/collectors/__main__.py`：把原来 Bitunix/Weex 专用的 `_single_collector_builder`
换成 `_zendesk_builder`（复用 Zoomex 的多分类展开模式）——有 `categories` 结构就展开成
多个 collector 实例（每个用自己的 endpoint + category_key），没有就退化成原来的单实例
行为。`COLLECTOR_BUILDERS["bitunix"]`/`["weex"]` 都改用这个统一 builder。

#### 2b. 意外撞见的真实 bug：Zendesk 经典 offset 分页有硬性 page=100 上限

按原计划改完配置后首次真实采集 `weex --locale EN` 时，`listings_delistings`
（3199 篇，per_page=30）在翻到 `page=101` 时收到 **HTTP 400**，`latest_announcements`
（1038 篇）不受影响。排查发现：Zendesk 的经典 offset 分页（`page=`/`per_page=`）不管
`per_page` 设多大，**硬性限制最多翻到 page=100**——超过这个页码直接 400，即使响应里
明明白白给了一个 `next_page` 链接指向 page=101（服务端自己生成了一个会 400 的链接，
不是我们拼错的）。Bitunix（1534 篇，per_page=100，16 页）和 Weex latest_announcements
（1038 篇，per_page=30，35 页）都在 100 页以内，之前从未触发过这个上限，纯粹是运气好。

用真实请求验证了修复方案：改成 Zendesk 支持的 **JSON:API cursor 分页**
（`page[size]=N` + `page[after]=<cursor>`，响应体 `meta.has_more`/`meta.after_cursor`），
完整翻完 3199 条无障碍（32 次请求，`page[size]=100`）。过程中又撞见第二个 bug：响应里
的 `links.next` 字段本身有问题——URL 缺了 `.json` 后缀（`/articles` 而不是
`/articles.json`），直接请求这个链接会 **415 Unsupported Media Type**。解决办法是不
依赖 `links.next`，只取 `meta.after_cursor` 这个 opaque 游标值，用 collector 自己已知
的 `endpoint`（含 `.json` 后缀）重新拼 URL。

也验证了 cursor 分页下 `sort_by=updated_at&sort_order=desc` 仍然生效（3199 条抽样全程
严格降序），watermark「遇到 update_time <= since 就提前退出翻页」的核心机制不受影响。
cursor 分页已在 Bitunix（support.bitunix.com）和 Weex（weexsupport.zendesk.com）两个
不同的 Zendesk 实例上验证可用，判定为标准 Zendesk API 能力（非账号定制），**改成对
Bitunix 和 Weex 全部生效**，不是只给 listings_delistings 打补丁——offset 分页的
page=100 硬上限是系统性风险，任何 category 只要文章数超过 `100 × per_page` 就会复现
同样的 400，一次性把这类风险消除比逐个类别加特判更稳妥。

`src/parsers/zendesk.py` 的 `get_next_page(payload) -> Optional[str]`（读
`next_page` 字段，返回完整 URL）替换为 `get_next_cursor(payload) -> Optional[str]`
（读 `meta.after_cursor`，只返回游标值，不返回 URL——URL 拼接是 collector 的事，因为
需要 collector 已知的 `endpoint`）。`src/collectors/zendesk_base.py` 的
`fetch_list()` 相应改用 `_page_url(cursor)` 自己拼页面 URL。`config/sources.yaml`
里 Bitunix/Weex 全部 5 个 `pagination` 配置块从 `{type: offset, param: page,
page_size_param: per_page, page_size: N}` 改成 `{type: cursor, page_size: N}`
（`param`/`page_size_param` 字段已经不被代码读取，避免留着误导人）。

#### 2c. Weex P2P Announcement section（用户确认后追加，同一 session 内完成）

任务 1 发现的待定项——Weex `P2P Trading` category 下的 `P2P Announcement` section
（section_id=49365180202777，跟公告没有本质区别但同 category 下另 3 个 section 是纯
FAQ）——报给用户后确认「现在就补」。验证方式：Zendesk 标准 API 除了
`/categories/{id}/articles.json`，同样支持 `/sections/{id}/articles.json`（真实请求
验证过，cursor 分页机制完全一样），不需要额外写代码——直接在 `sources.yaml` 的
`weex.EN/FR.categories` 下加第三个 key `p2p_announcement`，`endpoint` 换成
section 级 URL 即可，`_zendesk_builder`/`ZendeskCollector` 的多分类展开逻辑对
"category 端点" 还是 "section 端点" 完全无感（只认 `endpoint` 字符串）。

`config/category_mapping.yaml` 的 weex 块新增 `"49365180202777": campaign`
（P2P Announcement，注释里记录真实采集 EN total=17 / FR total=6，抽样标题确认是
campaign 性质）。真实采集：

```
python -m src.collectors --source weex --locale EN --category p2p_announcement   # new=17
python -m src.collectors --source weex --locale FR --category p2p_announcement   # new=6
```

23 条全部无残留 HTML 标签，`raw_category` 均为 `49365180202777`，`category_mapping.yaml`
覆盖检查通过。

### 任务 3：多语言数据补齐 + 真实数据验证

真实采集命令与结果（`--force-full` 的 changed 计数见任务 4，这里是首次采集的
new/unchanged）：

```
python -m src.collectors --source bitunix --locale FR    # new=906
python -m src.collectors --source bitunix --locale ID    # new=407
python -m src.collectors --source weex --locale EN       # latest_announcements: unchanged=0（水位线挡住，
                                                           #   之前已采集过 1038 条）；listings_delistings: new=3199
python -m src.collectors --source weex --locale FR       # latest_announcements: new=207；listings_delistings: new=1100
```

各源 × locale 入库条数（任务 3 完成时点，不含任务 2c 之后追加的 P2P Announcement）：

| source | locale | 条数 |
|---|---|---|
| Bitunix | EN | 1534 |
| Bitunix | FR | 906 |
| Bitunix | ID | 407 |
| Weex | EN | 4237（latest_announcements 1038 + listings_delistings 3199） |
| Weex | FR | 1307（latest_announcements 207 + listings_delistings 1100） |
| Zoomex | EN | 40（批次 2 遗留，未动） |

**本 session 最终**（任务 2c 追加 P2P Announcement 后）：Weex EN = 4254（+17），
Weex FR = 1313（+6），全库合计 8454 条，其余源不变。

Weex listings_delistings 按 raw_category（= section_id）拆分：EN 里 New spot listings
2529 条远超 New/Futures delisting 合计（144+188=332）——符合直觉，新上币公告频率远高于
下架，下架通常一次公告涵盖多个交易对。

**group_id 跨语言一致性验证**（`group_id = f"{prefix}_{article_id}"`，Zendesk
article_id 本身跨 locale 一致是这个设计成立的前提）：

| source | 总 group 数 | 跨 >1 locale 的 group 数 | 分布 |
|---|---|---|---|
| Bitunix | 1534 | 915 | 3 locale: 398 / 2 locale: 517 / 1 locale: 619 |
| Weex | 4240 | 1304 | 2 locale: 1304 / 1 locale: 2936 |
| — | 异常（跨 locale 数超过该源实际 locale 数）| 0（两个源都是 0）| — |

抽查 5 组跨语言 group（每个源各 5 组，共 10 组）人工核对标题语义，确认均为同一篇公告的
不同语言版本翻译（如 `bitunix_59907915093657`：EN "Bitunix to Launch GEVUSDT, VRTUSDT"
/ FR "Bitunix va Lancer GEVUSDT, VRTUSDT" / ID "GEVUSDT, VRTUSDT Terdaftar di
Bitunix!"——同一个上币事件），没有发现 id 撞车（不同公告碰巧用了同一个 article_id）的
情况。

### 任务 4：html_text.py 真实 bug——表格单元格嵌套 `<p>` 导致内容丢失

Weex 新增的上币公告（listings_delistings）暴露了一个 html_text.py 此前没覆盖到的真实
HTML 模式：交易对信息表格的单元格用 `<td><p>文字</p></td>` 包裹（不是
`<td>文字</td>` 直接文本节点），批次 1/2.5 用来调 html_text.py 的样本里没有这种嵌套。

**具体 bug**：`<p>` 是块级标签，块级标签的处理逻辑原来是无条件调用顶层 `_flush()`
（把 `self._buffer` 推进 `self.blocks` 并清空）。但在表格单元格内部时，`_start_cell()`
已经把 `self._buffer` 清空并开始收集这个单元格自己的文字；单元格内如果嵌套一个
`<p>`，`</p>` 结束标签触发的 `_flush()` 会把这段文字提前推到顶层 `self.blocks`（脱离
表格结构，变成一个独立段落），然后 `</td>` 触发的 `_end_cell()` 拿到的 `buffer` 已经
被清空——整行变成一串没有内容的空 tab（`\t\t\t\t`）。真实样本
`article_id=56648741969433`（"Initial listing: ADI (ADIUSDT)..."）修复前的输出：表头
"Trading pair"/"Launch time"/... 和数据 "ADIUSDT"/"Apr 4, 2026"/... 都变成了独立的
`\n` 分隔段落，表格本身塌成 `\t\t\t\t\n\t\t\t` 这样的空壳。

**修复**：`_HtmlTextExtractor` 新增 `_block_boundary()` 方法，取代原来块级标签处理里
直接调 `_flush()`——判断 `self._in_cell`：单元格内部只在 buffer 里插入一个空格分隔
（避免"line1line2"连读，不产生新的顶层 block）；单元格外部行为不变（原来的
`_flush()`）。修复后同一篇文章正确输出
`"Trading pair\tLaunch time\tLeverage\tMargin mode\tTrade\nADIUSDT\tApr 4, 2026,
08:50 (UTC+0)\t1 – 20× (adjustable)\tCross margin Isolated margin\n..."`。
`tests/parsers/test_html_text.py` 新增两个测试：一个用最小化样本复现并锁定这个 bug
的修复（`<td><p>text</p></td>` 结构），另一个是反例——确认"源站本来就没填内容的空
单元格"（如获奖名单待公布的占位表格）不受这次修复影响，仍然正确输出空 tab（不是这次
修的 bug，是真实存在的稀疏表格）。

**扫描 + 消化噪音**（Phase 2.6 定的约定：改了清洗规则要记录 + 对已入库数据跑
`--force-full` 消化噪音 changed，不留到 Phase 6 上线后在推送里炸出来）：

修复前全库扫描（8431 行，含本次新采的 Weex listings_delistings）：128 行命中"连续多个
tab"的可疑模式（121 Weex + 7 Bitunix）。对 Bitunix EN/FR/ID、Weex EN/FR 依次跑
`--force-full`：

```
Bitunix EN: changed=11 unchanged=1523
Bitunix FR: changed=7  unchanged=899
Bitunix ID: changed=5  unchanged=402
Weex EN (latest_announcements):  changed=90  unchanged=948
Weex EN (listings_delistings):   changed=131 unchanged=3068
Weex FR (latest_announcements):  changed=11  unchanged=196
Weex FR (listings_delistings):   changed=69  unchanged=1031
```

合计 323 行被判定为 `changed`（比 128 略多，因为这次修复影响的不只是"全空单元格"这一种
极端表现，任何嵌套 `<p>` 的单元格哪怕本身有内容也会受影响，只是不一定表现为连续空
tab）。force-full 后重新扫描：全库仍命中的"连续 tab"模式从 128 降到 15，逐条对照真实
源 HTML 确认这 15 条都是源站本来就发布的稀疏表格（如中奖名单模板里 UID/ROI 列留白待
公布，`<td> </td>` 里就是一个空格，不是解析丢的），不是本次 bug 的残留，不需要继续处理。
另有一条误报（Weex FR article_id=52538068089113，正文里有一段疑似遗留的 CAT 翻译工具
占位符字面量 `{B>...<B}`，被我自己验证用的简单正则误判成"HTML 标签"；核对原始响应后
确认这是源站内容本身的瑕疵，不是标签，html_text.py 没有处理错，不需要改代码）。

### 已知遗留（如实记录，未处理）

- **crawl_state 有一条孤儿行**：`(Weex, EN, category='')`（Phase 2.7 改多分类结构前
  的旧水位线，产生于本 session 更早时候一次配置改造中间态的真实运行）。不影响功能
  （collector 现在只会用 `category='latest_announcements'`/`'listings_delistings'`
  查询/写入，这条 `''` 的行永远不会再被读到），纯粹是历史痕迹，dev 环境删库重建可以
  自然清掉，未做额外清理脚本（不是本次任务范围）。
- ~~Weex "P2P Announcement" section~~ —— 已解决：用户确认后在任务 2c 里补采集，见上文。
- **Weex "User Guide" 里 2 篇功能上线通知**：信噪比太低不建议整类收录，也未做更细粒度
  的按文章过滤。

### 验收记录

```
pytest
# 82 通过：Phase 2.6 的 80 个 + Phase 2.7 新增（parsers/test_zendesk.py 分页测试从
# next_page 改写成 get_next_cursor 语义、collectors/test_zendesk_collectors.py 新增
# 多分类/cursor 分页测试、collectors/test_main_builders.py 新文件覆盖 _zendesk_builder
# 单分类向后兼容 + 多分类展开 + --category 过滤、parsers/test_html_text.py 新增表格
# 嵌套 <p> 回归测试）
```

单分类源（Bitunix）向后兼容验证：`ZendeskCollector.__init__` 不传 `category_key` 时
`self.category == ""`，`crawl_state` 主键仍是 `(source, locale, '')`，跟批次 1/2.5/2.6
时代的行为逐字节一致，没有因为 Weex 改多分类而破坏 Bitunix 的既有路径。

## Phase 3 完成情况：跨语言归组一致性扫描 + 分类打标 + 地区独占标记

### 范围（有意的切分，不是欠债）

只处理 Bitunix + Weex（8414 条，EN/FR/ID）；Zoomex（40 条，批次 2 遗留数据）顺手一起
跑了，不作为验收对象。Phemex/BingX/Lbank 的 collector 还不存在，它们的 category_mapping
和 Phemex 的 i18n 归组问题**明确留白**，架构上 `src/pipeline/` 各模块都以 `sources` 参数
传入源列表，接入新源只需要 collector 落库 + category_mapping.yaml 补映射，不需要改
pipeline 代码。清洗（Phase 2.5 已前移到采集层）和 `content`/`content_hash` 本 Phase 完全
不碰——任何对它们的回写都会触发 `upsert_announcement` 的变更检测，制造技术噪音。本
Phase 的写操作只有两列：`announcements.category`、`announcements.is_region_exclusive`，
全部走裸 `UPDATE`，不经过 `upsert_announcement`。

### 架构：`src/pipeline/`

- `config.py`：`load_category_mapping()`（读 `config/category_mapping.yaml`）、
  `load_source_locales()`（解析 `config/sources.yaml`，从每个源的真实配置结构里提取
  locale key 集合——用正则 `^[A-Z]{2}(-[A-Za-z]+)?$` 区分 locale key（EN/FR/ID/VN/
  EN-Asia）和其它小写配置 key（endpoint/method/...），不是写死的硬编码表，源新增
  locale 时这里自动跟着更新）。
- `grouping.py`：`scan_group_consistency()`，纯防御性扫描，不做任何归组决策
  （group_id 在采集阶段已经生成好，Phase 2.7 已用真实数据验证过一致性）。
- `category.py`：三层分类，见下一节。
- `region.py`：地区独占标记。
- `eval.py`：分层抽样人工核验工具。
- `__main__.py`：`python -m src.pipeline {group-check,classify,region,eval}`。

### 1. 跨语言归组一致性扫描

对 Bitunix+Weex+Zoomex 共 5831 个 group 做了两类防御性检查：同一
`(source, article_id)` 是否拼出了不止一个 `group_id`（应该为 0，group_id 拼接是
确定性函数）；某个 group 里出现的 locale 数是否超过该源实际配置的 locale 数（应该
为 0，超过说明 group_id 撞车或 locale 配置有问题）。

```
python -m src.pipeline group-check
# 检查了 5831 个 group。group_id 重复异常：0 条。locale 数溢出异常：0 条。PASS。
```

### 2. 分类打标：三层结构，本次只实现前两层

第一层（`raw_category` 精确字典查找）+ 第二层（标题关键词，仅对第一层映射到
`other` 或 `raw_category` 为 NULL 的行生效）。**先跑 dry-run 摸底，落到第三层
（LLM 兜底）的行数为 0** —— Bitunix/Weex/Zoomex 当前入库的每一个 `raw_category`
值都已经在 `category_mapping.yaml` 里有映射（Phase 2.6/2.7 验证过的覆盖关系），
所以第三层在本次范围内**没有实现**，`classify_row()` 保留了 `llm_pending` 这个
分支（返回 `category=None`），等 Phemex（`raw_category` 可能有新分类）或 Lbank
（`raw_category` 恒 NULL，会大量落到关键词层，关键词也不命中的会落到第三层）接入
时再实现。

```
python -m src.pipeline classify --dry-run --sources Bitunix,Weex,Zoomex
# 共扫描 8454 行
#   native             8008  ( 94.7%)
#   native_other        169  (  2.0%)
#   keyword             277  (  3.3%)
#   unmapped_native       0  (  0.0%)
#   llm_pending           0  (  0.0%)
```

**开发过程中发现并修复了一个真实的关键词匹配 bug**：`classify_by_keyword()` 最初
用纯子串包含判断关键词是否命中，但 "delisting" 逐字节包含 "listing"（de-**listing**），
listing 分类的关键词列表排在 delisting 前面（按 prompt 给定的优先级 listing >
delisting > campaign > product > other），导致任何真正的下架标题只要落到关键词层
（`raw_category` 映射到 other 或为 NULL 的行），就会被 "list"/"listing" 关键词提前
误判成 listing，delisting 关键词组永远没有机会被检查到。真实入库数据里已经复现了
2 条（Weex `article_id=49415263379481`"Notice on Delisting of Some Migrated Spot Pro
Trading Pairs"、`48357385309081`"BLUM Contract Delisting and Compensation Application
Announcement"，两条 `raw_category=18540289930137`"Latest updates"→other，第一版代码
误判成 listing）。修复：ascii 关键词改用词边界正则 `\bkeyword\b` 而不是子串包含——
`\blist\b`/`\blisting\b` 不会命中 "delisting" 内部（"delist" 和 "ing" 之间没有单词
边界），但 "delisting" 关键词本身（整词）能正确匹配。中文关键词（"下架"）保留子串
包含，因为中文文本没有空格分词，`\b` 在连续汉字之间几乎不产生边界，用词边界正则
反而会让合法的中文关键词匹配不上。修复后 `native_other` 从 163 升到 169、`keyword`
从 283 降到 277（净变化 6，不是 2——一部分是同一个 bug 在其它类目组合下的连带影响），
已对已写入的 8454 行重新跑 `classify --apply` 更正。

**已知限制（如实记录，不是本次修复范围）**：修复后仍有 43 行标题包含
"delist"/"下架" 但最终 `category` 不是 `delisting`——逐一核对，这 43 行**不是**
关键词 bug，是**第一层 native 映射的真实结果**：Bitunix/Weex 自己的 Zendesk 后台
把这些下架公告放进了 "Derivatives & Perpetual Futures"（17983093824153，17 行）、
"Products and Services"（16707858318105，10 行）、"Earn"（57068501750297，1 行）、
Weex "New spot listings"（18540509241753，15 行）这些**非下架专属**的 section，
不是 "Delisting"/"Spot delisting/maintenance" 专属 section。本项目的分类设计原则
是信任源端自己的分类归属（raw_category 是 ground truth，第一层优先级高于关键词
层），所以这 43 行按当前设计规则正确地拿到了 native 层给出的 category，只是跟标题
语义直觉不完全一致——这是源站自己的分类粒度/一致性问题，不是本项目的 bug。是否要
为 "标题明确提到 delist 但 raw_category 给出别的分类" 这种情况加一条特判覆盖规则
（会打破"raw_category 优先级高于关键词"的既有设计），留给用户决定，本次未擅自处理。

分类打标结果（`classify --apply` 落库后，8454 行）：

| category | 行数 |
|---|---|
| campaign | 1230 |
| product | 954 |
| listing | 5319 |
| delisting | 717 |
| other | 234 |

### 3. 地区独占标记

按每个源实际配置的 locale 集合（不是全局 locale 集合）判断：某 group 只在一个
非 EN 的 locale 出现 → `is_region_exclusive=true`；只在 EN 出现、或跨多个 locale
出现 → false。

```
python -m src.pipeline region --sources Bitunix,Weex,Zoomex
# 共 5831 个 group，其中 3 个判定为地区独占（全部是 Weex/FR）
# 更新行数：is_region_exclusive=true 3 行，=false 8451 行
```

3 条独占样本：`weex_45604647841433`"USDC/USDT transaction au comptant 0 frais"、
`weex_44208025871769`"...privilèges VIP 2 pendant 30 jours"、`weex_47369739219609`
"WEEX Futures lance le contrat perpétuel HUMA"。跟真实分组统计交叉验证过：Bitunix
的单 locale group 全部是 619 个 EN-only（0 个 FR-only/ID-only），Weex 的单 locale
group 里 2944 个 EN-only + 3 个 FR-only，跟脚本判定结果逐一对上。

### 4. eval 脚本

`src/pipeline/eval.py`：跨 `(source, locale, category)` 分层抽样（不是纯随机——纯
随机会被 Weex 的 listing 类目因基数最大而淹没其它类目），轮询取样直到凑够目标条数
或所有组合耗尽。`python -m src.pipeline eval --sample 30` 人工抽查了 30+ 条，未发现
除上述已修复 bug 外的其它分类错误。

### 验收记录

```
pytest
# 109 通过：Phase 2.7 的 82 个 + Phase 3 新增 27 个
# （tests/pipeline/{test_grouping,test_category,test_region,test_eval}.py，
#  离线临时库，不依赖网络/真实 category_mapping.yaml 内容）
```

### 未做 / 已知限制（有意留给以后）

- 第三层 LLM 兜底：`classify_row()` 的 `llm_pending` 分支已经预留，但函数体没有
  调用任何 LLM——本次范围内 0 行需要它，暂不实现；Phemex/BingX/Lbank 接入后大概率
  会真正用到（尤其 Lbank，`raw_category` 恒 NULL）。
- 43 行"标题语义是下架，但源端 raw_category 归到别的 section"：如实记录，未处理，
  见上文分析，是否加特判规则等用户拍板。
- `crawl_state` 里 Phase 2.7 遗留的 `(Weex, EN, category='')` 孤儿行：跟本 Phase
  无关，未处理（不影响 pipeline，纯历史痕迹）。
- Zoomex 只有 40 条批次 2 遗留数据参与了本次扫描/分类/地区标记，不代表 Zoomex 全量
  （EN 全部 3-4 个 menu_id、其余 4 个 locale）已经跑过 Phase 3 pipeline——等 Zoomex
  全量数据入库后，直接重跑 `python -m src.pipeline classify/region --sources Zoomex`
  即可，不需要改代码。

## Phase 3 之后补丁：Zoomex daily 增量改分页数上限 + 全量建仓

进 Phase 4 之前，用户要求先把 Zoomex（我方基线）的全量历史数据采集齐（此前只有批次 2
遗留的 EN/new_product_announcement 40 条），作为后续分析的基线。采集前，用户同时指出
Phase 2 批次 2 定下的"`fetch_list()` 每轮翻完全部页"这个设计，如果每天都要跑一次，
翻页开销偏大（menu_id 26 EN 552 条 ≈ 19 次列表请求，5 个 locale × 3-4 个 menu_id
合计更多），要求改成 daily 增量默认只翻前若干页。

### 设计改动：`pagination.max_pages` + `force_full` 语义扩展

- `src/collectors/base.py`：`BaseCollector` 新增 `force_full: bool = False` 类属性，
  `run()` 一开始把参数同步成 `self.force_full = force_full`。这是给子类的
  `fetch_list()` 用的一个只读信号——之前 `fetch_list(self, since)` 完全不知道当前是
  daily 增量还是 `--force-full` 全量核查，只能通过 `since is None` 间接猜测（并不
  准确，因为 `since` 对 Zoomex 而言本来就一直被忽略）。默认值 `False` 保证不经过
  `run()`、直接调用 `fetch_list()` 的既有单测行为不受影响。
- `src/collectors/zoomex.py` 的 `fetch_list()`：`force_full=False`（daily 增量，
  即正常不带 `--force-full` 的调用）时最多翻 `pagination.max_pages` 页就停
  （`config/sources.yaml` 5 个 zoomex locale 块均已设为 `5`）；`force_full=True`
  时忽略这个上限，行为等同 Phase 2 批次 2 原设计（翻完全部页）。配合已有的
  `needs_detail()`，daily 增量只对这个页数窗口内、且 `update_time` 变化的条目发详情
  请求。
- **这是一个有意接受的正确性权衡，不是没有代价**（已写入 `zoomex.py` 顶部注释）：
  Phase 2 批次 2 已经用真实数据证伪了"列表接口按任何可靠字段排序"这个假设——既不是
  `update_time` 降序也不是 `created` 降序。也就是说"最近被编辑的文章一定出现在前
  5 页"这个前提本身没有被验证过，无法排除某篇排在第 10+ 页的旧文章被源站编辑、而
  daily 增量的 5 页窗口扫不到它，导致这次更新被漏采（不是永久丢失，下一次
  `--force-full` 全量核查会捕获到，只是两次全量之间存在滞后）。接受这个权衡的前提：
  Zoomex 是我方基线、非竞品情报本身，且需要靠运维定期跑 `--force-full` 兜底
  （具体 cadence 留给 Phase 8 调度设计决定，本次不假设）。
- `tests/collectors/test_zoomex_collector.py` 新增
  `test_fetch_list_caps_pages_when_not_force_full` /
  `test_fetch_list_ignores_page_cap_when_force_full` 两个用例，直接断言翻页次数
  （不只看落库结果），锁定"非 force_full 受 max_pages 限制、force_full 不受限"这两
  条边界行为。

### 全量建仓采集（2026-07-14，真实网络请求）

`python -m src.collectors --source zoomex --force-full`（不带 `--locale`/`--category`，
一次性跑满 5 个 locale × 3-4 个 menu_id = 16 个组合，`--force-full` 忽略新加的分页
上限、也忽略 `needs_detail` 增量判断，逐条请求详情，等价于一次完整历史回填）。

```
source     locale   new    changed  unchanged  failed
Zoomex     EN       552    0        0          0      (platform_announcement)
Zoomex     EN       0      0        40         0      (new_product_announcement，批次2遗留数据，force_full 复核后 unchanged)
Zoomex     EN       101    0        0          0      (platform_events)
Zoomex     FR       266    0        0          0      (platform_announcement)
Zoomex     FR       16     0        0          0      (new_product_announcement)
Zoomex     FR       75     0        0          0      (platform_events)
Zoomex     EN-Asia  126    0        0          0      (platform_announcement)
Zoomex     EN-Asia  66     0        0          0      (exclusive_events)
Zoomex     EN-Asia  3      0        0          0      (new_product_announcement)
Zoomex     EN-Asia  121    0        0          0      (platform_events)
Zoomex     VN       433    0        0          0      (platform_announcement)
Zoomex     VN       35     0        0          0      (new_product_announcement)
Zoomex     VN       68     0        0          0      (platform_events)
Zoomex     ID       56     0        0          0      (platform_announcement)
Zoomex     ID       7      0        0          0      (new_product_announcement)
Zoomex     ID       53     0        0          0      (platform_events)
```

总耗时约 37 分钟（16:26:19–17:03:45），全程 0 failed；途中一次
`getArticleById` 请求收到 `Connection reset by peer`，重试机制在第 1 次重试即成功，
未影响最终结果（`http.py` 的指数退避重试路径第一次在真实网络条件下被触发，行为符合
预期）。

`data/competitor_intel.db` 落库验证：
```sql
SELECT COUNT(*) FROM announcements WHERE source='Zoomex';                 -- 2018
SELECT locale, raw_category, COUNT(*) FROM announcements
  WHERE source='Zoomex' GROUP BY locale, raw_category;
-- 每个 (locale, menu_id) 组合的条数与 sources.yaml 侦察阶段记录的 totalCount 逐一对上
-- （EN-Asia/exclusive_events 是 66 条，比 Phase 1 侦察记录的 65 条多 1——期间新发布了
-- 一篇，属预期内的真实增量，不是核对偏差）
SELECT MIN(post_time), MAX(post_time) FROM announcements WHERE source='Zoomex';
-- 2022-03-09T09:45:29Z ~ 2026-07-14T08:23:19Z（全量历史，远超"过去90天"的建仓需求）
```

抽查发现 8 行 `content` 为空字符串（article_id 2873 五个 locale + article_id 4077
五个 locale 里各自命中的部分），已用真实请求核对详情接口原始响应排除解析 bug：
`article_id=2873` 的 Slate JSON 本身只有一个 `image` 节点、无文字（纯图片公告，
"元旦贺信"类内容），`parse_slate_content` 正确提取出空文本；`article_id=4077`
（"Claim $5,000 in BTC & $5,000 in SPACE X!" 活动）用真实请求确认全部 10 个 locale
的 `content` 字段在源端本身就是空字符串（不是只有某个 locale 缺失）——这是源站数据
本身的特征（该活动详情可能完全依赖图片横幅渲染，不落在这个富文本字段里），不是
本项目的采集/解析缺陷，无需修代码。

### 未做 / 已知限制

- 本次只改了 Zoomex；Bitunix/Weex 的 watermark 早停机制本来就不依赖"翻完全部页"，
  不受这次改动影响，未做任何变更。
- `pagination.max_pages` 目前是纯配置数字，没有做过"5 页够不够覆盖 daily 更新量"
  的实测校准（比如统计 Zoomex 历史上单日新增/编辑条目数分布）——如果后续发现 5 页
  经常不够（比如某天编辑量暴增导致漏采频率变高），应该用真实数据调整这个值，不是
  凭感觉改。
- 定期 `--force-full` 兜底目前没有接入任何调度（`scripts/run_daily.sh` 是 Phase 8
  的事，还没实现），本次只是把"支持这么做"的机制做好，实际 cadence 未落地。

### 数据库清理：只保留本次 Zoomex 全量结果作为唯一标准（2026-07-14）

用户要求把 `data/competitor_intel.db` 清成只有本次 Zoomex 全量建仓跑出来的数据，
Bitunix/Weex（其它平台）的数据全部清空——后续会在明确指定测试时重新单独跑，当前
不作为"标准"数据保留。分两步执行（同一 session 内，第二步是用户看到第一步结果后
追加的更严格要求）：

1. **第一步**：删除 `announcements`/`crawl_state` 里 `source != 'Zoomex'` 的全部行
   （Bitunix 2847 行、Weex 5567 行），`content_history` 靠 `ON DELETE CASCADE`
   自动清空（`src/db/connection.py` 的 `get_connection()` 默认开
   `PRAGMA foreign_keys = ON`，级联生效，未额外手工删）。删除前先把整个 db 文件复制
   一份备份（`data/competitor_intel.db.bak_20260714_170841`，gitignored，不会入
   版本库），保留可回滚的退路。之后对剩下的 2018 行 Zoomex 数据跑了
   `python -m src.pipeline classify --apply --sources Zoomex` +
   `region --sources Zoomex`。
2. **第二步（已撤销，见下方"订正"）**：用户一开始要求"只保留这一轮实际跑出来的
   1978 条"，把批次 2 遗留的 40 条（EN / new_product_announcement / menu_id=123）
   也删掉了。

### 订正：`new`/`unchanged` 是"是否已存在于 DB"的标记，不是"是否属于本轮"的标记

删掉那 40 条后，用户追问「这一轮的 1978 不应该是全量吗，之前的 40 条不应该包含
进去吗」，追问是对的，这暴露了第二步删除背后一个概念性误判：

`status=unchanged` 不代表"这行数据这一轮没跑到、沿用的是旧数据"，而是代表
"`--force-full` 这一轮**确实重新请求了详情、重新算了 `content_hash`**，只是跟 DB
里已有的内容一致，所以没有产生新行，只更新了 `fetched_at`"。这 40 条和其余 1978
条一样，都是本轮 `--force-full` 的真实产出，只是因为在批次 2 时已经入过库，状态
标签是 `unchanged` 而不是 `new`。**本轮真实的全量结果是 1978 + 40 = 2018 条**，
把 1978 当全量、删掉这 40 条，等于凭空抹掉了 EN/new_product_announcement 这个
真实存在（源站 total=40，Phase 1 侦察记录早就confirm过）的分类。

**恢复方式：从清理前的完整备份里精确抽回这 40 行 + 对应 1 行 crawl_state，不重新
发网络请求**（用户要求"不重跑、直接撤回"，因为重新采集可能因为源端内容真的发生
变化而产生跟原始记录不完全一致的字段值，比如 `fetched_at`——精确恢复能保证状态
分毫不差）：

```sql
ATTACH DATABASE 'data/competitor_intel.db.bak_20260714_170841' AS bak;
INSERT INTO announcements
  SELECT * FROM bak.announcements
  WHERE source='Zoomex' AND locale='EN' AND raw_category='123';
INSERT INTO crawl_state
  SELECT * FROM bak.crawl_state
  WHERE source='Zoomex' AND locale='EN' AND category='new_product_announcement';
DETACH DATABASE bak;
```

恢复后发现这 40 行的 `category='product'`、`is_region_exclusive=0` 已经是正确值
（不是空的）——因为这 40 条本来就是 Phase 3（历史上更早的一次 session，`git log`
里的 `d8236e4 phase3` 提交）分类打标时处理过的旧数据，`upsert_announcement` 的
`unchanged` 分支从来不碰 `category`/`is_region_exclusive`（那是 pipeline 的职责，
不是采集层的职责），所以这两列全程没有因为今天的 `--force-full` 或删除/恢复而
丢失或过期。

恢复后重跑 `group-check`/`classify --apply`/`region --sources Zoomex` 做一次
一致性核对（`classify --apply` 是幂等操作，已经正确的行不会被改写）：
`group` 数回到 **846**（第二步删除时曾错误地降到 842，4 个只剩 EN 单一版本的
group 因删除而"消失"，现在正确地恢复存在），`is_region_exclusive=true` 回到
**151** 行（第二步删除时曾因为"EN+另一 locale"共存的组被砍掉 EN 版本而错误地
虚高到 171，现已恢复成真实值），`category` 分布回到
`other 1091 / campaign 534 / delisting 220 / product 168 / listing 5`——
跟本次全量建仓最初跑完时的结果完全一致。**最终 Zoomex 总数是 2018 条，不是
1978 条**，这才是本轮 `--force-full` 的真实全量结果。

`data/competitor_intel.db.bak_20260714_170841` 继续保留，不删除（仍是回滚安全网）。

Bitunix/Weex 的 collector/parser/pipeline 代码本身完全未改动，只是 DB 里的行被
清空——下次用户指定测试这两个源时，重新跑
`python -m src.collectors --source bitunix`（或 weex）即可，不需要改任何代码。

## Phase 4 完成情况：LLM 分析层（批次级 summary + ZMX 差异）

代码按 phasePrompts.md Phase 4 的批次级设计全部实现，**本 session 未跑真实 LLM
调用/真实网络请求**：本地 `data/competitor_intel.db` 当前只有 Zoomex 数据（见上一节
「数据库清理」，Bitunix/Weex 已被清空），且 Zoomex 全量建仓当时仍在后台跑，用户
明确要求本 session 先跳过"用真实库跑一遍"这一步，所有验收都用临时/内存 SQLite +
mock LLM 完成。真实验收（重跑 Weex collector 拿到当日数据 → pipeline classify/region
→ `python -m src.analysis --dry-run` 看 prompt/token 预估 → 配置真实 LLM 凭证跑一次
真实调用）留到下个 session 或用户确认数据就绪后再做，不要假装已经验收过。

### 核心设计：分析单元是批次，不是单条公告

同一天同一 `(source, category, locale)` 的全部 `status IN (new, changed)` 公告合并
成一次 LLM 调用，产出一行 `insights`（schema v3，见上方 schema 表格）。批次 PK =
`SHA256(source_category_locale_batch_date)`，同一天重跑会追加新公告到 `related_uids`
并用全量重新调用 LLM 覆盖原记录（`created_at` 保留首次写入时间，`updated_at` 刷新）。

### 架构（`src/analysis/`）

- `config.py`：`load_analysis_config()` 读 `config/analysis.yaml`（temperature/
  max_tokens_by_category/prompt_versions/zmx_index/content_truncation 等非敏感参数）；
  `load_llm_credentials()` 读 `.env`（`LLM_API_KEY`/`LLM_API_BASE`/`LLM_MODEL`）——没有
  引入 `python-dotenv`（项目运行时依赖至今只有 PyYAML/certifi），手写十几行 `KEY=VALUE`
  解析足够，真实环境变量优先于 `.env` 文件内容。
- `zmx_index.py`：Zoomex 基线的轻量全文检索，**纯 Python 实现 TF-IDF，没有引入
  sklearn**（单个 category×locale 近 90 天的语料规模用不到那套向量化管线，符合项目
  最小依赖原则）。`build_index(conn, category, locale, lookback_days=90)` 按
  category+locale 过滤、只索引 `content` 非空的行；`search(query_text, top_k)` 用
  batch 内全部标题拼接做 query，只返回真正有词面重叠（`similarity_score > 0`）的
  文档——命中数量本身就是"基线是否充分"的信号，不做任何补全。
- `batch.py`：`compute_batch_id()`、`list_batch_keys()`（按
  `(source, category, locale != 'EN', locale)` 排序，保证同一 `source×category` 下
  EN 一定排在其它 locale 前面）、`can_derive_from_en()`（判断当前 locale 批次的全部
  `group_id` 是否都被 EN 批次的 `related_uids` 覆盖——存在地区独占条目就不能复用，
  只能老老实实调 LLM）。
- `prompts.py`：campaign/product/listing/delisting 四套 prompt **逐字实现**
  phasePrompts.md 给的文本。**实现时发现一个坑并绕开**：原文里 JSON 结构示例混用了
  Python `str.format()` 习惯的占位符写法，如果直接照抄容易手滑写成 `{{`/`}}` 双花括号
  —— 本模块的变量替换**不是**用 `str.format()`（那样爬来的公告正文里偶然出现的
  `{`/`}` 会导致 `KeyError` 或错误替换，公告原文不可控，不能假设它不含花括号），
  而是自己写的 `render()`：正则只匹配形如 `{ALL_CAPS_NAME}` 的占位符
  （`\{[A-Z][A-Z0-9_]*\}`），单遍扫描替换、不递归重扫替换后的内容，所以：
  - 模板里的 JSON 结构一律用**单花括号**（`{`/`}`），不需要转义；
  - 公告正文/标题里出现的任何花括号（包括恰好长得像 `{SOURCE}` 这种全大写模式的
    文本）都不会被误当成占位符处理，因为替换只扫描"模板"这一遍，不会再扫描已经
    填进去的 `ARTICLES_BLOCK`/`ZMX_BLOCK` 内容。
  两处「如果 X: ...」的条件描述文本（status=changed 时追加"变更前正文"、
  ZMX_COUNT=0 时的提示语）按 Python 条件分支实现（`build_articles_block()` 逐条判断
  `status=='changed'`、`build_zmx_note()` 按命中数返回三种文案：0 命中 / 命中数 <
  `min_hits_for_full_confidence`（配置默认 3，"基线数据有限"提示）/ 充分），不是把
  「如果...」这段元指令原样发给 LLM。`priority_reason` 字段（各 prompt 都要求 LLM
  输出，但 `insights` schema 没有单开一列）拼进 `zmx_diff` 文本末尾（"\n优先级依据：
  ..."），不是丢弃。
- `llm.py`：`call_llm()` 走 `src/collectors/http.py` 的 `fetch()`（复用同一套指数
  退避重试 + certifi 方案），固定拼 `{LLM_API_BASE}/chat/completions`（OpenAI 兼容
  格式，"任何 OpenAI 兼容接口"这个约束下最大公约数的调用形态，Anthropic 需经由官方
  OpenAI-compatible endpoint 接入，本模块不做按厂商分支的双协议实现）。
  `validate_and_normalize()` 实现全部四条入库前校验规则（JSON 解析失败 → 分析字段
  全 NULL 不重试；`diff_type` 不在枚举内 → 强制"不适用"；`diff_type` != 不适用但
  `evidence_indices` 空 → 强制"不适用"，防幻觉；`articles[].uid` 不在
  `related_uids` 内 → 丢弃该条目），listing 的合法枚举**不含**"ZMX玩法不同"、
  delisting 恒强制"不适用"（即使 LLM 输出了别的值）。额外做了一层防御：
  `_strip_code_fences()` 剥掉 LLM 可能违反指令、仍然包了一层 ` ```json ` 代码块
  标记的情况（真实模型即使明确要求纯 JSON 也偶尔会这样输出，不是过度设计）。
  `compute_cache_key()` = `SHA256(SHA256(排序后的 content_hash 拼接) || prompt_version)`
  ——content_hash 先排序再拼接，保证同一批次不管 SQL 返回顺序如何都算出同一个 key。
- `run.py`：`run(conn, batch_date=None, sources=None, categories=None, dry_run=False)`
  按 `list_batch_keys()` 枚举当天批次，对每个批次：`can_derive_from_en()` 命中就
  直接复制 EN 分析结果（`articles_analysis` 里的 `uid` 通过 `group_id` 换成本
  locale 的 uid，见 `_remap_articles_to_locale()`）、`llm_tokens_used=0`，完全不
  碰 `zmx_index`/LLM；否则建 Zoomex 索引（delisting 类目不建索引、不做 ZMX 差异，
  prompts.py 的 delisting 模板本身也没有 ZMX 部分）、查缓存、缓存未命中才真正调用
  LLM。`changed` 条目的"变更前正文"取自 `content_history` 里最近一条归档记录
  （`get_content_history()` 按 id 升序返回，最后一条就是这次变更前的直接上一版本）。
  `dry_run=True` 时只打印每个批次的粗略 token 预估（`len(text)//4`，量级参考，不
  追求精确）和 prompt 预览，不调 LLM、不写库、也不需要 LLM 凭证（`credentials` 留
  `None`，跳过 `validate()`）。CLI：`python -m src.analysis [--date YYYY-MM-DD]
  [--source Bitunix,Weex] [--category campaign] [--dry-run]`。

### schema v3 迁移：scripts/migrate_v3.py

`insights` 表从"逐条公告一行"（v1/v2）改成"批次级一行"，字段几乎全部替换，沿用
`migrate_v2.py` 的标准流程（建 `insights_v3` → `INSERT SELECT` 能对上语义的旧列 →
`DROP` 旧表 → `RENAME`）。旧数据量极少（Phase 4 之前 `insights` 表从未真正产出过
数据，LLM 分析层这次才实现），迁移只搬 `id`/`source`/`category`/`created_at` 几列
还能直接照抄的字段（`category` 为 NULL 的旧行灌不进新版 NOT NULL 的 `category`
列，直接跳过不搬），`prompt_version` 统一填 `"migrated-from-v2"` 作为标记值，其余
新列留 NULL/默认值，后续重跑 `python -m src.analysis` 会产出全新的批次行覆盖它们。
同时新建 `llm_cache` 表。用法：`python scripts/migrate_v3.py [db_path]`，幂等（已是
v3 结构直接跳过）。**本 session 没有对本地 `data/competitor_intel.db` 实际执行这个
迁移**（用户要求跳过真实库操作），下个 session 使用真实库前需要先跑一次。

### 验收记录（2026-07-14，全部离线，无真实网络请求）

```
.venv/bin/python -m pytest
# 167 通过：Phase 3 之前的 109 个 + Phase 4 新增 58 个
#（tests/test_migrate_v3.py 6 个 + tests/analysis/{test_zmx_index,test_batch,
#  test_prompts,test_llm,test_run}.py 共 52 个）
```

关键场景覆盖（对应 phasePrompts.md 第七步要求的单测清单）：
- `zmx_index`：category/locale 过滤、近 90 天窗口排除更早数据、空 content 跳过、
  空基线时 `search()` 返回 `[]`、TF-IDF 相关性排序、`top_k` 截断、预览截断长度。
- `batch`：批次 PK 幂等且随任一分量变化、`can_derive_from_en()` 的满足/不满足
  （地区独占条目导致不满足）/EN 自身/EN 批次不存在/当前批次为空四种场景、
  `list_batch_keys()` 的 EN 优先排序与 `category=other` 过滤。
- `prompts`：占位符替换只认 `{ALL_CAPS}`、不重扫替换后内容（验证公告正文里出现
  `{SOURCE}` 字面量不会被二次替换）、四个 category 都能正确出 prompt、
  `changed`/`old_content` 的条件包含、`delisting` 没有 ZMX 部分、`ZMX_NOTE` 的
  零命中/有限命中/充分命中三态文案、未知 category 报错。
- `llm`：JSON 解析失败、markdown 代码块剥离、`uid` 越界丢弃、`evidence_indices`
  空强制"不适用"、`diff_type` 非法枚举强制修正、`listing` 拒绝"ZMX玩法不同"、
  `delisting` 恒"不适用"（即使给了 evidence）、`evidence_indices` 到 Zoomex uid 的
  映射、越界 index 被忽略、cache key 的顺序无关性与随内容/版本变化、
  `call_llm()` 的请求体结构（mock `http_fetch`，断言 URL 拼接、Authorization
  header、messages 结构）。
- `run`：`dry_run` 不写库不调 LLM、`category=other` 批次被跳过（不产出 insights）、
  完整批次落库字段正确、同日重跑命中 `llm_cache` 不二次调用、EN→FR 复用不调 LLM
  且 `derived_from_id`/`llm_tokens_used=0` 正确、FR 批次存在地区独占条目时不复用
  （EN+FR 都要真调用）。

### 未做 / 已知限制（如实记录，留给下个 session）

- **完全没有真实 LLM 调用验收**：`config/.env.example` 里 `LLM_API_KEY`/
  `LLM_API_BASE`/`LLM_MODEL` 从未真正配置过真实值，`call_llm()` 的 OpenAI 兼容
  `/chat/completions` 请求格式没有对着任何真实服务商（OpenAI/其它兼容服务）验证过；
  下个 session 需要先在 `.env` 填真实凭证，再跑一次 `python -m src.analysis
  --dry-run` 看 prompt 是否合理，然后去掉 `--dry-run` 观察真实响应能否被
  `validate_and_normalize()` 正常解析。
- **没有用真实数据跑过 `can_derive_from_en`/`zmx_index`**：本地库目前只有 Zoomex
  数据（Bitunix/Weex 已清空，见上一节），phasePrompts.md Phase 4 原话要求"实际调用
  weex collector，跑一下获取今日完整数据，然后对其和目前本地的 zoomex 数据进行
  必要的前置处理（地区，category）后进行测试"——这一步本 session 特意跳过（用户
  要求），下个 session 需要：`python -m src.collectors --source weex --locale EN`
  （拿到当日新数据）→ `python -m src.pipeline classify --apply --sources Weex` +
  `region --sources Weex`（补 category/is_region_exclusive）→
  `python scripts/migrate_v3.py`（如果本地库还是 v2 结构）→
  `python -m src.analysis --dry-run --source Weex` 走一遍真实数据的批次划分/EN 复用
  判断/Zoomex 检索命中率，再决定要不要接真实 LLM。
- **`content_truncation.article_content_chars`（4000）/`zmx_preview_chars`（400）
  没有用真实公告长度校准**：数值是按经验估的合理默认值，等真实批次跑起来后如果
  发现常规公告经常超过这个长度被截断到丢失关键信息（比如活动规则表格很长），需要
  用真实分布调整，不是拍脑袋定死。
- **`estimate_tokens()`（`len(text)//4`）是粗略量级估算，不是真实 tokenizer**：
  没有引入任何厂商的 tokenizer 库（不知道最终接哪家），`--dry-run` 的 token 预估
  只用于摸底数量级、判断要不要担心成本，不能拿去做精确的预算控制。
- **`priority_reason` 没有单独的 `insights` 列**：拼进了 `zmx_diff` 文本末尾（见
  上文 `llm.py` 小节），如果以后 Phase 5/6 需要单独展示这个字段（比如飞书多维表
  要单独一列"定级依据"），需要回来加列，目前先用字符串拼接省一次 schema 变更。
- **`insights.category` 的 NOT NULL 约束意味着 `other` 类目永远不会出现在这张表**：
  `run()` 里 `list_batch_keys()` 已经过滤掉了 `category='other'`（跟 CLAUDE.md
  一直以来"other 是噪音，不推送"的口径一致），这是有意的范围收窄，不是遗漏。
- **`BuiltPrompt`/`AnalysisResult` 等内部 dataclass 没有考虑 Phemex/BingX/Lbank**：
  这三个源的 collector 仍未实现（批次 3/4 遗留），`prompts.py` 的四套模板本身跟
  `source` 无关（`{SOURCE}` 只是个字符串变量），新源接入 Phase 4 不需要改
  `src/analysis/` 任何代码，只要 `announcements.category` 能正常打上标就行。

## Weex 数据源迁移（2026-07-14，Phase 4 真实数据验证过程中发现并修复）

为了按 phasePrompts.md Phase 4 的要求"实际调用 weex collector，跑一下获取今日完整
数据"做真实验证，重新跑了 `python -m src.collectors --source weex`（不限定 locale/
category，像真实每日任务那样跑），暴露出一个比"漏采"严重得多的问题：**Weex 公告的
真实数据源已经从 Phase 1/2/2.6/2.7 反复验证过的 Zendesk 公开 REST API
（weexsupport.zendesk.com）迁移到了 www.weex.com 自己的 Next.js 前台**，旧 API 已经
过期。本节记录发现过程、验证证据、新方案的实现，以及仍然遗留的工作。

### 发现过程

1. 第一次重跑 `--source weex`（无过滤）表面上"成功"，new=5567，但排查后发现这是因为
   Phase 2.7 之后那次"只保留 Zoomex"的清库操作把 Weex 的 `crawl_state`（水位线）也
   一并删了——水位线一清空，watermark 策略的首次运行天然等价于全量回填，5567 正好是
   Phase 2.7 验收记录的历史总量，不是真实单日新增量。这批数据已撤回重清。
2. 用户指出：Weex 真实网站里点进「现货活动」等分区，能看到当天更新的公告
   （`https://www.weex.com/zh-CN/help/sections/33143373088665`
   （现货活动/Spot events）、`https://www.weex.com/zh-CN/help/categories/1010101`
   （最新文章）两个页面截图，日期分别是 2026/07/12、2026/07/09、2026/07/14），但我们
   采集到的 Weex 数据最新只到 2026-05-16——差了近两个月。
3. 直接对 `weexsupport.zendesk.com` 的真实 REST API（3 个已配置 endpoint、该
   category 下全部 6 个 section、en-us/zh-cn/fr 三个 locale）逐一发真实请求核对，
   确认这个 API **真的**从 2026-05-16 起再未返回过任何更新——不是我们的分页/排序/
   locale 参数用错了，是这个 API 本身已经停止更新。`weex.com/help` 页面本身仍然在
   HTML 里引用 `weexsupport.zendesk.com`（没有整体换域名），但页面渲染出来的内容不
   再从这个公开 REST API 读取。
4. 用 Playwright 打开用户给的真实文章所在分区页面（`.../help/sections/
   33143373088665`）抓包，确认**没有任何可发现的 JSON/XHR 接口**——数据是 Next.js
   服务端直接渲染进返回的 HTML 里的（React Server Components "flight" 流，
   `<script>self.__next_f.push([1,"..."])</script>`），前端拿到页面后不需要另外
   发请求。这也解释了为什么 Playwright 网络抓包是空的：这套机制根本不经过浏览器
   发起的 XHR。
5. 逐层解析这个 flight 流，找到真实的 `articleListData` JSON 数组（列表页）和
   `<div class="zendesk-html">...</div>`（详情页正文，直接是服务端渲染好的 HTML，
   不需要解析 flight 流）。用真实文章（`edgeX (EDGE) WE-Launch...`）的
   `createdAt=1783848600000` 反解为 `2026-07-12T09:30:00Z`（北京时间 17:30），跟
   用户截图上的日期/时间精确对上，证实数据是真的、解析方式是对的。

### 新方案（完整技术细节见 `src/parsers/weex_web.py` 顶部注释、
`config/sources.yaml` weex 块注释、`src/collectors/weex.py` 顶部注释）

- 列表页：`GET https://www.weex.com/{locale_path}/help/categories/{category_id}
  ?page={n}`（或 `.../sections/{section_id}?page={n}`），从内嵌 flight 流里解析
  `articleListData`（article_id/title/createdAt/sectionId/prioritise/url）+
  `pageInfo`/`totalCount`/`totalPage`。改用 **category 级聚合**而不是逐个已知
  section 硬编码，额外发现了一个旧 Zendesk `sections.json` 里根本查不到的新
  section（`608152974386`「All about TradFi」，6 篇，已映射为 product，见
  `category_mapping.yaml`）——这是選用聚合而不是硬编码列表的直接收益：新分区不会
  被静默漏采。
- 详情页：`GET https://www.weex.com/{locale_path}/help/articles/{id}`，正文摘自
  服务端渲染好的 `<div class="zendesk-html">`（不需要解析 flight 流，比列表页
  简单），交给已有的 `html_text.py` 转纯文本，跟 Bitunix/Weex 旧数据同一套转换器。
  新旧两种 article_id 格式（新文章是小写字母数字 slug，旧文章在新系统里仍能查到、
  ID 还是原来的 Zendesk 数字）走同一套详情页结构，都验证过。
- 无 per-item `update_time`（只有 `createdAt`），`strategy` 改判 **full_scan**
  （不写 `crawl_state.high_watermark`），content_hash 兜底检测变更，跟 BingX/
  Phemex/Lbank 同一类源。`pagination.max_pages`（默认 5）限制 daily 增量的扫描
  深度，`--force-full` 时忽略上限翻到 `totalPage` 为止（同 Zoomex daily 增量
  补丁设计）。列表条目的 `prioritise`（置顶）标记可能不按时间顺序排最前——不依赖
  排序做提前退出翻页（同 Zoomex 批次 2 教训）。
- `sectionId` 沿用旧 Zendesk 的数值体系，`config/category_mapping.yaml` 现有的
  weex 映射对新数据直接适用，不需要重新对照（只新增了上面提到的
  `608152974386`）。
- `src/collectors/__main__.py` 的 `_zendesk_builder` 改名成
  `_categorized_collector_builder`（原名字已经不准确——Weex 不再是
  ZendeskCollector 子类），继续通过鸭子类型支持 Bitunix（仍是 ZendeskCollector）
  和 Weex（现在是独立的 BaseCollector 子类）共用同一套"单分类/多分类展开"逻辑。

### 一处真实 bug：flight 流拼接文本的错误解码（mojibake）

第一版 `src/parsers/weex_web.py` 用 `text.encode("utf-8").decode("unicode_escape")`
把拼接后的 flight 流文本转成"真实文本"，这是错的——`html` 在更早的
`resp.read().decode("utf-8")` 那一步就已经是正确的 Python str，里面的非 ASCII
字符（中文、法语重音字符）是**原样的 Unicode 字符**，不是转义序列；再把这个正确的
字符串编码成 UTF-8 字节、又用 unicode_escape（本质是 Latin-1 + 转义处理）解码，会把
每个多字节 UTF-8 字符拆成几个乱码字符（典型 mojibake）。2026-07-14 真实网络验收时
第一次发现：法语 P2P 公告标题 `"Offre spéciale WEEX P2P..."` 被解析成
`"Offre spÃ©ciale WEEX P2P..."`，英文内容因为全 ASCII 没有暴露这个问题，回头检查
连 Phase 1 侦察阶段那次法语抽样（`"WEEX WE-Launch â Synvine..."`）也已经中招，只是
当时没注意到。修复：改用 `json.loads(f'"{joined}"')`——把拼接文本当一个 JSON
字符串字面量解析，只有真正的转义序列（`\"`/`\\`/`\n`/`\uXXXX` 等）会被处理，字面量
的 Unicode 字符完全不受影响。`tests/parsers/test_weex_web.py` 新增
`test_parse_article_list_decodes_non_ascii_titles_correctly`（用真实法语 fixture
锁定这个修复）。已清空 fix 前采集的 32 条 Weex 数据（EN 20 + FR 12 的
p2p_announcement）重新采集，确认标题/正文均无残留 `Ã` 类 mojibake 字符。

### 真实网络验收记录（2026-07-14）

```
python -m src.collectors --source weex --locale EN --category p2p_announcement
# 首轮：new=20 changed=0 unchanged=0 failed=0
# 第二轮：new=0 changed=0 unchanged=20 failed=0（full_scan 靠 content_hash 判断
#   unchanged，不是水位线挡住——crawl_state 里确认 0 行 Weex 记录）
python -m src.collectors --source weex --locale FR --category p2p_announcement
# new=12 changed=0 unchanged=0 failed=0，标题正确带重音字符，无 mojibake

pytest
# 186 通过（新增 tests/parsers/test_weex_web.py 11 个、
# tests/collectors/test_weex_collector.py 11 个；旧的 test_zendesk_collectors.py
# 里 Weex 专属用例已删除/迁移，Bitunix 用例原样保留未受影响）
```

真实数据抽样验证（EN p2p_announcement，按 post_time 降序）：
`"WEEX P2P notice on the delisting of PHP (Philippine Peso)"`（2026-06-22）、
`"...ETB (Ethiopian Birr)"`（2026-06-11）——都是新系统才有、旧 Zendesk API 完全
拿不到的近期真实内容，确认新方案有效解决了"数据过期"问题。

`python -m src.collectors --source weex`（不限定 locale/category，覆盖全部
2 locale × 3 category，默认 `max_pages=5` 的 daily 增量范围）已在后台跑，用于给
Phase 4 `pipeline classify/region` + `python -m src.analysis --dry-run` 提供真实
测试数据，结果见下次记录或本节后续更新。

### 未做 / 已知限制

- **Bitunix 是否也存在同样的数据源迁移风险，本次未验证**：用户当时明确选择"现在就
  做完整侦察，重写 Weex collector"，没有同时要求核查 Bitunix；Bitunix 仍然假设
  `support.bitunix.com`（Zendesk）是真实数据源，建议下次找机会用同样的方法核对
  一下（对比几个已知分类的最新 `updated_at` 跟真实网站显示的日期）。
- **`608152974386`「All about TradFi」只抽样看了 6 篇标题**：没有逐篇精读判断
  campaign/product/other 边界，映射成 product 是基于整体基调的合理判断，不是
  逐篇核实过。
- **详情页里 `prioritise=true` 的置顶文章语义未深挖**：只确认了它可能不按时间顺序
  排在列表最前面（不依赖排序提前退出的原因），没有进一步确认"置顶"本身是否有
  额外的业务含义（比如运营手动置顶的高优先级公告），如果之后分析层想利用这个信号，
  需要在 `RawItem.extra` 里把它透传出来（目前 `parse_article_list` 有解析这个字段，
  但 `WeexCollector.fetch_list()` 没有把它放进 `RawItem`，因为当前用不上）。
- **`www.weex.com` 页面结构本身没有版本化保护**：这套解析完全依赖 Next.js flight
  流的字面量结构（`articleListData` 键名、`zendesk-html` class 名）和页面路由
  （`/help/categories/{id}?page=N`），任何一次前端改版都可能悄悄改变这些细节而
  不报错（返回 200 但解析不到数据，`parse_article_list`/`extract_article_body_html`
  会返回空 list/None，不会抛异常）。建议在 Phase 8 调度/监控里加一条"Weex 采集
  连续 N 天 new+changed=0"之类的哨兵检查，及早发现类似本次的静默过期。

## Phase 4 之后补丁：Zoomex 第二层关键词分类修复（2026-07-14）

用户发现 Zoomex 分类结果里 `other` 占比异常高（2018 条里 1091 条，54%）、`listing`
异常低（仅 5 条）——对一个持续上新的交易所来说明显不合理，怀疑第二层关键词匹配
对 Zoomex 失效。排查确认：**只有** `menu_id=26`（"Platform Announcement"，raw_category
第一层映射到 `other`）会落到第二层关键词匹配（其余 menu_id 都有专属映射，第一层直接
命中，不经过关键词层）；抽样这 1091 条发现约 275 条是真实的新币种/新合约上线公告，
但 Zoomex 的措辞（`"X are now live"`、`"X is now available on Zoomex Spot"`、
`"perpetual contract(s) are available"`、`"Launching Soon on Zoomex Spot"`）完全不
包含 `list`/`listing` 这两个词——`KEYWORD_RULES` 里 listing 分类的关键词是照着
Bitunix/Weex 的措辞（"New Listing: X"）调的，对 Zoomex 这种一句话都不用"list"字样
的风格完全失效。

**没有采纳"合并成 platform/listing/delisting 三分类"的提案**：实测发现 `other` 桶
里除了这 275 条误判的 listing，剩下约 800 条是真实的"既非 listing 也非
delisting"内容（资金费率区间调整、风险限额调整、钱包/充提维护、社区招募、新年
贺词），把这些强行归进 listing/delisting 会制造新的误判；而合并三个分类本身会
牵动 `schema.sql` 的 CHECK 约束、`prompts.py` 四套按 category 区分的 LLM 模板、
`push_rules.yaml` 的按 category 推送规则——这些改动的收益不成正比，问题的根源
是"关键词覆盖不够"，不是"分类粒度设计错了"。

**修复（第一版，已订正见下）**：最初把新词直接加进 `KEYWORD_RULES` 的 listing
分组，结果发现会连带影响本不该动的行——原因见下方"订正"。

**订正**：用户指出，"正确"的判断基准是 `category_mapping.yaml` 里按 menu_id 做的
第一层原生映射（`145/69 -> campaign`、`123 -> product`），这次修复本来就只应该
影响"因为 menu_id=26 映射到 other、才需要靠关键词兜底"的那部分行，不应该动任何
已经有明确归属（不管是第一层原生映射、还是第二层关键词已经命中过）的分类结果。
第一版实现没有完全做到这一点：新词被塞进 `KEYWORD_RULES` 的 listing 分组后，
因为 `KEYWORD_RULES` 里 listing 排在 campaign/product 前面，会抢先命中那些标题里
恰好也包含 "trading" 等词的行——这些行在改动前已经被原有关键词命中过（即使命中
的词是"trading"这种宽泛误报），被新词从 campaign 抢先改判成 listing，不在这次
修复的授权范围内。

修正后的实现：`src/pipeline/category.py` 新增独立的 `LISTING_FALLBACK_KEYWORDS`
列表（`now live / is now available / now available on / contract are available /
contracts are available / launching soon`），**不**塞进 `KEYWORD_RULES`，而是在
`classify_by_keyword()` 里等 `KEYWORD_RULES` 全部检查完、仍然没有任何命中时才
兜底检查——`listed` 这个词因为不依赖新短语、原本就该属于标准 listing 关键词，
保留在 `KEYWORD_RULES` 本身。这样任何已经被 `KEYWORD_RULES`（含原生映射短路
之后才会走到的关键词层）命中过的行，不管命中的是不是"合理"的词，都不会被这批
新词影响；新词只救回那些原来完全没有关键词命中、纯粹靠 `native_other` 兜底判成
`other` 的行。

```
python -m src.pipeline classify --apply --sources Zoomex
# 各 layer 命中数：{'keyword': 639, 'native_other': 794, 'native': 585, '_written': 2018}
```

修复前后 Zoomex 全量 category 分布：

| category | 修复前 | 修复后 |
|---|---|---|
| other | 1091 | 818 |
| campaign | 534 | 534（不变，第一版曾错误降到 526，订正后已恢复） |
| listing | 5 | 278 |
| product | 168 | 168（不变） |
| delisting | 220 | 220（不变） |

`campaign`/`product`/`delisting` 三列在订正后逐字节不变，只有 `other`→`listing`
之间发生了 273 条真实的重新分类（1091-818=273，5+273=278），跟第一版"顺带误伤
8 条 campaign"的问题已经消除。

`is_region_exclusive` 不受影响（151 行不变，region 判断只看 group 归属的 locale，
跟 category 无关）。

**遗留**：`LISTING_FALLBACK_KEYWORDS` 是全源共用的（不是按 source 分开的配置），
这批新短语理论上也会应用到 Lbank（`raw_category` 恒 NULL，全量走关键词层）和
未来的 Phemex/BingX；`data/competitor_intel.db` 目前 Bitunix/Weex 数据已清空
（见「数据库清理」一节），没有真实数据可交叉验证这组新词会不会在它们的"other"
分区标题里误触发。等 Bitunix/Weex 数据重新采集回来后，应该跑一次
`python -m src.pipeline classify --dry-run --sources Bitunix,Weex` 交叉核对
`keyword` 层命中数有没有异常增长——但因为是"全不命中才兜底"的设计，风险已经比
第一版低很多（不会抢在任何已有规则前面）。

## Weex 修复版 collector 真实数据验证 + Phase 4 pipeline/analysis 打通（2026-07-14）

`src/pipeline/category.py` 在本节写入时正处于另一个并行 session 的修改过程中（见
上一节，`KEYWORD_RULES` 里 campaign/product/other 三组暂时被注释掉），本节的验证
特意避开触碰这个文件，只跑 `classify --apply`（用的是文件当时的实际内容，不是
完整版）——如果后续该文件恢复完整，`Weex` 的 category 分布可能会因为 keyword 层
命中数变化而略有不同，属于预期之内，不代表本节记录的数字是错的。

**订正（2026-07-14，Bitunix 试运行 session 期间用户确认）**：`KEYWORD_RULES` 里
campaign/product/other 三组的注释掉是**有意为之的最终设计，不是待恢复的中间态**。
理由：一旦第一层 `raw_category` 原生映射把一行判成 `other`，就不应该再靠标题关键词
把它拉回 `product`/`campaign`——除 Zoomex 外的其它平台（Bitunix/Weex/…）
`raw_category` 到 category 的映射本身就是全的（Phase 2.6/2.7 逐个 section 核实过），
不需要关键词层兜底二次判断；只有 Zoomex 的 `menu_id=26`"Platform Announcement"
把 listing/delisting/product 混在一个不分 section 的原生分类里，才需要
`LISTING_FALLBACK_KEYWORDS` 这种专门的兜底（已实现，见上一节）。`KEYWORD_RULES`
现在只保留 listing/delisting 两组（这两组是跨平台都可能需要的，不是 Zoomex 专属）。
`tests/pipeline/test_category.py` 里假设 campaign/product/other 关键词层仍然生效的
5 个用例（`test_keyword_campaign`/`test_keyword_product`/`test_keyword_other`/
`test_native_other_refined_by_keyword`/`test_dry_run_counts_layers_without_writing`）
已同步改写为断言"这些关键词不再命中、行为落回 native_other"，不是恢复代码去迁就
旧测试。

### 真实数据修复验证（`id` 替代 `documentId` 的修复版本）

用修好 `documentId`/`id` 重复问题（见上一节「Weex 数据源迁移」）之后的版本重新跑了
`python -m src.collectors --source weex`（EN+FR × 3 category，`max_pages=5`，
仍然不做日期过滤——用户明确要求这一步先只限制页数，日期窗口留到指定"当天"时再加）：

```
Weex EN latest_announcements   new=323 changed=0 unchanged=0 failed=2（2 次真实 502/超时，非代码问题）
Weex EN listings_delistings    new=324 changed=0 unchanged=0 failed=1
Weex EN p2p_announcement       new=20  changed=0 unchanged=0 failed=0
Weex FR latest_announcements   new=324 changed=0 unchanged=1 failed=0
Weex FR listings_delistings    new=323 changed=0 unchanged=0 failed=2
Weex FR p2p_announcement       new=12  changed=0 unchanged=0 failed=0
```

验收结果：Weex 总计 1326 行，交叉核对：
- 重复 URL：0（修复前是 1 组，见上一节）
- mojibake（`Ã` 类残留）：0
- 正文残留 HTML 标签：0
- `content_hash` 为 NULL：0
- `raw_category` 值全部在 `category_mapping.yaml` 里能查到（含新发现的
  `12312367451234`「All about Earn delistings/maintenance」，已按标题抽样
  ["Annonce de WEEX concernant le retrait du produit de staking flexible XAUT"]
  映射为 delisting）
- `post_time` 范围 `2025-01-31` ~ `2026-07-14T11:30:00Z`，含真实当天数据

**顺带发现一类不影响正确性、但值得记录的怪异真实数据**：`article_id`/`id` 字段里
有 302 行是 `help_article_{数字}` 这种明显像"占位符"的字符串（而不是真实数字
Zendesk ID 或 slug），真实抽查这批文章的 `documentId` 字段反而是正常的 slug——
跟"Weex 数据源迁移"一节记录的那类 bug（documentId 重复、id 才稳定）恰好相反。
交叉检查确认这批 `help_article_N` 值本身互不重复、每个对应一篇真实存在的历史
文章（`url` 字段也用同一个值，duplicate url 检查=0），推测是 Weex 自己更早一批
内容迁移时给"当时也没有真实 slug/数字 ID"的文章生成的占位符规则，**不是我们代码
的 bug，也没有造成数据重复**，无需处理，只是留个记录以防以后遇到类似模式会
困惑。

### pipeline classify/region 真实验证

```
python -m src.pipeline classify --apply --sources Weex
# native=1106 keyword=3 native_other=217 _written=1326

python -m src.pipeline group-check --sources Weex,Zoomex
# 检查了 1689 个 group，0 异常

python -m src.pipeline region --sources Weex
# 共 843 个 group，176 个判定为地区独占（抽样确认标题确实是区域限定内容，如
# "Suspension temporaire des dépôts sur le réseau TON pour maintenance système"
# 这类只在 FR 出现的公告）
```

### Phase 4 `--dry-run` 真实数据验证

```
python -m src.analysis --source Weex --dry-run
```

正确产出 8 个批次（`campaign/delisting/listing/product` × `EN/FR`，`other` 被
正确排除），token 预估从 ~4700（Weex/delisting/EN，11 篇）到 ~126000
（Weex/campaign/EN，239 篇）不等，四套 prompt 模板输出结构目视检查正常（标题/
正文/UID 正确嵌入，ZMX 基线段落按命中数量正确显示"有限"/"不适用"提示或
真实检索结果）。

**`--dry-run` 模式下 EN→FR 复用没有触发（`derived=0`）是设计使然，不是 bug**：
`can_derive_from_en()` 需要查到当天已写入的 EN `insights` 行，但 `dry_run=True`
从头到尾不写库（连 EN 自己的分析结果都没有落库），所以同一次 `--dry-run` 调用里
FR 永远找不到可复用的 EN 批次。要真正验证 EN→FR 复用路径，需要一次非 dry-run
的真实/mock LLM 调用（`tests/analysis/test_run.py` 已经用 mock 覆盖了这个场景，
但真实数据 + 真实 LLM 的端到端验证仍然留待下次配置好 `.env` 凭证后补齐）。

### 仍然遗留（如实记录）

- **真实 LLM 调用仍未验证**：`.env` 里 `LLM_API_KEY`/`LLM_API_BASE`/`LLM_MODEL`
  还是空的，`--dry-run` 只验证了批次划分/ZMX 检索/prompt 构建这些不需要真实
  调用的部分。
- **`content_truncation` 的截断阈值在真实数据下的实际触发情况未检查**：
  `Weex/campaign/EN` 单批次就有 239 篇文章、预估 12 万+ token，如果接入真实 LLM，
  这么大的单次调用可能会超过很多服务商的单请求 token 上限——需要考虑是否要把
  "批次"进一步按天然分页拆分成更小的子批次，这是 phasePrompts.md 原设计没有
  预见到的真实数据规模问题（原设计假设的"一天新增"量级明显比 top-5-pages 回填出来
  的量级小很多），下次接真实 LLM 前需要跟用户确认怎么处理超大批次。

## Bitunix 当日数据试运行：全 locale × category，验证 Phase 2→4 全链路（2026-07-14）

用户要求给 Bitunix（此前在「数据库清理」一节被清空，`announcements`/`crawl_state`
均为空）做一次真实试运行：限定只拉「更新时间/创建时间为今天」的数据，覆盖全部
locale × category，然后走完 pipeline（归组/分类/地区标记）和 Phase 4 分析层。

### 怎么把"只拉今天"接到现有 watermark 机制上，没有改任何 collector 代码

`ZendeskCollector.fetch_list()` 本来就有「服务端按 `updated_at` 降序排列 + 遇到
`update_time <= since` 立刻停止翻页」的机制（Phase 2 批次 1 就有，日常增量靠它）。
`since` 的来源是 `run()` 里读 `crawl_state.high_watermark`。既然 Bitunix 的
`crawl_state` 已经被清空，直接用 `src.db.operations.set_crawl_state` 给 EN/FR/ID
三个 locale（Bitunix 只有一个分类 "Announcements"，没有 `categories` 结构，"全
locale × category" 对 Bitunix 而言就是 3 个 locale 各一次）预置
`high_watermark="2026-07-14T00:00:00Z"`，再正常跑 `python -m src.collectors
--source bitunix`（不带 `--force-full`）——完全复用现成的早停逻辑，不用碰
collector/CLI 代码，也不会像"删空 crawl_state 后直接跑"那样退化成全量回填。

用 `update_time` 做过滤下界是有意的选择而不是巧合：Zendesk 文章创建时
`updated_at` 恒等于 `created_at`，之后只会更大不会更小，所以「`update_time` 在
今天」这个集合，天然是「`post_time` 在今天」∪「今天被编辑过的历史公告」的并集，
覆盖了"创建时间或更新时间为今天"这个过滤口径，不需要额外判断 `post_time`。

### 真实结果

```
python -m src.collectors --source bitunix
# EN new=5 changed=0 unchanged=0 failed=0
# FR new=4 changed=0 unchanged=0 failed=0
# ID new=5 changed=0 unchanged=0 failed=0
```

14 条全部 `update_time` 落在 2026-07-14T05:04:58Z ~ 2026-07-14T09:35:55Z 之间
（`post_time` 大多是 2025-12-30~2026-07-10，即"今天被编辑过的历史公告"，没有一条
`post_time` 本身是今天——纯属当天真实数据的样子，不是过滤逻辑的问题）；正文抽查
0 行残留 HTML 标签。5 个 `(article_id)` 分组在 EN/FR/ID 均有出现（trilingual 同步
发布），无任何 locale 缺失。

```
python -m src.pipeline group-check
# 检查了 1694 个 group，0 异常
python -m src.pipeline classify --apply --sources Bitunix
# 14 行，native 层 100% 命中（{'native': 14, '_written': 14}），0 行落到 keyword/
# LLM 兜底层——跟 Phase 3 时"Bitunix 当前入库 raw_category 值都已被
# category_mapping.yaml 覆盖"的结论一致
python -m src.pipeline region --sources Bitunix
# 共 5 个 group，0 个判定为地区独占（跟"5 组全部 3 语言同时发布"的观察一致，
# 地区独占要求"只在一个非 EN locale 出现"，这批数据不满足）
```

`category` 落库结果：EN/FR/ID 各 3 条 `product`（tick size/risk limit 调整、Chart
Trading 升级）+ 2 条 `listing`（BOTUSDT/FWDIUSDT、GEVUSDT/VRTUSDT 上新）。

```
python -m src.analysis --source Bitunix --dry-run
# 正确产出 6 个批次：listing×{EN,FR,ID} + product×{EN,FR,ID}，token 预估
# 1200~3600 量级（远小于 Weex 那次验证时 239 篇/12 万+ token 的大批次，符合"只有
# 14 条"的数据规模）。ZMX 基线检索命中数（zmx_hits）在 0~5 之间，四套 prompt 模板
# 目视检查结构正常。
```

**真实 LLM 调用仍未执行**：跟 Weex 那次验证一样，`.env` 里 `LLM_API_KEY`/
`LLM_API_BASE`/`LLM_MODEL` 仍为空（只有 `.env.example` 模板），`--dry-run` 是
当前能做到的最大验证深度——批次划分、EN 复用判断的前置条件（因为 dry-run 不写库，
`can_derive_from_en` 天然测不到复用命中）、ZMX 检索、prompt 构建全部验证过，
真实 LLM 响应能否被 `validate_and_normalize()` 正常解析、以及 EN→FR 复用路径在
真实数据上的表现，仍然留给配置好凭证之后的下个 session。

### 已知限制

- 这次试运行样本量很小（14 条，2 个 category），`campaign`/`delisting`/`other`
  三个 category 当天都没有新数据，Phase 4 的批次划分逻辑在这 3 个 category 上
  没有被这次试运行覆盖到（但 Weex 那次验证已经覆盖过 `campaign`/`delisting`，
  代码路径本身不是新的）。
- 水位线预置（`set_crawl_state` 直接写今天 00:00 UTC）是为了这次"只要今天"的
  试运行需求，手动做的一次性操作，不是一个新增的 CLI 能力——项目目前没有
  `--since`/`--date` 这类参数可以直接对 collector 说"只要某天"，如果以后经常
  需要这种按天试跑，可以考虑给 `python -m src.collectors` 加一个显式参数，而不是
  每次手动 `set_crawl_state`。

## 水位逻辑策略调整（2026-07-14）

用户复盘时指出：项目里"水位"（watermark）判断逻辑对大多数源已经名不副实——除
Bitunix 外，Weex 从数据源迁移起就已经是 full_scan（没有 per-item update_time），
BingX/Phemex/Lbank 从 Phase 1 侦察起就确认过 `has_update_time` 字段存在但不可靠
或者干脆没有真正的分页能力。继续让每个新源各自设计一套"怎么找增量"的专用机制
（如 Phase 2.6 曾给 BingX 记录的 sitemap diff 方案）成本越来越高，也偏离了这些
源的真实能力。本次批次 3/4 实现前，用户拍板统一简化：

- **全量回填（`--force-full`）默认关闭，只有 Zoomex 保留这个能力**——Zoomex 是
  我方基线（对比基准），本来就需要定期全量核查兜底（`fetch_list()` 翻完全部页），
  这个既有设计不变。
- **其余源日常调用固定只拉一个有限窗口**，靠 `content_hash` 判断
  new/changed/unchanged，不再为每个源单独设计增量算法：
  - Weex：已有的 `pagination.max_pages`（默认 5）机制不变，这本来就是这次
    政策的雏形。
  - BingX/Phemex：列表接口从 Phase 1 起就没有真正可用的分页参数（`?page=`
    等 query 不生效，翻页是未逆向的客户端交互），"有限窗口"退化成固定的
    这一屏/这一页（BingX 首屏跨 12 分区聚合约 20 条，Phemex 每个分类固定
    20 条）。Phase 2.6 给 BingX 记录的 sitemap diff 全量回填设计**已废弃**，
    改用这个统一简化模型，不再实现。
  - Lbank：从 Phase 2.5 起就是 full_scan（翻页未逆向，只能拿默认聚合视图固定
    10 条），不受这次调整影响，只是重申"全量历史回填对 Lbank 没有可行路径"
    这个既有结论。
- **`force_full` 对 BingX/Phemex/Lbank 是 no-op**（`True`/`False` 结果一样）——
  如实记录这三个源"没有除默认窗口外的可靠数据源"这个事实，不假装支持全量历史
  回填。需要全量历史时，可以按 Phase 1/2.6 记录的 sitemap 方案另起一个 session
  实现（BingX/Phemex 的 sitemap 覆盖性 Phase 1 已经验证过，只是本次没有采纳）。
- **Bitunix 的 watermark 早停机制不受影响**：`sort_by=updated_at&sort_order=desc`
  + 遇到 `update_time <= since` 提前退出翻页，这套机制从 Phase 2 批次 1 起就用
  真实数据反复验证过确实工作正常（批次 1、2.7、以及本次之前 Bitunix 当日试运行
  都复核过），不在"水位逻辑不能用"的范围内，继续保留。

测试策略同理调整：验收/单测不追求"跑满全量"，覆盖全部 locale × category 组合、
但每个组合只取默认能拿到的这一个窗口（不额外翻页），见下方批次 3/4 验收记录。

## Phase 2 完成情况：批次 3/4 — BingX + Phemex

批次 3/4 一起做（都受上面「水位逻辑策略调整」影响，架构模式相同：真实请求探明
列表/详情页的具体数据结构 → 写 parser → 写 collector → 离线单测（真实 fixture）
→ 真实网络验收）。**本 session 用户明确要求跳过 Phase 1 记录之外的大范围重新
侦察**，但实现前发现 Phase 1 的 `field_mapping` 只记录了字段名、没有记录字段
在响应里的具体嵌套路径（BingX 的 devalue 解引用规则、Phemex 的宽松 JS 对象
字面量结构），照抄字段名不足以写出能工作的 parser，属于「不允许猜测数据」
铁律的边界情况——做了少量针对性真实请求（各 1 次列表页 + 1 次详情页，不是
重新做 Phase 1 级别的全量侦察）来核对具体路径，再落笔实现。

### BingX（`src/parsers/bingx_web.py` + `src/collectors/bingx.py`）

- 列表页/详情页数据都在 `<script type="application/json" data-nuxt-data=
  "nuxt-app" id="__NUXT_DATA__">` 里，devalue 格式的扁平数组（整数元素是同一
  数组内的索引引用）。写了一个通用的最小解引用器（`_resolve_all` + `_normalize`），
  不引入第三方 devalue 库：先把每个数组下标解引用成实际值（缓存 + 环路守卫），
  再单独一趟收尾清洗，去掉 `["ShallowReactive"/"Reactive"/"Ref", <ref>]` 这层
  响应式包装、把 `["null", k1, v1, k2, v2, ...]`（`Object.create(null)` 实例的
  devalue 编码）转成普通 dict。
- 真实请求验证：列表页（`support-notice-center` 相关 key 下）固定 20 条，字段
  articleId/newArticleId/sectionId/newSectionId/weight/title/createTime/
  updateTime/promoted，`createTime==updateTime` 逐字节相等（跟 Phase 1 抽样
  结论一致，watermark 确认不可靠）。详情页（`articleData` 字段）：categoryId/
  categoryPathsStr/articleId/sectionId/title/body（HTML）/lang/createTime——
  **确认了详情页也有 sectionId**（跟 Phase 1"sectionId 只有首屏/详情页才有"的
  记录一致），`fetch_detail()` 用详情页的 sectionId 覆盖列表页的值（更接近
  文章当前真实归属）。
- `force_full` 是 no-op：首屏本来就不是分页接口，见「水位逻辑策略调整」。
- 时间格式 `2026-07-14T17:48:29.000+08:00`（显式 +08:00 偏移，不是 UTC），
  新增 `timeutil.offset_iso_to_utc_iso()` 转换（`datetime.fromisoformat` 原生
  支持解析偏移，Python 3.11+ 都可以，不需要手写偏移计算）。

### Phemex（`src/parsers/phemex_web.py` + `src/collectors/phemex.py`）

- `window.preloadedData = {...}` 是 JS 对象字面量（key 不带引号、字符串单
  引号），不是严格 JSON，不能 `json.loads`。写了一个手写字符级递归下降解析器
  `_JsLiteralParser`（支持 object/array/string/number/true/false/null，容忍
  尾随逗号，字符串支持 `\n`/`\t`/`\uXXXX` 等转义），不用正则替换 key/引号——
  正则在字符串内容恰好包含 `key:`/单引号等模式时会误判结构边界（公告标题里
  常见 "X / Y" 这类文本）。
- 真实请求验证：`pageData.total` + `pageData.articles[]`（固定 20 条，无
  分页，字段 id/locale/title/slug/desc/author/publishedTime/publishedAt/
  headerImage/url/month/day/year，**不含 content**）。详情页 `pageData.id/
  title/content`（HTML）/publishedTime/`i18n.updatedAt`/category（`{id,name}`，
  **不落库这个字段**，locale 相关翻译文本，Phase 2.6 早就订正过这个坑）。
- `raw_category` 直接用采集时的 `categories.*` 配置 key（news/activities/
  newsletter），不解析响应字段，跟 Phase 2.6 订正的设计一致。
- `force_full` 是 no-op：列表页本来就不是分页接口，见「水位逻辑策略调整」。

### 真实网络验收记录（2026-07-14）

```
python -m src.collectors --source bingx
# EN new=20 changed=0 unchanged=0 failed=0
# VN new=20 changed=0 unchanged=0 failed=0

python -m src.collectors --source phemex
# EN/news       new=20 changed=0 unchanged=0 failed=0
# EN/activities new=20 changed=0 unchanged=0 failed=0
# EN/newsletter new=12 changed=0 unchanged=0 failed=0   （newsletter 总量本来就小）
# FR/news       new=20 changed=0 unchanged=0 failed=0
# FR/activities new=20 changed=0 unchanged=0 failed=0
# FR/newsletter new=4  changed=0 unchanged=0 failed=0
```

覆盖了两个源全部 locale × category 组合（BingX 无分类结构，2 个 locale；Phemex
2 个 locale × 3 个分类 = 6 个组合），符合用户"测试不用跑全量，但要覆盖全部
locale × category"的要求——这几个源本来也没有"多翻几页"的空间，覆盖到的这一
窗口就是当前能拿到的全部。落库抽查：Phemex EN newsletter 3 篇正文为空
（`Phemex September/August/July Newsletter`），真实请求详情页核对确认这几篇
本身就是纯图片邮件（`<img>` 拼起来的 HTML，没有文字节点），`html_to_text` 正确
转出空字符串，不是解析 bug（跟 Zoomex 当年"纯图片公告"是同一类真实数据特征）。
其余全部行正文非空、无残留 HTML 标签、`raw_category` 均在
`config/category_mapping.yaml` 里能查到。

`pytest`：新增 `tests/parsers/test_bingx_web.py`（6 个）、
`tests/parsers/test_phemex_web.py`（11 个，含 `_JsLiteralParser` 的白盒测试）、
`tests/collectors/test_bingx_collector.py`（8 个）、
`tests/collectors/test_phemex_collector.py`（9 个），复用 Phase 1 侦察阶段
已经存在的真实 fixture（`tests/fixtures/bingx_{EN,VN}{,_detail}.html`、
`phemex_{EN,FR}{,_activities,_newsletter,_detail}.html`），未新增重复 fixture。

### 已知限制

- BingX/Phemex 的 sitemap 全量历史回填（Phase 1/2.6 已验证 sitemap 覆盖性）
  本次没有实现，`force_full` 对这两个源是纯 no-op，见「水位逻辑策略调整」。
- 只验证了 EN 的详情页结构（VN/FR 复用同一套解析逻辑，未针对每个 locale 单独
  发真实请求核对——理由同 Bitunix/Weex 早期批次："locale 只是 URL/参数变化，
  核心机制一致"这个假设本项目反复验证过，风险可控）。

## Phase 2 完成情况：批次 4/4 — Lbank（Phase 2 全部完成）

### Lbank（`src/parsers/lbank_web.py` + `src/collectors/lbank.py`）

跟 Weex 迁移后一样，走 Next.js RSC flight 流（`self.__next_f.push([1,"..."])`），
拼接 + 当 JSON 字符串字面量解析（`json.loads(f'"{joined}"')`，不能用
`unicode_escape`，会把多字节字符拆成 mojibake，Weex 已经踩过这个坑，见
`weex_web.py`）。这次拼接后的文本本身就是合法 JSON（不是 Phemex 那种 JS 对象
字面量），可以直接 `json.loads` 取子结构。

- 真实请求验证：列表页（默认聚合视图，固定 10 条，`?pageNo=`/`?page=` 均不
  生效，跟 Phase 1 结论一致）`latestNews.resultList[]`：noticeId/code/
  contentId/langCode/title/subtitle/content/contentShowTime。
- **发现一个真实的协议细节，Phase 1/Weex 都没有记录过**：列表条目的
  `content` 字段不总是字面量，有的是 `"$43"` 这种 RSC 文本分段引用——Next.js
  flight 流协议里，数字 id 打头的分段用 `<id>:T<十六进制长度>,<原始文本>`
  声明一段文本，其它地方用 `"$<id>"` 引用它。真实抓取时第一条样本
  （`noticeId=17019`）的 content 就是这种引用，第二条是字面量——不是"列表
  条目 content 不可靠所以不用"就能规避的问题，因为**同一个字段在详情页
  也会出现引用**（真实抓取到一个案例：GLMR/MOONBEAM 公告详情页的
  `noticeContent.content` 本身也是 `"$43"`）。`_resolve_text_ref()` 按这个
  协议在拼接后的完整 flight 文本里查找 `<id>:T<hexlen>,` 声明并还原引用，
  找不到声明时原样返回那个 `"$N"` 字符串（不抛异常，调用方看到这种不像正文
  的短字符串至少不会被当成正常内容误用）。列表页的 `content` 字段本身则完全
  不使用（不管字面量还是引用），正文一律取自详情页（反正 detail_mode 本来就
  需要详情页拿 updateTime），避免维护两套引用解析逻辑。
- **另一个真实发现，订正 Phase 1 记录**：详情页 `noticeContent` 有
  `columnId`（叶子分类数值 id）+ `columnIds`（从顶层 tab 到叶子分类的完整树，
  `code` 如 "CO00000064"、`name` 人类可读名称，跟 Phase 1 补充侦察记录的页面
  级 tab 代码树是同一套编号体系）。Phase 1 记录 `field_mapping.category: null`
  是因为当时只采样了列表页（列表页确实没有这个字段），没有采集详情页做交叉
  核对——批次 4 实现时用真实请求发现了这个字段，已改为落库
  （`config/sources.yaml` 的 `category: columnId`）。真实数据只确认过一条样本
  （columnId=66「Spot System Maintenance」，隶属顶层 tab 64「System Upgrades
  & Maintenance」），`config/category_mapping.yaml` 只登记了这一个已确认值，
  其余未登记的 columnId 会安全落到 Phase 3 关键词层（不是 bug，是设计好的
  兜底路径，跟其它源查不到映射 key 时的行为一致）。
- 标题/正文里观察到 Lbank 自己的高亮模板标记 `[[N]]文字[[/N]]`
  （如 "...for [[0]]ERC20[[/0]] chain tokens at [[2]]05:30...(UTC)[[/2]]..."），
  推测是 CMS 标记"需要高亮的关键词/时间"的模板语法，不是 HTML 标签，
  `html_text.py` 不会处理它。`_strip_highlight_markup()` 剥掉这层标记，只保留
  内层文字，避免这类噪音污染正文可读性和 `content_hash` 稳定性。
- `detail_endpoint` 的 VN/ID locale 前缀（`/vi-VN/`、`/id/`）沿用列表页的
  locale 前缀构造，未在实现阶段单独发真实请求验证（EN 机制已验证），本次
  验收run（下方）已经用真实请求跑过 VN/ID，确认这个构造是对的，不是遗留风险。
- `force_full` 是 no-op：翻页从 Phase 2.5 起就没有逆向成功，不是本次新增限制。

### 真实网络验收记录（2026-07-14）

```
python -m src.collectors --source lbank
# EN new=10 changed=0 unchanged=0 failed=0
# VN new=10 changed=0 unchanged=0 failed=0
# ID new=10 changed=0 unchanged=0 failed=0
```

覆盖全部 3 个 locale（Lbank 无分类结构）。落库抽查：30 行全部正文非空、无
残留 HTML 标签、无 `"$N"` 引用残留（`_resolve_text_ref` 正确解析）、VN/ID 的
`detail_endpoint` 构造确认可用（不是 404，验证了上面提到的"未单独验证"的风险
点）。

`pytest`：新增 `tests/parsers/test_lbank_web.py`（11 个，含 RSC 文本引用解析、
高亮标记剥离的专项回归测试）、`tests/collectors/test_lbank_collector.py`
（8 个），复用 Phase 1 已有的真实 fixture（`lbank_{EN,VN,ID}.html`、
`lbank_EN_detail.html`——这份 fixture 恰好是 content 字段为 `"$43"` 引用的
真实样本，直接拿来当"引用解析"的回归测试用例，不用另外构造）。

全部 6 个交易所现在都已在 `src/collectors/__main__.py` 的 `COLLECTOR_BUILDERS`
里登记（Phase 2 批次 1-4 全部完成）。

### 累计验收（Phase 2 批次 3/4 + 4/4 一起统计）

```
pytest
# 240 通过：本次批次 3/4 新增 53 个（tests/parsers/test_bingx_web.py 6 +
# test_phemex_web.py 11 + test_lbank_web.py 11 + tests/collectors/
# test_bingx_collector.py 8 + test_phemex_collector.py 9 +
# test_lbank_collector.py 8），其余为此前批次累计
```

### 未做 / 已知限制

- **BingX/Phemex/Lbank 均未跑 Phase 3 pipeline**（`classify`/`region`/
  `group-check`）——本次范围是"实现采集器 + 真实验收"，Phase 3 的
  跨语言归组/分类打标/地区标记需要另起一次调用，代码本身不需要改
  （`src/pipeline/` 各模块已经是按 `sources` 参数传入源列表的通用设计，
  Phase 3 完成情况里早就写明"等批次 3/4 采集器落地后回来接"，现在可以接了）。
- **全量历史回填**（Zoomex 之外）如「水位逻辑策略调整」所述，本次不实现，
  需要时另起 session 按 Phase 1/2.6 记录的 sitemap 方案做。
- **Bitunix 是否也存在类似 Weex 的数据源迁移风险，仍未复查**（「Weex 数据源
  迁移」一节记录的遗留项，本次同样未处理，不在本次任务范围）。

## Phase 3 完成情况：BingX + Phemex + Lbank（批次 3/4 补跑）

批次 3/4 采集器落地后，按 Phase 3 完成情况里记录的"等采集器落地后回来接"，跑了
`group-check`/`classify --apply`/`region`（`--sources BingX,Phemex,Lbank`）。

```
python -m src.pipeline group-check --sources BingX,Phemex,Lbank
# 检查了 127 个 group，0 异常

python -m src.pipeline classify --apply --sources BingX,Phemex,Lbank
# {'native': 68, 'native_other': 50, 'unmapped_native': 27, 'keyword': 21, '_written': 139}
# 139/166 行写入 category；27 行留 NULL，全部是 Lbank（旧版 RSC 采集器落库的
# columnId 54/57/65 三个值，当时 category_mapping.yaml 只登记了一个样本 66）；
# BingX/Phemex 0 行 unmapped（映射表提前准备充分）

python -m src.pipeline region --sources BingX,Phemex,Lbank
# 127 个 group，45 个判定为地区独占（BingX 1 / Phemex 44 / Lbank 0）
```

**已知偏差（如实记录）**：Phemex 的 44 个"地区独占"里，相当一部分很可能是假阳性——
当时 BingX/Phemex 都还是"只有一屏/一页固定窗口、没有真正翻页"的实现（批次 3/4
刚完成时的状态），EN/FR 各自采到的 20 条很可能是两个不重叠的时间切片，不是真的
"这条内容只有 FR 有"。这批 classify/region 结果基于的是**旧版**采集数据，本节
之后 Lbank 完全重写、Phemex 接入真实分页，重新采集的数据量级完全不同，需要重新
跑一遍 classify/region 才能得到有意义的结果（见后面「Lbank 真实 API 重写」「Phemex
分页升级」两节的验收记录）。

## Lbank 真实 API 重写（2026-07-14）

用户核对批次 4 的采集结果时提出一个关键疑问：Lbank 配置了 5 页的分页上限
（`pagination.max_pages`），但实际每次只采到 10 条——这是不是根本没有真正翻页？
现场用 curl 直接对比 `/support/announcement`（无参数）跟 `?pageNo=2`/`?page=2`，
三次请求返回的 10 条 noticeId 逐字节相同，坐实了 Phase 1 的结论：这几个 query
参数服务端根本不读，`max_pages=5` 这个配置对 Lbank 从未真正生效过（一直只是
个位数级别翻页，翻不出更多内容），跟 BingX/Phemex 是同一类"没有真正分页接口"的
限制。

用户进一步给了一个具体线索：`https://www.lbank.com/support/sections/latest-
news/notice`（"Latest Announcements"聚合 tab），并确认这个 tab 在浏览器里能看到
聚合了各个子 tab 的内容。plain curl 这个 URL 依然只返回导航壳（0 条 noticeId，
跟 Phase 1 当年测试这个 URL 的结论一致）——但用户明确要求投入一次 headless
browser 抓包（同 Zoomex 当年破解 SPA 的方法）去找真正的客户端请求，而不是到此
为止。

**抓包结果（Playwright 拦截真实 XHR/fetch）**：找到了三个匿名可访问、不需要
cookie/签名的真实 JSON API：

- `POST https://www.lbank.com/lbk-api/huamao-media-center/notice/latestList`
  body `{"pageNo":N,"pageSize":M,"topCategory":"NOTICE","categoryCode":"<code>"}`
  ——**真正支持翻页**（pageNo=1 vs 2 返回完全不重叠的 15 条，已用真实请求验证）、
  **真正支持分类筛选**（`categoryCode` 传顶层 tab code 会自动聚合其全部子分类，
  如 "CO00000053" New Listings 聚合 Spot/Futures/Copy Trading 三个子分类，
  `total=6909`，远超默认视图的 10 条）、响应里每条已经带完整 `content`
  （真实抽样比对跟详情接口的正文实质一致，字符数几乎相等，不存在截断）。
  `pageSize` 实测最高测到 100 可用。
- `GET .../notice/content/{code}?noticeCode={code}`：返回 `noticeContent.
  columnId`（叶子分类数值 id，可靠）+ `createTime`/`updateTime`（unix 毫秒，
  可靠）。`content` 字段本身是指向另一域名（`jiz.lbank.com`）静态文本文件的
  URL，不是字面量，本项目不解析这个字段（列表接口的 `content` 已经够用）。
- `POST .../notice/category/list`：返回完整分类树（7 个顶层 tab，`categoryId`/
  `code` 跟 Phase 1 补充侦察记录的页面级 tab 代码树完全对上：129 LBank VIP /
  53 New Listings / 58 Event Announcements / 64 System Upgrades & Maintenance /
  69 Platform Updates / 57 Delisting Information / 63 Fiat）。
- **语言切换不是标准 `Accept-Language` 请求头**（实测传 "id" 无效，仍返回英文），
  是应用自定义头 `ex-language`，三个值均已真实验证生效：`en-US`/`vi-VN`/`id`。

### 架构调整

`src/parsers/lbank_web.py`（RSC flight 流解析，含 `"$N"` 引用解析、高亮标记
剥离等专门为旧方案写的逻辑）已完整删除，替换为 `src/parsers/lbank.py`（薄
JSON 解析层，跟 `zoomex.py` 一样直接消费已经是合法 JSON 的响应，不需要任何
宽松解析器）。`src/collectors/lbank.py` 完整重写：

- 一个 `LbankCollector` 实例 = 一个 locale × 一个顶层分类（`categories.*` 的
  `category_key`/`category_code`），crawl_state 用 category 区分，跟 Zoomex
  的 menu_id 模式一致（`src/collectors/__main__.py` 新增 `_lbank_builder`，
  替换掉之前误用的 `_categorized_collector_builder`）。
- **force_full 不再是 no-op**：真正忽略 `max_pages` 上限、翻到 `resultList`
  返回空为止，等同 Zoomex 的全量核查语义——但默认（`force_full=False`）仍然
  只翻前 `max_pages`（5）页，遵守「水位逻辑策略调整」的既定政策，不是每天都
  全量翻一遍。
- 正文来源反过来了：列表接口的 `content` 才是权威正文来源（`fetch_detail()`
  只补 `columnId`/`updateTime`，不覆盖 `content`/`title`），因为详情接口的
  `content` 字段是要多一跳网络请求的静态文件 URL。
- `raw_category` 落库详情接口给的 `columnId`（真实叶子分类，比请求时用的顶层
  `category_code` 更精确，跟 Weex 用真实 `section_id` 做 `raw_category` 是
  同一个惯例），详情请求失败时兜底退回顶层 `category_code`。
- `config/category_mapping.yaml` 的 lbank 块按完整分类树重写（7 个顶层 + 全部
  子分类，共 22 个 key），3 个叶子值（54/57/65）已用真实采集数据核对过标题
  语义，其余按分类树人类可读名称的语义判断。

### 真实网络验收记录（2026-07-14）

`python -m src.collectors --source lbank`（不加 `--category`，一次性跑全部 21
个组合）两次尝试均被后台任务强制终止（单次运行时间过长——每个组合默认翻 5 页
×每条都要一次详情请求，21 个组合累计预估要 30+ 分钟，超过后台任务的运行时限）。
改成按 `--category` 拆成多次调用，每次覆盖一个分类 × 全部 3 个 locale：

```
python -m src.collectors --source lbank --category lbank_vip        # EN/VN/ID 各 new=1
python -m src.collectors --source lbank --category fiat             # EN new=59 / VN new=58 / ID new=58
python -m src.collectors --source lbank --category platform_updates # EN/VN/ID 各 new=250（命中 max_pages=5 的上限）
```

已验证 3/7 个分类（`lbank_vip`/`fiat`/`platform_updates`，共 928 行），证明
新架构（真分页、真分类、force_full 非 no-op）在真实数据上正确工作，包括
`platform_updates`/ID 那次真实触发了一次网络重试（`http.py` 的指数退避在真实
条件下正常工作）。**剩余 4 个分类**（`new_listings`/`event_announcements`/
`system_maintenance`/`delisting_information`，真实总量分别是
6909/2754/3422/796，每个都会命中 `max_pages=5` 的上限、每个分类×3 locale 预估
各要 15-20 分钟）本次没有跑——不是遗漏，是当前这种"从空库开始建仓"的场景才会
需要一次性拉这么大的量，日常增量运行（数据库里已经有大部分历史数据后）只需要
处理新增/变更的一小部分，不会像这次一样耗时。需要这几个分类的真实数据时，
直接对每个分类单独跑一次 `--category <key>` 即可，不需要改任何代码。

`pytest`：`src/parsers/lbank_web.py` 和 `tests/parsers/test_lbank_web.py` 删除，
新增 `tests/parsers/test_lbank.py`（7 个）+ 重写 `tests/collectors/
test_lbank_collector.py`（12 个），复用真实抓取的 JSON fixture
（`tests/fixtures/lbank_api_*.json`）。旧的 RSC-based HTML fixture
（`lbank_EN.html` 等）保留在 `tests/fixtures/` 里作为历史存档，不再被任何
测试引用。

### 已知限制

- `category_mapping.yaml` 里除 54/57/65 外的叶子分类映射未逐条用真实标题核对，
  按分类树名称语义判断（置信度同 BingX 当初只凭反查到的 section 名称做映射）。
- `noticeId`/`columnId` 的可靠性依赖详情接口，如果某条公告详情请求失败会退回
  用顶层 `category_code`（字符串格式跟正常的数值 columnId 不一致，如 "CO0000
  0053" vs "54"），是刻意的降级容错，不是 bug。

### 简化补丁：省掉详情请求，改用顶层分类（2026-07-15）

上面记录的 `fetch_detail()` 逐条请求详情（补 `columnId`/`updateTime`）在真实使用中
被确认是不必要的开销：`updateTime` 对 Lbank 的 `full_scan` 策略没有作用（不驱动
watermark，变更检测只看 `content_hash`）；`columnId`（叶子分类）的精度超过了下游
实际需要的粒度——用户提供的 Lbank 官网公告中心真实截图确认下游只需要识别 7 个顶层
tab（All / LBank VIP / New Listings / Event Announcements / System Upgrades &
Maintenance / Platform Updates / Delisting Information / Fiat），正好对应
`config/sources.yaml` 已配置的 7 个 `categories.*`。

**改动**：`LbankCollector.fetch_detail()` 改成恒等函数（直接 `return item`，不发
任何请求）；`normalize()` 的 `raw_category` 直接等于请求时用的顶层 `category_code`
（如 `"CO00000053"`），不再依赖详情接口。`src/parsers/lbank.py` 的
`parse_detail_response()` 函数整个删除（确认只有 `lbank.py` collector 和
`tests/parsers/test_lbank.py` 两个测试引用它，无其它调用方，对应测试一并删除）。
`config/category_mapping.yaml` 的 `lbank` 块从 22 个 key（7 顶层 + 15 叶子）精简
成只有 7 个顶层 key——**保留了简化前已用真实标题验证过的判断**：`CO00000129`
（LBank VIP）和 `CO00000063`（Fiat）仍然是 `campaign`（不是直觉上可能会写的
`other`），因为这两个顶层 tab 本身没有子分类、简化前就已经拿真实标题核对过是活动/
促销性质。`config/sources.yaml` 的 lbank 三个 locale 块同步更新了几处会变得文档
不实的字段：`has_update_time` 由 `true` 改 `false`、`field_mapping.category` 由
`columnId` 改 `category_code`，`detail_endpoint`/`detail_mode` 字段本身保留（不影响
`src/probe/core.py` 对 `detail_mode=="blocked"` 的判断），但加注释说明已不再被
`fetch_detail()` 调用。

**真实验证记录（`data/run_20260715_bitunix_phemex_lbank.db`，`--category
new_listings --locale EN`）**：这个组合在更早的 session 里已经跑过一轮
（100 条，`raw_category` 是旧版本的叶子值）。改动后重跑：

```
new=1 changed=0 unchanged=99 failed=0，耗时 ~3s（此前同等规模的采集因为每条 500ms
的详情请求，耗时量级是 ~50s+）
```

第二次重跑（幂等验证）：`new=0 changed=0 unchanged=100 failed=0`，同样 ~3s。

**一个在验证中确认的真实行为，值得记录（不是本次改动引入的 bug，是
`upsert_announcement` 一直以来的既有逻辑，见 `src/db/operations.py` 的
`unchanged` 分支）**：`unchanged` 分支只更新 `fetched_at`/`raw_category`，**不会
触碰 `update_time`**。所以这次重跑的 99 条 `unchanged` 行，`raw_category` 被正确
静默回填成 `CO00000053`，但 `update_time` 保留的是旧版本详情接口抓到的真实历史值
（非 NULL）；只有这次新增的 1 条 `new` 行、以及以后任何真正 `content` 发生变化触发
`changed` 分支的行，才会拿到新代码传入的 `update_time=None`。也就是说"这次改动后
`update_time` 恒为 NULL"只对**从现在起新采集/新变更**的行成立，已经入库的存量行
（`content` 没有变化的部分）会一直带着旧的 `update_time` 值，直到该行内容真正变更
一次为止——这是 `unchanged` 分支设计的自然结果（Phase 2.6 就是照着这个逻辑给
`raw_category` 加的静默更新，`update_time` 从来不在这个分支的更新范围内），不需要
额外处理。

**已知遗留**：这个 db 里 Lbank 的其余 6 个分类（`lbank_vip`/`event_announcements`/
`system_maintenance`/`platform_updates`/`delisting_information`/`fiat`，以及
`new_listings` 的 VN/ID 两个 locale）本次未重新采集，`raw_category` 仍是旧版本的
叶子值（如 54/55/59/60/62/65/66/67/70/71/72/73/129/130），不在新的 7-key 顶层
映射表里。如果之后对这些行重跑 `pipeline classify`，会因为查不到映射而变成
`unmapped_native`，需要重新采集（而不是重新分类）才能拿到新版 `raw_category`。
本次任务范围只验证了这一个组合，不处理这批存量数据。

## Phemex 分页升级（2026-07-14）

同一次用户要求核实"BingX/Phemex 是否有跟 Lbank 同类的隐藏真实接口"时，用
headless browser 抓包 Phemex 公告列表页也找到了真实分页 API：

`GET https://prod-cms-api.phemex.com/articles/query?categoryKey=
AnnouncementCategory<id>&entryKey=Announcement&language=<lang>&pageNo=N&
pageSize=M`——**真正支持翻页**（pageNo 递增返回不同文章，已用真实请求验证），
**完全匿名，不需要签名/cookie**（这一点是跟 BingX 最大的区别，见下一节）。

**关键发现**：`categoryKey` 的数字部分不随 locale 变化，一直是 EN 侧的
432/442/452——不是 Phase 2.6 记录的"i18n 各 locale 独立编号"那组值（FR 侧
438/448/458，那组值来自 `window.preloadedData.pageData.category.id`，是另一
套跟这个新接口无关的编号）。切换语言完全靠 `language` 参数（`en`/`fr`），真实
验证 `language=fr` 返回法语标题，News/Activities/Newsletter 总数分别为
1658/158/5，跟 Phase 1 侦察记录的 FR 总数 1641/158/4 基本吻合（小幅增长是数据
自然更新，不是核对偏差）。

这个接口只给 `id`/`slug`/`title`/`publishedTime`/`desc`（**截断预览**，不是
完整正文）——**完整正文仍然要靠详情页 `window.preloadedData`**
（`parse_article_detail()`，完全未受这次改动影响，之前已验证详情页正文完整、
无截断问题）。

### 架构调整

`src/parsers/phemex_web.py` 的 `parse_article_list()`（旧版列表页
`window.preloadedData` 解析，只能拿固定 20 条）已删除（确认无其它调用方后
按"确定不用就彻底删除"原则清理，不是留着当兼容层），新增 `parse_query_response()`
解析 `prod-cms-api.phemex.com/articles/query` 的响应。`src/collectors/
phemex.py` 的 `fetch_list()` 改用这个新接口分页（`pagination.page_size`
默认 20、`max_pages` 默认 5，force_full 忽略上限），`fetch_detail()`/
`normalize()` 完全不变。`src/collectors/__main__.py` 新增 `_phemex_builder`
（原来的 `_categorized_collector_builder` 假设"每个分类各自独立 endpoint"，
现在 Phemex 全部分类共用同一个 `list_endpoint`，只是 `list_category_id`
不同，改用专属 builder）。

`config/sources.yaml` 的 phemex 块：`categories.*` 从 `{endpoint, category_id}`
改成 `{list_category_id}`（EN/FR 共用同一组数值 432/442/452），新增顶层
`list_endpoint`/`language` 字段；原来 FR 块记录的 438/448/452 那组 locale
相关编号予以保留（仅注释存档，代码不再消费）。

**force_full 不再是 no-op**：真正忽略 `max_pages` 上限、翻到 `rows` 返回空
为止，等同 Zoomex/Weex/Lbank 的全量核查语义；默认仍只翻前 5 页。

### 真实网络验收记录（2026-07-14）

```
python -m src.collectors --source phemex
# EN/news        new=100  EN/activities new=100  EN/newsletter new=13
# FR/news        new=100  FR/activities new=100  FR/newsletter new=5
```
覆盖全部 2 个 locale × 3 个分类 = 6 个组合，全部一次性真实跑通（418 行），每个
组合默认翻 5 页（远超旧版固定的 20 条单页），耗时约 8 分钟。news/activities
两个大分类都稳定命中 `max_pages=5` 的上限（新旧对比：从固定 20 条提升到
100 条），newsletter 分类总量本来就小（13/5 条），不受分页上限影响。

**踩过一次坑**：第一次尝试时跟 Lbank 的采集任务同时在后台跑，两边并发写同一个
SQLite 文件，Phemex 这边跑完全部 6 个组合、真实请求全部成功，但退出时的最终
`conn.commit()` 因为 Lbank 那边还占着写锁抛出 `database is locked`，整个进程
崩溃、这一轮的 418 行全部没有落库（`get_connection()` 是整个 CLI 运行期间只开
一个连接，最后才统一 commit，中途没有分批提交）。改成不并发跑（严格串行）后
第二次一次成功。这是本 session 操作层面的教训，不是代码 bug：`python -m
src.collectors` 目前假设同一时间只有一个进程在写同一个 db 文件，跑多个 source
时不要用后台并发，除非改用不同的 `--db-path`。

`pytest`：`tests/parsers/test_phemex_web.py` 删掉 `parse_article_list` 相关
用例、新增 `parse_query_response` 用例（复用真实抓取的
`tests/fixtures/phemex_api_query_news_{en,fr}.json`）；`tests/collectors/
test_phemex_collector.py` 的 `fetch_list` 系列用例改 mock `fetch_json`（原来
mock 的是 `http_fetch`，因为列表请求从"抓 HTML 页面"变成"调 JSON API"）。

### 已知限制

- `desc` 截断预览具体截断长度未测（不影响正确性，反正不使用这个字段做正文）。
- FR 的 `categoryKey` 复用 EN 数值这一发现只对 News/Activities/Newsletter 三个
  已知分类验证过，如果以后 Phemex 增加新分类，需要重新确认新分类的
  `categoryKey` 是否也遵循"EN 数值 + language 参数"这个规律。

## BingX 签名保护（2026-07-14，调查未采纳深入破解）

同一次核实里，headless browser 抓包 BingX 也找到了一个更强的真实接口：
`GET https://api-app.qq-os.com/api/customer/v1/announcement/listArticles?
sectionId=<id>&page=N&pageSize=20`，响应结构跟首屏聚合视图完全一致但支持真正
按 section 翻页。**但这个接口有签名保护**：请求头带 `sign`（看起来是 HMAC 类
签名，随 `timestamp` 变化）+ `device_id`/`app_version` 等一整套疑似移动端/
官方 App 共享的鉴权体系。验证过两种绕过尝试均失败：不带任何签名头直接请求
返回 `{"code":100003,"msg":"设备时间不正确"}`；原样重放刚抓到的完整请求头
（含 `sign`）几秒后返回 `{"code":100005,"msg":"安全策略已升级"}`（像是防重放/
时间窗口校验）。

报给用户后，用户确认**不投入**逆向这个签名算法（需要下载/分析混淆过的 JS
bundle 找 HMAC key，工作量不可预估）。BingX 维持现有实现（首屏 NUXT_DATA
聚合视图，固定约 20 条，`force_full` 对 BingX 仍然是 no-op），这是本次调查后
确认过的、有意接受的限制，不是遗漏。

## Weex 路径问题（2026-07-14，用户搁置，未处理）

本次 session 同时被要求"删除现有 Weex 数据、按同样约束重新采集、跑到 Phase 3"，
执行 `python -m src.collectors --source weex` 时用户打断，反馈"weex路径还是有
问题"，要求先搁置。**Weex 现有数据在本 session 内已被删除**（连同 crawl_state），
尚未重新采集——下个 session 处理 Weex 之前需要先确认用户所说的"路径问题"具体
指什么（可能是 URL 构造、locale_path 前缀，或是别的 Weex 特有问题），不要在
不了解具体问题的情况下直接重跑，那样只会重复同样的错误。
  迁移」一节记录的遗留项，本次同样未处理，不在本次任务范围）。

## daily 增量分页上限 5→2 + Bitunix/Phemex/Lbank 独立 db 试运行（2026-07-15）

用户认为「水位逻辑策略调整」（2026-07-14）定下的 `pagination.max_pages: 5` 每天
翻 5 页仍然偏多，要求收紧到 **2 页**；同时要求对 Bitunix/Phemex/Lbank 三个源跑
一遍（Weex 因「Weex 路径问题」尚未解决被排除，BingX/Zoomex 本次未涉及），且
**本轮数据单独存储**，跑完后继续推进到 Phase 4。

### max_pages 配置改动

`config/sources.yaml` 全部 12 处 `max_pages: 5` → `max_pages: 2`（Weex EN/FR、
Phemex EN/FR、Lbank EN/VN/ID、Zoomex 5 个 locale 的 categories 块），配套注释
"翻前 5 页" 同步改成 "翻前 2 页"。`force_full` 语义不变（仍然忽略这个上限翻到
底），只影响默认（非 `--force-full`）的 daily 增量深度。**Bitunix 不受影响**：
它是 watermark 早停机制，从来不用 `max_pages`。CLAUDE.md 里其它历史 session
记录（如「Lbank 真实 API 重写」「Phemex 分页升级」）中出现的 `max_pages=5` /
`max_pages（默认 5）` 字样是**当时**的真实运行记录，予以保留不回改——这些是
历史日志，不是当前配置的文档spec。

### 独立 db 试运行

新建 `data/run_20260715_bitunix_phemex_lbank.db`（`python -m src.db init
--db-path ...`），三个源的采集/pipeline/analysis 全部指向这个新库，
**完全不碰** `data/competitor_intel.db`（主库，含 Zoomex 全量基线 + 之前的
Bitunix/Weex 试运行残留）。因为是全新空库，Bitunix 的 watermark 首次运行
天然做了一次全量回填（跟 Phase 2.7 时代的行为一致，不是本次刻意要求全量）；
Phemex/Lbank 受新的 `max_pages=2` 限制，只翻前 2 页。

```
python -m src.collectors --source bitunix --db-path data/run_20260715_bitunix_phemex_lbank.db
# EN new=1534  FR new=906  ID new=407（全量回填，逐字节匹配 Phase 2.7 历史记录）

python -m src.collectors --source phemex --db-path data/run_20260715_bitunix_phemex_lbank.db
# EN/news=40 EN/activities=40 EN/newsletter=13 FR/news=40 FR/activities=40 FR/newsletter=5
# （max_pages=2 × page_size=20，大分类精确命中 40 条上限；newsletter 总量本来就小，未触顶）

python -m src.collectors --source lbank --category <7 个分类各自单独跑> \
  --db-path data/run_20260715_bitunix_phemex_lbank.db
# lbank_vip: EN/VN/ID 各 1（总量本来就小）
# fiat: EN=59 VN=58 ID=58（总量小，未触顶）
# event_announcements / system_maintenance / platform_updates /
# delisting_information / new_listings：EN/VN/ID 各 100
# （max_pages=2 × page_size=50，5 个大分类全部精确命中 100 条上限）
```

**Lbank 分 7 个 `--category` 拆开跑，不是一条命令跑全部 21 个组合**：单个
`--source lbank`（不限category）预估耗时会超过后台任务的运行时限（「Lbank
真实 API 重写」一节记录过同类问题），拆开跑也是为了避免多进程并发写同一个
db 文件触发 `database is locked`（「Phemex 分页升级」一节记录过的真实教训）
——本次全程严格串行，一个分类跑完再跑下一个。

db 校验：
```sql
SELECT source, COUNT(*) FROM announcements GROUP BY source;
-- Bitunix 2847 / Lbank 1678 / Phemex 178，逐一匹配上面命令行输出的总和
```

### Phase 3 pipeline（同一独立 db）

```
python -m src.pipeline --db data/run_20260715_bitunix_phemex_lbank.db group-check
# 检查了 1534 个 group，0 异常

python -m src.pipeline --db data/run_20260715_bitunix_phemex_lbank.db classify --dry-run --sources Bitunix,Phemex,Lbank
# 共扫描 4703 行：native 4136 / native_other 503 / keyword 64 / unmapped_native 0 / llm_pending 0
python -m src.pipeline --db data/run_20260715_bitunix_phemex_lbank.db classify --apply --sources Bitunix,Phemex,Lbank
# 已写入，_written=4703

python -m src.pipeline --db data/run_20260715_bitunix_phemex_lbank.db region --sources Bitunix,Phemex,Lbank
# 共 2302 个 group，85 个判定为地区独占（is_region_exclusive=true 85 / false 4618）
```

category 分布（三源合计 4703 行）：

| source | campaign | product | listing | delisting | other |
|---|---|---|---|---|---|
| Bitunix | 151 | 794 | 1437 | 296 | 169 |
| Phemex | 80 | 0 | 4 | 58 | 36 |
| Lbank | 478 | 300 | 302 | 300 | 298 |

**地区独占抽样发现一个可疑模式，如实记录未处理**：85 个地区独占 group 里，
抽样看到多条 Phemex/FR 判定为独占的公告，标题却是纯英文（如
"Phemex Will Delist the KORUUSDT Futures on July 15, 2026"）。有两种可能：
① Phemex 的下架类通知本来就不做本地化、FR 端也发英文原文（真实情况，非
bug）；② 沿用「Phemex 分页升级」一节记录过的旧疑虑——EN/FR 各自的翻页窗口
可能是两个不完全重叠的时间切片，`is_region_exclusive` 把"这次窗口恰好没抓到
EN 那条"误判成"独占"。本次未展开调查（不在任务范围内），下次如果要认真使用
Phemex 的地区独占标记，需要先厘清这一点。

### Phase 4（同一独立 db，`--dry-run`，仍无真实 LLM 凭证）

```
python -m src.analysis --db data/run_20260715_bitunix_phemex_lbank.db --source Bitunix,Phemex,Lbank --dry-run
# 正确产出 30 个批次（Bitunix 12 + Lbank 12 + Phemex 6，category=other 全部正确排除）
# 预估 tokens 合计 ≈ 216 万（30 个批次里最大的 Bitunix/listing/EN 单批次 746 篇 ≈ 36.8 万 token）
```

**`zmx_hits` 全部 30 个批次恒为 0**——这是本次"单独存储"选择的直接代价：
ZMX 基线检索（`zmx_index.py`）需要同一个 db 里有 Zoomex 数据才能建索引，
这个独立 db 只导入了 Bitunix/Phemex/Lbank，完全没有 Zoomex 数据，所以每个
批次的 ZMX 差异分析在这次 dry-run 里全部退化成"基线数据有限"的空提示，不
代表 prompt 构建逻辑有问题（同一套代码在含 Zoomex 数据的主库里跑 `--dry-run`
时是有真实检索命中的，见「Bitunix 当日数据试运行」一节）。如果以后需要在
独立 db 里也看到真实 ZMX 命中率，需要额外把 Zoomex 数据也导入这个独立 db
（或者反过来，把这三个源的数据合并回主库），本次按用户"单独存储"的要求未
这样做。

**Bitunix 的批次体量不代表真实"每日"量级**：因为是全新空库上的首次
watermark 全量回填，`Bitunix/listing/EN` 一个批次就有 746 篇历史公告（对应
~36.8 万 token），这是"全量回填当天全部落库"的产物，不是"某一天真实新增
746 条"。Phemex/Lbank 因为受 `max_pages=2` 限制，批次体量更接近"一次运行能
看到的窗口"而不是真实自然日增量，同样仅供参考。

**真实 LLM 调用仍未执行**：`.env` 仍未配置任何凭证，跟之前几次记录
（Weex/Bitunix 试运行）状态一致。

### 已知限制

- 本次范围明确排除 Weex（路径问题未解决）、BingX、Zoomex；主库
  `data/competitor_intel.db` 完全未被本次改动触碰。
- 独立 db 里没有 Zoomex 数据，Phase 4 的 ZMX 差异分析这次只验证了"批次划分/
  prompt 构建能正确工作"，没有验证"真实检索命中"这条路径（该路径已在别的
  session 用含 Zoomex 数据的主库验证过）。
- Phemex 地区独占标记的可疑假阳性（见上文）未展开排查。
- `max_pages: 2` 对 daily 增量是否足够覆盖真实单日更新量，同样没有做过实测
  校准（跟「Zoomex 全量建仓」一节记录的"5 页够不够"是同一类未决问题，这次
  只是把默认值改小了，没有回答"多少页才够"）。

## Phase 4 新增 LLM 后端：Cursor Background Agent（cursor_sdk）（2026-07-15）

用户提供了一个 Cursor 官方 API key，要求接入 Phase 4 分析层，先用
`data/test_daily_20260715.db` 里 Bitunix 当天的批次做一次真实消耗/链路测试。

### 关键发现：这不是 OpenAI 兼容的 /chat/completions 端点

用户最初给的 `https://api.cursor.com/v1/agents` + key 直接怼 `LLM_API_BASE` 会走不通——
反编译 `cursor_sdk` wheel（pip download + unzip 检查源码，没有凭记忆猜）确认这是
**Cursor Background Agent API**：`Agent.prompt()` 背后是完整的 Cursor 编码 agent（内置
Node bridge + 平台相关原生二进制，wheel 本体 47MB），需要一个 `local.cwd` 目录、支持
`AgentModeOption = "agent"/"plan"`、有 `sandbox_options`/`custom_tools` 等工具调用相关
选项——是"在一个目录上下文里跑一个会用工具的编码 agent"，不是无副作用的纯文本补全接口。
跟 `src/analysis/llm.py` 的 `call_llm()`（`{LLM_API_BASE}/chat/completions`）是两套完全
不同协议，不能共用同一个调用函数，因此新增 `src/analysis/cursor_agent.py` 作为独立后端，
不是改造 `llm.py`。

### 安全边界：cwd 固定指向项目外的隔离沙箱目录

`Agent.prompt()` 必须给 `local.cwd`，如果直接传项目仓库路径，agent 有能力读写这个目录下
的真实代码。改为固定指向 `data/.cursor_agent_sandbox/`（新建的空目录，已加入
`.gitignore`：`data/.cursor_agent_sandbox/*`），并设 `sandbox_options=SandboxOptions(
enabled=True)` 进一步限制其文件系统/网络访问范围；prompt 末尾追加一句强指令"不要使用
任何工具，不要读取/搜索/列出/修改任何文件"。真实测试下这个约束是有效的——批次分析的
响应都是纯 JSON 文本，没有观察到 agent 尝试探索目录。

### 成本熔断：调用次数上限，不是真实美元

`RunResult` 只有 `usage`（`TokenUsage`：input/output/total token 数），没有价格字段，
Cursor 的计费方式跟本项目原有 OpenAI 兼容协议假设的"按 token 单价换算"对不上。跟用户
确认后放弃精确美元预算，改成 `config/analysis.yaml` 新增 `llm.max_calls_per_run`
（默认 `null` 不限制）：达到这个调用次数后，`run.py` 的 `run()` 循环对剩余批次直接跳过
（不写 `insight`、不算 `validation_failed`，计入新增的 `report.skipped_call_cap`），
留到下次重跑时重新尝试——不是失败，是主动节流。`python -m src.analysis` 新增
`--provider {openai_http,cursor_agent}` / `--max-calls N` 两个 CLI 参数，覆盖 yaml
默认值，方便一次性试跑不用改配置文件。

### 架构：`src/analysis/config.py` 新增 `CursorCredentials`

跟 `LlmCredentials` 分开的独立凭证类（没有 `api_base` 字段，`Agent.prompt()` 固定走
`cursor_sdk` 内置 bridge，不是拼 URL）。`.env` 新增 `CURSOR_API_KEY`/`CURSOR_MODEL`
（默认 `auto`），`config/.env.example` 同步加了注释说明这是两套互不相关的凭证。
`requirements.txt` 新增 `cursor_sdk>=0.1.9`，注释说明只有 `llm.provider=cursor_agent`
时才需要装这个包，默认 `provider=openai_http` 完全不受影响（`tests/analysis/test_run.py`
的 58 个用例全部走 mock 过的 `call_llm`/`load_llm_credentials`，`provider` 默认值不变，
未受本次改动影响，`pytest` 238 通过）。

### 真实连通性 + 真实批次测试记录（2026-07-15，用真实 API key）

**Step 1，最小连通性测试**（scratchpad 一次性脚本，prompt 只有"请只回复 OK"）：

```
耗时 17.6s（服务端 duration_ms=7126），status=finished
usage: input_tokens=11354 output_tokens=99 cache_read_tokens=1318 total_tokens=12771
result: "OK"
```

单次调用 1.2 万+ token 里绝大部分是 agent 框架自身的系统提示词/工具定义开销，不是我们
发送内容的大小决定的——这是接入前完全没有预料到的成本结构，如果不做这次最小化探测，
直接拿真实批次 prompt 去跑会把这个固定开销和"批次内容本身有多贵"混在一起，没法看清楚。

**Step 2，真实批次测试**（`python -m src.analysis --db data/test_daily_20260715.db
--source Bitunix --provider cursor_agent --max-calls 3`，Bitunix 当天 10 个批次，
`max_calls_per_run=3` 熔断）：

```
分析批次数：analyzed=3 derived=4 cache_hits=0 llm_calls=3 skipped_call_cap=3
validation_failed=0 total_tokens=48339
  - Bitunix/campaign/EN            （llm_tokens_used=16508）
  - Bitunix/delisting/EN           （llm_tokens_used=15099）
  - Bitunix/delisting/FR (derived from EN)   （0 token，EN 复用）
  - Bitunix/delisting/ID (derived from EN)   （0 token，EN 复用）
  - Bitunix/listing/EN             （llm_tokens_used=16732）
  - Bitunix/listing/FR (derived from EN)     （0 token，EN 复用）
  - Bitunix/listing/ID (derived from EN)     （0 token，EN 复用）
  - Bitunix/product/EN (skipped: call cap reached)
  - Bitunix/product/FR (skipped: call cap reached)
  - Bitunix/product/ID (skipped: call cap reached)
```

单次真实批次调用 token 消耗 15099~16732（比最小连通性测试的 12771 略高，符合"框架固定
开销 + 批次内容"的预期），`validation_failed=0`——`validate_and_normalize()` 全部正常
解析，说明 Cursor agent 虽然是编码 agent 出身，在"只回答要求的 JSON"这个指令约束下确实
遵守了格式要求。人工核对 3 条 `insights` 内容（campaign 的 UTC 会议活动、delisting 的
Eclipse/PoP Planet 下架、listing 的 3 个股票永续合约），summary/diff_type/priority 均
准确对应源公告内容，EN→FR/ID 的 derive 逻辑正确复用了 EN 分析结果（这次真实验证了
之前"Phase 4 完成情况"里一直标注"未跑真实 LLM 验收"的 EN 复用路径）。剩余
`Bitunix/product/{EN,FR,ID}` 3 个批次被熔断跳过，下次不带 `--max-calls` 限制（或提高
限制）重跑 `python -m src.analysis --db data/test_daily_20260715.db --source Bitunix
--provider cursor_agent` 即可补上，不需要改代码。

### 已知限制

- **没有真实美元定价**：`max_calls_per_run` 只是调用次数熔断，不是精确预算控制——3 次
  真实调用 token 消耗量在 15099~16732 区间波动（同一批次大小下也会变，agent 框架本身
  行为不是完全确定性的），如果以后拿到 Cursor 的真实计费规则，应该换成基于
  `usage.total_tokens` 的更精确控制。
- **`sandbox_options.enabled=True` 的实际限制范围未验证**：只确认了 agent 没有主动探索
  沙箱目录（该目录本来就是空的，没什么可探索的），没有专门测试"如果 prompt 诱导 agent
  尝试读取沙箱外的文件会不会被挡住"——目前的安全边界完全依赖"沙箱目录是空的 + prompt 明确
  要求不使用工具"这两层，不是 SDK 层面的强隔离保证。
- **只测试了 Bitunix 一个源、`campaign`/`delisting`/`listing` 三个 category**（`product`
  被熔断跳过、`other` 本来就不产出 insights），`Weex`/`Phemex`/`Lbank`/多语言真实 LLM 输出
  质量未测试。
- **cache_read_tokens 显示存在 prompt caching，但两次真实调用之间的缓存命中率/降本效果
  未测量**（3 次调用的 agent 各自独立创建/关闭，`CreateAgent`→`Send`→`CloseAgent`，没有
  刻意测试"连续复用同一个 agent 实例"是否更省 token）。
- **`openai_http` 仍是默认 provider**，本次改动完全是新增可选路径，不影响任何现有/历史
  session 记录的 OpenAI 兼容测试结果。

## Phase 5 完成情况：飞书多维表同步（2026-07-15）

`src/sinks/feishu_bitable.py`，用 `data/test_daily_20260715.db`（Bitunix 32 + Zoomex
2018 条 announcements，7 条 insights）做了真实网络验收，两张表分别真实建列、真实写入、
真实验证幂等。

### 架构

- announcements/insights 分别位于**两个独立的飞书多维表 app**（`FEISHU_ANNOUNCEMENTS_
  APP_TOKEN` / `FEISHU_INSIGHTS_APP_TOKEN` 不同，不是同一个 base 下的两张子表），domain
  固定 `open.larksuite.com`（sg.larksuite.com 账号），不是 `open.feishu.cn`（那个域名
  只服务中国大陆账号）。`.env` 变量名：`FEISHU_APP_ID`/`FEISHU_APP_SECRET` +
  `FEISHU_ANNOUNCEMENTS_APP_TOKEN`/`FEISHU_ANNOUNCEMENTS_TABLE_ID`/
  `FEISHU_INSIGHTS_APP_TOKEN`/`FEISHU_INSIGHTS_TABLE_ID`（`config/.env.example` 已同步
  更新，原先占位的 `FEISHU_BITABLE_APP_TOKEN`/`FEISHU_BITABLE_TABLE_ID_*` 命名是猜测的，
  已改成跟真实两个 app_token 的场景对齐）。
- 复用 `src/collectors/http.py` 的 `fetch_json()`，不引入新 HTTP 库。飞书的"业务错误"
  体现为 HTTP 200 + `body.code != 0`（`fetch_json` 本身不识别这个），`_request()` 单独
  判断：命中 token 失效相关错误码（`TOKEN_INVALID_CODES`）强制刷新 token 重试，其它非
  0 错误码按指数退避重试到 `max_retries` 次仍失败才抛 `FeishuApiError`。`tenant_access_
  token` 按 `app_id` 缓存在模块级 dict，过期前 60s 视为需要刷新。
- 幂等策略**不用飞书的按条件过滤查询接口**（避免依赖其 filter query 语法细节），改成
  一次性分页拉全表已有记录、按业务主键（`uid`/`id`）建本地索引，再逐行比较决定
  create/update/skip——几千条规模一次性全量拉取足够便宜，比对着每行发一次过滤查询更
  简单可靠。
- 字段类型映射：Text=1 / Number=2 / Checkbox=7（`FIELD_TYPE_TEXT`/`FIELD_TYPE_NUMBER`/
  `FIELD_TYPE_CHECKBOX`），`ANNOUNCEMENTS_FIELD_SPECS`/`INSIGHTS_FIELD_SPECS` 两个列表
  按 CLAUDE.md schema 表格顺序、业务主键排第一。
- `ensure_fields()` 幂等建列：只新建缺少的字段，已有字段不动；唯一例外是**全新空表**
  （只有飞书自动生成的默认主字段这一列时），把这个占位字段改名成业务主键（uid/id），
  让主键真正排在第一列，而不是留一个多余的占位列——这个改名只在"表里确实只有这一个
  自动生成字段"时才触发，不会动任何已经真实使用过的字段。

### 三个真实 bug（dry-run 测不出来，靠真实网络请求才暴露）

1. **飞书字段更新接口要求 body 带 `type`，即使只是改名**：`ensure_fields()` 第一次真实
   运行时，重命名默认主字段的 `PUT .../fields/{field_id}` 只传了 `{"field_name":...}`，
   返回 `code=99992402`「field validation failed: type is required」。修复：
   `rename_field()` 增加 `field_type` 参数，body 带上原字段的 `type`（不变值也要传）。
   `tests/sinks/test_feishu_bitable.py` 的 fake server 现在也会在 `type` 缺失时返回
   同样的错误码，防止这个调用方式回归。
2. **空字符串字段导致每次重跑都被误判成"需要 update"**：Zoomex 有 8 行 `content=''`
   （纯图片公告，Phase 3 之前的全量建仓记录里提到过）。`_build_fields()` 原来只跳过
   `None`，空字符串 `''` 会被当作有效值写入飞书；但飞书不会把写入的空字符串 Text 字段
   持久化成"空但存在"的值，读回来是 `None`——本地 `''` 跟远端 `None` 逐次比较都不相等，
   每次重跑都会被误判成 `changed`。真实验收时首次重跑观察到 8 行 announcements 被
   错误标记为 `updated`（不是 `skipped`），跟 Zoomex 空 content 行数精确对上。修复：
   `_build_fields()` 对 Text 类型字段，值转成字符串后如果是 `''` 也跳过（不写入这个
   key），跟 `None` 一视同仁。
3. **飞书 Number 字段读回来是字符串，不是 JSON 数字**：真实请求确认 `article_count`/
   `llm_tokens_used` 这两个 Number 字段，`GET records` 返回的值是 `"2"`/`"16732"` 这种
   字符串（不是 `2`/`16732`），推测是为了避免 JS 客户端的大数精度丢失，但连小整数也这样
   处理。`_record_needs_update()` 原来对 Number 字段不做任何转换直接比较，导致 7 条
   insights 全部在第二次重跑时被误判为需要 `update`（`"2" != 2`）。修复：新增
   `_extract_number_field()`，把字符串形式的数字转回 `int`/`float` 再比较。

以上三个 bug 都是靠对着真实飞书表反复重跑、观察 `sync_log` 的 action 分布不符合预期
才发现的——`--dry-run` 完全不碰飞书 API，测不出这类接口行为细节；纯 mock 单测如果不是
照着真实响应的具体形状写 fake server（而是想当然地假设"数字字段返回数字"），同样会
让这些 bug 溜过去。`tests/sinks/test_feishu_bitable.py` 的 `FakeFeishuServer` 已经按
真实观测到的行为调整（Text 字段读回来是分段数组、Number 字段读回来是字符串），新增
两个专门的幂等回归测试（`test_sync_insights_rerun_is_fully_idempotent_despite_
stringified_numbers` / `test_sync_announcements_rerun_is_idempotent_with_empty_
string_content`）锁定这两个真实场景。

### 真实网络验收记录（2026-07-15，`data/test_daily_20260715.db`）

```
python -m src.sinks.feishu_bitable --db-path data/test_daily_20260715.db --dry-run
# announcements: dry_run_rows=2050（不调用飞书 API，只打印字段映射）
# insights: dry_run_rows=7

python -m src.sinks.feishu_bitable --db-path data/test_daily_20260715.db
# 首轮：announcements created=2050 updated=0 skipped=0 failed=0
#       insights      created=7    updated=0 skipped=0 failed=0

python -m src.sinks.feishu_bitable --db-path data/test_daily_20260715.db
# 第二轮（修复前，暴露上面两个真实 bug）：
#   announcements created=0 updated=8 skipped=2042 failed=0
#   insights      created=0 updated=7 skipped=0    failed=0

# 修复 _build_fields()（空字符串）+ _record_needs_update()（Number 字符串化）后：
python -m src.sinks.feishu_bitable --db-path data/test_daily_20260715.db
# 第三轮：announcements created=0 updated=0 skipped=2050 failed=0
#         insights      created=0 updated=0 skipped=7    failed=0   —— 完全幂等
```

`sync_log` 累计校验（三轮真实运行的完整审计轨迹，符合预期）：
```
bitable_announcements / create / success: 2050  （首轮）
bitable_announcements / update / success: 8      （第二轮，修复前的误判）
bitable_announcements / skip   / success: 4092    （第二轮 2042 + 第三轮 2050）
bitable_insights      / create / success: 7      （首轮）
bitable_insights      / update / success: 7      （第二轮，修复前的误判）
bitable_insights      / skip   / success: 7       （第三轮）
```

`pytest`：253 通过（Phase 4 之后的 238 个 + Phase 5 新增 15 个
`tests/sinks/test_feishu_bitable.py`，mock 飞书 API：token 缓存/刷新、业务错误码重试、
建列幂等（含默认主字段改名 + `type` 必填回归）、新建/更新/跳过三种记录路径、
两个真实场景的幂等回归测试、`dry_run` 不调用 API、`sync_log` 写入校验）。

### 已知限制

- **只在 `data/test_daily_20260715.db` 上验收过**，未对 `data/competitor_intel.db`
  （含 Zoomex 全量基线 2018 条）或其它 Phase 2/3 产出的独立 db（如
  `data/run_20260715_bitunix_phemex_lbank.db`）跑过同步——不需要改代码，直接
  `--db-path` 指过去即可，只是本次任务范围没有要求。
- **两张飞书表里各观察到 5 行"空白"遗留记录**（没有任何字段值，含业务主键
  `uid`/`id` 都是空的），真实请求确认存在但推测是飞书新建表时的默认占位行（不是本次
  同步代码产生的——本模块的 create/update 路径从不会写出一个不含主键的空记录，
  `_index_existing_records()` 对这些行会因为拿不到 key 而直接跳过，不影响幂等判断），
  未做任何清理（删除用户飞书表里的行是有风险的写操作，不在授权范围内，如实记录留给
  用户自行决定是否手动清掉）。
- **`RATE_LIMIT_SLEEP_S=0.6` 是按"QPS 上限约 100/分钟"估的保守值，未做压测校准**：
  批量写入走 `batch_create`（不受这个节流影响），单条更新/建字段才会 sleep；本次
  2050+7 条里 8 条 update + ~37 个字段创建触发过这个节流，跑起来没有遇到真实的 QPS
  报错，但也没有专门测试"如果一次性有几千条都需要 update（不是 create）"这种全走单条
  PUT 的场景下节流是否足够。
- **飞书群机器人推送（`src/sinks/feishu_bot.py`）是 Phase 6 的事，本次未涉及**。

## Phase 7 完成情况：可视化看板（2026-07-15）

Phase 6（推送规则引擎）尚未实现，用户要求先跳过 Phase 6、直接做 Phase 7 可视化看板。
**本 session 全程未调用任何真实 LLM API**（不碰 `openai_http`，也不碰 `cursor_agent`），
是用户的明确要求；看板本身也未接入真实调度/GitHub Pages 配置（用户同样明确说了"先不用
执行具体的调度或者自动化的配置"），只保证"如果现在开启 GitHub Pages，这条链路能完整跑
起来"。

### 架构：静态导出 + 纯前端渲染，不是服务端 dashboard

- `src/dashboard/export_data.py`：`build_dashboard_data(db_path) -> dict` 一次性把
  给定 db 的当前状态压缩成一份 JSON（`export()` 落盘）。CLI：
  `python -m src.dashboard --db-path <db> --out docs/data/dashboard.json`。
- `docs/index.html`：单文件静态页面（无构建步骤、无 CDN 依赖、无第三方库），启动时
  `fetch('./data/dashboard.json')`，纯客户端渲染。这个切分是为了让"定时任务自动化访问
  数据并更新"这件事在以后接入时只有一步：跑一次 `python -m src.dashboard`，把
  `docs/data/dashboard.json` 提交/发布出去，`docs/index.html` 本身不需要动——它已经是
  完整可部署的 GitHub Pages 站点（仓库设置 Pages source = main /docs 即可，本 session
  未做这一步配置，如实记录）。
- 两者之间的契约就是这份 JSON 的 schema，`export_data.py` 顶部的 `COMPETITORS`/
  `BASELINE_LOCALES` 是唯一的"6 家交易所 × 各自 locale"硬编码来源，直接照抄 CLAUDE.md
  「竞品与语言范围」表，不是猜测值。

### "今天"怎么定义

不依赖调用方传参、也不依赖系统当前日期，而是取 `announcements` 表里（排除 Zoomex 基线）
`fetched_at` 出现过的最大日期（`_resolve_as_of_date`）。真实生产环境下，只要调度器一天
只跑一次，这个值天然就是"最近一次成功采集的那一天"；`overview`/`activity_ranking`/
`category_distribution`/`region_table` 全部统一用这个日期做 `date(fetched_at) = ?` 过滤，
是同一套口径，不是给不同模块各自定义"今天"。

### Demo 数据从哪来（如实记录，不是编造）

`data/dashboard_demo.db`（gitignored，`data/*.db` 规则覆盖，需要复现时跑下面两个脚本
重新生成）由 `scripts/build_dashboard_demo_db.py` 合并三个各自独立的真实库：

- Bitunix（32 条真实当日样本）+ Zoomex（2018 条真实基线）+ 7 条真实 insights，原样取自
  `data/test_daily_20260715.db`。
- BingX（40 条，真实分类结果）取自 `data/competitor_intel.db`。
- Phemex（178 条）+ Lbank（1679 条，真实分类结果）取自
  `data/run_20260715_bitunix_phemex_lbank.db`。
- **Weex 不合并，0 条**——如实反映「Weex 路径问题」（见更早的 session 记录）导致采集
  暂停的真实现状，看板会显式标出这个源当前无数据（数据源状态卡片 + 缺口提示），不编造
  假数据掩盖过去。

唯一的非原样搬运：BingX/Phemex/Lbank 三个源的 `fetched_at` 原始值分散在 2026-07-14 和
2026-07-15 两个真实日期（不同 session 实际抓取的时刻），合并时只改写日期部分（保留原始
时分秒）到 2026-07-15，让它们在"今天"的窗口判断里对齐成同一天——`title`/`content`/
`category`/`post_time` 等业务字段完全不动。生产环境不需要这一步，真实的每日调度会让
`fetched_at` 天然落在同一天。

`scripts/generate_mock_insights.py` 为 29 个缺失真实 LLM 分析的 (source, category,
locale) 组合生成结构合法但内容是模板拼接的**模拟 insights**（7 条真实 Bitunix insights
之外的全部）。诚实性设计：

- `prompt_version` 统一带 `-mock` 后缀，`llm_tokens_used` 恒为 `-1`（哨兵值，跟真实批次
  的 `>=0` 或 EN 复用批次的 `0` 区分开）——`export_data.py` 的 `InsightRef.is_mock` 就是
  读这个哨兵值判断的，前端每一处引用到 mock 批次的地方都会渲染一个「模拟」角标
  （`mockBadge()`），不会跟真实分析结果混在一起不做区分。
- `articles_analysis` 里的结构化字段（`token_symbol`/`market_type` 等）用正则/关键词从
  真实标题提取，提取不到一律留 `null`，照抄 `prompts.py` 本身"提取不到填 null，禁止
  编造"的规则，不因为是 mock 就放宽。
- `zmx_diff`/`diff_type`/`priority` 是没有真实检索作为依据的模拟判断（`zmx_evidence_uids`
  恒为空数组），文案里明确写"模拟数据，非真实比对结果"，不编造具体的 `[Z1]`/`[Z2]`
  证据编号。

### 区域 tab vs 全量 tab：为什么拆成两个入口

第一版实现里，区域 tab（EN/FR/VN/ID/EN-Asia）直接把"今天"全部数据塞进 CEX 表格，用户
反馈某些 tab 因此变成几百上千行的历史堆砌，不像"最新一批"。根因：BingX/Phemex/Lbank 是
`full_scan` 策略（见「水位逻辑策略调整」），这次 demo 合并的是它们各自"第一次跑满整个
`max_pages` 窗口"的真实产出（Lbank 一次就有 1679 条），跟 Bitunix 那种真正的小增量在
体量上完全不是一个量级，混在一起展示会让人误以为是历史堆积而不是当天新增。

拆分方案（`export_data.py`）：

- `CEX_ROWS_PER_CATEGORY = 15`——区域 tab 的"最新动态"表格只显示每个类目最新 15 条
  （按 `post_time` 降序），但 `cex_counts` 仍然是真实总数（单独一次 `COUNT(*)` 查询，不
  是展示列表的长度），表格下方有"本类目共 N 条，当前展示最新 15 条"+"在全量中查看全部
  →"跳转按钮。
- 新增 `archive`（`build_full_archive`）：全部 1929 行非基线数据（含 `source`/`locale`/
  `category`/`title`/`date`/`status`/`diff_tag`）一次性下发（`docs/data/dashboard.json`
  约 700KB indent=2 格式，静态站点场景下可接受），前端"全量"tab 在这份扁平数组上做
  region/时间范围（近 30/90/365 天，预设基于真实 `post_time` 跨度 2020-09-07 ~
  2026-07-15，不是编的）/来源/分类筛选 + 标题搜索 + 分页（50/页），不需要为每种筛选
  组合单独查库。

### 分类分析简报 + 每日 Summary：内容层级怎么分

用户看完第一版后指出两个问题，均已修复：

1. **"分类分析"卡片曾经把 `batch_summary` 和 `zmx_diff` 拆成两个视觉上独立的框**，读起来
   像是重复内容。修复：合并成一段连续文本（`[b.summary, b.zmx_diff].filter(Boolean).
   join('\n\n')`），不再有单独的 zmx_diff 边框/背景色；超过 140 字符时默认用
   `-webkit-line-clamp` 折叠到 2 行 + "展开全文 ▾"按钮——这是纯 CSS/交互层面的改动，不
   截断或改写任何真实文本内容。
2. **"今日 Summary"应该是把当天所有 insight 喂给 LLM 生成的综合结论，不是单纯的指标
   统计**，但本 session 明确不能真的调用 LLM。拆成"实现机制"和"是否执行"两件事：
   - `src/analysis/prompts.py` 新增 `daily-digest-v1`（`SYSTEM_DAILY_DIGEST` +
     `USER_DAILY_DIGEST_TEMPLATE` + `build_daily_digest_prompt()`），跟已有四套
     category prompt 不是同一个调用粒度——这套的输入不是公告原文，是"当天这个 locale
     已经产出的全部批次分析结果"（`batch_summary` + `zmx_diff`），任务是综合归纳出一段
     跨类目/跨来源的当日简报，不是重新分析公告。
   - `src/analysis/daily_digest.py`（新模块，不并入 `run.py` 的批次循环，不改动 Phase 4
     已经跑通、有 253 个测试兜底的批次逻辑）：`generate_daily_digest(conn, locale,
     batch_date, dry_run=True|False)`。`dry_run=True`（默认）只构建 prompt + cache_key，
     不查缓存、不发请求；`dry_run=False` 先查 `llm_cache`（`compute_digest_cache_key()`
     跟四套 category prompt 的 `compute_cache_key()` 同样的设计思路：key 只跟"当天这些
     批次的 id 集合"有关，不跟查询返回顺序有关），未命中才真正调用（需要传
     `credentials`）。复用 Phase 4 已有的 `llm_cache` 表，**没有新增 schema**。
   - `export_data.py` 的 `build_daily_digest()` 调用只读的 `peek_cached_digest()`（从不
     触发 LLM 调用——看板导出是静态快照生成，不应该在这个过程里发起网络请求），命中真实
     缓存就用 LLM 结果（`source: "llm"`），否则回退到原来的规则聚合文案
     （`_build_stats_digest_fallback`，纯统计口径的鸟瞰视角，不是"当日 insight"）。前端
     `digest-hero` 用一个角标显式区分这两种来源（"LLM 生成" vs "占位符 · 待接入 LLM
     生成"），不会把占位符文案冒充成真实分析结论。
   - 本次没有任何 `llm_cache` 命中（从未真正调用过），所以 5 个 locale 的"今日 Summary"
     全部落在 stats_fallback 分支——机制已经接好，等生产环境真的跑一次
     `generate_daily_digest(..., dry_run=False)` 并写入缓存，看板会自动改用真实 LLM 结果，
     不需要再改任何前端/导出层代码。
   - `tests/analysis/test_daily_digest.py`：7 个离线用例（`dry_run` 不查缓存/不调用、无
     批次时的行为、cache_key 对行序无关/对批次集合变化敏感、缓存命中路径的响应校验、
     无效 JSON 缓存返回未生成、非 dry_run 且缓存未命中但没传 credentials 时报错）。

### 视觉设计

用户点名要求参照 `nextlevelbuilder/ui-ux-pro-max-skill`（一个基于 GitHub 的外部 AI 设计
生成工具，161 条行业规则 + 192 套配色 + 84 种 UI 风格的检索式推荐系统），但这个 skill
**没有安装在本环境**（不是本地可加载的 skill 文件，是一个需要独立检索/推理能力的外部
系统，无法直接调用）——如实告知用户后，改为 `WebFetch` 它的 GitHub 说明页拿到可执行的
设计原则（4.5:1 最低对比度、200-300ms 过渡、375/768/1024/1440 响应式断点、避免"AI
紫粉渐变"套路、cursor-pointer + hover/focus 状态），手工套用到暗色主题重设计上，不是照抄
该 skill 的输出（它本身也生成不了，无法调用）。

配色沿用 `dataviz` skill 的方法论（`references/palette.md` 的 8 色分类色板，按固定顺序
取用不循环：`campaign`=blue、`product`=aqua、`listing`=yellow、`delisting`=red，
`other` 用中性灰不占用分类色槽；`diff_type` 走独立的 status 色板语义
`good`/`warning`/`serious`/`critical`，不复用分类色，避免"类目颜色"和"差异状态颜色"
两个不同语义轴互相串色）。**未能用 `scripts/validate_palette.js` 跑自动校验**——本机
环境没有 `node`/`deno`/`bun`，只能凭色板本身在 `palette.md` 里记录的相邻 ΔE 数据做手工
判断（连续 4 个分类色槽本来就是已验证序列的子集，理论上安全），这是本次相对 `dataviz`
skill 标准流程的一个已知缺口，如实记录。

### 已知限制 / 未做（如实记录，留给以后）

- **未接入任何真实调度**：`scripts/run_daily.sh`（Phase 8）不存在，`docs/` 也没有配置
  GitHub Pages（仓库 Settings → Pages）。真正启用只需要两步：① 仓库设置里把 Pages
  source 指向 `main` 分支的 `/docs` 目录；② 找一个调度机制（GitHub Actions cron 或外部
  定时任务）在每次采集/分析跑完后执行 `python -m src.dashboard --db-path
  data/competitor_intel.db --out docs/data/dashboard.json` 并 commit/push——两步都不在
  本次任务范围内，用户明确要求先不做。
- **`data/dashboard_demo.db` 是一次性合并的静态快照，不是持续更新的库**：如果以后想用
  真实的 `data/competitor_intel.db`（或任何单一真实库）跑这套看板，直接
  `python -m src.dashboard --db-path data/competitor_intel.db` 即可，不需要改代码——
  但当前 `competitor_intel.db` 里 Bitunix/Weex 数据已被清空（见更早 session 的"数据库
  清理"记录），看起来会比 demo 单薄，是否需要重新采集是数据层面的事，不是看板代码的事。
- **`推送候选`是按 `config/push_rules.yaml` 真实规则做的预览计算，不是 Phase 6 引擎**：
  `_push_candidate()` 里 `changed_rules_or_reward` 规则本该判断
  `diff_touches_rules_or_reward`（Phase 4 分析结果的派生字段，语义是"这次 changed 是否
  真的涉及规则或奖励"），但看板导出层没有真实的语义判断依据，退化成"campaign 类目下
  status=changed 就算命中"这个粗略近似，比真实规则宽松，数字仅供参考，前端已经标注
  "按规则预览 · Phase 6 未上线"。
- **`region_table`/`is_region_exclusive` 直接读 `announcements` 表已有的值**，Phemex 的
  地区独占标记此前已有 session 记录过"可能是假阳性（EN/FR 翻页窗口不重叠导致的误判）"
  的已知疑虑（见「daily 增量分页上限 5→2」一节），看板本身没有重新核实，原样展示。
- **`archive` 一次性下发全部 1929 行**（约 700KB JSON）：当前量级下静态站点场景可以接受，
  如果以后真实库积累到几万/十万行，需要考虑分页导出或者前端改成按需 fetch，不是这次
  设计要解决的问题。
- **没有自动化测试覆盖 `docs/index.html` 本身**（纯前端渲染逻辑，没有 JS 单测/e2e，
  只用 Artifact 预览 + `python -m http.server` 手工验证过）——`src/dashboard/
  export_data.py` 的输出数据本身有真实 export 跑通验证，但"JSON 格式对不对、前端渲染
  会不会崩"这一段目前只能靠人工点一遍，Phase 8 如果要接自动化部署，建议补一个"导出后
  用无头浏览器截图检查有没有 JS 报错"之类的最低限度冒烟检查。

## Phase 7 之后：飞书群截图推送（2026-07-15）

用户已自行把 Phase 7 的提交推到了 GitHub（本 session 不再碰 `git push`），接着要求
把「GitHub Pages」和「群推送」这两条链路搭起来。**这不是原计划 Phase 6（推送规则
引擎，逐条公告按 `push_rules.yaml` 匹配、发文字消息）**，是一条新的、更简单的路径：
每个区域 tab（EN/FR/VN/ID/EN-Asia）截一张当前渲染的完整截图，推到该 locale 在
`config/push_targets.yaml` 里配置的独立飞书群；「全量」「全局视角」两个 tab 不推送
（业务决定，这两个是给人主动去浏览的工具）。`push_rules.yaml` 规划的逐条规则引擎
如果以后要做，是独立于本节的另一条路径，不依赖这里新增的模块。

### GitHub Pages：未启用，需要用户手动做一次

没有 `gh` CLI、环境里也没有 `GITHUB_TOKEN`，无法用 API 自动开启。需要用户自己在仓库
Settings → Pages 里选 Source = Deploy from a branch，Branch = `main`，Folder =
`/docs`，保存即可（一次性，几秒钟）。`docs/index.html` 本身已经是完整可部署站点，
`docs/data/dashboard.json` 也已经提交，理论上开启后立刻就能看到当前（demo）数据。

### 架构：截图（Playwright）+ 图片上传（飞书 App API）+ webhook 推送

- `src/dashboard/screenshot.py`：`capture_locale_tabs(url, locales, out_dir)`。单个
  浏览器实例、单次页面加载（`dashboard.json` 只 fetch 一次），依次点击每个 locale
  tab 截全页图——比给每个 locale 各开一次浏览器快得多，也更接近真实用户点 tab 切换
  的行为。某个 locale 截图失败（选择器找不到等）只记警告、跳过，不影响其它 locale。
  真实验收：对本地 `python -m http.server` 跑通，5 个 locale 全部产出非空 PNG（EN
  934KB / FR 602KB / VN 682KB / ID 618KB / EN-Asia 94KB，`full_page=True`，EN 那张
  实测 1180×3742px——是"该 tab 的完整内容"，不是只截可视区域，符合"推送内容是该 tab
  下的截图内容"这个要求，但确实很长，chat 里查看体验如何未做进一步验证）。
- `src/sinks/feishu_bot.py`：飞书自定义机器人 webhook **本身不能直接带图片二进制**
  ——这是飞书协议的限制，不是设计选择：必须先用 App 凭证（`FEISHU_APP_ID/SECRET`，
  跟 `feishu_bitable.py` 用的是同一对，但权限范围不同，见下方真实验收）把图片传到
  `im/v1/images` 换一个 `image_key`，再拿这个 `image_key` 发到 webhook
  （`msg_type: image`）。`push_dashboard_screenshots(dashboard_url, db_path,
  dry_run=True)` 是编排入口，`dry_run=True`（默认）只截图、不调用任何飞书 API，安全
  默认值。`config/push_targets.yaml` 的 `${WEBHOOK_EN}` 占位符替换成 `.env` 里的
  `WEBHOOK_*` 真实值（目前全部为空——`.env` 里还没有任何一个 locale 配置真实 webhook，
  见下方已知限制），某个 locale 没配 webhook 就跳过（`skipped`），不是报错。
- 审计轨迹复用已有的 `sync_log` 表（CLAUDE.md schema 早就把 `bot_EN/bot_FR/bot_VN/
  bot_ID/bot_EN-Asia` 列进 `target` 的取值范围），`record_id` 用 `{locale}_{batch_date}`
  （原设计是"uid 或 insight_id"，这里没有天然的单行主键，是对这张表语义的合理延伸，
  不是滥用）。`push_status`（announcements 表那一列）完全不碰——它的语义是"这条
  公告有没有被单独推送过"，跟"今天有没有把整个 locale tab 的截图发过群"是两回事。

### 真实网络验收记录（2026-07-15，用真实 FEISHU_APP_ID/SECRET）

```
python -m src.sinks.feishu_bot --dashboard-url http://localhost:8731/index.html \
  --db-path data/dashboard_demo.db --dry-run
# 5 个 locale 全部截图成功；5 个都因为 .env 里没配 WEBHOOK_* 被跳过（skipped=5）
```

单独真实调用 `upload_image()`（不经过 webhook，只测图片上传这一步）：

```
{"code":234007,"msg":"App does not enable bot feature."}
```

**这是一个真实的、已确认的阻塞点，不是代码 bug**：multipart 请求格式本身是对的
（拿到的是飞书业务层 JSON 错误，不是"请求格式不对"那种传输层错误），但
`FEISHU_APP_ID` 对应的应用还没有在飞书开发者后台开通"机器人"能力——这跟 Phase 5
用的 Bitable 读写权限是两个独立的权限范围，不会因为 Bitable 那边能用就自动开通。
真正推送前需要用户去飞书开发者后台给这个应用开通机器人能力，本 session 没有权限
代为操作。

调试过程中发现并修了一个真实 bug：`get_tenant_access_token()` 最初照抄
`feishu_bitable.py` 用 `fetch_json()`（已解析好的 dict），但模块其它地方（图片
上传要发 multipart，不是 JSON）统一用原始的 `fetch()` 自己 `json.loads()`——两套
HTTP 调用混用，直接后果是单测只 mock 了 `fetch` 时，token 获取那一步会真的绕过
mock 打到 Feishu 真实 API（用假测试凭证 `"app-upload-1"/"s"` 请求，收到
`{"code":10003,"msg":"invalid param"}` 的真实响应，暴露了这个隐患）。修复：统一
成 `fetch()` + 手动 `json.loads()`，整个模块只有一个 HTTP 入口点需要 mock。

### 已知限制

- **`.env` 里没有任何一个 `WEBHOOK_*` 真实值**：`config/push_targets.yaml` 的
  5 个 locale 全部因为"未配置 webhook"被跳过，端到端的真实推送（图片真的发到群里）
  完全没有验证过——需要用户提供真实的飞书群自定义机器人 webhook URL，写进 `.env`。
- **`FEISHU_APP_ID` 对应的飞书应用未开通"机器人"能力**（见上方验收记录），这是
  `upload_image()` 目前唯一验证过的真实阻塞点，需要用户在飞书开发者后台手动开通。
- **截图是整页长图**（EN 实测 3742px 高），没有验证过在真实飞书群聊天窗口里的
  实际查看体验（是否需要点开才能看清、是否应该改成分段/摘要形式），如果用户反馈
  体验不好，需要回来调整 `full_page=True` 这个选择（比如改成只截可视区域，或者
  把 `docs/index.html` 加一个"推送专用精简视图"）。
- **没有接入任何调度**：`src/sinks/feishu_bot.py` 目前只能手动跑
  `python -m src.sinks.feishu_bot --dashboard-url <url> --execute`。是否要做成
  GitHub Actions workflow（每天定时跑 export → 截图 → 推送三步）留给用户决定，
  本次没有主动创建 CI/CD 配置（改动 CI/CD 是需要用户确认的高风险操作）。
- **`push_dashboard_screenshots` 的 `--dashboard-url` 目前必须指向一个已经能访问
  到的 URL**（本地 http.server 或已经开了 Pages 之后的线上地址），CLI 本身不会
  自动帮你起本地服务器或者检测 Pages 是否已经生效。

## `--lookback-days` 修复：daily 增量退化成全量回填的根因与修复（2026-07-15）

用户反馈：多次要求"清空非 Zoomex 数据、按每日调度的逻辑跑一次采集"，但每次拿到的都是
几百/几千条"new"，不是真正意义上的"今天的增量"，怀疑代码有问题。排查确认**是真实的代码
缺口，不是操作问题**，两类根因：

1. **Bitunix（watermark 策略）**：`base.py::run()` 里 `since = state["high_watermark"] if
   state else None`。空库（或 crawl_state 被清空）时 `since=None`；`zendesk_base.py::
   fetch_list()` 的早停判断是 `if since is not None and update_time <= since: stop`，
   `since=None` 时这个条件永远为 `False`，翻页永远不会早停，直接翻到 Zendesk 没有更多
   游标为止——**等价于一次全量历史回填**。这一点其实在 `zendesk_base.py` 顶部注释里早就
   写明了（"since=None 时代表首次全量抓取"），只是从未被当成"需要修的问题"看待。之前
   「Bitunix 当日数据试运行」一节记录的 14 条，靠的是手动 `set_crawl_state` 把水位线
   预置成当天 00:00 UTC——是一次性 SQL hack，不可复用，每次清库重跑都要重新手动做一遍。
2. **Weex/BingX/Phemex/Lbank（full_scan 策略）**：这四个源完全不做任何日期过滤，
   `fetch_list()` 的 `since` 参数被直接忽略，只靠 `pagination.max_pages`（当前=2）×
   `page_size` 圈一个固定的"页数窗口"，不管这个窗口对应的时间跨度是"今天"还是"过去两年"
   （取决于该分类的发布频率）。空库时 Lbank 一次最多 `50×2×7分类×3locale=2100` 条、
   Weex 最多约 780 条——这才是"几百几千条"真正的量级来源。

### 修复：`src/collectors/base.py` + `src/collectors/__main__.py` 新增 `--lookback-days`

- `RunStats` 新增 `skipped_by_date: int` 字段（因日期过滤被丢弃的条目数，跑批输出里可见，
  不是静默丢弃）。
- `BaseCollector.run()` 新增 `lookback_days: Optional[int] = None` 关键字参数：
  - 计算 `cutoff = (now_utc - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")`
    （`force_full=True` 时完全不生效，跟"全量核查不该被日期窗口限制"的既有语义一致）。
  - watermark 策略：`since is None`（crawl_state 为空）时用 `cutoff` 播种，不再退化成
    `None`。副作用是好的：如果这一轮窗口内一条真实新内容都没有，`max_update_time` 仍会
    是这个 `cutoff`，写回 `crawl_state.high_watermark`——等于**顺带把水位线正式建立起来**，
    不需要额外一步手动 SQL，下次不带 `--lookback-days` 的正常增量运行也不会退化。
  - full_scan 策略：`fetch_list()` 拿到条目后，按 `update_time or post_time >= cutoff`
    过滤，丢弃的计入 `stats.skipped_by_date`。`fetch_list()` 本身不用改（各源已经在
    `RawItem` 阶段就把时间字段转成 UTC ISO8601 字符串，是 Phase 2 批次 2 定下的既有
    契约），过滤逻辑完全在 `base.py` 里通用实现，不需要碰任何交易所专属代码
    （`zoomex.py`/`weex.py`/`bingx.py`/`phemex.py`/`lbank.py` 一行未改）。
  - Zoomex 的 `strategy` 字段虽然记成 `"watermark"`，但 `fetch_list()` 本来就忽略
    `since`（见 `zoomex.py` 顶部注释），播种 `since=cutoff` 对它是无害的 no-op。
- CLI 新增 `--lookback-days N`（默认 `None`，不传完全保留现状——历史上 Zoomex 全量建仓
  等用法不受影响），输出表格新增 `skipped_by_date` 列。
- 新增 `tests/collectors/test_lookback.py`（6 个用例，两个最小 fake collector，不依赖任何
  真实交易所逻辑）：watermark 空水位线播种 cutoff / full_scan 按日期过滤并计数 /
  `force_full` 让两者都失效 / 不传 `lookback_days` 时行为跟修改前逐字节一致（回归安全网）。
  `python -m pytest` 全量 280 通过，无回归。

### 真实验证：清空非 Zoomex 数据后按 `--lookback-days 1` 跑一遍（2026-07-15）

`data/competitor_intel.db` 备份为 `data/competitor_intel.db.bak_20260715_164319` 后，
`DELETE FROM announcements/crawl_state WHERE source != 'Zoomex'`（`content_history`/
`insights`/`sync_log`/`llm_cache` 当时都已经是空表，无需处理）。按用户确认（本轮跳过
Weex——路径问题仍未查清；Phase 4/可视化验收本轮不做，等数据量确认后再推进）串行跑：

```
python -m src.collectors --source bitunix --lookback-days 1
# EN new=10  FR new=7  ID new=9  （合计 26，skipped_by_date=0，watermark 早停机制生效）
python -m src.collectors --source bingx --lookback-days 1
# EN new=8 skipped_by_date=12  VN new=8 skipped_by_date=12  （合计 16）
python -m src.collectors --source phemex --lookback-days 1
# EN/news new=9 skipped=31，EN/activities new=0 skipped=40，EN/newsletter new=0 skipped=13
# FR 同上（news=9 skipped=31，其余 2 个分类 new=0）（合计 18）
python -m src.collectors --source lbank --lookback-days 1
# 7 分类 × 3 locale，每个 locale：new_listings=7 system_maintenance=5
# delisting_information=1，其余 4 个分类当天 0 条（合计 39，skipped_by_date 单个分类
# 最高 100，即命中 max_pages=2×page_size=50 上限但当天真实新内容为 0）
```

**总计 99 条**（Bitunix 26 + BingX 16 + Phemex 18 + Lbank 39），相比之前动辄几千条的
量级，符合"daily 增量"的真实预期。`Bitunix/{EN,FR,ID}` 的 `crawl_state.high_watermark`
被正式播种为 `2026-07-15T08:08:42Z`，往后不带 `--lookback-days` 的正常增量运行也会
自然维持小量级（早停机制现在真的有一个非空的 `since` 可用）。full_scan 四源的
`skipped_by_date` 证实了诊断：BingX/Phemex/Lbank 每次仍会请求同样大小的页数窗口
（网络成本不变，`max_pages` 仍是保护网络请求量的机制），但过滤后写入 DB 的"new"数量
只反映真实的当天新内容，不再被历史存量污染。

Pipeline（`classify --apply` / `region`，`--sources Bitunix,BingX,Phemex,Lbank`）：

```
classify: native=61 (61.6%) / native_other=20 (20.2%) / keyword=18 (18.2%) /
          unmapped_native=0 / llm_pending=0 —— 全部 99 行成功落 category，0 行需要
          将来的 LLM 兜底层
region: Phemex 有 9 行判定为地区独占（全部 FR）
```

**`group-check` 第一次跑漏了新源，已订正**：`python -m src.pipeline group-check`
不传 `--sources` 时默认只检查 `("Bitunix", "Weex", "Zoomex")`（`src/pipeline/
__main__.py` `cmd_group_check` 的默认值），第一次验收时忘了显式传 `--sources`，
实际只查了 846（Zoomex）+10（Bitunix）=856 个 group，BingX/Phemex/Lbank 这次新采
的 39 个 group 完全没被扫描到。用 `--sources Bitunix,BingX,Phemex,Lbank,Zoomex`
重新跑：`检查了 895 个 group，0 异常`（895 = 846 Zoomex + 10 Bitunix + 8 BingX +
18 Phemex + 13 Lbank，逐一对上 `SELECT source, COUNT(DISTINCT group_id) ...` 的
真实查询结果）。教训：这几个 pipeline 子命令的 `--sources` 默认值只覆盖历史上
最早实现的源，接入新源后跑 pipeline 命令必须显式传 `--sources`，不能依赖默认值。

category 分布：`BingX` delisting 4/listing 10/other 2；`Bitunix` campaign 1/
delisting 3/listing 12/other 3/product 7；`Lbank` delisting 3/listing 21/other 15；
`Phemex` 全部 18 行 delisting（EN/FR 各 9 条 "Will Delist the X" 标题，真实核对过
不是分类 bug，见下方已知限制）。

### 已知限制（如实记录，未处理）

- **Phemex 的"地区独占"9 行是已记录过的假阳性，不是这次新引入的问题**：EN/FR 同一篇
  下架公告（如 "KORUUSDT Futures"）在两个 locale 下 `article_id` 不同（EN=135479 vs
  FR=135481），`group_id` 是按 `article_id` 拼的，所以 EN/FR 版本被分进了两个不同的
  group，各自看起来像"只在一个 locale 出现"，被 `region` 误判成独占。这个疑虑在
  「daily 增量分页上限 5→2」一节就已经记录过，本次真实数据（EN/FR 标题逐字对应）
  进一步坐实了这个怀疑，但修复方式（可能需要按标题/内容相似度而不是 `article_id`
  做 Phemex 的跨语言归组）超出本次任务范围，未处理。
- **Weex 本轮跳过**：路径问题仍未查清（见「Weex 路径问题」一节），`announcements`
  里 Weex 仍是 0 行。
- **full_scan 四源的网络请求量没有减少**：`--lookback-days` 是落库前的过滤，不是
  提前退出翻页的优化——`max_pages` 窗口该请求多少页还是请求多少页（这四个源的
  列表排序可靠性没有被验证到能安全做早停，见各自 collector 顶部注释），`--lookback-
  days` 只解决"存进 DB 的数据量"，不解决"网络请求量"，两者当前解耦。
- **Phase 4（LLM 分析）与 Phase 7 可视化验收本轮未做**：按用户要求，本轮只做到
  Phase 3（采集 + pipeline），跑完把量级报给用户确认后再推进，不在本节涉及范围。

## Zoomex 基线改造：TF-IDF 检索 → 结构化 zmx_baseline 表

### 背景

Phase 4 原来的 ZMX 基线检索（`src/analysis/zmx_index.py`）是纯 Python TF-IDF：把
当天批次全部标题拼成一个 query，对 Zoomex 近 90 天同 category×locale 的公告做词面
检索，取 Top 5。用户指出这个设计有实际质量问题：一个批次如果同时混了入金活动/
交易赛/新手任务/邀请返佣等多种竞品公告，四类标题合并成一个 query 检索出的 Top 5
未必能覆盖全部类型，LLM 只看到局部 Zoomex 供给，容易在 `zmx_comparison` 里把
"检索没命中"误判成"Zoomex 没有"；英文词面检索对同义玩法（不同措辞但同一种机制）
识别也弱；喂给 LLM 的是原始正文摘要，不是结构化玩法信息，比较判断可信度低。

改造方向：不再检索原文，改成维护一张结构化的 `zmx_baseline` 表（每条 Zoomex 公告
→ mechanism_type/key_mechanics/reward_range/target_users/start_date/end_date），
每个批次按 category×locale 注入**全量类型覆盖**的结构化基线，取代"几条原文摘要"。
目标是同时提升分析质量（覆盖完整玩法类型）和降低单次调用 token（结构化字段远比
原文摘要短）。

### 已确认的设计取舍

1. **mechanism_type 用 LLM 自由生成标签，不是固定枚举**——接受"同类活动可能被起
   不同名字"的碎片化风险，提取 prompt 里把"这个 category×locale 下已经用过的标签"
   作为提示传入，要求语义相同时复用已有标签、确实是新玩法才新建，是轻量缓解，不是
   强制归一化的解决方案。
2. **提取是独立维护步骤**：新增 `python -m src.analysis.zmx_baseline` 命令，跟
   `python -m src.analysis`（竞品批次分析）完全解耦——后者只读已经提取好的
   `zmx_baseline` 表，运行时不现场触发任何提取逻辑，成本可预期、可控制。
3. **只处理近 90 天窗口，不做全量 2018+ 条历史回填**，且这是结构性约束不是默认
   参数：`list_pending_zoomex_rows()` 的 SQL 里 `post_time >= cutoff` 过滤没有绕过
   开关，任何一次运行都不可能把窗口外的 Zoomex 记录发给 LLM。
4. **真实执行受 $/token 双重硬预算控制**：用户报价 $0.03/15k tokens（≈ $0.002/1k
   tokens），要求本次真实提取总花费不超过 $10、累计 token 不超过 500 万，两者是
   同一个约束的两种表达（10 / 0.002 * 1000 = 5,000,000），双重检查是防止价格换算率
   本身配错时仍有 token 硬上限兜底，不是两个独立预算，任一触发即熔断、跳过剩余
   批次（已产出的结果保留，下次重跑续跑，不算失败）。

### 架构

- **Schema**：新增 `zmx_baseline` 表（`src/db/schema.sql`），纯新增不改动任何既有
  表的列/约束，不需要走 `scripts/migrate_v*.py` 那套建新表搬数据的流程——`init_db()`
  是全量 `CREATE TABLE IF NOT EXISTS` 的 `executescript`，已存在的开发库直接重跑
  `python -m src.db init` 即可拿到这张新表。`source_uid` 是主键（FK →
  `announcements.uid`，`ON DELETE CASCADE`），`category` CHECK 约束只允许
  `campaign/product/listing`——delisting 不建基线，跟 `prompts.py` 的 delisting
  模板一致（本来就没有 `zmx_comparison` 部分）。`content_hash` 冗余存一份提取时
  对应的 `announcements.content_hash`，用于增量判断（Zoomex 公告被编辑后能重新
  提取），不冗余存 `post_time`（查询时 JOIN `announcements` 拿，遵守"SQLite 里
  announcements 是唯一真相源"的既有原则）。
- **`src/analysis/zmx_baseline.py`**（新模块，提取 + 查询两部分）：
  - **提取**：`list_pending_zoomex_rows()` 找近 90 天窗口内、未提取或
    `content_hash` 变化的 Zoomex 公告（跟 Zoomex collector 的 `needs_detail()` 是
    同一类增量判断思路）；`run_extraction()` 按 `batch_size`（默认 15）打包成批，
    调用 LLM 结构化提取（复用 `src/analysis/llm.py` 的 `call_llm`/
    `compute_cache_key`/`get_cached_response`/`set_cached_response`——提取响应
    缓存复用同一张 `llm_cache` 表，`strip_code_fences` 从 `llm.py` 私有函数提升为
    公开函数给这里复用；也支持 `cursor_agent` provider），`upsert_baseline_rows()`
    按 `source_uid` 幂等写入。**成本/token 双重熔断**：`max_calls_per_run`/
    `max_cost_usd_per_run`/`max_tokens_per_run` 任一触发就跳过剩余批次（计入
    `ExtractionReport.skipped_budget_cap`）；**每处理完一个批次就 `conn.commit()`**
    （不等整个提取跑完才统一 commit）——这是熔断/任何意外中断都不丢失已产出结果的
    安全网，`run.py`（竞品批次分析）不受此影响，沿用原有的 `main()` 末尾统一
    commit。CLI：`python -m src.analysis.zmx_baseline [--locale] [--category]
    [--lookback-days] [--batch-size] [--provider] [--max-calls] [--max-cost-usd]
    [--max-tokens] [--dry-run]`，不传 `--locale`/`--category` 时遍历 Zoomex 全部
    已有数据的 locale×category 组合（跟 `python -m src.collectors --source zoomex`
    的既有约定一致）。
  - **查询**：`get_baseline_digest(conn, category, locale, lookback_days,
    max_entries, max_examples_per_type)` 取代 `zmx_index.build_index()`+`search()`。
    **类型覆盖优先于单类型深度**：按 `mechanism_type` 分组，第一轮每个类型取最新
    1 条（保证 `max_entries` 预算允许范围内类型全覆盖，直接解决"多类活动混在一个
    批次、检索结果覆盖不全"这个核心问题），第二轮预算有余量时按类型轮询补充
    （最多到 `max_examples_per_type` 条）。返回 `ZmxBaselineEntry` dataclass 列表
    （字段名保留 `uid` 而不是 `source_uid`，因为 `llm.py` 的
    `validate_and_normalize()` 用 `zmx_hits[i-1].uid` 映射 `evidence_indices`，
    保持这个调用点不用改）。
  - **循环 import 的规避**：`llm.py`/`prompts.py` 都需要 `ZmxBaselineEntry` 做类型
    注解，但 `zmx_baseline.py` 反过来要 import 这两个模块的函数（`call_llm`/
    `strip_code_fences`/`build_extraction_prompt` 等）——三个模块都已有
    `from __future__ import annotations`，用 `TYPE_CHECKING` 守卫导入解决（注解在
    运行时是字符串，不需要真的把类拿到），不是把 `ZmxBaselineEntry` 挪到独立文件
    这种更大的结构调整。
- **`prompts.py`**：新增 `SYSTEM_ZMX_EXTRACT`/`USER_ZMX_EXTRACT_TEMPLATE`/
  `build_extraction_prompt()`——三个 category（campaign/product/listing）共用同一套
  提取模板（字段形状相同，category 只是上下文变量，不需要三份重复模板）。
  `build_zmx_block()` 改造成渲染结构化字段（类型/机制/奖励/目标用户/时间窗口），
  不再是"UID | post_time | 标题 | 摘要"。三个竞品分析模板（campaign/product/
  listing）里 Zoomex 基线段落的说明文案改成"玩法类型总览"而不是"相关度最高的 N
  条"，如实反映"现在展示的是类型覆盖，不是检索相关度排序"这个变化。
  `build_zmx_note()`/`build_prompt()` 去掉了 `min_hits_for_full_confidence` 这个
  中间档位（原来是"命中数 < 3 条 → 提示置信度可能较低"，针对 TF-IDF"可能没搜全"
  设计的；结构化基线注入本身就是"尽力覆盖全部已知类型"的结果，不再有这层不确定性），
  简化成只保留"0 条基线 → 不适用"这一档。
- **`config/analysis.yaml`**：`zmx_index:` 段改名为 `zmx_baseline:`，去掉
  `top_k`/`min_hits_for_full_confidence`，新增 `max_entries_per_batch`/
  `max_examples_per_type`（查询端）和 `extraction.*`（提取端：`batch_size`/
  `prompt_version`/`response_max_tokens`/三个熔断上限/`price_usd_per_1k_tokens`）。
- **`run.py`**：`get_baseline_digest()` 取代 `build_index()`+`.search()` 那一段，
  不再需要拼 query_text（不是检索，是结构化拉取）。
- **删除 `src/analysis/zmx_index.py`**：TF-IDF 检索完全下线，不保留兼容层/
  fallback，跟项目一贯"确定不用就彻底删除"的做法一致。

### 验证记录（2026-07-15）

代码实现完成后先做了离线验证：`pytest` 272 通过（含删除 `zmx_index.py`
及其 24 个测试后的净变化，`test_run.py`/`test_prompts.py`/`test_llm.py` 里
`zmx_index.ZmxArticle` 已改成 `zmx_baseline.ZmxBaselineEntry`），另外手写了几个
一次性 smoke check（不是正式测试文件，`tests/analysis/test_zmx_baseline.py` 仍
待补）验证了三个关键行为：90 天窗口结构性排除（构造窗口内外混合数据，断言窗口外
不进候选集合）、`get_baseline_digest` 类型覆盖优先于深度（4 个 mechanism_type 各
若干条，`max_entries=3` 时仍返回 3 个不同类型而不是同类型的 3 条）、budget 熔断
+ 逐批 commit（token 上限触发后确认已完成批次的数据已经落盘、剩余批次被跳过）。
熔断检查点在"发起新调用前用当前累计值判断"，不是预测下一次调用的开销，所以最后
一次把预算推过线的调用会被允许完成——这是设计上的选择，不是 bug（真实预算量级
下单次调用的 token 占比很小，超额可忽略）。

### 真实执行记录（2026-07-15，`data/competitor_intel.db`，`--provider cursor_agent`）

`.env` 当时只配置了 `CURSOR_API_KEY`（`LLM_API_KEY`/`LLM_API_BASE` 仍为空），
真实执行显式传了 `--provider cursor_agent`。先 `python -m src.db init`（幂等）
给这个已存在 2018 条 Zoomex 历史数据的库补上 `zmx_baseline` 空表，不影响任何
既有数据。

```
python -m src.analysis.zmx_baseline --provider cursor_agent --max-cost-usd 10 --max-tokens 5000000
# 提取结果：extracted=226 cache_hits=0 llm_calls=23 validation_failed=0
#           total_tokens=532071 total_cost_usd=1.0641 skipped_budget_cap=0
```

23 次真实调用覆盖全部 13 个有数据的 (locale, category) 组合（EN/EN-Asia/FR/ID/VN
× campaign/product/listing，VN listing 因为窗口内条数太少这次没有出现在候选里）。
总花费 $1.06、53.2 万 token，远低于 $10/500 万 token 的双重预算，未触发熔断。
`extracted=226` 比 90 天窗口理论总数（232）少 6：5 条是已知的"纯图片公告、
`content` 恒为空字符串"（`list_pending_zoomex_rows` 结构性要求 `content != ''`，
见「Zoomex 全量建仓」一节记录过的 `article_id=4077` 那条 10-locale 空正文活动，
这次落在窗口内的分身恰好命中每个 locale 一次）+ 1 条是 LLM 返回了一个不在本批次
`related_uids` 内的 uid，被 `parse_extraction_response` 的防御性校验正确丢弃
（日志：`丢弃提取条目：uid=... 不在本批次内`，EN-Asia/campaign 那一批 14/15）——
这条公告的 `content_hash` 没有写进 `zmx_baseline`，下次重跑会自动重新尝试，不需要
手工处理。

**真实数据抽查（5 条随机样本）**：字段提取具体、可信，没有出现"信息不足"类占位符
滥用——如 `"🏆 World Cup Airdrop Carnival..."` 提取出
`key_mechanics="新用户注册首充、阶梯首充（需完成首充>50U、合约交易量>10000U及
KYC）及全员合约交易量达10万U等任务领取空投..."`、`reward_range="最高1500 USDT"`、
`start_date/end_date` 精确到日；listing 类目正确识别出杠杆倍数、交易对代码，
`reward_range`/`target_users` 在没有奖励信息的上币公告里正确留 `null`（没有编造）。

**真实数据暴露的问题：campaign 类目标签碎片化比预期严重**。`product`（11 个）/
`listing`（8 个）两个类目的 `mechanism_type` 收敛得不错，但 `campaign` 类目
~168 行产出了 **114 个不同标签**，其中能看到明显的近义碎片化，例如
"交易量阶梯"(4)/"交易量阶梯奖"(3)/"交易量阶梯赛"(2)/"交易量阶梯奖励"(1) 大概率
是同一种"按交易量分档给奖励"机制被起了 4 个不同名字。这是选择"LLM 自由生成标签"
而不是固定枚举时已经确认接受的风险（见上方"已确认的设计取舍" 1），提示词里"复用
已有标签"这句话对 campaign 类目的效果明显弱于 product/listing——一部分原因可能
是 campaign 本身营销活动的真实多样性确实很高（不完全是碎片化，也有真实的不同
玩法），但至少上面这组"交易量阶梯"系列看起来是可以合并的。**这会削弱
`get_baseline_digest` 类型覆盖优先设计的实际效果**：`max_entries_per_batch`
默认 20 的预算下，如果 campaign 类目实际有效类型远多于 20 个（还夹杂近义碎片
占用名额），"类型全覆盖"这个目标不会真正达成，只是比 TF-IDF Top 5 覆盖面更广。
是否需要一道标签归一化/合并的后处理（比如定期跑一次"相似标签聚类"人工或 LLM
辅助合并），本次未处理，留给用户决定优先级——当前实现是可用状态，只是 campaign
类目的类型覆盖收益打了折扣，不是功能性缺陷。

## 真实数据全流程验证：Phase 4 → 看板导出 → GitHub Pages → 群推送（2026-07-15）

用户要求对「daily 增量分页上限 5→2」一节记录的 99 条真实数据（Bitunix 26 +
BingX 16 + Phemex 18 + Lbank 39，均已完成 Phase 2/3）跑一遍从 Phase 4 开始的
完整流程，ZMX 基线使用上一节已经真实处理好的近 90 天结构化 `zmx_baseline`
（226 条），并要求顺带打通看板发布（GitHub Pages）和飞书群截图推送两条链路。

### Phase 4：真实调用 cursor_agent，22 个批次全部产出

`.env` 仍然只有 `CURSOR_API_KEY`（`LLM_API_KEY`/`LLM_API_BASE` 为空），显式传
`--provider cursor_agent`：

```
python -m src.analysis --db data/competitor_intel.db --source Bitunix,BingX,Phemex,Lbank --provider cursor_agent
# 分析批次数：analyzed=10 derived=12 cache_hits=1 llm_calls=9 skipped_call_cap=0
# validation_failed=0 total_tokens=173745
```

22 个批次（campaign/product/listing/delisting × 4 源 × 各自 locale，`other`
被正确排除）全部落库，无一失败。**一个值得记录的真实现象**：`Phemex/delisting/FR`
既没有走 EN 复用（`can_derive_from_en()` 判定失败，因为「daily 增量分页上限 5→2」
一节记录过的已知设计缺陷——Phemex 同一篇下架公告在 EN/FR 下 `article_id` 不同，
`group_id` 因此拼不到一起，FR 侧的 group 不被 EN 的 `related_uids` 覆盖），也没有
真正调用 LLM（`llm_tokens_used=0`），而是命中了 `llm_cache`——因为 Phemex 的下架
类通知本来就不做本地化（CLAUDE.md 早前已记录过这个真实观察：FR 端抓到的标题都是
纯英文原文），FR 批次的 9 篇文章 `content` 跟 EN 批次逐字节相同，`content_hash`
集合完全一致，缓存 key 天然撞车，直接省下了一次真实 LLM 调用。副作用：这次
`articles_analysis` 字段是空数组——因为缓存复用的响应里 `uid` 是 EN 批次的
uid，被 `validate_and_normalize()` 的越界校验正确丢弃（9 条 `WARNING 丢弃
articles 条目...不在本批次内`），但 `summary`/`zmx_diff`/`priority`/`diff_type`
这些不挂 uid 的字段原样保留，人工核对内容跟真实 9 篇下架公告一致，不是错误
结果，是"跨 locale 内容重复触发缓存命中，但逐条 uid 映射按设计被拒绝"的正确
行为。

`insights` 表 22 行全部写入，抽样人工核对（Bitunix/campaign、Lbank/listing 等）
summary/diff_type/priority 均准确对应源公告内容，Zoomex 结构化基线（`zmx_hits`
不再是 0，跟「独立 db 试运行」一节因为没有 Zoomex 数据而全部退化成"基线有限"
形成对比）被正确注入 prompt。

### Phase 7：用主库重新导出看板数据

```
python -m src.dashboard --db-path data/competitor_intel.db --out docs/data/dashboard.json
# insights: 22（模拟 0）；Bitunix/BingX/Phemex/Lbank 今日=累计（全新窗口首次导出）；
# Weex 今日 0 / 累计 0（路径问题仍未解决，如实显示无数据，不编造）
```

这次导出完全替换了此前 Phase 7 session 用 `dashboard_demo.db`（合并 demo 数据 +
29 条模拟 insights）产出的旧 `docs/data/dashboard.json`——数据量因此从演示用的
大规模历史堆积（Zoomex 2018 + Bitunix 32 + BingX 40 + Phemex 178 + Lbank 1679）
收缩成真实 daily 增量规模（99 条非基线数据），这是预期之内的结果（测的是真实
每日调度产出的量级，不是 demo 丰富度），不是回归。

装了 Playwright 的 Chromium（此前 session 从未真正装过浏览器二进制，这次是
第一次跑 `playwright install chromium` 成功）用本地 `http.server` 截了一遍
5 个 locale tab 做视觉核验，渲染正确（22 条 insights 分布、Weex 空态提示均正常
显示），确认导出数据结构和前端渲染没有问题后再提交。

**一个环境相关的真实坑，记录以防再遇到**：默认只读/受限 sandbox 下调用
`playwright.chromium.launch()` 会报 `Executable doesn't exist at .../chrome-
headless-shell-mac-x64/...`——但实际磁盘上装的是 `mac-arm64`（机器本身是
arm64，`platform.machine()` 也确认是 arm64）。只有加 `required_permissions:
["all"]`（完全禁用 sandbox）才能正常 launch，怀疑是 sandbox-exec 环境下 Node
驱动进程的架构探测在这个环境被干扰成了 x64。这是本地沙箱环境的兼容性问题，
不是 Playwright 或本项目代码的 bug，以后在这个环境里跑任何 Playwright 相关
命令（截图、GitHub Pages 相关自动化等）都需要 `required_permissions: ["all"]`。

`git add docs/data/dashboard.json && git commit && git push origin main`
（commit `325017d`）已推送到 `origin/main`。

### GitHub Pages：确认未启用，无法用现有凭证自动开启

用 `git credential fill` 拿到的凭证查询 `GET /repos/joinjaye/CompAgent/pages`
返回 404，确认 Pages 从未被启用过。尝试用同一个凭证 `POST` 创建 Pages 站点被
安全审查拦截（"把 push 用的凭证挪去改仓库设置"被判定为凭证用途外的敏感操作，
没有强行申请批准）——改为指导用户手动在 `https://github.com/joinjaye/
CompAgent/settings/pages` 里选 `Deploy from a branch` / `main` / `/docs` 保存，
这一步仍然需要用户自己动手，跟「Phase 7 之后：飞书群截图推送」一节记录的结论
一致，本次没有新进展。

### 飞书群推送：webhook 已配置齐全，但仍卡在同一个应用权限问题

跟 Phase 7 之后那次验收时不同，`.env` 里 `WEBHOOK_EN/FR/VN/ID/EN_ASIA` 这次
**已经全部配置好了**（当时全部为空）。`--dry-run` 显示 5 个 locale 全部会正常
截图+推送，去掉 `--dry-run` 真实执行：

```
python -m src.sinks.feishu_bot --dashboard-url http://localhost:8731/index.html --db-path data/competitor_intel.db --execute
# pushed=0 skipped=0 failed=5，5 个 locale 全部在"上传图片"这一步失败
```

用同样的 `get_tenant_access_token()`+`im/v1/images` 调用单独排查（绕开
`upload_image()` 只包裝 HTTP 状态码、丢失响应体的问题，直接读
`urllib.error.HTTPError.read()`），确认真实响应体：

```json
{"code":234007,"msg":"App does not enable bot feature.","error":{"log_id":"..."}}
```

跟「Phase 7 之后：飞书群截图推送」一节记录的阻塞点**完全是同一个**、尚未解决：
`FEISHU_APP_ID` 对应的应用在飞书开放平台后台仍然没有开通"机器人"能力（这跟
Bitable 读写用的是不同的权限范围，webhook 配置齐全也绕不开这一步）。需要用户
去飞书开放平台后台给这个应用开通机器人能力，这是纯粹的第三方平台配置操作，
本 session 没有权限代为操作，也没有找到绕过的办法。

### 本次未做 / 仍遗留

- **飞书群截图推送端到端未验证成功**：卡在应用机器人权限，5 个 locale 全部
  `failed`；`sync_log` 确实记录了这次失败（`target=bot_EN/FR/VN/ID/EN-Asia`，
  `action=create`，`status=failed` 各 1 条），审计轨迹按设计正常工作，图片
  上传失败也没有被吞掉。
- **GitHub Pages 仍未启用**，需要用户手动去 Settings 里点一下（见上文步骤）。
- Phemex 的地区独占/`group_id` 跨 locale 归组缺陷（`article_id` 不同导致
  EN/FR 拼不到同一个 `group_id`）在这次真实数据上又复现了一次（体现为
  `Phemex/delisting/FR` 无法走 EN 复用），依然是已知问题，未修复。
- 本次范围内 Weex 仍是 0 条（路径问题未解决），BingX/Zoomex 全量、其余
  Lbank 分类均未涉及。

## 飞书群推送架构切换：webhook → 应用机器人 chat_id（2026-07-15）

用户在飞书开发者后台给应用开通了"机器人"能力、补齐了权限 scope，并把机器人邀请进
了几个真实测试群后，反馈"群推送图片不能使用原始 webhook，需要通过应用机器人实现"、
"chat id 现在可以访问了，用真的"——把此前「Phase 7 之后：飞书群截图推送」一节遗留的
阻塞点（应用未开通机器人能力）和悬而未决的推送路径选择一次性解决掉。

### 为什么整个换成应用机器人主动发消息，不是"修好 webhook 那条路"

`push_image_to_webhook()` 原来的设计（自定义机器人 webhook 发 `msg_type=image`）
本身协议上没问题，但发图片前必须先用应用凭证把二进制传到 `im/v1/images` 换
`image_key`——**这一步天生依赖应用的"机器人"能力**，跟维不维护 webhook 无关。也就是
说"机器人能力"这个硬依赖躺不掉，webhook 只是在这基础上又加了一层"每个 locale 单独
一个 webhook 密钥"的维护成本。既然机器人能力已经开通，直接让应用机器人自己
`POST im/v1/messages?receive_id_type=chat_id` 主动发消息更省事：不需要为每个 locale
在飞书后台创建自定义机器人、复制 webhook URL 存进 `.env`，只需要把真正的机器人
（一个应用只有一个）邀请进对应的群（一次性群操作），配置里维护"群名"字符串就行。

### 架构改动

- **`src/sinks/feishu_bot.py`**：
  - 新增 `list_bot_chats(credentials) -> dict[群名, chat_id]`（`GET im/v1/chats`，
    分页遍历 `page_token`，返回机器人当前已加入的全部群）。
  - 新增 `send_image_via_bot(chat_id, image_key, credentials)`（`POST
    im/v1/messages?receive_id_type=chat_id`，body `{"receive_id": chat_id,
    "msg_type": "image", "content": json.dumps({"image_key": image_key})}`），
    跟 `upload_image()` 同款 token 失效重试逻辑（`code in (99991661, 99991663,
    99991664)` 强制刷新 token 重试一次）。
  - **删除** `push_image_to_webhook()` 和 `_ENV_VAR_RE`（`${WEBHOOK_*}` 占位符替换
    逻辑）——不是保留兼容层，彻底换掉，跟项目一贯"确定不用就删除"的做法一致。
  - `load_push_targets()` 签名简化成 `load_push_targets(path=PUSH_TARGETS_PATH)`，
    不再需要 `env` 参数（群名不是密钥，不用从 `.env` 做变量替换，直接读 YAML 原文）。
  - `push_dashboard_screenshots()`：`dry_run=True` 时行为不变（不调用任何飞书 API，
    只打印"会发到哪个群名"，不解析 `chat_id`）；`dry_run=False` 时先调一次
    `list_bot_chats()` 拿到当前群名→`chat_id` 映射，再对每个 locale 查表——查不到
    （机器人没加入该群，或群名对不上）按 `skip`（`chat_not_found`）处理，不是
    `failed`，跟"没配置群名"是同一档"预期内跳过"，语义上跟 EN-Asia 目前的真实情况
    完全对应（见下方验收记录）。
- **`config/push_targets.yaml`**：`webhook: ${WEBHOOK_EN}` 全部换成
  `chat_name: "CompAgent_EN"`（EN/FR/VN/ID 四个真实群名，2026-07-15 用
  `GET im/v1/chats` 核对过机器人确实已加入）。`EN-Asia` 配的 `chat_name:
  "CompAgent_EN-Asia"` **目前没有对应的真实群**——机器人已加入的群列表里只有
  `CompAgent_EN/FR/VN/ID` 四个 + 一个命名不属于这套约定的 `CompAgent_KR`（未确认
  是不是打算给 EN-Asia 用，注释里如实记录、未擅自猜测复用）。
- **`config/.env.example`**：删除 `WEBHOOK_EN`/`WEBHOOK_FR`/`WEBHOOK_VN`/
  `WEBHOOK_ID`/`WEBHOOK_EN_ASIA` 五个变量（不再需要），保留 `WEBHOOK_OPS`
  ——那是 Phase 8 运维告警群用的，走的是另一条独立的"自定义机器人 webhook"路径，
  跟这次业务群推送的架构变更无关，不受影响。
- **`tests/sinks/test_feishu_bot.py`**：整份重写，覆盖新增的
  `list_bot_chats()`/`send_image_via_bot()`（含分页、业务错误、token 刷新）、
  `push_dashboard_screenshots()` 在"配置了 chat_name 但机器人没加入该群"场景下
  正确 skip（不是 failed）。

### 真实网络验收记录（2026-07-15，真实 chat_id，真实推送到 4 个测试群）

先用真实凭证查询机器人已加入的群，确认真实 `chat_id`：

```
GET im/v1/chats -> CompAgent_EN=oc_bbea8d58f4f1cf7f321a90f819882c5d
                    CompAgent_FR=oc_f9baa4646b34101c00c09e25709be64d
                    CompAgent_KR=oc_24a7adb4942ecf1ebab28b05baacee62（未使用）
                    CompAgent_VN=oc_11ceacda2fb83a7912cabb20ab47d2fb
                    CompAgent_ID=oc_4bb28a20a50dd4c19842d04ed5d459d3
```

改完代码后，用本地 `http.server`（`docs/` 已发布的最新 `dashboard.json`）跑一次真实
截图 + 真实推送：

```
python -m src.sinks.feishu_bot --dashboard-url http://localhost:8731/index.html \
  --db-path data/competitor_intel.db --execute
# 5 个 locale 截图全部成功
# EN: 推送成功 -> CompAgent_EN
# FR: 推送成功 -> CompAgent_FR
# VN: 推送成功 -> CompAgent_VN
# ID: 推送成功 -> CompAgent_ID
# EN-Asia: 应用机器人未加入群「CompAgent_EN-Asia」（或群名不匹配），跳过
# pushed=4 skipped=1 failed=0
```

`sync_log` 核对：`bot_EN/FR/VN/ID` 四行 `action=create status=success`，
`bot_EN-Asia` 一行 `action=skip status=success error=chat_not_found`——跟「Phase 7
之后」一节记录的更早一批 `failed`（应用机器人能力未开通时代）区分开，历史失败记录
原样保留在 `sync_log` 里，不是本次的产出，如实反映"这条推送链路从阻塞到打通"的
真实过程。`pytest` 全量 276 通过（`tests/sinks/test_feishu_bot.py` 18 个，全部离线
mock）。

### 已知限制

- **`CompAgent_EN-Asia` 群还不存在**：`config/push_targets.yaml` 已经按预期命名
  配好 `chat_name`，只要之后真的建一个这个名字的群、把机器人邀请进去，`EN-Asia` 的
  推送会自动打通，不需要改任何代码。是否要把 `CompAgent_KR` 复用成 `EN-Asia`
  的目标群，本次未擅自决定，留给用户确认。
- **同名群撞车没有保护**：`list_bot_chats()` 按名字建字典，如果飞书里真的出现两个
  同名群，后出现的会静默覆盖先出现的——当前机器人只加入了个位数测试群，风险低，
  真实生产铺开后如果群命名不严格唯一，需要改成直接配置 `chat_id`（而不是
  `chat_name`）来规避这个问题。
- **`CompAgent_EN/FR/VN/ID` 目前是用户自建的测试群**（命名规律是"CompAgent_
  {locale}"，不是 CLAUDE.md 早前占位的"竞品情报-{locale}"这套命名），是否要切换成
  正式的业务群、群名是否要统一成中文命名规范，是业务侧决定，本次未涉及，
  `config/push_targets.yaml` 的 `name` 字段（人类可读名称，不参与匹配逻辑）仍然
  保留了"竞品情报-EN"这套占位命名，跟真实 `chat_name` 是两个独立字段。
- **没有接入任何调度**：跟「Phase 7 之后」一节记录的限制一样，仍然只能手动执行
  `python -m src.sinks.feishu_bot --dashboard-url <url> --execute`。

## Phase 5 真实同步：主库 data/competitor_intel.db 首次全量灌入飞书多维表（2026-07-15）

Phase 5（`src/sinks/feishu_bitable.py`）此前只在专门的验收库
`data/test_daily_20260715.db` 上跑过（见「Phase 5 完成情况」一节）。本次用户要求
对当前主库 `data/competitor_intel.db`（含本 session 之前跑完的 99 条新数据、
Zoomex 全量基线、22 条真实 Phase 4 insights）执行真实同步，代码本身零改动，
只是换了一个 `--db-path` 目标。

```
python -m src.sinks.feishu_bitable --db-path data/competitor_intel.db --dry-run
# announcements: dry_run_rows=2117　insights: dry_run_rows=22（字段映射全部正常）

python -m src.sinks.feishu_bitable --db-path data/competitor_intel.db
# 首轮：announcements created=2117 updated=0 skipped=0 failed=0
#       insights      created=22   updated=0 skipped=0 failed=0

python -m src.sinks.feishu_bitable --db-path data/competitor_intel.db
# 第二轮（幂等验证）：announcements created=0 updated=0 skipped=2117 failed=0
#                     insights      created=0 updated=0 skipped=22   failed=0
```

`sync_log` 核对：`bitable_announcements`/`bitable_insights` 各一批
`action=create status=success`，行数分别 2117/22，跟命令行输出逐一对上。这是这个
主库第一次真正同步到飞书多维表（此前的验收都在独立的临时/demo 库上做），后续
`competitor_intel.db` 里的数据（不管是重跑采集、Phase 4 分析产出新 insights，还是
Phase 3 pipeline 改了 category/is_region_exclusive）都可以直接重跑同一条命令
增量同步，不需要额外操作。

## Phase 4 逐条字段扩展 + Phase 7 看板 category-first 重构 + 推送管道重设计（2026-07-20）

用户给出一份完整的三段式实现 prompt（Phase 4 逐条字段扩展 → export 改造 → 看板
重构）。计划阶段探索发现一个真实冲突：新的 category-first 看板 IA 会移除
`src/dashboard/screenshot.py` 依赖的顶层 `.locale-tab` 元素，导致飞书群截图推送
失效——用户选择"围绕新 IA 重新设计推送机制"（而不是保留兼容的 locale-tab 标签、
也不是放任推送损坏），所以本次实际是 4 段：Phase 4 字段扩展 → export 改造 → 看板
重构 → 推送管道重设计。**真实 LLM 调用严格限制在 20 次以内**（用户明确要求），
实际只用了 4 次（campaign/product/listing/delisting 各 1 批），验证在从主库拷贝
出的 scratch db 上进行，主库 `data/competitor_intel.db` 全程未被真实 LLM 调用
触碰。

### Stage 1：Phase 4 每篇公告逐条分析字段扩展

`insights.articles_analysis[]`（每篇公告的结构化分析）从只有描述性字段
（mechanics/feature_description/token_symbol/...）扩展成额外携带 5 个新字段：
`diff_type`/`priority`/`follow_up`（四个 category 通用）+ `evidence_indices`
（campaign/product/listing，delisting 无 ZMX 对比不需要）+ `change_kind`
（**仅 campaign**，仅当该条自己 status=changed 时才可能有值）+ `listing_kind`
（**仅 listing**，spot/perp，从 market_type 归约，两者均有/不明时填 null 不猜测）。

- `config/analysis.yaml`：四个 `prompt_versions` 全部 v1→v2（改了 prompt 正文，
  按项目铁律必须递增，这会让 `llm_cache` 全部失效、下次真实全量重跑会重新调用
  LLM——预期成本，不是 bug）；`max_tokens_by_category` 各上调 500-800（响应体
  随文章数线性增长，不再是单个 zmx_comparison 对象的固定开销）。
- `src/analysis/prompts.py`：四套模板的 `articles[]` JSON 示例块逐条加了上述
  字段+强制规则说明；模板顶部注释标注这些字段的合法性由 `llm.py` 程序性强制，
  不是单纯信任 LLM 输出遵守文字说明。
- `src/analysis/llm.py`：`validate_and_normalize()` 新增
  `article_status: dict[uid, status] | None = None` 参数（默认 None 保证既有
  调用方/测试不受影响，等价于"每条状态都不是 changed"）；新增
  `_normalize_article_fields()`，在既有的 uid-membership 过滤循环内，对每条
  articles 做字段级校验——**只置空/强制单个字段，绝不因为某个字段不合法丢弃
  整条**（丢弃整条的唯一触发条件仍是 uid 不在 related_uids 内，这条既有约束
  没有变）。`priority` 无法识别时置 `null` 而不是编造一个"低"的安全默认值
  （沿用项目"宁可 null 不要编造"的一贯做法）。
- `src/analysis/run.py`：调用点新增 `article_status = {r["uid"]: r["status"]
  for r in rows}` 并传入 `validate_and_normalize()`。`_remap_articles_to_locale()`
  （EN→FR/VN/ID 复用）完全不需要改——它对每条 article dict 做的是浅拷贝再只覆盖
  `uid` 键，5 个新字段作为兄弟键自动跟着透传。
- **零 DB migration**：`articles_analysis` 从 Phase 4 建表起就是无 CHECK 约束的
  schemaless TEXT 列（跟有 CHECK 约束的批次级 `diff_type`/`priority` 列不同），
  5 个新字段完全是应用层（prompts.py + llm.py）的事。

**真实数据验证（cursor_agent，4 次真实调用，Bitunix 2026-07-15 批次，scratch db）**：
campaign/product/listing/delisting 各 1 个真实批次全部产出正确形状——
`listing_kind` 正确归约成 `perp`（4 篇美股永续合约上币公告）；`change_kind` 在
这批真实数据里全部正确为 `null`（因为这批公告全部是 `status=new`，没有一条
`changed`，程序性门控生效，不是恰好没测到）；delisting 正确不产出
`evidence_indices`/`listing_kind`/`change_kind`；EN→FR/ID 复用正确把新字段原样
带过去。`pytest` 全量 64 个 analysis 测试通过（新增 13 个逐条字段校验测试）。

### Stage 2：export 层重写（`src/dashboard/export_data.py`，locale-first → category-first）

顶层 schema 从 `overview/daily_digest/analysis_blocks/highlights/cex_tables/
cex_counts`（全部按 locale 分 key）改成 `meta/overview/trend/campaign/product/
listing/markets/search_index`（按 category 分 key，locale 变成每行的一个字段）。

- 新增 `_load_article_index(conn)`：把全部 insights 行的 `articles_analysis`
  展开成 `{uid: {diff_type, priority, follow_up, change_kind, listing_kind,
  description, is_mock, is_locale_derived}}` 的扁平索引（`description` 是新增的
  便利字段，从每个 category 对应的描述性字段——mechanics/feature_description/
  project_brief/reason——里取一个统一名字，供看板直接展示用）。老数据/校验失败
  批次（`articles_analysis` 为 NULL 或不含新字段）一律 `.get()` 取默认值，
  不抛异常。
- `build_category_section()`：campaign/product/listing/delisting/other 共用的
  单 category 构建函数（最新一批，`status IN (new, changed)`），listing 对外
  的 `listing` 段是 `listing_only + delisting` 两次调用结果拼接，delisting
  行显式标 `category: "delisting"` 供前端区分。
- `overview`：4 个 chip（Campaign/Product/Listing/Announcement=delisting+other），
  每个带 diff_type 分布；highlights 是跨 category 的 priority=高 逐条公告，按
  "priority 高→中→低，同级内 diff_type 严重度"排序。
- `trend`/`markets`/`search_index`：全部历史（不限最新一批），`search_index`
  字段刻意窄（不含正文），`markets` 只有跨地区矩阵（不重复导出按 locale 切片
  的数据，前端对 campaign/product/listing 自己按 locale 客户端再过滤）。
- **今日 Summary（daily-digest-v1 LLM 综述）整体移除**：用户明确决定"drop it
  from the rewrite"——新 Overview（chip+highlights）没有它的位置。
  `src/analysis/daily_digest.py` 本身不动（仍然是正确、有测试覆盖的模块），只是
  不再被 export_data.py 调用，成为暂时未使用但保留的代码。
- `src/dashboard/__main__.py` 同步更新（`meta.as_of_date`→`meta.batch_date`，
  顶层 `sources`→`meta.source_coverage`）。

新增 `tests/dashboard/test_export_data.py`（8 个用例，覆盖 8 个顶层 key 齐全、
search_index 不泄漏正文、老数据优雅降级、delisting 行正确打标、EN-Asia 无竞品
数据不报错、overview chip 计数/highlights 排序）。真实导出验证：对主库
`data/competitor_intel.db`（尚未跑 Stage 1 的真实 LLM 全量重跑，insights 还是
老形状）导出，确认新老数据混跑不报错、老数据正确显示中性默认——这本身就是一次
"优雅降级"要求的真实数据验证，不只是合成测试。

### Stage 3：看板重构（`docs/index.html`，locale-first tabs → category-first tabs）

顶层 tab 从 `EN/FR/VN/ID/EN-Asia/全量/全局视角` 改成 `Overview/Campaign/Product/
Listing/Markets/Search`，默认落地 Overview。完整沿用旧文件的 `:root` 设计
token（`--hue-*`/`--status-*`/`--loc-*`/`--font-*`）和组件（`.tag`/`.tag-mock`/
`.status-chip`/`.locale-dot`），新增 `.priority-high/-medium/-low` 三个正式的
优先级 CSS class（旧版是内联 style 现改成语义类）、全局共用的
`sortByPriorityThenDiff()` 排序函数（每个 tab 的列表都调用同一个函数，不各自
重新实现一遍）。

- Overview：4 个 chip + 跨 category highlights + 当日按竞品采集量排行（复用
  `meta.source_coverage`，客户端计算，未新增导出字段）+ Quick Jump 按钮。
- Campaign（最详细，卡片默认展开）/ Product（默认折叠）：共用同一个
  `detailCard()` 渲染函数，只有"是否默认展开"这一个参数不同。
- Listing：紧凑表格，delisting 行以 `category=delisting` 内嵌展示（无独立
  diff 列，因为 delisting 恒"不适用"）。
- Markets：EN/FR/SEA(=VN+ID 显示层 rollup)/KR(灰显占位，本期不做) 四个市场
  sub-tab，复用 Campaign/Product/Listing 的同一套卡片渲染（按 locale 过滤），
  外加跨地区矩阵（全部历史）+ EN-Asia 基线专属说明卡片。
- Search：完整复用旧版"全量"tab 的筛选/分页模式（locale/来源/分类/差异/
  日期区间 + 标题搜索），数据源从 `archive` 换成 `search_index`。

**手工验证（Playwright 无头浏览器，本地 http.server，真实 `data/competitor_intel.db`
导出的数据）**：6 个 tab 全部渲染无 console/page error；Listing 来源筛选、
Markets 切换 EN/FR/SEA（KR 正确不可点）、Search 文本+分类组合筛选、Campaign
展开/收起按钮，全部真实点击验证工作正常；"计数只算一次"规则用真实数据核对——
`overview.chips.campaign` 总数与 `data.campaign.length` 逐一对上，
`listing`（71 条 = listing 43 + delisting 28）在 Overview/Listing tab 两处
显示一致。

### Stage 4：推送管道重设计（新增范围，非原始 prompt 要求）

用户在计划阶段被告知一个真实冲突：`src/dashboard/screenshot.py` 靠点击顶层
`.locale-tab` 截图推送到飞书群，新 IA 移除了这个顶层入口——用户选择"围绕新 IA
重新设计"而不是保留兼容标签或放任其损坏。

- `docs/index.html` 新增 URL 触发的推送视图：`?view=push&locale=EN`（不是
  六个 tab 之一，正常点击不可达），`renderPushView()` 渲染紧凑内容（该 locale
  的 campaign/product/listing 统计条 + 最多 8 条 priority=高 重点，客户端从
  已有导出数据过滤/聚合，未新增导出字段），EN-Asia 走基线专属分支。渲染完成后
  设置 `[data-push-ready="<locale>"]` 标记供截图脚本等待。
- `src/dashboard/screenshot.py`：`capture_locale_tabs()`（单次加载+逐个点击）
  换成 `capture_push_views()`（每个 locale 各自 `page.goto()` 到推送视图 URL，
  等 `[data-push-ready]` 出现再截图）——代价是 dashboard.json 多 fetch 4 次
  （数据量小，可接受），换来不需要 Playwright 感知页面内部 JS 函数名。
- **真实发现的 bug**：`wait_for_selector` 默认要求元素"可见"（非零宽高），
  但 push-ready 标记是个空 `<div>`，永远等不到——真实 Playwright 跑第一次就
  超时暴露，改成 `state="attached"` 修复（不是等可见，只等元素挂载到 DOM）。
  这是纯手工合成测试测不出来的一类 bug，靠真实浏览器跑一遍才发现。
- `src/sinks/feishu_bot.py`：只改了 import 名 + 一处调用（`capture_locale_tabs`
  → `capture_push_views`），其余（chat 解析、图片上传、消息发送、sync_log、
  `PUSH_LOCALES` 常量、graceful skip 逻辑）完全不动，按计划确认过这是唯一
  需要碰这个文件的地方。

**真实验证**：本地 http.server + 真实 Playwright 对 5 个 locale 各自截图，
文件从旧版整页截图的 934KB（EN，3742px 高）降到新版 ~25KB（同样是 EN）——直接
解决了此前一直标注"未验证图片在飞书聊天里是否好看"的遗留疑虑。`python -m
src.sinks.feishu_bot --dashboard-url ... --dry-run` 真实跑通全流程（截图 5 张 +
正确解析出 5 个真实群名，无一失败）。

新增 `tests/dashboard/test_screenshot.py`（3 个用例：URL 构造、连字符 locale
文件名、单 locale 失败不影响其它），`tests/sinks/test_feishu_bot.py` 的 6 处
`capture_locale_tabs` mock 全部改名同步。

### 验收记录汇总

```
pytest
# 302 通过（Phase 5 完成时的 268 个 + Stage 1 新增 13 个 + Stage 2 新增 8 个 +
# Stage 4 新增 3 个 tests/dashboard/test_screenshot.py，tests/sinks/test_feishu_bot.py
# 既有 18 个原样通过，只是 mock 目标改名）
```

### 已知限制 / 遗留

- **主库 `data/competitor_intel.db` 的 22 条 `insights` 仍是 Phase 4 -v2 之前
  的老形状**（本次真实 LLM 验证只在 scratch db 上跑了 4 次，主库全程未碰）——
  下次对主库跑一次不限 `--max-calls` 的 `python -m src.analysis` 才会让新的
  5 个字段在生产数据里真正出现；在此之前，看板的 Campaign/Product/Listing/
  Overview 会一直显示"不适用"/无优先级（这是本次验证过的正确降级行为，不是
  bug，但如实记录：这是"数据还没跑"而不是"功能坏了"）。
- **`src/analysis/daily_digest.py` 变成暂时未使用的代码**：模块本身、其
  prompt（daily-digest-v1）、`peek_cached_digest()` 全部原样保留（仍有测试
  覆盖，仍然正确），只是新看板不再调用它，如果以后想恢复"今日综述"功能，
  逻辑都还在，只需要在 Overview 里重新接一个入口。
- **`docs/data/dashboard.json` 已用新 schema 重新生成并覆盖本地文件**，但
  **本次 session 没有执行任何 git 操作**，是否 commit/push 由用户决定。
- **GitHub Pages 仍未启用**（此前 session 遗留问题，本次未涉及）。
- Markets 的 KR 灰显占位已在下述 2026-07-20 优化中删除；EN-Asia 的看板与推送
  占位也一并删除。底层 Zoomex EN-Asia 采集范围不受影响。

## LLM 成本/质量优化 + Listing/Delisting 退出分析链路（2026-07-20）

用户确认新的正式业务约束：**只有 Campaign/Product 做 LLM 分析和 Zoomex 比较；
Listing/Delisting 不做任何 LLM 调用或 ZMX 比较，只在看板做确定性汇总和详情展示。**
同时暂不做 EN-Asia/KR 市场展示，删除已有占位。

### 分析调用边界

- `src/analysis/run.py` 新增 `ANALYZED_CATEGORIES={"campaign","product"}`，批次枚举后
  立即过滤；当日只有 listing/delisting 时不加载 LLM 凭证、不建 prompt、不查 ZMX
  基线、不写 insights，真实调用数为 0。
- `config/analysis.yaml` 删除 listing/delisting 的 max_tokens 和 prompt_version，
  Campaign/Product 升为 v3，响应预算分别降为 2200/2000。
- 旧 listing/delisting insights 暂不物理删除（保留历史可追溯性），但 Dashboard
  导出层明确忽略其中的 diff/priority/follow_up/listing_kind，避免展示过期分析。

### Campaign/Product v3 Prompt

- Prompt 只保留业务可用的短结构：事实描述、变化、ZMX 证据、具体优先级理由、
  `action_type`、`owner`、可执行 `follow_up`；不再要求模型重复返回 title。
- 正文不再简单截前 4000 字：程序保留首尾及包含数字、日期、奖励、规则、用户门槛的
  高价值句，再按 2400 字硬上限截断。
- changed 公告不再同时注入两份完整正文，改用句子级 `-before/+after` diff。
- 缓存 key 除 content hash/prompt version 外加入 model 和 Zoomex baseline digest
  hash；切换模型或基线变化会重新比较，不复用过期结果。

### Dashboard / Markets

- Listing 类型只在标题有明确词证据时以规则识别：`spot` → spot，
  `perpetual/perp/future/contract` → perp；冲突或无证据时显示未知，不猜测。
- Listing 表移除“优先级/差异”两列，只保留来源、地区、标题、类型、日期、状态。
- `build_markets()` 不再导出 `baseline_by_locale`；前端删除 EN-Asia 专属基线卡、
  EN-Asia push 分支、Asia 矩阵列和 KR 灰显 tab。
- `config/push_targets.yaml` 删除 EN-Asia 占位群，只保留 EN/FR/VN/ID。
- Zoomex EN-Asia 的采集与底层基线数据本次没有删除；本次约束仅针对展示/推送占位。

### 验证

```
.venv/bin/python -m pytest -q
# 302 passed
```

新增覆盖：listing/delisting 不加载凭证且零 LLM 调用、v3 句子级 diff、
Dashboard 忽略旧 listing insight、标题规则识别 perp、EN-Asia placeholder 不导出。
`docs/data/dashboard.json` 已用主库重新生成。

### v3 后续质量门禁与基线压缩

- `get_baseline_digest()` 仍先取最多 20 条“玩法类型覆盖池”，但 `run.py` 在构建
  Prompt 前新增 `select_relevant_baseline()`：以当前批次标题/正文和基线的
  title/mechanism/key_mechanics/reward/target_users 做确定性词项重叠排序，实际只
  注入最多 8 条（`candidate_entries_per_batch`）。无 embedding、无额外 LLM 调用；
  零重叠时以原类型覆盖顺序和 diversity bonus 保守退化。
- v3 逐条输出新增程序质量门禁：`action_type`、`owner` 按 category 枚举校验；
  高/中优先级缺 `priority_reason` 会记录 issue；“建议关注/建议评估”等无执行内容
  的泛化 follow-up 会被置空；有 action 但没有可执行 follow-up 会记 issue。
- 校验器会检测 LLM 漏返回的 UID（`missing_article_uids`）和重复 UID；重复项直接
  丢弃，漏项会计入本次 `validation_failed` 监控但仍保留同批次其它合法结果，
  避免“一条漏项导致整批可用分析全部作废”。
- 新增 `tests/analysis/test_baseline_selection.py` 以及 action/owner/漏项/重复 UID
  校验用例；全量测试总数更新为 308。
