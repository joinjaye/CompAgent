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

交付物：`src/analysis/`，写入 insights 表
验收：每条 insight 的 related_uids 都能链回真实 uid；diff_type 不出现无依据的「ZMX已有」

```
实现分析层，产出汇总分析表的核心字段。

【前置：建立 Zoomex 基线】
Zoomex 现在已经完全可用（Phase 2 已接入 api2.zoomex.com 的 3 个接口），把 Zoomex 的 announcements 按 category 索引（近 90 天），作为对比语料库。

Zoomex 的分类结构：
- Platform Announcement（menu_id=26）→ 综合公告，对应 other 为主
- New Product Announcement（menu_id=123）→ product
- Platform Events（menu_id=145）→ campaign
- Exclusive Events（menu_id=69，仅 EN-Asia）→ campaign

索引方式：关键词索引 + TF-IDF 向量（scikit-learn 即可，不引入重型向量库），
支持按 category 过滤后检索 Top5 相似公告。

【任务 A：特点/玩法 summary】
对每个 category in (campaign, product, listing) 的公告组（按 group 维度），用 LLM 抽取结构化信息：
- 玩法机制（一句话）
- 参与门槛
- 奖励形式与规模（数字必须从原文抽取）
- 时间窗口
- 目标用户群
然后生成 2-3 句中文 summary。

【任务 B：ZMX 差异分析】
把该公告的 title + summary 作为 query，在 Zoomex 基线中按同 category 检索 Top5。
连同检索结果一起传给 LLM，要求输出：
- diff_type: ZMX已有 / ZMX缺失 / ZMX玩法不同 / 不适用
- zmx_diff: 具体差异描述
- priority: 高 / 中 / 低
- evidence_uids: Zoomex 公告 uid 列表

【硬性约束（防幻觉）】
- LLM 只能基于传入的 Zoomex 基线语料判断。Top5 检索不足以判断时，必须输出 diff_type="不适用"。严禁编造。
- 判断依据必须引用具体 Zoomex 公告 uid。无法引用则 diff_type 只能是"不适用"或"ZMX缺失"。
- status=changed 的公告：把 content_history 旧版本和新版本一起传入，让 LLM 输出"改了什么"。

【成本控制】
- 所有 LLM 调用按 (content_hash, prompt_version) 缓存
- 记录每次调用的 token 数和成本到日志
```

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