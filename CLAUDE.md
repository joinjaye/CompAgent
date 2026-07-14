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
  采集器（Collectors）── watermark 模式 / full_scan 模式
       ↓
  SQLite（唯一真相源）
       ↓
  清洗 & 打标（Pipeline）── 归组 / 分类 / 地区独占标记
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
     存入 `content_history`，不覆盖丢失。
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
│   │   └── slate_json.py       # ✅ Zoomex 详情 content 字段：Slate.js 富文本 JSON → 纯文本（保留表格结构）
│   ├── pipeline/                # 清洗、归组、分类打标（Phase 3）
│   ├── analysis/                 # LLM summary & ZMX 差异（Phase 4）
│   ├── sinks/
│   │   ├── feishu_bitable.py   # 多维表同步（Phase 5）
│   │   └── feishu_bot.py       # 飞书群推送（Phase 6）
│   └── dashboard/               # 可视化看板生成（Phase 7）
├── tests/
│   ├── fixtures/                # 每个源的真实响应快照（Phase 1 起填充，供离线单测）
│   ├── parsers/                  # parser 离线单测（Phase 2 起）
│   ├── collectors/               # collector 离线单测，mock HTTP（Phase 2 起）
│   └── test_db.py               # db 层单测（Phase 0）
├── data/
│   ├── competitor_intel.db      # SQLite 数据库文件（不入版本控制）
│   └── logs/                    # 每日跑批日志（Phase 8）
└── scripts/
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
| content | TEXT | 清洗后正文 |
| content_hash | TEXT | `SHA256(content)`，变更检测用 |
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
| 3 | 清洗、归组、分类打标 | src/pipeline/ | 待开始 |
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
| Zoomex（我方基线） | EN / FR / EN-Asia / VN / ID | ✅ 通 | watermark | 页面本身是纯客户端渲染 SPA（curl 拿不到），但用一次性 headless browser（Playwright）拦截运行时请求，找到了匿名公开、无需登录态的真实 API：`POST api2.zoomex.com/gw/pub/v1/helpCenter/getArticleListByMenuId`（真正支持服务端翻页，是本项目目前唯一原生分页可用的源）+ `getArticleById`（详情，正文是 Slate.js 风格富文本 JSON，不是 HTML）。3-4 个分类（Platform Announcement / New Product Announcement / Platform Events，EN-Asia 多一个 Exclusive Events）。gmtCreatedAt/gmtUpdatedAt 抽样 10/10 存在真实差异。详见 `config/sources.yaml` zoomex 块注释 |
| Bitunix | EN | ✅ 通 | watermark | 真实公告在 Zendesk（support.bitunix.com），非主站 SPA；主站 platformgateway.bitunix.com 已确认死路（403/Cloudflare）。29/30 抽样有真实 updated_at 差异 |
| Bitunix | FR | ✅ 通 | watermark | 同 EN 机制 |
| Bitunix | ID | ✅ 通 | watermark | 同 EN 机制 |
| Weex | EN | ✅ 通 | watermark | 跑在 Zendesk 上（weexsupport.zendesk.com），标准 Help Center API，匿名可访问，正文 inline。category 18540264809497="Latest Announcements" |
| Weex | FR | ✅ 通 | watermark | 同 EN 机制，locale=fr |
| BingX | EN | ✅ 通 | full_scan | Nuxt 3 SSR，`__NUXT_DATA__` 内嵌数据；首屏仅~20条，**已确认是跨 12 个分区的聚合视图（非单分区）**，完整历史用 sitemap（7829 URL，已确认扁平覆盖全部分区）替代；createTime==updateTime 恒等 |
| BingX | VN | ✅ 通 | full_scan | 同 EN 机制，article_id 跨 locale 一致可做 group_id；VN sitemap 不完整需借用 EN sitemap |
| Phemex | EN | ✅ 通 | full_scan | SSR，`window.preloadedData` 内嵌数据，detail 页 inline 正文；updatedAt 只是秒级发布噪音；**News/Activities/Newsletter 3 个分类均已确认可抓（拆成 3 个 categories.\* endpoint）**；sitemap_Announcement.xml 给全量文章（已验证覆盖全部 3 个分类）+跨语言映射 |
| Phemex | FR | ✅ 通 | full_scan | 同 EN 机制，3 个分类 |
| Lbank | EN | ✅ 通 | watermark | Next.js SSR，正文内嵌在列表页 RSC JSON 里；updateTime 需另请求 detail 页（/support/articles/{code}）。仅默认聚合视图第 1 页（10条）可稳定拿到；**7 个页面级 tab 的分类代码树已找到（可用于 category 命名映射），但按 tab 单独抓取已确认不可行（curl 三种候选 URL 均只返回导航壳，0 条实际公告），翻页/按 tab 筛选均需 headless browser** |
| Lbank | VN | ✅ 通 | watermark | 同 EN 机制，noticeId/code 跨 locale 一致，可直接做 group_id |
| Lbank | ID | ✅ 通 | watermark | 同 EN 机制 |

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
- Bitunix/Weex 的 `field_mapping.category`（Zendesk `section_id`）没有落库，需要 Phase 3 决定
  归类方式（映射到 campaign/product/listing/other，还是新增字段）后再回来接上。
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
- Zoomex 的 `field_mapping.category`（对应用的是哪个 menu_id/categories.\* 分类名）没有落库，
  跟 Bitunix/Weex 的 `section_id` 一样留给 Phase 3 决定怎么用。
- `crawl_state.category` 目前没有正式的 SQLite migration 机制（见上方"架构新增"里的
  说明）——现在只有开发态数据无所谓，但 Phase 8（调度与监控）上线前，如果 schema 还会再变，
  需要认真设计一版 migration 而不是继续依赖"删库重建"。
