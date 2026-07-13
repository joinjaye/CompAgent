## 竞品情报平台（Competitor Intelligence Platform）

### 项目简介

自动采集加密交易所竞品公告中心内容，经清洗、分类、LLM 分析后沉淀至飞书多维表，按区域推送飞书群日报，为运营和产品团队提供持续的竞品情报支持。

### 数据流

```
竞品公告 API / HTML
       ↓
  采集器（Collectors）
  ├─ watermark 模式：按 update_time 增量拉取
  └─ full_scan 模式：拉前 N 页 + content_hash 变更检测
       ↓
  SQLite（唯一真相源）
       ↓
  清洗 & 打标（Pipeline）
  ├─ HTML → 纯文本
  ├─ 跨语言归组（group_id）
  ├─ 分类（campaign / product / listing / other）
  └─ 地区独占标记（is_region_exclusive）
       ↓
  LLM 分析（Analysis）
  ├─ 特点/玩法 summary
  └─ ZMX 差异分析 + 优先级
       ↓
  ┌──────────┬──────────────┐
  │ 飞书多维表 │  可视化看板    │
  │（业务视图） │（静态 HTML）   │
  └──────────┴──────────────┘
       ↓
  飞书群日报（按 locale 分群推送）
```

### 竞品范围（Phase 1）

| 交易所 | 语言 | 角色 |
|---|---|---|
| Bitunix | EN, FR, ID | 竞品 |
| Weex | EN, FR | 竞品 |
| BingX | EN, VN | 竞品 |
| Phemex | EN, FR | 竞品 |
| Lbank | EN, VN, ID | 竞品 |
| Zoomex | EN, FR, EN-Asia, VN, ID | 我方（对比基线） |

### 核心设计原则

1. **SQLite 是唯一真相源**。飞书多维表只是同步出去的业务视图，所有重跑、补数、改分类只操作 SQLite。
2. **两种增量策略，按源选择**。优先用 update_time 水位线；源不支持时回退为 N 页全量扫描 + content_hash 变更检测。两种模式均保留 content_hash 二次校验。
3. **跨语言归组用于分析，不用于推送去重**。推送按 locale 分群，各群独立；group_id 服务于汇总分析（跨区域对比、地区独占识别）。
4. **合规**：遵守 robots.txt、控制请求频率、不绕过登录墙、不抓非公开内容。

### 目录结构

```
├── CLAUDE.md                  # CC 每次 session 必读的项目上下文
├── README.md
├── config/
│   ├── sources.yaml           # 数据源配置（endpoint / 策略 / 字段映射）
│   ├── push_targets.yaml      # locale → 飞书群 webhook 映射
│   ├── push_rules.yaml        # 推送规则（配置化）
│   └── .env.example           # 飞书凭证模板
├── src/
│   ├── db/                    # SQLite schema & 操作层
│   ├── collectors/            # 每个交易所一个 adapter
│   ├── pipeline/              # 清洗、归组、分类打标
│   ├── analysis/              # LLM summary & ZMX 差异
│   ├── sinks/
│   │   ├── feishu_bitable.py  # 多维表同步
│   │   └── feishu_bot.py      # 飞书群推送
│   └── dashboard/             # 可视化看板生成
├── tests/
│   ├── fixtures/              # 每个源的真实响应快照
│   └── ...
├── data/                      # SQLite 数据库文件
└── scripts/
    ├── run_daily.sh           # 每日跑批入口
    └── backfill.sh            # 补数脚本
```

### SQLite 核心表

**announcements**（原始层）

| 字段 | 类型 | 说明 |
|---|---|---|
| uid | TEXT PK | `{source}_{locale}_{article_id}` 哈希 |
| group_id | TEXT | 跨语言归组 |
| source | TEXT | Bitunix / Weex / BingX / Phemex / Lbank / Zoomex |
| locale | TEXT | EN / FR / ID / VN / EN-Asia |
| article_id | TEXT | 该站原生文章 ID |
| url | TEXT | 原文链接 |
| title | TEXT | |
| content | TEXT | 清洗后纯文本 |
| content_hash | TEXT | SHA256(content)，变更检测 |
| post_time | TEXT | 发布时间，UTC ISO8601 |
| update_time | TEXT | 源端更新时间（如有），UTC |
| fetched_at | TEXT | 抓取时间 |
| status | TEXT | new / changed / unchanged |
| category | TEXT | campaign / product / listing / other |
| is_region_exclusive | BOOLEAN | 是否地区独占 |
| push_status | TEXT | pending / pushed / skipped，按 locale 维度 |
| source_endpoint | TEXT | 来源 API endpoint |

**content_history**（变更历史）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| uid | TEXT FK | 关联 announcements |
| content_hash | TEXT | 旧版本 hash |
| content | TEXT | 旧版本正文 |
| captured_at | TEXT | 快照时间 |

**insights**（分析层 / 汇总分析表）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| related_uids | TEXT | JSON 数组，回链原始层 |
| source | TEXT | 竞品名 |
| category | TEXT | |
| summary | TEXT | 特点/玩法 summary |
| zmx_diff | TEXT | ZMX 差异分析 |
| diff_type | TEXT | ZMX已有 / ZMX缺失 / ZMX玩法不同 / 不适用 |
| priority | TEXT | 高 / 中 / 低 |
| created_at | TEXT | |

**crawl_state**（采集水位线）

| 字段 | 类型 | 说明 |
|---|---|---|
| source | TEXT | |
| locale | TEXT | |
| high_watermark | TEXT | 上轮最大 update_time，ISO8601 UTC |
| strategy | TEXT | watermark / full_scan |
| updated_at | TEXT | |
| PK | | (source, locale) |

**sync_log**（飞书同步日志）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| target | TEXT | bitable / bot_{locale} |
| record_id | TEXT | uid 或 insight_id |
| action | TEXT | create / update / skip |
| status | TEXT | success / failed |
| error | TEXT | |
| synced_at | TEXT | |

### 推送规则

| 场景 | 动作 | 备注 |
|---|---|---|
| 新增活动 | 推送 | status=new & category=campaign |
| 活动规则/奖励变化 | 推送 | status=changed & diff 涉及规则或奖励 |
| 新玩法 | 推送 | diff_type=ZMX缺失 & priority=高 |
| 地区独占公告 | 推送 | is_region_exclusive=true |
| 与 Zoomex 一致 | 不推送 | diff_type=ZMX已有 |
| category=other | 不推送 | 维护、风控等噪音 |
| 已推送过 | 不推送 | push_status=pushed |

### Roadmap

| Phase | 内容 | 交付物 |
|---|---|---|
| 0 | 项目骨架 + 数据模型 | CLAUDE.md、schema、目录 |
| 1 | 数据源侦察 | sources.yaml + fixtures |
| 2 | 采集器 + 增量/变更检测 | collectors |
| 3 | 清洗、归组、分类打标 | pipeline |
| 4 | LLM 分析（summary + ZMX 差异） | analysis |
| 5 | 飞书多维表同步 | sinks/bitable |
| 6 | 推送规则引擎 + 飞书群日报 | sinks/bot + push_rules |
| 7 | 可视化看板 | dashboard |
| 8 | 调度与监控 | cron + 告警 |

行业热点模块（Phase 2 规划）待业务明确定义后启动，不在 Phase 1 范围内。
