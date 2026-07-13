
---

# Phase Prompts（完整更新版）

以下每个 Phase 独立开一个 CC session。上一个 Phase 结束后确保 CLAUDE.md 已更新，下个 session 自然会读到。

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
- Phemex: EN, FR
- Lbank: EN, VN, ID
- Zoomex: EN, FR, EN-Asia, VN, ID ← 我方，作为对比基线，不是竞品

【核心设计约束，必须遵守】
1. SQLite 是唯一真相源，飞书多维表只是同步出去的业务视图。所有重跑/补数只操作 SQLite。
2. 两种增量采集策略：
   a) watermark 模式（默认）：按源端 update_time 做水位线，只拉 update_time > high_watermark 的内容。适用于 API 返回可靠 update_time 的源。
   b) full_scan 模式（fallback）：拉前 N 页，用 content_hash 检测变更。适用于无 update_time 或该字段不可靠的源。
   两种模式都保留 content_hash 校验。同一 URL 的正文若 hash 变了，识别为「变更」，旧版本存入 content_history。
3. 跨语言归组（group_id）：同一竞品同一条公告的多语言版本归为一组。归组用于分析（跨区域对比、地区独占识别），不用于推送去重。
4. 推送按 locale 分群，每个 locale 对应一个飞书群，各群独立推送，不做跨语言去重。
5. 合规：遵守 robots.txt、控制请求频率、不绕过登录墙。

【本次任务】
1. 生成项目目录结构：
   src/db, src/collectors, src/pipeline, src/analysis, src/sinks, src/dashboard
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
     source TEXT, locale TEXT,
     high_watermark TEXT（上轮最大 update_time）,
     strategy TEXT（watermark/full_scan）,
     updated_at TEXT,
     PRIMARY KEY (source, locale)

   - sync_log（飞书同步日志）：
     id INTEGER PK AUTOINCREMENT,
     target TEXT（bitable/bot_EN/bot_FR/...）,
     record_id TEXT, action TEXT（create/update/skip）,
     status TEXT（success/failed）, error TEXT, synced_at TEXT

3. 写 config/sources.yaml 模板（每个交易所 × 每个 locale 一个条目）。字段：
   endpoint, method, headers, locale_param, pagination, rate_limit_ms, detail_mode（inline/separate_api/html）, has_update_time（待 Phase 1 填）, field_mapping（article_id/title/content/post_time/update_time/category 对应的 response key，待填）
   值先全部留空或占位。

4. 写 config/push_targets.yaml 模板：
   locale → webhook_url 映射（EN/FR/VN/ID/EN-Asia）

5. 写 config/push_rules.yaml 模板：
   把推送规则配置化（推送条件列表 + 排除条件列表），不写死在代码里。

6. 写 CLAUDE.md，包含：项目背景、上述所有约束、schema 说明、目录结构、Phase 0-8 的规划摘要。

7. 给 db 层写单测：建库、插入、去重、变更检测、水位线读写。

不要写任何 HTTP 请求代码。不要猜任何 API 地址。
```

---

## Phase 1｜数据源侦察

交付物：填满的 sources.yaml + 每个源一份真实响应 fixture
验收：`python -m src.probe --all` 对所有通了的源能返回 ≥1 条真实公告，受阻的源有明确记录

```
这个 session 只做一件事：把 6 家交易所公告中心的真实数据源摸清楚，填进 config/sources.yaml。不写正式采集器。

【铁律】
- 严禁凭记忆或猜测写 API 地址。每一个 endpoint 都必须用 curl 实际请求成功、拿到真实 JSON/HTML 后才能写进配置。
- 每摸清一个源，把一份真实响应存到 tests/fixtures/{exchange}_{locale}.json（或 .html），后续所有 parser 单测都基于 fixture 离线跑，不依赖网络。
- 如果某个源没有 JSON API，只有 HTML 页面，如实记录，标记 detail_mode: html，不要伪造 API。
- 如果某个源请求失败（403 / 需要 JS 渲染 / Cloudflare），如实记录 blocked 原因和尝试过的方法，不要跳过或假装成功。

【目标源】
Bitunix(EN/FR/ID), Weex(EN/FR), BingX(EN/VN), Phemex(EN/FR), Lbank(EN/VN/ID), Zoomex(EN/FR/EN-Asia/VN/ID)

【每个源需要产出】
1. 公告列表接口：URL、method、必要 header、locale 怎么传（path/query param/header）、分页参数名与机制（offset/page/cursor）、单页条数上限
2. 详情内容获取：列表接口是否已含正文？若否，详情接口或详情页 URL 模式是什么
3. 字段映射：article_id / title / content / post_time / update_time / category 分别对应响应里的哪个 key
4. update_time 可靠性验证（关键！）：
   - 是否存在 update_time（或 updated_at / modify_time 等）字段？
   - 随机查看 5-10 条公告，update_time 与 create_time 是否全部相同？如果全部相同，说明该字段不可靠，标记 strategy: full_scan
   - 如果存在且有差异，标记 strategy: watermark
5. 该站自带的公告分类体系（category / catalogId / type），列出所有值
6. 时间字段格式与时区（Unix 时间戳 ms/s？ISO 字符串？什么时区？）
7. 速率限制观察：连续请求 10 次，间隔 500ms，是否被限流

【工作方式】
一次只搞定一家，搞定一家 commit 一次，更新 sources.yaml 和 CLAUDE.md 里的「数据源现状表」。
先做 Zoomex（因为是我方基线），再做竞品。

最后给我一份汇总表：
| 交易所 | locale | 状态 | 策略 | 备注 |
通了的标 ✅ watermark 或 ✅ full_scan，受阻的标 ❌ 并说明原因和建议方案。
```

---

## Phase 2｜采集器 + 增量/变更检测

交付物：`src/collectors/*.py`，每家一个 adapter
验收：连跑两次，第二次新增 0 条、变更 0 条（幂等）；手动改 DB 里一条 content_hash 后重跑，能识别为「变更」；watermark 模式下手动把 high_watermark 调早 1 天，能拉到增量

```
基于 Phase 1 的 sources.yaml 和 fixtures，实现采集层。

【要求】

1. 统一 Collector 基类：
   fetch_list(since: Optional[datetime]) → List[RawItem]
   fetch_detail(item: RawItem) → RawAnnouncement（仅 detail_mode != inline 时需要）
   normalize(raw: RawAnnouncement) → Announcement
   每个交易所一个子类，通过 sources.yaml 驱动。

2. 增量逻辑（两种模式，按 crawl_state.strategy 分派）：

   watermark 模式：
   - 读 crawl_state 拿到 high_watermark
   - fetch_list(since=high_watermark)，拉取 update_time > watermark 的条目
   - 对拉回的每条：
     · uid 不存在 → status=new，入库
     · uid 存在且 content_hash 变了 → status=changed，旧版本写 content_history，更新主表
     · uid 存在且 content_hash 相同 → status=unchanged，只更新 fetched_at
   - 跑完后把本轮最大 update_time 写回 crawl_state.high_watermark

   full_scan 模式：
   - 拉前 N 页（N 可配置，默认 3）
   - 同样的三路判断（new / changed / unchanged）
   - 不更新 high_watermark

3. 稳健性：
   - 超时重试（指数退避，最多 3 次）
   - 单源失败不影响其他源，失败信息写日志
   - 请求间隔可配置（sources.yaml 里的 rate_limit_ms）
   - 支持 --source 和 --locale 参数，可以单独跑某一个源

4. 每个 parser 必须有基于 fixture 的离线单测，测试：
   - 正常解析
   - 字段缺失时不崩（graceful degradation）
   - 时间格式转 UTC

5. 所有时间统一转 UTC 存储。

先实现 Zoomex 和 Bitunix 两家，我验收后再补其余四家。
```

---

## Phase 3｜清洗、跨语言归组、分类打标

交付物：`src/pipeline/`
验收：随机抽 30 条人工核对，分类准确率 ≥ 90%；同一活动的多语言版本 group_id 一致

```
实现清洗与打标流水线，作用于 announcements 表中 status in (new, changed) 的记录。

【1. 清洗】
- HTML 转纯文本（保留段落结构）
- 去导航栏、页脚、免责声明等模板内容
- 压缩多余空白
- 保留正文里的表格结构（活动奖池信息经常在表格里，丢了等于丢了核心数据）

【2. 跨语言归组（group_id）】
同一交易所、不同 locale 的同一条公告归为一组。按优先级尝试：
a) 该站自身的 article_id 在多语言下是否相同（Phase 1 探测结果应已记录在 sources.yaml）—— 若相同直接用
b) 若不同，匹配条件：
   · post_time 在 ±24h 内
   · 标题语义相似（翻译后比较，或用数字特征：金额、币种、日期）
   · 正文中的关键数字特征一致（奖池金额、活动时间）
归组结果写 group_id。
注意：group_id 的用途是分析层的跨区域对比和地区独占识别，不用于推送去重（推送按 locale 分群，各群独立）。

【3. 分类打标】
类别：campaign / product / listing / other
- 第一层：规则匹配
  · Phase 1 记录的各站原生 category 映射（比如某站 type=3 就是 listing）
  · 标题关键词匹配（listing / launchpool / new coin / competition / trading contest / maintenance / system upgrade ...）
- 第二层：规则命中不了的，走 LLM 分类
  · 输入：title + content 前 500 字
  · 输出：JSON { "category": "...", "confidence": 0.0-1.0, "reason": "..." }
  · 明确允许输出 other，不要为了填满三个类而强行归类
  · LLM 结果按 content_hash 缓存，同一内容不重复调用

【4. 地区独占标记】
扫描所有 group：若某 group 只在非 EN 的单一 locale 出现（比如只有 VN 版），标 is_region_exclusive=true。
这是重要情报——竞品只在某个区域做的活动，说明该区域是其重点。

【5. eval 脚本】
输出一个 eval 脚本：随机抽样 30 条，打印 title / 原生 category / 我方 category / 分类依据（规则 or LLM + reason），方便人工校验。
```

---

## Phase 4｜LLM 分析层（summary + ZMX 差异）

交付物：`src/analysis/`，写入 insights 表
验收：每条 insight 的 related_uids 都能链回真实 uid；diff_type 不出现无依据的「ZMX已有」

```
实现分析层，产出汇总分析表的核心字段。

【前置：建立 Zoomex 基线】
把 Zoomex 自己的 announcements 按 category 索引好（近 90 天），作为对比语料库。
索引方式：关键词索引 + 简单的 TF-IDF 向量（用 scikit-learn 即可，不要引入重型向量库），支持按 category 过滤后检索 Top5 相似公告。

【任务 A：特点/玩法 summary】
对每个 category in (campaign, product, listing) 的公告组（按 group 维度，不是单语言条目），用 LLM 抽取结构化信息：
- 玩法机制（一句话）
- 参与门槛
- 奖励形式与规模（数字必须从原文抽取）
- 时间窗口
- 目标用户群
然后生成 2-3 句中文 summary 写入 insights.summary。

【任务 B：ZMX 差异分析】
把该公告的 title + summary 作为 query，在 Zoomex 基线语料中按同 category 检索 Top5。
连同检索结果一起传给 LLM，要求输出：
- diff_type: ZMX已有 / ZMX缺失 / ZMX玩法不同 / 不适用
- zmx_diff: 具体差异描述
  · 若"ZMX玩法不同"：必须指出差异点（如"竞品奖池 50 万 USDT vs ZMX 10 万"）
  · 若"ZMX缺失"：说明竞品在做什么我们没做
  · 若"ZMX已有"：指出对应的 Zoomex 公告
- priority: 高 / 中 / 低
- evidence_uids: 判断依据指向的 Zoomex 公告 uid 列表

【硬性约束（防幻觉）】
- LLM 只能基于传入的 Zoomex 基线语料判断。如果 Top5 检索结果不足以判断，必须输出 diff_type="不适用"，reason 说明"基线中无相关内容"。严禁编造"Zoomex 已有类似活动"。
- prompt 明确要求：判断依据必须引用具体 Zoomex 公告 uid，写入 related_uids。如果无法引用，diff_type 只能是"不适用"或"ZMX缺失"。
- status=changed 的公告：把 content_history 里的旧版本和新版本一起传入，让 LLM 直接输出"改了什么"（此信息直接服务于推送规则里的"活动规则变化/奖励变化"判断）。

【成本控制】
- 所有 LLM 调用按 (content_hash, prompt_version) 做缓存，同一内容 + 同一 prompt 版本不重复调用
- 记录每次调用的 token 数和成本到日志表
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
   同步前先按主键查询飞书表：
   - 不存在 → 新建行
   - 存在且内容有变（status=changed）→ 更新该行
   - 存在且无变化 → 跳过
   每次操作结果写 sync_log。

2. 批量写入 + 频控：
   - 飞书 API 有 QPS 限制（约 100 次/分钟），做限流控制
   - 失败重试（最多 3 次，指数退避）
   - 部分行失败不中断整批，失败行记录到 sync_log

3. status=changed 的记录：更新飞书原有行，不是新增行；在"状态"字段体现"已变更"。

4. --dry-run 模式：只打印将要写入的内容（包含操作类型、行数），不实际调飞书 API。

5. 凭证管理：
   - app_id / app_secret 从 .env 读取
   - tenant_access_token 自动获取和刷新（有效期 2 小时）
   - 不硬编码任何凭证

6. 提供 --table 参数，可以单独同步内容表或分析表。

先只跑内容表，我验收后再接汇总分析表。
```

---

## Phase 6｜推送规则引擎 + 飞书群日报

交付物：`src/sinks/feishu_bot.py` + `src/pipeline/push_rules.py`
验收：同一条公告对同一个群不会被推两次；各 locale 群只收到对应语言的内容

```
实现推送决策与飞书群机器人日报。

【推送目标架构】
不是单一群，而是按 locale 分群：
config/push_targets.yaml:
  EN:
    webhook: ${WEBHOOK_EN}
    name: "竞品情报-EN"
  FR:
    webhook: ${WEBHOOK_FR}
    name: "竞品情报-FR"
  VN:
    webhook: ${WEBHOOK_VN}
    name: "竞品情报-VN"
  ID:
    webhook: ${WEBHOOK_ID}
    name: "竞品情报-ID"
  EN-Asia:
    webhook: ${WEBHOOK_EN_ASIA}
    name: "竞品情报-EN-Asia"

每个群只收到对应 locale 的公告。各群独立，不做跨语言去重。

【推送规则引擎（从 config/push_rules.yaml 加载，不写死在代码里）】

推送条件（满足任一即推）：
- status=new AND category=campaign → 推送
- status=changed AND (diff 涉及规则或奖励字段) → 推送
- status=new AND diff_type=ZMX缺失 AND priority=高 → 推送
- is_region_exclusive=true → 推送

排除条件（优先于推送条件）：
- push_status=pushed（对该 locale 群已推过）→ 不推
- diff_type=ZMX已有 → 不推
- category=other → 不推

规则引擎要支持后续新增规则，只改 YAML 不改代码。

【日报消息格式（飞书 interactive card）】
每个 locale 群各生成一份日报，内容只含该 locale 的公告：

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

（如某类别当日为空，该板块不显示。）
（如当日全部为空，推送"今日无重点动态"，不静默。）

【要求】
- 使用飞书 interactive card 格式，不是纯文本
- 推送成功后回写 push_status（按 locale 维度），保证同一条公告对同一个群不重复推
- --dry-run 模式：打印卡片 JSON 和目标群，不实际发送
- webhook URL 从环境变量读取（push_targets.yaml 里用 ${VAR} 引用）
```

---

## Phase 7｜可视化看板

交付物：`src/dashboard/`，产出静态 HTML 文件
验收：本地浏览器打开能正常筛选和展示

```
做一个可视化看板（单文件 HTML + 内嵌 JS/CSS，读取从 SQLite 导出的 JSON 数据文件，不需要后端服务）。

【顶层结构】
最顶部：locale tab 切换器（EN / FR / VN / ID / EN-Asia / 全局视角）
切换 tab 后下面所有模块只展示对应 locale 的数据。
"全局视角" tab 是特殊视角：展示跨区域对比（哪些活动是多区域统一推的、哪些是地区独占的）。

【各模块（每个 locale tab 下）】

① 今日概览（Overview）
6 个数字卡片：今日抓取数 / 新增数 / 变更数 / 推送数 / 覆盖竞品数 / 覆盖数据源数

② 今日重点（Highlights）
展示当日 priority=高 的 insight 列表：竞品名 + 标题 + diff_type + 一句话 summary
ZMX缺失的高亮显示（如红色标签），这是运营最关心的。

③ CEX 动态
按 campaign / product / listing 三个子板块展示。
每条：竞品名 + 标题 + 时间 + diff_type 标签
支持按竞品名筛选。

④ 行业热点（占位）
显示"Phase 2 建设中"占位卡片，预留区域。

⑤ 今日 Insights（规则模板生成，不用 LLM）
一段自动生成的文字：
"今日共监测到 {total} 条竞品动态，其中新增活动 {campaigns} 个，内容变更 {changes} 个，重点 {highlights} 个。{most_active} 本周最活跃。"

【"全局视角" tab 的特殊内容】
- 跨区域活动对比表：列出每个 group，标注它出现在哪些 locale，地区独占的单独标记
- 各竞品活跃度排名（按近 7 天公告数）
- ZMX 缺失汇总：按 category 分类，列出所有 diff_type=ZMX缺失 的条目

【数据源】
写一个 export_dashboard_data.py 脚本，从 SQLite 导出当日所需的 JSON 文件。
看板 HTML 启动时加载该 JSON。
每日跑批时先跑 export 再生成看板。

【设计要求】
信息密度高、干净、可筛选。ZMX缺失项目要一眼能看出来。
```

---

## Phase 8｜调度与监控

交付物：调度脚本 + 监控告警
验收：模拟一个源失败，能收到告警；连续 3 天某源 0 新增，能收到静默告警

```
接调度编排和监控，先用 cron 或 GitHub Actions，不要引入 Airflow。

【每日流程编排】
scripts/run_daily.sh，按顺序执行：
1. 采集（python -m src.collectors.run --all）
2. 清洗打标（python -m src.pipeline.run）
3. LLM 分析（python -m src.analysis.run）
4. 导出看板数据（python -m src.dashboard.export）
5. 同步飞书多维表（python -m src.sinks.bitable --all）
6. 推送飞书日报（python -m src.sinks.bot --all-locales）

每步的退出码决定是否继续：
- 步骤 1 部分源失败（退出码 1）→ 继续后续步骤（已成功的源数据照常处理），但发告警
- 步骤 1 全部失败（退出码 2）→ 中止，发告警
- 步骤 2-6 失败 → 中止，发告警
- 每步均可独立重跑（幂等），不需要从头开始

【监控告警】
发送到指定的运维飞书群（单独的 webhook，写在 .env 里）。

实时告警：
- 任何步骤失败：告警内容包含 步骤名 + 失败源 + 错误摘要 + 日志路径

静默失败监控：
- 每日跑批结束后，检查 crawl_state：若某 source×locale 连续 3 天 新增 0 条 → 告警
  "⚠️ {source} {locale} 连续 3 天无新增，可能对方改版导致 parser 失效，请检查"
- 这是最重要的监控，因为 parser 挂了不会报错，只会静默返回空结果

【日志】
- 每日跑批日志写到 data/logs/YYYY-MM-DD.log
- 保留最近 30 天日志，自动清理更早的

【cron 示例（写在 README 里）】
# 每天 UTC 02:00（北京时间 10:00）执行
0 2 * * * cd /path/to/project && bash scripts/run_daily.sh >> data/logs/cron.log 2>&1
```