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
│   └── .env.example            # 飞书 / LLM 凭证模板
├── src/
│   ├── db/                     # SQLite schema & 操作层（Phase 0，已完成）
│   │   ├── schema.sql
│   │   ├── connection.py       # connect / get_connection / init_db
│   │   ├── operations.py       # upsert_announcement / crawl_state 读写
│   │   └── __main__.py         # `python -m src.db init`
│   ├── probe/                   # Phase 1 数据源验活 CLI（不是采集器）
│   ├── collectors/              # 每个交易所一个 adapter（Phase 2，进行中：批次 2/4 完成）
│   │   ├── http.py             # 通用 HTTP 客户端（超时重试 + certifi + rate_limit_seconds()）
│   │   ├── timeutil.py         # 时间格式转换（unix ms <-> UTC ISO8601），批次 2 新增
│   │   ├── base.py             # BaseCollector：fetch_list/fetch_detail/normalize/needs_detail 契约 + run() 编排
│   │   ├── zendesk_base.py     # Zendesk Help Center 通用采集逻辑（Bitunix/Weex 共用）
│   │   ├── bitunix.py          # ✅ 批次 1
│   │   ├── weex.py             # ✅ 批次 1
│   │   ├── zoomex.py           # ✅ 批次 2（我方基线，多分类 menu_id）
│   │   └── __main__.py         # `python -m src.collectors --source <x> --locale <y> [--category <c>] [--force-full]`
│   ├── parsers/                  # 每种响应格式一个 parser，离线可单测
│   │   ├── zendesk.py          # ✅ Bitunix/Weex 共用（标准 Zendesk articles.json）
│   │   ├── zoomex.py           # ✅ getArticleListByMenuId / getArticleById 响应解析（按 lang 匹配 contents[]）
│   │   ├── slate_json.py       # ✅ Zoomex 详情 content 字段：Slate.js 富文本 JSON → 纯文本（保留表格结构）
│   │   └── html_text.py        # ✅ Phase 2.5 新增：HTML → 纯文本（保留表格结构，跟 slate_json.py 同一套
│   │                            #    表格表示法），Bitunix/Weex/后续 Phemex/BingX/Lbank 共用
│   ├── pipeline/                # 跨语言归组、分类打标（Phase 3；清洗已在 Phase 2.5 前移到采集层）
│   ├── analysis/                 # LLM summary & ZMX 差异（Phase 4）
│   ├── sinks/
│   │   ├── feishu_bitable.py   # 多维表同步（Phase 5）
│   │   └── feishu_bot.py       # 飞书群推送（Phase 6）
│   └── dashboard/               # 可视化看板生成（Phase 7）
├── tests/
│   ├── fixtures/                # 每个源的真实响应快照（Phase 1 起填充，供离线单测）
│   ├── parsers/                  # parser 离线单测（Phase 2 起）
│   ├── collectors/               # collector 离线单测，mock HTTP（Phase 2 起）
│   ├── test_db.py               # db 层单测（Phase 0）
│   └── test_migrate_v2.py        # migrate_v2.py 单测（Phase 2.5）
├── data/
│   ├── competitor_intel.db      # SQLite 数据库文件（不入版本控制）
│   └── logs/                    # 每日跑批日志（Phase 8）
└── scripts/
    ├── migrate_v2.py             # schema v1 -> v2 迁移（Phase 2.5，见下文）
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

### insights（分析层 / 汇总分析表）

一行 = 一次 LLM 分析结论，可回链多个 `announcements`（跨语言/跨条目）。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| related_uids | TEXT | JSON 数组，回链 `announcements.uid` |
| source | TEXT | 竞品名 |
| category | TEXT | campaign / product / listing / delisting / other |
| summary | TEXT | 特点/玩法 summary |
| zmx_diff | TEXT | ZMX 差异分析 |
| diff_type | TEXT | ZMX已有 / ZMX缺失 / ZMX玩法不同 / 不适用 |
| priority | TEXT | 高 / 中 / 低 |
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
| 2 | 采集器 + 增量/变更检测 | src/collectors/*.py + src/parsers/*.py | 🔄 进行中（批次 2/4：Bitunix+Weex+Zoomex 完成） |
| 2.5 | schema 收口 + 清洗前移 | raw_category 列、category CHECK 约束加 delisting、src/parsers/html_text.py、scripts/migrate_v2.py | ✅ 已完成 |
| 2.6 | category_mapping.yaml 修复 + 真实数据核验 | config/category_mapping.yaml 改按 raw_category 原始值做 key（原 key 是猜测/不稳定的人类可读名称，Phase 3 会全线 miss）、raw_category 的 unchanged 分支更新逻辑 | ✅ 已完成 |
| 2.7 | Weex Listings/Delistings + P2P Announcement 分类补采、多语言数据补齐 | Zendesk 分类覆盖主动核查、Weex 三分类改造（含 section 级采集）、Bitunix/Weex 全部 locale 入库、ZendeskCollector 改 cursor 分页（修复 Zendesk offset 分页 page=100 硬限制）、html_text.py 表格单元格嵌套 `<p>` 的 bug 修复 | ✅ 已完成 |
| 3 | 跨语言归组、分类打标 | src/pipeline/（清洗已在 Phase 2.5 前移到采集层，不再是 Phase 3 的事） | 待开始 |
| 4 | LLM 分析（summary + ZMX 差异） | src/analysis/，写入 insights 表 | 待开始 |
| 5 | 飞书多维表同步 | src/sinks/feishu_bitable.py | 待开始 |
| 6 | 推送规则引擎 + 飞书群日报 | src/sinks/feishu_bot.py + src/pipeline/push_rules.py | 待开始 |
| 7 | 可视化看板 | src/dashboard/，静态 HTML | 待开始 |
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
