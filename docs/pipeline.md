# 处理与分析流程说明

本文档整理竞品情报平台从采集到展示/推送的**完整处理逻辑**，按数据实际流经的顺序
组织，每一节说明"这一步做什么、为什么这么做、关键取舍是什么"。

**跟其它文档的分工**：
- `CLAUDE.md`：项目状态速览 + schema 定义 + Phase 进度，每个 session 必读，但
  偏"现状快照"，不是流程讲解。
- `docs/history.md`：按时间顺序的完整调试/修复日志，记录"某天发现了什么问题、
  怎么修的"，偏事件记录，不是结构化参考。
- **本文档**：不按时间顺序，按数据流顺序，回答"这一步的逻辑是什么"，供新开
  session 或需要理解某一环节时查阅。如果本文档跟代码实际行为不一致，以代码为准
  ——本文档会随流程变化更新，但更新可能滞后。

---

## 0. 总览

```
竞品公告 API / HTML（6 家源，见下）
       │
       ▼
┌──────────────────┐
│ 1. 采集 Collectors │  watermark / full_scan 两种增量策略 + 清洗（HTML→纯文本）
└──────────────────┘
       │  upsert_announcement()（去重/变更检测/历史归档）
       ▼
┌──────────────────┐
│   SQLite（唯一真相源）│  announcements / content_history / crawl_state
└──────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ 2. Pipeline 后处理（src/pipeline/）          │
│    grouping 校验 → dedup 去重 → category 分类 │
│    → region 地区独占标记                     │
└──────────────────────────────────────────┘
       │
       ├─────────────────────────────┐
       ▼                             ▼
┌──────────────────────┐   ┌──────────────────────────┐
│ 3a. Zoomex 能力目录     │   │ 3b. 竞品 LLM 分析（staged-v1） │
│ zmx_catalog.py         │   │ Stage1 事实抽取 → Stage2 确定性 │
│ extract（结构化提取）    │◄──┤ 召回 → Stage3 业务判断         │
│ rollup（按 mechanism_   │   │（campaign/product）           │
│ type 汇总 exists_flag） │   │ + listing/delisting 轻量分类   │
└──────────────────────┘   └──────────────────────────┘
       │                             │
       └──────────────┬──────────────┘
                       ▼
              insights 表（批次级，含 EN→其它 locale 复用）
                       │
                       ▼
┌──────────────────────────────────────────┐
│ 4. 当日 LLM Summary                         │
│    Overview + Campaign + Product（各 2～4 句）│
└──────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────┐
│ 5. Dashboard 导出 + 产物验收                │
│    SQLite 快照 → docs/data/dashboard.json    │
│    三份摘要 + Listing/Markets 字段强校验      │
└──────────────────────────────────────────┘
       │
       ├───────────────────────┬──────────────────────┐
       ▼                       ▼                      ▼
┌──────────────┐      ┌──────────────────┐   ┌──────────────────────┐
│ docs/index.html│      │ 飞书多维表同步       │   │ 飞书群卡片 + 截图推送   │
│ 静态看板（纯前端）│      │ feishu_bitable.py │   │ screenshot.py + bot.py │
└──────────────┘      └──────────────────┘   └──────────────────────┘
```

**竞品与语言范围**：Bitunix（EN/FR/ID）、Weex（EN/FR）、BingX（EN/VN）、Phemex
（EN/FR）、Lbank（EN/VN/ID）是竞品；Zoomex（EN/FR/EN-Asia/VN/ID）是我方基线，
不参与"竞品分析"，只作为对比目录的数据源。

### 0.1 常规执行入口

包含飞书同步与群推送的完整日常链路统一由下面一个命令执行：

```bash
scripts/run_daily.sh
```

该入口覆盖指定 UTC 当天的新增/更新采集、归组与分类、地区标记、去重、Zoomex
能力目录、Campaign/Product 分析与对比、Listing/Delisting 赛道分类、三份当日
Summary、Dashboard 导出和最终产物验收；之后把当日业务结果同步到 Campaign、
Product、Listing & Delisting 三张飞书表。群日报会显式选择顶部“最新批次”并生成
Overview 截图，再由应用机器人把截图、三份 Summary、三张业务表入口和可视化看板
入口合并为一张交互卡片发送到 EN 群。

竞品平台采集属于强依赖，任一竞品失败都会中止日报。Zoomex 只作为能力对比基线，
因此独立采集：如果云端 Runner 被 Zoomex API 以 HTTP 403 拒绝，流程会记录警告并
使用数据库中最近一次成功的 Zoomex 能力目录继续运行；在可访问 Zoomex 的本地或
自托管环境中仍正常更新当天基线。

可通过 `DB_PATH`、`DASHBOARD_OUT`、`BATCH_DATE`、
`LLM_PROVIDER`、`LLM_MAX_TOKENS` 环境变量调整运行参数。脚本启用严格失败模式；任何
步骤失败、三份 Summary 不完整，或最终 JSON 缺少关键分析字段，都会以非零状态退出。

---

## 1. 采集（src/collectors/）

每个源一个 `BaseCollector` 子类，`fetch_list()` → `needs_detail()` →
`fetch_detail()` → `normalize()` → `run()` 编排落库。两种增量策略：

| 策略 | 适用源 | 逻辑 |
|---|---|---|
| `watermark` | Bitunix、Zoomex（列表接口有可靠 `update_time`） | 按源端更新时间做水位线；Zoomex 因列表排序不可靠，实际靠 `needs_detail()` 逐条比对 DB 里的 `update_time` 做增量，`high_watermark` 只做观测用 |
| `full_scan` | Weex、BingX、Phemex、Lbank（无可靠 `update_time` 或翻页机制本身很浅） | 拉前 N 页，`content_hash` 二次校验变更 |

**清洗**在采集层完成（Phase 2.5 前移，不再是 Pipeline 的事）：`content` 落库时
已经是纯文本，`content_hash = SHA256(清洗后正文)`。

**唯一保留全量回填能力的源是 Zoomex**（`--force-full`）；其余竞品源政策是"只做
`--lookback-days` 限定的每日增量，永不对主库做历史回填"（2026-07-21 拍板，见
`docs/history.md`）。

落库统一走 `upsert_announcement()`（`src/db/operations.py`）：处理去重（`uid =
SHA256(source_locale_articleId)`）、变更检测（`content_hash` 对比）、旧版本归档
（写入 `content_history`，不覆盖丢失），任何 collector 都不应该自己实现这三件事。

---

## 2. Pipeline 后处理（src/pipeline/）

四个独立步骤，`python -m src.pipeline <子命令>` 依次跑，互不依赖（除了执行顺序上
`dedup` 应该在 `classify`/`region` 之前，避免重复行污染下游统计）：

1. **`group-check`**（`grouping.py`）：不做归组（`group_id` 在采集阶段已生成），
   只做防御性一致性扫描——同一篇文章是否被拆成了两个 `group_id`、某个 group 的
   locale 数是否超过该源实际配置的 locale 集合。
2. **`dedup`**（`dedup.py`）：判重口径是「同 source + locale + **标题+正文完全
   一致**」，不是只按 `content_hash`——源站会有"同一通知换新 article_id 重发"
   （真重复，要合并）和"不同事件复用同一段模板正文"（假重复，如 Zoomex 两次不同
   代币上线公告共享模板、只标题不同，不能合并）两种情况，只看 hash 会把后者也
   误判。命中的行写 `duplicate_of` 指回最早一条，下游（分析批次查询、
   `zmx_catalog`、Dashboard 导出）统一 `WHERE duplicate_of IS NULL` 排除，但
   **`source_coverage` 的采集量统计不排除**（如实反映抓取量）。默认扫描全部源
   （含 Zoomex），是本 CLI 里唯一的例外。
3. **`classify`**（`category.py`）：三层结构，目前只实现前两层——第一层
   `raw_category` 精确字典查找（`config/category_mapping.yaml`，key 是
   raw_category 原始值，不是人读名称）；第二层是标题关键词兜底（只在第一层落
   `other`/无映射时生效，`listing > delisting` 优先级，另有两个按
   `(source, raw_category)` 精确限定的兜底规则，见文件顶注）。第三层"LLM 兜底"
   留白，未实现。`raw_category` 有值但不在映射表里 → 显式标 `unmapped_native`，
   不静默落关键词层（那样会掩盖需要人工补映射的信号）。
4. **`region`**（`region.py`）：某 `group_id` 只在该源配置的非 EN 单一 locale
   出现 → `is_region_exclusive=true`。必须按该源在 `sources.yaml` 里**实际配置**
   的 locale 集合判断，不能用全局 locale 集合（Bitunix EN/FR/ID 三语，Weex 只有
   EN/FR，两者独立）。

`eval.py` 是人工抽查工具（分层抽样打印分类结果，供肉眼核对），不是自动化流程的
一部分。

---

## 3a. Zoomex 能力目录（src/analysis/zmx_catalog.py）

Zoomex 是我方基线，不做"竞品分析"，而是被结构化提取成一份**能力目录**，供竞品
分析阶段做对比参照。取代了早期的 `zmx_baseline` 机制（已退休）。

- **`extract`**：LLM 把 Zoomex 自己的公告提取成目录条目（`zmx_summary` 表），
  `mechanism_type` 必须从 `config/zmx_mechanism_taxonomy.yaml` 定义的封闭枚举里
  选（campaign 18 个 + other，product 10 个 + other），不允许自造新类型——不匹配
  的一律落 `other` + `raw_mechanism_label` 记录原始描述。**没有 lookback 窗口**，
  覆盖 Zoomex 全量历史，这是"能不能断言缺失"的关键前提。
- **`rollup`**：把 `zmx_summary` 按 `(category, mechanism_type)` 汇总成
  `zmx_catalog_entry` 表，`exists_flag` 三态：
  - `yes`：该 mechanism_type 下 `zmx_summary` 有 ≥1 条命中（精确标签匹配）。
  - `partial`：精确匹配 0 条，但在 `other` 桶里用词项重叠找到近似条目——
    `capability_desc` 会带一句"建议人工核对"，置信度低于 `yes`。
  - `no`：完全没有，这才是"Zoomex 真的没有这个能力"的确认信号。

这一层是**目录级、粗粒度**的信号（"Zoomex 有没有做过这类事"），跟下面 3b 的
**逐篇、细粒度**比对（"这一篇具体公告有没有对应的 Zoomex 条目"）是两条独立路径，
在 Dashboard 导出层会再合起来展示（见第 4 节）。

---

## 3b. 竞品 LLM 分析：staged-v1（src/analysis/staged.py + run.py + prompts.py + llm.py）

`python -m src.analysis` 的编排逻辑：按 `(source, category, locale)` 分组当日
`status IN (new, changed)` 的公告，每组一次分析。`category=other` 不分析；
`listing`/`delisting` 走独立的轻量分类（见下）；`campaign`/`product` 走三段式：

### Stage1：事实抽取（每篇独立，per-article 缓存）

每篇公告单独一次短 prompt，只抽事实（`mechanism`/`feature`/`event_type`/
`reward`/`target_users`/`changes`/`evidence`/`confidence`），不评价、不跟 Zoomex
比较、不使用外部知识。缓存 key 只跟 `content_hash` 相关（`extraction_cache_key`），
同一篇文章无论后面 prompt 怎么改都能命中缓存，除非正文真的变了。

### Stage2：确定性候选召回（无 LLM，纯词项重叠）

分两级窄化，都基于同一套逻辑——把文本转成词项集合、算交集：

1. **批次级窄化**（`zmx_catalog.select_relevant_catalog`）：`get_catalog_digest`
   先按 `(category, locale)` 从 `zmx_summary` 拉全量、按 mechanism_type 做
   类型覆盖优先的采样（`max_entries_per_batch=20`，每类型最多 2 条），再用整批
   公告标题+正文跟这 20 条做词项重叠，选出 `candidate_entries_per_batch=8` 条
   **全批共享**的候选池。
2. **单篇级召回**（`staged.recall_candidates`）：从上面 8 条共享池里，用**这一篇**
   的 `mechanism/eligibility/reward/target_users/feature` 跟每个候选的
   `mechanism_type/key_mechanics/reward_range/target_users/title` 算词项交集，
   取 top-4，零重叠不返回候选。

**2026-07-22 修复的关键教训**：两处 `_terms()`（`staged.py` 一份、
`zmx_catalog.py` 一份，各自独立维护，避免循环 import）原本只要求 token 长度
≥2/≥3，不过滤停用词。中文因为 `_TOKEN_RE` 天然过滤掉单字虚词（"的"/"了"），
这套算法在中文语料下工作正常；但英文的 "is"/"users"/"trading"/"account"/
"platform"/"support" 这类高频虚词或行业通用词长度都够格，导致几乎任何两段英文
文本都能碰出重叠——而召回门槛只要求 **score≥1**，形同虚设。真实后果：一整批
主题完全不同的英文 Product 公告全部被强行召回同一两个 Zoomex 条目。修复是给
两处 `_terms()` 都加了停用词表（语法虚词 + 高频无区分度行业套话，刻意保守，不碰
任何可能承载语义的具体机制词）。详见 `docs/history.md`「追查真正根因」一节。

### Stage3：批量业务判断（一次调用，读 Stage1+Stage2 产出，不重新读原文）

一批文章的 facts + 各自候选一次性喂给 LLM，输出每篇的：

- `gap_type`：`confirmed_gap`（确认缺失）/ `different_mechanism`（同类不同玩法）
  / `covered`（已有）/ `baseline_not_found`（没召回到候选，不能断言缺失）/
  `not_applicable`（不适用）。**无候选时必须是 `baseline_not_found`，不得直接断言
  `confirmed_gap`**——这是防误报缺失的核心规则，因为候选是词项重叠召回的，召回
  不到不等于目录里真的没有。
- `zmx_evidence`：引用的候选序号（`z1`/`z2`/...），`gap_type` 断言
  `different_mechanism`/`covered` 但引用不到真实证据 → 程序性降级回
  `baseline_not_found`（防幻觉）。
- `business_impact` / `novelty` / `urgency`：供 `calculate_priority()` 程序化
  算分用，LLM 不直接产出 `priority`。

`gap_type` → 中文 `diff_type` 的翻译只在写库前这一个边界做一次
（`GAP_TYPE_TO_DIFF_TYPE`）：`confirmed_gap→ZMX缺失`，`different_mechanism→
ZMX玩法不同`，`covered→ZMX已有`，`baseline_not_found`/`not_applicable→不适用`。

**`validate_business_judgment()` 的防御规则**（`src/analysis/llm.py`）：
- 候选为空但 `gap_type` 断言了 confirmed_gap/different_mechanism/covered → 强制
  降级 `baseline_not_found`。
- `gap_type` 断言 different_mechanism/covered 但没有真实证据 → 同样降级。
- **`gap_type=baseline_not_found`/`not_applicable` 且证据为空，但 `reason` 里
  点名了具体候选序号**（2026-07-22 真实数据发现的模型自相矛盾：一边说"没候选
  可比"一边描述某个候选的具体异同）→ 记一条 `issue`，**不擅自改写 `gap_type`**
  （代码猜不出真实该是三种"有候选"取值里的哪一个），只是让这类矛盾在
  `report.validation_failed` 里可见，供人工/下次重跑复核。

`priority`（高/中/低）完全程序化算分（`calculate_priority`）：
`event_type` 权重 + `gap_type` 权重 + `business_impact` 权重 + `confidence`
权重 + `novelty`/`urgency`（各 0-3，×2）线性相加，≥70 高、≥40 中，其余低——不是
LLM 直接产出，保证同样输入永远同样档位。`action_type`/`owner`/`follow_up` 同样
不由 LLM 产出，改成 Phase⑤ 的确定性规则（见第 5 节 `_derive_follow_up`）。

### Listing/Delisting：独立的轻量分类（src/analysis/listing.py）

不走上面三段。LLM 只判断币种赛道（AI/Meme/Layer2/DeFi/GameFi/RWA/DePIN/Other），
**不**参与 Listing Type/Status/Token/交易对/上线时间/ZMX 差异/优先级判断——这些
全部由标题正则 + 确定性规则派生（`_listing_kind_from_title` 等）。`diff_type`
写死 `"不适用"`（这一类目从设计上就不做 ZMX 对比）。

### 缓存与熔断

三段各自独立缓存 key（`extraction_cache_key`/`comparison_cache_key`/
`listing_cache_key`），改 prompt 正文必须递增对应 `prompt_versions.*`（否则
线上缓存会用旧 prompt 的响应，新逻辑形同虚设）。`--max-calls`/`--max-tokens`
熔断按调用次数/累计 token 数算，达到即停止对**剩余批次**发起新调用，已产出的
正常入库，跳过的批次留到下次重跑（不是失败）。

`--include-unchanged`（2026-07-22 新增）：`get_batch_rows()`/`list_batch_keys()`
默认只看 `status IN (new, changed)`，公告一旦被后续每日增量重新抓取过
（`fetched_at` 滚到更晚日期、`status` 变回 `unchanged`），就会永久查不到——
按原历史 `--date` 查不到（`fetched_at` 已翻篇），按当前日期查也查不到
（`status` 不再是 new/changed）。这个 flag 连 `unchanged` 也纳入批次，专门
用于补跑这类"当天 daily 分析没跑、缺口从此卡死"的历史遗留批次，配合当前日期
（不是原始历史日期）使用。

---

## 4. 批次落库与 locale 复用（src/analysis/batch.py + run.py）

一行 insight = 一次 `(source, category, locale, batch_date)` 批次分析结论。
PK = `SHA256(source_category_locale_batchDate)`。

**EN → 其它 locale 复用**（`can_derive_from_en`）：同一天同 `source×category`
下，非 EN locale 如果跟 EN 当天批次的 `group_id` 集合完全相同（不是子集判断，
避免"少文章的 locale 复用了包含额外文章的 EN summary"），直接复用 EN 的分析
结果（`is_locale_derived=true`，`llm_tokens_used=0`），不重新调用 LLM。

---

## 5. Dashboard 导出（src/dashboard/export_data.py）

`python -m src.dashboard` 把 SQLite 快照成 `docs/data/dashboard.json`，纯前端
消费，**不含实时查询**。这一层做的不只是"读表拼 JSON"，还包括几处关键的**二次
合成逻辑**：

### diff_tag 的实际计算（两级信号合并）

前端看到的 `diff_tag` **不是** `insights.diff_type` 的简单改名，是这样合成的：

1. `DIFF_TYPE_TAG` 把中文 `diff_type` 映射成前端 tag：`ZMX缺失→missing`，
   `ZMX玩法不同→diff`，`ZMX已有→same`，`不适用→na`（`混合→mixed` 只在批次级
   `insights.diff_type` 出现，不会出现在逐条 `articles_analysis` 里）。
2. **`na` 的二次判断**（2026-07-22 新增，对称的一升一降）：`diff_tag=='na'`
   （Stage3 说"这篇没找到具体对应的候选"）时，再看 `zmx_exists`（目录按
   `mechanism_type` 标签查全量历史）进一步细化：
   - `zmx_exists=='yes'`（精确命中，不是 `'partial'` 近似匹配）且没有具体
     `zmx_counterpart` → 降级改成 `'broad'`（"已有同类型 · 粗粒度"）——避免
     右侧卡片有内容但标签说"没比过"的视觉矛盾。
   - `zmx_exists=='no'`（rollup 覆盖全量历史后**确认**这个 mechanism_type
     从未出现过，不是"没查"）→ 升级改成 `'missing'`（"未检索到同类"）——这是
     比 Stage3 单批次窄召回更强的缺失证据，不该继续显示"没比过"。
   - `zmx_exists` 为 `None`（mechanism_type 没打标签，或标签在目录里还没有
     对应条目）→ 维持 `'na'`，这才是真正"不知道"的情况。
   两种情况都只改 `diff_tag`，不改写 `diff_type`/`diff_detail`——那两个字段
   仍然如实保留 Stage3 自己的逐篇结论，供追溯。

**完整语义速查表**：

| `diff_tag` | 中文标签 | 含义 | 来源 |
|---|---|---|---|
| `missing` | 未检索到同类 | Stage3 确认 Zoomex 缺失（`confirmed_gap`） | Stage3 逐篇比对 |
| `diff` | 已有同类 · 机制不同 | Stage3 找到具体对应但玩法不同（`different_mechanism`） | Stage3 逐篇比对 |
| `same` | 已有同类 | Stage3 确认 Zoomex 已有（`covered`） | Stage3 逐篇比对 |
| `mixed` | 需人工核验 | 批次内多种非"不适用"取值混合（只在 insights 批次级出现） | 程序聚合 |
| `broad` | 已有同类型 · 粗粒度 | Stage3 没找到具体对应，但目录按标签精确命中 | 导出层二次合成 |
| `na` | 未进行对比 | 真没找到任何信号（`baseline_not_found`/`not_applicable`，且目录也没有精确命中） | Stage3 逐篇比对 |
| `pending` | 待分析 | 这条公告还没被任何一次分析运行覆盖，不是"确认无差异" | `comparison_status` |

**筛选器 UI 只暴露 4 类**（2026-07-22，`docs/index.html` 的 `DIFF_FILTER_TAGS`
常量）：`missing`/`diff`/`broad`/`na`。`same`/`mixed` 定这 4 类时真实数据里
还从未出现过（`same` 需要 Stage3 找到具体证据且判完全一致；`mixed` 只在批次级
`insights.diff_type` 出现，不会落到逐条 `diff_tag` 上）——`same` 后来在
"历史遗留批次补跑"之后已经真实出现过（见 `docs/history.md`），但筛选器范围
当时已经拍板不含它，未跟着扩大，`pending` 语义上不是"差异"分类，本来就不在
筛选范围。`DIFF_LABEL` 字典本身仍保留全部 7 个 key——`same`/`mixed`
出现时表格里的标签文字仍能正确渲染，只是不能通过
筛选器专门选出来。

**粗粒度匹配（`broad`）的可跳转链接**：右侧卡片/紧凑表格列都会显示
"查看同类型示例 ↗"（`zmx_capability_url`，取 `zmx_catalog_entry.example_uids`
第一条 join `announcements.url`），链接指向的是**同 mechanism_type 下 Zoomex
某一篇公告**，不是"这一篇竞品公告的精确对应文章"（那是 `zmx_counterpart` 的
职责，两者语义不同，不要混淆）。

### 其它导出层逻辑

- **`zmx_counterpart`**：具体对照示例（标题/链接/摘要/奖励），只在 Stage3 真的
  引用了某个候选证据（`zmx_counterpart_uids` 非空）时才有值——跟 `zmx_exists`/
  `zmx_capability_desc`（目录级、按标签查）是完全独立的两个字段，不要混淆。
- **Follow-up 确定性派生**（`_derive_follow_up`）：`ZMX缺失` 且该
  `mechanism_type` 本批次被 ≥2 个不同竞品触及 → "建议评估跟进"；`ZMX玩法不同`
  → "建议观察差异"；`ZMX已有` → "无需关注"；其余不产出。不是 LLM 输出。
- **去重展示**：`duplicate_of IS NOT NULL` 的行从内容列表（campaign/product/
  listing/delisting 卡片、trend、markets、search_index）里排除，但
  `source_coverage` 采集量统计不排除。
- **Listing/Delisting 不读取或展示历史版本的 ZMX 比较/priority/follow_up**——
  这一类目从 Phase②起就不产出这些字段，即使数据库里有过时的旧值也不显示。

---

## 6. 当日综述（src/analysis/daily_digest.py）

比 insights 批次分析再上一层："这个 locale 今天整体发生了什么"，把当天已产出的
全部批次 summary/zmx_diff 综合成一段简报。跟 Phase 4 四套 category prompt 不是
同一个调用粒度（那是"公告原文→批次分析"，这是"批次分析结果→当日综述"），独立
成模块，不改动 `run.py` 的批次循环。导出层本身不主动触发 LLM；常规入口
`scripts/run_daily_pre_lark.sh` 会在 Dashboard 导出前运行真实调用，一次生成并缓存
Overview、Campaign、Product 三份摘要。生产入口使用 `--require-generated`，任一摘要
缺失即停止；导出后再由 `python -m src.dashboard.validate` 验收最终 JSON，确保三份
摘要和 Listing/Markets 所需字段已经真正进入前端数据。

---

## 7. 前端展示（docs/index.html）

单文件静态看板，`fetch('./data/dashboard.json')` 纯前端渲染，不发任何其它请求
（CSP 允许的唯一外部依赖是同目录下的 JSON）。六个顶层 tab：Overview / Campaign /
Product / Listing & Delisting / Markets / Search，category-first 设计（2026-07-20
起，取代早期的 locale-first）。

Detail 抽屉（Campaign/Product 行可点击展开）四栏：Basic Info / Rule-Reward-
Timeline（或 Feature） / AI Summary / Zoomex Comparison。Zoomex Comparison 栏
用 `zmxCompareTwoColumn()` 渲染，三种情况：

1. `comparison_status !== 'analyzed'`：这条从没被任何分析运行覆盖过，明确提示
   "不是确认无差异"。
2. 有具体 `zmx_counterpart`：真实匹配到的 Zoomex 文章，标题可点击（真实 URL，
   见下方"Zoomex 详情页 URL"说明）。
3. 没有具体 `zmx_counterpart` 但 `diff_tag==='broad'`：显示
   "同类型参考 · 粗粒度匹配，非逐篇核对" + 目录级 `zmx_capability_desc`，
   diff 说明文字加"具体比对："前缀——明确区分"粗粒度参考"和"这篇的逐篇结论"
   两句话，不让它们看起来互相矛盾。
4. 其余（`zmx_exists==='no'`/`null`）：如实显示"目前没有此类玩法/功能"或
   "暂无具体对照示例"。

推送视图（`?view=push&locale=<X>`）是独立于六个 tab 的紧凑页面，专供
`screenshot.py`/`feishu_bot.py` 截图推送用，不是给人日常浏览的入口。

**Zoomex 详情页 URL**（2026-07-22 修复）：`https://www.zoomex.com/{url_locale}/
help/article/{articleId}`，`url_locale` 因 locale 而异（EN→`en`，FR→`fr-FR`，
EN-Asia→`en-AS`，VN→`vi-VN`，ID→`id-ID`，见 `config/sources.yaml` 各 locale
块），已用 Playwright 实测验证（`help.zoomex.com/{url_locale}/article/{id}` 会
客户端重定向到上述规范 URL）。

---

## 8. 飞书同步与推送

- **多维表同步**（`src/sinks/feishu_business_tables.py`）：使用 Campaign、Product、
  Listing & Delisting 三张业务表（可分别位于不同 Base），只同步指定批次日
  `new/changed` 内容。
  Campaign/Product 包含 AI Summary 和 ZMX 对比；Listing & Delisting 只包含币种、
  类型、状态、赛道、Markets 等事实字段。以 `uid` 幂等 create/update/skip。
- **一次性表结构重置**：使用 `--reset-schema --confirm-reset
  RESET_THREE_BUSINESS_TABLES` 清空三表记录、移除旧的非主字段并建立新 schema。
  这是破坏性操作，不属于每日 Pipeline。
- **群日报推送**（`src/dashboard/screenshot.py` + `src/sinks/feishu_bot.py`）：
  Playwright 打开 Overview、显式点击顶部“最新批次”并截图；应用机器人上传图片取得
  `image_key`，再把 Overview/Campaign/Product 三份缓存 Summary、三张业务表链接、
  公开看板链接和截图合并成一张 interactive 卡片，经 `im/v1/messages` 发送到 EN 群。
  日报不使用群 webhook；图片失败时降级为纯文本卡片。
- Phase 6 原计划的"逐条规则推送引擎"（`push_rules.yaml` 匹配单篇公告决定要不要
  推文字消息）从未实现，是另一条独立、未开始的路径，不要跟上面的截图推送混淆。

---

## 9. 关键设计原则（贯穿全流程）

1. **SQLite 是唯一真相源**；飞书/看板都是只读同步出去的视图，重跑/补数/改分类
   只操作 SQLite。
2. **不允许猜测数据**：任何 API 字段映射、URL 规则都必须来自真实请求验证
   （Playwright/curl），不允许凭记忆编造；不确定的字段如实标注"待验证"。
3. **LLM 只做窄范围判断，宽范围结论程序化派生**：`priority`/`follow_up`/
   `action_type` 都不是 LLM 直接产出，是确定性规则从 LLM 的窄输出（
   `event_type`/`gap_type`/`business_impact`/`novelty`/`urgency`）算出来的，
   保证同样输入永远同样结论。
4. **候选召回是确定性算法（词项重叠），不是 LLM**——Stage2 全程无 LLM 调用，
   代价是依赖分词/停用词质量（2026-07-22 的教训：英文语料下不过滤停用词会
   让门槛形同虚设）。
5. **无法确认的信号不能伪装成确认的信号**：`baseline_not_found`≠`confirmed_gap`
   （没召回到候选 ≠ 确认缺失）；`comparison_status='pending'`≠"确认无差异"；
   `exists_flag='partial'`≠`'yes'`（近似匹配 vs 精确匹配，前端不能同等对待）。
6. **多个独立信号共存时，展示层要让读者看出这是几条不同粒度的结论**，而不是
   悄悄挑一个显示、造成看起来自相矛盾（`broad` 标签 + "具体比对：" 前缀就是
   这条原则的直接产物）。

---

## 10. 已知的粗糙之处（如实记录，不是本文档要掩盖的）

- Zoomex `mechanism_type` 分类本身可能偏粗——2026-07-22 真实案例：Lbank 一个
  "首次 P2P 购买超低价"促销被 Stage1 分类成 `zero_risk_new_user`（"首单亏损
  包赔"类），字面对上号但语义不完全一致。目前认为这类粗粒度误差收益递减，
  不值得为此改造分类 prompt，改在展示层承认"这是粗粒度匹配"（见第 5/7 节
  `broad` 逻辑）。
- Product 类目仍有一部分"未进行对比"是真实业务情况——Zoomex 的能力目录里
  天然没有 Bitunix 那类运营/合规类公告（tick size 调整、AUSTRAC 注册、SEPA
  主体变更）的对应物，这是类目结构差异，不是 bug（2026-07-22 修复 na→missing/
  broad 二次判断 + 补跑历史遗留批次后，真实占比已大幅下降，但不会归零）。
- **daily 分析如果某天没跑（或跑了但预算熔断跳过），缺口不会被后续 daily 跑
  自动补上**——`get_batch_rows()` 按 `status IN (new,changed) AND
  date(fetched_at)=batch_date` 查询，公告一旦被后续增量重新抓取（`fetched_at`
  滚到更晚日期、`status` 变回 `unchanged`），就再也查不到了，只能用
  `--include-unchanged`（2026-07-22 新增 CLI flag）配合当前日期手动补跑。
  这类缺口 2026-07-22 发现过一次真实案例（11+6 条 campaign/product 公告长期
  停留在 `comparison_status=pending`），已补跑修复，但没有自动化机制防止
  同类缺口再次累积——`run_daily.sh`/`run_daily_pre_lark.sh` 目前不带
  `--include-unchanged`，如果想彻底杜绝需要额外加一道"定期核查是否有长期
  pending 的 campaign/product 行"的监控，目前还没有。
- 没有真实的 OpenAI 兼容 LLM 后端，全部真实调用走 `cursor_agent`（Cursor
  Background Agent）替代后端，且该后端偶发不稳定，单次调用真实 token 消耗
  也可能远超预估（2026-07-22 补跑历史缺口时，45 次调用吃掉约 90 万 token，
  比同等调用次数的常规 daily 跑高一个数量级——`--max-tokens` 熔断预算要按
  实际批次文章数量级重新估算，不能直接套用过去的经验值）。
- 飞书群日报已完成“EN 群交互卡片 + Overview 截图”链路。

详细的每一次修复过程、真实验证记录，见 `docs/history.md`。
