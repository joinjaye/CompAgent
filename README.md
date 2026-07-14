下面是基于最终版 sources.yaml 全面更新的 README + 8 个 Phase prompt。所有侦察结论、每源的特殊处理、已知局限全部写进去了。

---

# README.md

## 竞品情报平台（Competitor Intelligence Platform）

### 项目简介

自动采集加密交易所竞品公告中心内容，经清洗、分类、LLM 分析后沉淀至飞书多维表，按区域推送飞书群日报，为运营和产品团队提供持续的竞品情报支持。

### 数据流

```
竞品公告 API / HTML
       ↓
  采集器（Collectors）
  ├─ watermark 模式：按 update_time 增量拉取（Bitunix, Weex, Zoomex）
  └─ full_scan 模式：固定窗口扫描 + content_hash 变更检测（BingX, Phemex, Lbank）
       ↓
  SQLite（唯一真相源）
       ↓
  清洗 & 打标（Pipeline）
  ├─ HTML / Slate.js JSON → 纯文本
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

### 数据源总览（Phase 1 侦察完成）

| 交易所 | 语言 | 角色 | 接口类型 | 增量策略 | Parser 复杂度 |
|---|---|---|---|---|---|
| Bitunix | EN, FR, ID | 竞品 | Zendesk JSON API | watermark | 简单 |
| Weex | EN, FR | 竞品 | Zendesk JSON API | watermark | 简单 |
| BingX | EN, VN | 竞品 | Nuxt SSR（__NUXT_DATA__） | full_scan | 中等（devalue 索引解析） |
| Phemex | EN, FR | 竞品 | SSR（window.preloadedData） | full_scan | 中等（JS 对象字面量解析） |
| Lbank | EN, VN, ID | 竞品 | Next.js RSC streaming | full_scan | 较难（RSC 转义 JSON） |
| Zoomex | EN, FR, EN-Asia, VN, ID | 我方基线 | REST POST JSON API | watermark | 中等（Slate.js 富文本 JSON） |

已知局限：
- Lbank：每次只能拿到最新 10 条，翻页 API 未逆向，无 per-item category
- BingX / Phemex：首屏条目有限，完整历史需走 sitemap 回填
- Zoomex content 字段是 Slate.js 富文本 JSON，需专门 parser 转纯文本

### 核心设计原则

1. **SQLite 是唯一真相源**。飞书多维表只是同步出去的业务视图，所有重跑、补数、改分类只操作 SQLite。
2. **两种增量策略，按源选择**。优先用 update_time 水位线（Bitunix / Weex / Zoomex）；源不支持时回退为固定窗口扫描 + content_hash 变更检测（BingX / Phemex / Lbank）。两种模式均保留 content_hash 二次校验。
3. **跨语言归组用于分析，不用于推送去重**。推送按 locale 分群，各群独立；group_id 服务于汇总分析（跨区域对比、地区独占识别）。
4. **合规**：遵守 robots.txt、控制请求频率（≥500ms 间隔）、不绕过登录墙、不抓非公开内容。

### 目录结构

```
├── CLAUDE.md
├── README.md
├── config/
│   ├── sources.yaml           # 数据源配置（已由 Phase 1 填充完毕）
│   ├── category_mapping.yaml  # 各源原生分类 → 我方标签映射
│   ├── push_targets.yaml      # locale → 飞书群 webhook 映射
│   ├── push_rules.yaml        # 推送规则（配置化）
│   └── .env.example
├── src/
│   ├── db/                    # SQLite schema & 操作层
│   ├── collectors/            # 每个交易所一个 adapter
│   │   ├── base.py            # Collector 基类
│   │   ├── zendesk.py         # Bitunix + Weex 共用
│   │   ├── bingx.py
│   │   ├── phemex.py
│   │   ├── lbank.py
│   │   └── zoomex.py
│   ├── parsers/               # 各源专用解析器
│   │   ├── nuxt_devalue.py    # BingX __NUXT_DATA__ 解析
│   │   ├── preloaded_data.py  # Phemex window.preloadedData 解析
│   │   ├── rsc_stream.py      # Lbank Next.js RSC 解析
│   │   └── slate_json.py      # Zoomex Slate.js 富文本 JSON → 纯文本
│   ├── pipeline/              # 清洗、归组、分类打标
│   ├── analysis/              # LLM summary & ZMX 差异
│   ├── sinks/
│   │   ├── feishu_bitable.py
│   │   └── feishu_bot.py
│   └── dashboard/
├── tests/
│   └── fixtures/              # 每个源的真实响应快照（Phase 1 产出）
├── data/                      # SQLite 数据库 + 日志
└── scripts/
    ├── run_daily.sh
    └── backfill.sh
```

### SQLite 核心表

**announcements**（原始层）

| 字段 | 类型 | 说明 |
|---|---|---|
| uid | TEXT PK | `{source}_{locale}_{article_id}` 的 SHA256 |
| group_id | TEXT | 跨语言归组 |
| source | TEXT | bitunix / weex / bingx / phemex / lbank / zoomex |
| locale | TEXT | EN / FR / ID / VN / EN-Asia |
| article_id | TEXT | 该站原生文章 ID |
| url | TEXT | 原文链接 |
| title | TEXT | |
| content | TEXT | 清洗后纯文本 |
| content_hash | TEXT | SHA256(content) |
| post_time | TEXT | 发布时间，UTC ISO8601 |
| update_time | TEXT | 源端更新时间（如有），UTC |
| fetched_at | TEXT | 抓取时间 |
| status | TEXT | new / changed / unchanged |
| category | TEXT | campaign / product / listing / other |
| is_region_exclusive | BOOLEAN | 是否地区独占 |
| push_status | TEXT | pending / pushed / skipped |
| source_endpoint | TEXT | 来源 API endpoint |

**content_history** / **insights** / **crawl_state** / **sync_log**：同前一版定义，此处不重复。

### 推送规则

| 场景 | 动作 |
|---|---|
| 新增活动 | 推送 |
| 活动规则/奖励变化 | 推送 |
| 新玩法（ZMX缺失 + 高优先级） | 推送 |
| 地区独占公告 | 推送 |
| 与 Zoomex 一致 | 不推送 |
| category=other | 不推送 |
| 已推送过 | 不推送 |

### Roadmap

| Phase | 内容 | 状态 |
|---|---|---|
| 0 | 项目骨架 + 数据模型 | ✅ 完成 |
| 1 | 数据源侦察 | ✅ 完成（含补充侦察） |
| 2 | 采集器 + 增量/变更检测 | 待开始 |
| 3 | 清洗、归组、分类打标 | |
| 4 | LLM 分析 | |
| 5 | 飞书多维表同步 | |
| 6 | 推送规则引擎 + 飞书群日报 | |
| 7 | 可视化看板 | |
| 8 | 调度与监控 | |

行业热点模块待业务明确定义后启动，不在当前范围内。

---

# Phase Prompts

---

## Phase 0｜项目骨架 + 数据模型

交付物：CLAUDE.md、目录结构、SQLite schema、配置模板
验收：`python -m src.db init` 建出库，`pytest` 通过

```
我要搭一个竞品情报平台（Python）。这个 session 只做骨架和数据模型，不写任何爬虫逻辑。

【业务背景】
自动采集 6 家加密交易所公告中心的内容 → 清洗去重 → 分类 → LLM 分析 → 同步飞书多维表 → 按区域分群推送飞书群日报。

竞品与语言：
- Bitunix: EN, FR, ID
- Weex: EN, FR
- BingX: EN, VN
- Phemex: EN, FR（每个 locale 有 3 个分类：News/Activities/Newsletter）
- Lbank: EN, VN, ID
- Zoomex: EN, FR, EN-Asia, VN, ID ← 我方对比基线，不是竞品。有 4 个菜单分类（Platform Announcement / New Product Announcement / Platform Events / Exclusive Events，其中 Exclusive Events 仅 EN-Asia 有数据）

【核心设计约束】
1. SQLite 是唯一真相源，飞书多维表只是业务视图。
2. 两种增量采集策略：
   a) watermark 模式（Bitunix, Weex, Zoomex）：按源端 update_time 做水位线增量拉取，content_hash 做二次校验。
   b) full_scan 模式（BingX, Phemex, Lbank）：固定窗口扫描 + content_hash 变更检测。Lbank 窗口固定为 10 条且无法扩大（翻页 API 未逆向）。
3. 跨语言归组（group_id）用于分析层的跨区域对比和地区独占识别，不用于推送去重。
4. 推送按 locale 分群（EN/FR/VN/ID/EN-Asia），各群独立。
5. 合规：遵守 robots.txt、请求间隔 ≥500ms、不绕过登录墙。

【本次任务】
1. 生成项目目录结构：
   src/db, src/collectors, src/parsers, src/pipeline, src/analysis, src/sinks, src/dashboard
   tests/fixtures
   config, data, scripts

2. 设计并实现 SQLite schema（所有时间字段统一 UTC ISO8601）：
   - announcements（原始层）：
     uid TEXT PK（{source}_{locale}_{article_id} 的 SHA256）,
     group_id TEXT,
     source TEXT, locale TEXT, article_id TEXT,
     url TEXT, title TEXT, content TEXT,
     content_hash TEXT（SHA256(content)）,
     post_time TEXT, update_time TEXT, fetched_at TEXT,
     status TEXT（new/changed/unchanged）,
     category TEXT（campaign/product/listing/other）,
     is_region_exclusive BOOLEAN DEFAULT false,
     push_status TEXT DEFAULT 'pending',
     source_endpoint TEXT

   - content_history：
     id INTEGER PK AUTOINCREMENT,
     uid TEXT FK → announcements,
     content_hash TEXT, content TEXT, captured_at TEXT

   - insights（分析层）：
     id TEXT PK,
     related_uids TEXT（JSON 数组）,
     source TEXT, category TEXT,
     summary TEXT, zmx_diff TEXT,
     diff_type TEXT（ZMX已有/ZMX缺失/ZMX玩法不同/不适用）,
     priority TEXT（高/中/低）,
     created_at TEXT

   - crawl_state（采集水位线）：
     source TEXT, locale TEXT, menu_or_category TEXT DEFAULT 'default',
     high_watermark TEXT,
     strategy TEXT（watermark/full_scan）,
     updated_at TEXT,
     PRIMARY KEY (source, locale, menu_or_category)
     注意：Zoomex 和 Phemex 每个 locale 有多个子分类（menu_id / category），需要每个子分类独立维护水位线。

   - sync_log（飞书同步日志）：
     id INTEGER PK AUTOINCREMENT,
     target TEXT, record_id TEXT, action TEXT,
     status TEXT, error TEXT, synced_at TEXT

3. 写 config/sources.yaml —— 已由 Phase 1 填充完毕，直接使用项目中的现有文件。

4. 写 config/category_mapping.yaml：各源原生分类 → 我方标签（campaign/product/listing/other）的映射表。初始值：
   bitunix:
     "Events and Activities" → campaign
     "Products and Services" → product
     "Derivatives & Perpetual Futures" → product
     "Earn" → product
     "Copy Trading" → product
     "New Listings" → listing
     "Delisting" → listing
     "Maintenance and Upgrade" → other
   weex:
     "Futures events" → campaign
     "Spot events" → campaign
     "Reward distribution" → campaign
     "Latest updates" → other
   bingx:
     "Latest Promotions" → campaign
     "EventX" → campaign
     "Product Updates" → product
     "Spot Listing" → listing
     "Futures Listing" → listing
     "Innovation Listing" → listing
     "Delisting" → listing
     "Latest Announcements" → other
     "Asset Maintenance" → other
     "System Maintenance" → other
     "Funding Rate" → other
     "Crypto Scout" → other
   phemex:
     "Activities" → campaign
     "News" → other
     "Newsletter" → other
   lbank: null  # 无 per-item category，全部走标题关键词 + LLM
   zoomex:
     "Platform Events" → campaign
     "Exclusive Events" → campaign
     "New Product Announcement" → product
     "Platform Announcement" → other

5. 写 config/push_targets.yaml 模板（locale → webhook 映射）。

6. 写 config/push_rules.yaml 模板（推送/排除条件配置化）。

7. 写 CLAUDE.md：项目背景、约束、schema、源特征汇总（每源的接口类型 / 策略 / parser / 局限）、目录结构、Phase 规划。

8. 给 db 层写单测。

不要写任何 HTTP 请求代码。不要猜任何 API 地址。
```

---

## Phase 1｜数据源侦察（已完成）

Phase 1 及补充侦察已全部完成。产出物：
- 填充完毕的 config/sources.yaml
- tests/fixtures/ 下每个源的真实响应快照
- CLAUDE.md 中的数据源现状表

6 家全部可用（Lbank 有 10 条窗口限制但不阻塞）。直接进 Phase 2。

---

## Phase 2｜采集器 + 增量/变更检测

交付物：`src/collectors/*.py` + `src/parsers/*.py`
验收：连跑两次，第二次新增 0 条变更 0 条（幂等）；手动改 DB 里一条 content_hash 后重跑，能识别为「变更」

```
基于已完成的 Phase 1 侦察结论和 config/sources.yaml，实现采集层。
sources.yaml 里有完整的 endpoint、字段映射、策略、已知局限，请严格按其内容实现，不要猜测或修改 API 地址。

【架构】

1. 统一 Collector 基类（src/collectors/base.py）：
   fetch_list(since: Optional[datetime]) → List[RawItem]
   fetch_detail(item: RawItem) → RawAnnouncement（仅 detail_mode != inline 时）
   normalize(raw) → Announcement
   每个交易所一个子类，通过 sources.yaml 驱动。

2. 独立 Parser 模块（src/parsers/）：
   解析逻辑从 collector 中分离出来，方便单测。每种响应格式一个 parser：
   - zendesk.py：Bitunix + Weex 共用，标准 Zendesk JSON，最简单
   - nuxt_devalue.py：BingX 的 <script id="__NUXT_DATA__"> 解析。
     它是一个 JSON 数组，整数值是同数组内的索引引用（devalue 格式），
     可以直接 json.loads 再按下标取值，不需要第三方 devalue 库。
   - preloaded_data.py：Phemex 的 window.preloadedData 解析。
     这是 JS 对象字面量（key 不带引号、字符串用单引号），不是标准 JSON，
     不能直接 json.loads。用正则提取 `window.preloadedData = {...}` 后，
     可以用 demjson3 或写一个简单的预处理（加引号）再 json.loads。
   - rsc_stream.py：Lbank 的 Next.js RSC streaming 解析。
     数据嵌在 self.__next_f.push([...]) 调用的字符串参数里，是转义的 JSON。
     需要提取这些字符串、反转义、再 json.loads。这是最脆弱的 parser，
     Lbank 前端改版最容易导致它失效。
   - slate_json.py：Zoomex 的 Slate.js 富文本 JSON → 纯文本。
     content 字段格式如 [{"type":"paragraph","children":[{"text":"..."}]}]，
     需要递归遍历所有节点提取 text 值，拼接时保留段落换行。
     同时保留表格结构（如果有 type:"table" / type:"table-row" / type:"table-cell"），
     因为活动奖池信息经常在表格里。

【增量逻辑（两种模式，按 crawl_state.strategy 分派）】

watermark 模式（Bitunix, Weex, Zoomex）：
- 读 crawl_state 拿 high_watermark
- 拉取 update_time > watermark 的条目
  · Bitunix/Weex：?sort_by=updated_at&sort_order=desc，翻页直到遇到 ≤ watermark 的条目为止
  · Zoomex：POST getArticleListByMenuId，逐页拉取，按 gmtUpdatedAt 降序判断停止点。
    注意：Zoomex 每个 locale 有 3-4 个 menu_id（见 sources.yaml 的 categories），
    每个 menu_id 独立维护 crawl_state（PK 是 source + locale + menu_or_category）。
- 对拉回的每条：
  · uid 不存在 → status=new，入库
  · uid 存在且 content_hash 变了 → status=changed，旧版本写 content_history，更新主表
  · uid 存在且 content_hash 相同 → status=unchanged，只更新 fetched_at
- 跑完后把本轮最大 update_time 写回 crawl_state

full_scan 模式（BingX, Phemex, Lbank）：
- BingX：抓取首屏列表页（~20 条跨分区聚合），解析 __NUXT_DATA__。
  对每条新/变更的条目，再请求详情页 /support/articles/{articleId} 拿正文。
- Phemex：每个 locale × 每个分类（news/activities/newsletter）各抓一次列表页。
  详情已内嵌在 preloadedData 里，不需要二次请求。
  注意 EN 和 FR 各有 3 个子源，需要全部抓取。
- Lbank：抓取列表页（固定 10 条），解析 RSC streaming 拿到文章列表。
  对每一条（无论新旧）都请求 detail 页 /support/articles/{code} 拿 updateTime
  和完整正文做 content_hash 比对——因为列表页的 contentShowTime 与 detail 页的
  updateTime 最多差 22 分钟，不可靠。
  每个 locale 每次 = 1 次列表 + 10 次详情 = 11 次请求，按 500ms 间隔约 6 秒。
- 同样的三路判断（new / changed / unchanged）
- 不写回 high_watermark

【回填模式（--backfill 参数，仅 BingX 和 Phemex）】
- BingX：下载 en/sitemap-support.xml（7829 URL），枚举全部 /support/articles/{articleId} 详情页。
  VN 版本用 EN sitemap 的 articleId，把 URL /en/ 换成 /vi/。
  按 500ms 间隔，预计 ~65 分钟完成 EN。
- Phemex：下载 sitemap_output/sitemap_Announcement.xml（已验证覆盖全部 3 分类），
  逐条请求详情页。EN 3485 条 / FR 1834 条。sitemap 里每条 <url> 含 hreflang
  alternate links，可一次拿到跨语言 URL 映射。
  按 500ms 间隔，EN 约 29 分钟，FR 约 15 分钟。
- 回填与日常增量共用同一个 normalize → 入库流程，不需要单独的入库逻辑。
- 回填时 rate_limit_ms 可适当放宽到 300ms（但不低于 200ms），加 --rate-limit 参数。

【时间处理】
- Bitunix / Weex：ISO8601 UTC（Z 后缀），直接存
- BingX：ISO8601 带 +08:00 偏移，必须转 UTC 再存
- Phemex：publishedTime 是朴素 'YYYY-MM-DD HH:MM:SS'（按 UTC 处理）；createdAt/updatedAt 是 ISO8601 UTC
- Lbank：unix 毫秒，转 UTC ISO8601
- Zoomex：unix 毫秒，转 UTC ISO8601

【稳健性】
- 超时重试（指数退避，最多 3 次）
- 单源失败不影响其他源，失败写日志
- 请求间隔按 sources.yaml 的 rate_limit_ms
- 支持 --source 和 --locale 参数，可单独跑某一个源

【跨语言 group_id 在采集阶段的处理】
以下源在采集阶段就可以直接设 group_id（article_id 跨 locale 一致）：
- Bitunix：Zendesk id 跨 locale 一致 → group_id = "bitunix_{id}"
- Weex：同上 → group_id = "weex_{id}"
- BingX：articleId 跨 locale 一致 → group_id = "bingx_{articleId}"
- Lbank：noticeId 跨 locale 一致 → group_id = "lbank_{noticeId}"
- Zoomex：article.id 跨 locale 一致 → group_id = "zoomex_{article_id}"
以下源需要额外处理：
- Phemex：各 locale 的 id 不同，需要读详情页 i18n map（i18n.<locale> 给出其他语言版本的 id）做映射。
  策略：以 EN 版 id 为 group anchor，EN 详情页的 i18n.fr 给出 FR 版 id，
  group_id = "phemex_{en_id}"。如果先抓到 FR 版，暂时用 "phemex_{fr_id}"，
  等 EN 版入库后做一次 group_id 合并。

【单测】
每个 parser 必须有基于 tests/fixtures/ 的离线单测：
- 正常解析
- 字段缺失时不崩（graceful degradation）
- 时间格式转 UTC
- content_hash 一致性（同一输入产出相同 hash）

【开发顺序】
第一批：Bitunix + Weex（Zendesk，最简单，验证整个管道）
第二批：Zoomex（我方基线，Phase 4 的前置依赖）
第三批：Phemex + BingX
第四批：Lbank（最复杂最脆弱，最后做）
每批完成后我验收再继续下一批。
```

---

## Phase 3｜清洗、跨语言归组、分类打标

交付物：`src/pipeline/`
验收：随机抽 30 条人工核对分类准确率 ≥ 90%；同一活动的多语言版本 group_id 一致

```
实现清洗与打标流水线，作用于 announcements 表中 status in (new, changed) 的记录。

【1. 清洗】
- HTML → 纯文本（Bitunix/Weex/BingX/Phemex/Lbank 的 content 都是 HTML）
- Zoomex 的 content 已在 Phase 2 的 slate_json.py 中转为纯文本，这里不需要重复处理
- 去导航栏、页脚、免责声明等模板内容
- 压缩多余空白
- 保留正文里的表格结构（活动奖池信息经常在表格里，不能丢）
- 清洗后的纯文本回写 content 字段并更新 content_hash

【2. 跨语言归组（group_id）验证与补全】
Phase 2 采集阶段已为 5 个源直接设置了 group_id（article_id 跨 locale 一致）。
本阶段需要：
a) 验证 Phemex 的 i18n map 归组是否正确（Phase 2 的策略是用 EN id 做 anchor，FR 通过 i18n map 关联）
b) 对 Phase 2 中暂时没能归组的 Phemex FR 条目（先于 EN 版入库的），做一次扫描合并
c) 扫描全库，检查是否有相同 source + 不同 locale + 相同 article_id 但 group_id 不一致的异常

注意：group_id 的用途是分析层的跨区域对比和地区独占识别，不用于推送去重（推送按 locale 分群，各群独立）。

【3. 分类打标】
类别：campaign / product / listing / other

第一层——原生分类映射（读 config/category_mapping.yaml）：
- Bitunix：section_id → section name → 映射表
  注意 section_id 是数值，需要先转为人类可读名称（查 /categories/{id}/sections.json 或用 Phase 1 记录的对照表）
- Weex：section_id → section name → 映射表（同上）
- BingX：sectionId → 用 Phase 1 记录的 sectionId 对照表直接映射，无需额外请求
- Phemex：category 固定来自抓取时的子源（news/activities/newsletter），直接用
- Lbank：无 per-item category，跳过此层，直接进第二层
- Zoomex：category 固定来自抓取时的 menu_id，直接用

第二层——标题关键词匹配（适用于第一层映射到 other 或无映射的）：
listing 关键词：list / listing / launchpool / new coin / 上线 / 上架
delisting 关键词：delist / delisting / 下架 / 退市
campaign 关键词：competition / contest / trading / reward / bonus / airdrop / 活动 / 奖励
product 关键词：update / upgrade / launch / feature / 更新 / 升级 / 新功能
other 关键词：maintenance / system / upgrade / risk / 维护 / 风险

第三层——LLM 分类（仅第一二层都未命中时）：
- 输入：title + content 前 500 字
- 输出：JSON { "category": "...", "confidence": 0.0-1.0, "reason": "..." }
- 明确允许输出 other
- LLM 结果按 content_hash 缓存，同一内容不重复调用

预期命中率分布：第一层（原生分类）覆盖约 80%（除 Lbank），第二层（关键词）再覆盖 10-15%，第三层（LLM）兜底 5-10%。

【4. 地区独占标记】
扫描所有 group：
- 若某 group 只在非 EN 的单一 locale 出现 → is_region_exclusive = true
- 若某 group 只在 EN 出现而其他 locale 都没有 → 不标记（EN-only 是常态）
- Zoomex 的 Exclusive Events（menu_id=69，仅 EN-Asia 有数据）天然就是地区独占

【5. eval 脚本】
随机抽样 30 条，打印：source / locale / title / 原生 category / 我方 category / 分类层（rule_native / rule_keyword / llm）/ 依据，方便人工校验。
```

---

## Phase 4｜LLM 分析层（summary + ZMX 差异）

基于已完成的 Phase 3 pipeline，实现批次级 LLM 分析层。

【核心概念变更：分析单元是批次，不是单条公告】

每次采集运行结束后，把当日（因为这个流程每天跑一次，所以每日相当于是当轮批次下的所有内容） status IN (new, changed) 的公告按
(source, category, locale) 分组，每组作为一个分析批次，整组传入一次 LLM，
产出一行 insights 记录。原版「逐条公告两次调用」方案废弃，改为批次级单次调用。

批次 PK 设计：SHA256(source || "_" || category || "_" || locale || "_" || batch_date)
- 同一天同一批次重跑：追加新公告到 related_uids，用本日全量重新调 LLM，覆盖原记录
- 不同天的记录各自独立，不合并

【第一步：schema 迁移（migrate_v3.py）】

insights 表废弃旧版全部字段，替换为以下结构：

  CREATE TABLE insights (
    id                TEXT PRIMARY KEY,
    batch_date        TEXT NOT NULL,           -- UTC date, YYYY-MM-DD
    source            TEXT NOT NULL,
    category          TEXT NOT NULL CHECK(category IN
                        ('campaign','product','listing','delisting','other')),
    locale            TEXT NOT NULL,
    article_count     INTEGER NOT NULL DEFAULT 0,
    related_uids      TEXT NOT NULL DEFAULT '[]',  -- JSON array of uid strings
    is_locale_derived BOOLEAN NOT NULL DEFAULT 0,
    derived_from_id   TEXT REFERENCES insights(id),
    summary           TEXT,                    -- batch_summary 字段的 LLM 输出
    articles_analysis TEXT,                    -- JSON array，每篇公告的结构化分析
    zmx_diff          TEXT,                    -- zmx_comparison.analysis 的文字部分
    diff_type         TEXT CHECK(diff_type IN
                        ('ZMX已有','ZMX缺失','ZMX玩法不同','混合','不适用')),
    priority          TEXT CHECK(priority IN ('高','中','低')),
    zmx_evidence_uids TEXT NOT NULL DEFAULT '[]',  -- JSON array of Zoomex uid strings
    prompt_version    TEXT NOT NULL,
    llm_tokens_used   INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
  );

  CREATE INDEX idx_insights_batch ON insights(batch_date, source, category, locale);
  CREATE INDEX idx_insights_source_cat ON insights(source, category);

migrate_v3.py 沿用 Phase 2.5 建立的迁移惯例：
  建 insights_v3 → INSERT SELECT 能对上的旧列（id/source/category/created_at）→
  DROP insights → RENAME insights_v3 → insights。
旧数据量极少（只有开发态数据），旧字段大多对不上新结构，迁移只保留 PK 和时间戳，
其他字段全部 NULL，后续重跑自动补齐。

【第二步：Zoomex 基线索引（src/analysis/zmx_index.py）】

把 announcements 表里 source='Zoomex' 的近 90 天数据建成可检索的基线语料库。
采用轻量全文检索（这部分数据目前还在运行中）：

  build_index(conn, category, locale) -> ZmxIndex
    - 按 category + locale 过滤，只索引 content 非空的行
    - 对每条记录做简单 TF-IDF 权重计算（用 sklearn CountVectorizer + TfidfTransformer
      即可，或自己实现：词频 / log(文档数/含该词文档数)）
    - 返回可调用的索引对象

  search(query_text, top_k=5) -> List[ZmxArticle]
    - 把 batch 内所有公告的 title 拼接成 query（不是 content，title 更能代表主题）
    - 返回 top_k 条，附带 uid / title / content[:400] / post_time

  ZmxArticle: uid, title, content_preview, post_time, similarity_score

索引可能命中很少或为空。处理规则：
  - 命中 < 3 条时，如实传入（不补全），并在 prompt 里注明「基线数据有限」
  - 命中 0 条时，跳过 ZMX 差异分析，diff_type 强制填「不适用」，不调 LLM 的
    zmx_comparison 部分（减少 token 浪费）

【第三步：locale 复用判断（src/analysis/batch.py）】

在正式调用 LLM 之前，先检查是否可以复用 EN 批次的分析：

  can_derive_from_en(conn, source, category, locale, batch_date) -> Optional[str]
    # 返回 EN 批次的 insight id，如果满足复用条件；否则返回 None

  复用条件（同时满足）：
    1. locale != 'EN'（EN 自己不能复用自己）
    2. 当日同 source × category × EN 的 insights 行已存在
    3. 当前 locale 批次内所有公告的 group_id，都能在 EN 批次的 related_uids 里找到
       对应的 EN 版本（即没有 locale 独占条目）

  满足复用条件时：
    - 复制 EN 批次的 summary / articles_analysis / zmx_diff / diff_type /
      priority / zmx_evidence_uids / prompt_version
    - 设 is_locale_derived=true，derived_from_id 指向 EN 批次 id
    - llm_tokens_used=0（没有调 LLM）
    - related_uids 用本 locale 自己的 uid 列表（不是 EN 的）
    - articles_analysis 里的 uid 字段也替换成本 locale 对应的 uid

  不满足复用条件（批次内有 is_region_exclusive=true 的独占条目）时：
    - 正常调 LLM，只传本 locale 的独占条目 + Zoomex 同 locale 的基线

  注意：EN 批次必须在其他 locale 批次之前处理（run() 的循环顺序按 locale 排序，
  EN 排第一）

【第四步：LLM 调用（src/analysis/llm.py）】

模型：通过 .env 配置，支持 OpenAI / Anthropic / 任何 OpenAI 兼容接口。
参数从 config/analysis.yaml 读取（temperature=0，确保输出稳定；max_tokens 按
category 分别设定，campaign/product=2000，listing=1500，delisting=800）。

prompt_version 格式："{category}-v{N}"（如 "campaign-v1"），写在每套 prompt
的顶部注释里，改 prompt 必须递增版本号，便于追溯历史批次用的哪版。

输出格式：严格 JSON，不包含任何 markdown 代码块标记或前缀文字。
入库前校验：
  - JSON 解析失败 → 记日志，该批次 summary/articles_analysis/zmx_diff 全部填
    NULL，不重试（避免无限消耗 token），下次重跑会触发覆盖
  - diff_type 不在枚举值内 → 强制改为「不适用」并记日志
  - diff_type 不是「不适用」但 evidence_indices 为空数组 → 强制改为「不适用」
    并记日志（核心防幻觉校验）
  - articles 数组里的 uid 值不在本批次 related_uids 内 → 丢弃该条目并记日志

缓存：按 (SHA256(所有related_uids的content_hash拼接), prompt_version) 缓存。
同批次内容没变、prompt 版本没变时直接返回缓存，不调 LLM。
缓存存 SQLite 一张新表 llm_cache(cache_key TEXT PK, response TEXT, created_at TEXT)。

【第五步：四套 Prompt（按 category 分发）】

以下是每套 prompt 的完整文本，实现时按字面量使用，用 Python f-string 填入变量。
变量占位符格式：{VARIABLE_NAME}。

---

# prompt_version: campaign-v1

SYSTEM:
你是一名加密交易所竞品分析师，服务对象是运营团队。你的职责是分析竞品的活动公告，
提炼可操作的情报。输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。

USER:
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条（新增 {NEW_COUNT} 条，变更 {CHANGED_COUNT} 条）

【活动公告列表】
{ARTICLES_BLOCK}
（每条格式：
[{index}] UID: {uid}
标题：{title}
状态：{status}
正文：{content}
{如果 status=changed: 变更前正文：{old_content}}
）

【Zoomex 基线（同类目、同地区，近 90 天相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
（每条格式：[Z{index}] UID: {zmx_uid} | {post_time} | 标题：{title} | 摘要：{content_preview}）
{如果 ZMX_COUNT=0: 注意：当前 Zoomex 基线数据不足，无法进行差异判断，zmx_comparison 的 diff_type 必须填「不适用」。}

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
    "analysis": "具体叙述与 Zoomex 的差异。必须引用 [Z{index}] 编号（如「[Z2] 所示，Zoomex 在 2026-05 也举办过类似交易竞赛，但奖池规模（5,000 USDT）明显低于本批次」）。diff_type=「混合」时，逐条标注各篇公告的具体情况。无充分依据时填「基线数据不足，无法判断」，diff_type 同时改为「不适用」。",
    "evidence_indices": [整数数组，引用了哪几条 [Z{index}]，未引用任何基线时必须为空数组 []],
    "priority": "高 / 中 / 低。高：ZMX 缺失的高价值玩法或奖池规模显著高于 ZMX 同类活动；中：ZMX 已有类似玩法但规模/机制有差异；低：与 ZMX 高度雷同或信息量不足。",
    "priority_reason": "一句话说明定级依据，必须包含具体数字或事实，不接受「因为差异较大」这类空话。"
  }
}

【强制规则，违反时输出视为无效】
1. uid 字段原样照抄，不得改动任何字符
2. mechanics 里的数字必须来自正文，禁止出现「大量」「丰厚」「一定数量」等模糊词
3. evidence_indices 为空数组时，diff_type 只能是「不适用」
4. 整个输出必须是合法 JSON，不加任何注释（// 或 /* */ 均不允许）

---

# prompt_version: product-v1

SYSTEM:
你是一名加密交易所竞品分析师，服务对象是产品团队。你的职责是分析竞品的产品更新公告，
识别功能差距和迭代方向。输出必须是合法 JSON，不包含任何 markdown 标记或解释文字。

USER:
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条（新增 {NEW_COUNT} 条，变更 {CHANGED_COUNT} 条）

【产品更新公告列表】
{ARTICLES_BLOCK}

【Zoomex 基线（同类目、同地区，近 90 天相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
{如果 ZMX_COUNT=0: 注意：当前 Zoomex 基线数据不足，zmx_comparison 的 diff_type 必须填「不适用」。}

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
    "analysis": "Zoomex 是否有同类功能，功能成熟度和覆盖范围的对比。必须引用 [Z{index}] 编号。对于「ZMX缺失」的判断要保守：基线里没有搜到不等于 ZMX 真的没有，应表述为「基线中未见相关记录，建议人工复核」。",
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

---

# prompt_version: listing-v1

SYSTEM:
你是一名加密交易所竞品分析师，服务对象是运营和产品团队。你的职责是分析竞品的上币公告，
识别竞品的上币策略和潜在的 ZMX 上币机会。输出必须是合法 JSON。

USER:
【本批次信息】
竞品：{SOURCE}
地区/语言：{LOCALE}
日期：{BATCH_DATE}
公告数：{ARTICLE_COUNT} 条

【上币公告列表】
{ARTICLES_BLOCK}

【Zoomex 基线（近 90 天上币记录，相关度最高的 {ZMX_COUNT} 条）】
{ZMX_BLOCK}
{如果 ZMX_COUNT=0: 注意：Zoomex 上币基线数据不足，diff_type 必须填「不适用」。}

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
    "analysis": "逐一说明本批次各代币在 Zoomex 基线中的情况。对每个代币：基线中有记录则标注 [Z{index}] 引用；基线中无记录则表述为「基线中未见 {token_symbol} 上币记录」（不得直接断言 ZMX 没有上线，因为 Zoomex 全量数据尚未入库）。",
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

---

# prompt_version: delisting-v1

SYSTEM:
你是一名加密交易所竞品分析师。你的职责是分析竞品的下架公告，提取关键信息供运营团队
参考和风险预警。输出必须是合法 JSON。

USER:
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

---

【第六步：批次编排（src/analysis/run.py）】
实际调用 weex collector，跑一下获取今日完整数据，然后对其和目前本地的zoomex数据进行必要的前置处理（地区，category）后进行测试
run(conn, batch_date=None, sources=None, dry_run=False):
  - batch_date 默认今日 UTC date
  - 查询 announcements 表：status IN ('new','changed') AND
    date(fetched_at) = batch_date AND source IN sources AND category != 'other'
  - 按 (source, category, locale) 分组，locale 排序 EN 排第一
  - 对每个批次：
    1. 检查 can_derive_from_en() → 可复用则直接写入，跳过 LLM
    2. 构建 ZmxIndex，search 取 top5
    3. 检查缓存
    4. 构建 ARTICLES_BLOCK（changed 条目附旧版本：从 content_history 取最近一条）
    5. 调 LLM，解析 JSON，做入库前校验
    6. upsert insights 行（已存在则更新 updated_at + 所有分析字段）
  - dry_run=True 时打印每个批次的 token 预估和 prompt 预览，不调 LLM，不写库

  CLI：python -m src.analysis [--date YYYY-MM-DD] [--source Bitunix,Weex]
       [--category campaign] [--dry-run]

【第七步：单测】

tests/analysis/ 目录，离线测试（mock LLM 返回）：
  - test_zmx_index.py：TF-IDF 检索按 category 过滤、空基线处理
  - test_batch.py：locale 复用判断（满足/不满足条件各一个场景）、
    批次 PK 生成幂等
  - test_llm.py：JSON 解析失败处理、evidence_indices 空数组强制改「不适用」、
    uid 字段不在 related_uids 内时丢弃并记日志
  - test_run.py：dry_run 模式不写库、category=other 被跳过


---

## Phase 5｜飞书多维表同步

交付物：`src/sinks/feishu_bitable.py`
验收：重复跑 3 次，多维表不产生重复行

```
实现 SQLite → 飞书多维表的同步层（只写不读，SQLite 仍是唯一真相源）。

两张目标表：内容表、汇总分析表。字段与 CLAUDE.md 中定义的一致。

【要求】
1. 幂等：以 uid（内容表）/ insight_id（分析表）作为业务主键。
   同步前先按主键查飞书表：不存在 → 新建；存在且内容有变 → 更新；无变化 → 跳过。
   每次操作写 sync_log。

2. 批量写入 + 频控：飞书 API 约 100 次/分钟 QPS 限制，做限流。
   失败重试（最多 3 次，指数退避），部分失败不中断整批。

3. status=changed 的记录更新原有行，不新增。

4. --dry-run 模式：打印将要写入的内容，不调飞书 API。

5. 凭证：app_id / app_secret 从 .env，tenant_access_token 自动刷新。

6. --table 参数：可单独同步内容表或分析表。

先只跑内容表，验收后再接分析表。
```

---

## Phase 6｜推送规则引擎 + 飞书群日报

交付物：`src/sinks/feishu_bot.py` + `src/pipeline/push_rules.py`
验收：同一条公告对同一个群不会被推两次；各 locale 群只收到对应语言的内容

```
实现推送决策与飞书群机器人日报。

【推送目标】
按 locale 分群，config/push_targets.yaml：
  EN:
    webhook: ${WEBHOOK_EN}
  FR:
    webhook: ${WEBHOOK_FR}
  VN:
    webhook: ${WEBHOOK_VN}
  ID:
    webhook: ${WEBHOOK_ID}
  EN-Asia:
    webhook: ${WEBHOOK_EN_ASIA}
每个群只收到对应 locale 的公告，各群独立，不做跨语言去重。

【推送规则引擎（从 config/push_rules.yaml 加载，不写死在代码里）】

推送条件（满足任一即推）：
- status=new AND category=campaign
- status=changed AND (diff 涉及规则或奖励)
- status=new AND diff_type=ZMX缺失 AND priority=高
- is_region_exclusive=true

排除条件（优先于推送条件）：
- push_status=pushed（对该 locale 群已推过）
- diff_type=ZMX已有
- category=other

规则引擎支持后续新增规则，只改 YAML 不改代码。

【日报消息格式（飞书 interactive card）】
每个 locale 群各一份日报：

【竞品日报 - {locale}】
📅 日期：YYYY-MM-DD
📈 今日新增：N 条 ｜ 🔄 内容变更：M 条 ｜ 🔥 重点：K 条

【活动 Campaign】
• Bitunix 新增 Trading Competition → ZMX 缺失
• Weex 更新奖池：10万→50万 USDT

【上币 Listing】
• Lbank 上线 XXX/USDT

【产品 Product】
• BingX 更新跟单规则

👉 完整看板 [链接]
👉 历史数据 [多维表链接]

某类别当日为空则不显示。全部为空则推"今日无重点动态"。

【要求】
- 飞书 interactive card，不是纯文本
- 推送成功后回写 push_status（按 locale 维度）
- --dry-run 打印卡片 JSON 和目标群
- webhook URL 从环境变量读取
```

---

## Phase 7｜可视化看板

交付物：`src/dashboard/`，静态 HTML
验收：本地浏览器打开能正常筛选展示

```
做一个可视化看板（单文件 HTML + 内嵌 JS/CSS，读取从 SQLite 导出的 JSON，不需要后端服务）。

【顶层结构】
最顶部：locale tab 切换器（EN / FR / VN / ID / EN-Asia / 全局视角）
切换 tab 后下面所有模块只展示对应 locale 的数据。

【各模块（每个 locale tab 下）】

① 今日概览（Overview）
6 个数字卡片：今日抓取数 / 新增数 / 变更数 / 推送数 / 覆盖竞品数 / 覆盖数据源数

② 今日重点（Highlights）
当日 priority=高 的 insight 列表：竞品名 + 标题 + diff_type + 一句话 summary
ZMX缺失高亮（红色标签）。

③ CEX 动态
按 campaign / product / listing 三个子板块。
每条：竞品名 + 标题 + 时间 + diff_type 标签。
支持按竞品名筛选。

④ 行业热点（占位）
"Phase 2 建设中"占位卡片。

⑤ 今日 Insights（规则模板，不用 LLM）
"今日共监测到 {total} 条竞品动态，其中新增活动 {campaigns} 个，内容变更 {changes} 个，重点 {highlights} 个。{most_active} 本周最活跃。"

【"全局视角" tab】
- 跨区域活动对比表：每个 group 出现在哪些 locale，地区独占的标记
- 各竞品活跃度排名（近 7 天公告数）
- ZMX 缺失汇总：按 category 分类列出所有 diff_type=ZMX缺失

【数据源】
export_dashboard_data.py：从 SQLite 导出当日 JSON。
每日跑批先 export 再生成看板。

【设计】
信息密度高、干净、可筛选。ZMX缺失一眼能看出来。
```

---

## Phase 8｜调度与监控

交付物：调度脚本 + 监控告警
验收：模拟一个源失败能收到告警；模拟连续 3 天 0 新增能收到静默告警

```
接调度和监控，先用 cron，不引入 Airflow。

【每日流程 scripts/run_daily.sh】
1. 采集（python -m src.collectors.run --all）
2. 清洗打标（python -m src.pipeline.run）
3. LLM 分析（python -m src.analysis.run）
4. 导出看板数据（python -m src.dashboard.export）
5. 同步飞书多维表（python -m src.sinks.bitable --all）
6. 推送飞书日报（python -m src.sinks.bot --all-locales）

退出码规则：
- 步骤 1 部分源失败（退出码 1）→ 继续后续步骤 + 发告警
- 步骤 1 全部失败（退出码 2）→ 中止 + 发告警
- 步骤 2-6 失败 → 中止 + 发告警
- 每步均可独立重跑（幂等）

【监控告警（发到运维飞书群）】

实时告警：任何步骤失败 → 步骤名 + 失败源 + 错误摘要 + 日志路径

静默失败监控（每日跑批结束后检查）：
- 某 source×locale 连续 3 天新增 0 条 → 告警
  "⚠️ {source} {locale} 连续 3 天无新增，可能对方改版导致 parser 失效"
- Lbank 专项：单次拉取 10 条全部是新条目 → 告警
  "⚠️ Lbank {locale} 本次 10 条全为新增，可能有更多条目被挤出窗口"

【日志】
data/logs/YYYY-MM-DD.log，保留 30 天，自动清理。

【cron】
0 2 * * * cd /path/to/project && bash scripts/run_daily.sh >> data/logs/cron.log 2>&1
```

---

以上是完整更新版。三个待定决策（历史回填是否做、Delisting 是否拆类、分类映射表确认）我已经在对应的 Phase prompt 里用当前最合理的默认值先写进去了，不阻塞开发。你随时可以改，改了告诉我一声我帮你更新对应的 prompt。