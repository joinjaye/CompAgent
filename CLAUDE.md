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
├── readme.md                  # 项目简介（业务背景，人读）
├── phasePrompts.md             # Phase 0-8 的完整任务 prompt 存档
├── pytest.ini
├── requirements.txt            # 运行时依赖（目前仅 PyYAML）
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
│   ├── collectors/             # 每个交易所一个 adapter（Phase 2）
│   ├── pipeline/                # 清洗、归组、分类打标（Phase 3）
│   ├── analysis/                 # LLM summary & ZMX 差异（Phase 4）
│   ├── sinks/
│   │   ├── feishu_bitable.py   # 多维表同步（Phase 5）
│   │   └── feishu_bot.py       # 飞书群推送（Phase 6）
│   └── dashboard/               # 可视化看板生成（Phase 7）
├── tests/
│   ├── fixtures/                # 每个源的真实响应快照（Phase 1 起填充，供离线 parser 单测）
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
| category | TEXT | campaign / product / listing / other，可为 NULL（Phase 3 之前未分类） |
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
| category | TEXT | campaign / product / listing / other |
| summary | TEXT | 特点/玩法 summary |
| zmx_diff | TEXT | ZMX 差异分析 |
| diff_type | TEXT | ZMX已有 / ZMX缺失 / ZMX玩法不同 / 不适用 |
| priority | TEXT | 高 / 中 / 低 |
| created_at | TEXT | |

### crawl_state（采集水位线）

| 字段 | 类型 | 说明 |
|---|---|---|
| source | TEXT | PK (source, locale) |
| locale | TEXT | |
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
| 与 Zoomex 一致 | 不推送 | diff_type=ZMX已有 |
| category=other | 不推送 | 维护、风控等噪音 |
| 已推送过 | 不推送 | push_status=pushed |

## Phase 规划摘要

| Phase | 内容 | 交付物 | 状态 |
|---|---|---|---|
| 0 | 项目骨架 + 数据模型 | CLAUDE.md、目录结构、SQLite schema、配置模板、db 层单测 | ✅ 已完成 |
| 1 | 数据源侦察 | 填满的 sources.yaml + 每个源一份真实响应 fixture | 待开始 |
| 2 | 采集器 + 增量/变更检测 | src/collectors/*.py | 待开始 |
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
