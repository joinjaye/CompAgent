-- 竞品情报平台 SQLite schema（schema 版本 v3，见 CLAUDE.md「Phase 4 完成情况」；
-- zmx_summary/zmx_catalog_entry 是纯新增的表，不改动任何既有表的列/约束，不需要走
-- migrate 脚本，见 CLAUDE.md「Zoomex Capability Catalog」小节）
-- 所有时间字段统一使用 UTC ISO8601 字符串（如 2026-07-13T02:30:00Z），不使用 SQLite 原生 DATETIME。
-- SQLite 是唯一真相源；飞书多维表只是同步出去的业务视图。

PRAGMA foreign_keys = ON;

-- ============================================================
-- announcements（原始层）
-- 一行 = 一个 source × locale × article_id 的公告。
-- 同一竞品同一条公告的多语言版本各占一行，用 group_id 归组。
-- ============================================================
CREATE TABLE IF NOT EXISTS announcements (
    uid                  TEXT PRIMARY KEY,   -- SHA256({source}_{locale}_{article_id})
    group_id             TEXT,               -- 跨语言归组，Phase 3 填充
    source               TEXT NOT NULL,      -- Bitunix / Weex / BingX / Phemex / Lbank / Zoomex
    locale               TEXT NOT NULL,      -- EN / FR / ID / VN / EN-Asia
    article_id           TEXT NOT NULL,      -- 源站原生文章 ID
    url                  TEXT,
    title                TEXT,
    content              TEXT,               -- 清洗后正文。Phase 2.5 起清洗前移到采集层，
                                              -- content_hash 的语义即「清洗后正文的 SHA256」
                                              -- （不再是原始 HTML 的 hash）
    raw_category         TEXT,               -- 源站原生分类的原始值，不做任何映射转换（数值型
                                              -- 转字符串存）：Bitunix/Weex 是 Zendesk section_id，
                                              -- Zoomex 是 menu_id，BingX 是 sectionId，Phemex 是抓取
                                              -- 子源名（news/activities/newsletter）；Lbank 恒 NULL
                                              -- （源端无 per-item 分类）。映射到 campaign/product/
                                              -- listing/delisting/other 是 Phase 3 的事，见
                                              -- config/category_mapping.yaml
    content_hash         TEXT,               -- SHA256(content)，变更检测用
    post_time            TEXT,               -- 发布时间，UTC ISO8601
    update_time          TEXT,               -- 源端更新时间（如有），UTC ISO8601
    fetched_at           TEXT,               -- 本次抓取时间，UTC ISO8601
    status               TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'changed', 'unchanged')),
    category             TEXT
                         CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'delisting', 'other')),
    is_region_exclusive  BOOLEAN NOT NULL DEFAULT 0,
    push_status          TEXT NOT NULL DEFAULT 'pending'
                         CHECK (push_status IN ('pending', 'pushed', 'skipped')),
    source_endpoint      TEXT                -- 来源 API endpoint，便于溯源排障
);

CREATE INDEX IF NOT EXISTS idx_announcements_source_locale
    ON announcements (source, locale);
CREATE INDEX IF NOT EXISTS idx_announcements_group_id
    ON announcements (group_id);
CREATE INDEX IF NOT EXISTS idx_announcements_status
    ON announcements (status);
CREATE INDEX IF NOT EXISTS idx_announcements_push_status
    ON announcements (push_status);
CREATE INDEX IF NOT EXISTS idx_announcements_category
    ON announcements (category);

-- ============================================================
-- content_history（变更历史）
-- 每当 announcements.content_hash 发生变化，旧版本先存一份到这里再覆盖主表。
-- ============================================================
CREATE TABLE IF NOT EXISTS content_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL REFERENCES announcements (uid) ON DELETE CASCADE,
    content_hash    TEXT,
    content         TEXT,
    captured_at     TEXT   -- 该版本被归档时的时间，UTC ISO8601
);

CREATE INDEX IF NOT EXISTS idx_content_history_uid
    ON content_history (uid);

-- ============================================================
-- insights（分析层 / 批次级汇总分析表，Phase 4 起 schema v3）
-- 一行 = 一次「批次」分析结论：同一天同一 (source, category, locale) 的全部
-- status IN (new, changed) 公告合并成一次 LLM 调用的产出。不再是逐条公告一行
-- （v1/v2 的设计），见 CLAUDE.md「Phase 4」。
-- PK: id = SHA256(source || "_" || category || "_" || locale || "_" || batch_date)
-- ============================================================
CREATE TABLE IF NOT EXISTS insights (
    id                 TEXT PRIMARY KEY,
    batch_date         TEXT NOT NULL,          -- UTC date, YYYY-MM-DD
    source             TEXT NOT NULL,
    category           TEXT NOT NULL
                      CHECK (category IN ('campaign', 'product', 'listing', 'delisting', 'other')),
    locale             TEXT NOT NULL,
    article_count      INTEGER NOT NULL DEFAULT 0,
    related_uids       TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，回链 announcements.uid
    is_locale_derived  BOOLEAN NOT NULL DEFAULT 0,  -- true = 复用同日 EN 批次分析，未调 LLM
    derived_from_id    TEXT REFERENCES insights (id),
    summary            TEXT,                    -- batch_summary 字段的 LLM 输出
    articles_analysis  TEXT,                    -- JSON 数组，每篇公告的结构化分析
    zmx_diff           TEXT,                    -- zmx_comparison.analysis 的文字部分
    diff_type          TEXT
                      CHECK (diff_type IS NULL OR diff_type IN ('ZMX已有', 'ZMX缺失', 'ZMX玩法不同', '混合', '不适用')),
    priority           TEXT
                      CHECK (priority IS NULL OR priority IN ('高', '中', '低')),
    zmx_evidence_uids  TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，引用到的 Zoomex uid
    prompt_version     TEXT NOT NULL,           -- 如 "campaign-v1"，改 prompt 必须递增
    llm_tokens_used    INTEGER,                 -- 复用 EN 分析时为 0
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_insights_batch
    ON insights (batch_date, source, category, locale);
CREATE INDEX IF NOT EXISTS idx_insights_source_cat
    ON insights (source, category);

-- ============================================================
-- llm_cache（Phase 4 新增）
-- key = SHA256(本批次全部 related_uids 的 content_hash 拼接 || prompt_version)。
-- 同一批次内容没变、prompt 版本没变时直接返回缓存，不重复调用 LLM。
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key    TEXT PRIMARY KEY,
    response     TEXT NOT NULL,   -- 原始 LLM 响应 JSON 字符串
    created_at   TEXT NOT NULL
);

-- ============================================================
-- zmx_summary / zmx_catalog_entry（Zoomex 能力目录，Phase① 新增，取代
-- zmx_baseline——旧表连同其自由生成的 mechanism_type 标签一起下线，见
-- CLAUDE.md「Zoomex Capability Catalog」）。
--
-- zmx_summary：一行 = 一条 Zoomex 公告（campaign/product，delisting/listing 不建
-- 目录）的结构化提取结果。mechanism_type 现在是 config/zmx_mechanism_taxonomy.yaml
-- 定义的封闭/半封闭枚举 key，不再是 LLM 自由生成的中文标签——这是修复标签碎片化
-- 的根本手段。提取覆盖 Zoomex 全量历史，没有任何 lookback 窗口（旧表的 90 天窗口
-- 是"无法断言缺失"问题的根因，这里结构性移除）。
--
-- zmx_catalog_entry：rollup 结果，一行 = 一个 (category, mechanism_type) 枚举 key
-- 的目录条目，纯 SQL 聚合产生，不调用 LLM。覆盖枚举里定义的全部 key（不只是观察到
-- 的），这样 exists_flag='no' 才能真正代表"Zoomex 没有这个能力"，不是"没检索到"。
-- ============================================================
CREATE TABLE IF NOT EXISTS zmx_summary (
    source_uid           TEXT PRIMARY KEY REFERENCES announcements (uid) ON DELETE CASCADE,
    group_id             TEXT,
    category             TEXT NOT NULL CHECK (category IN ('campaign', 'product')),
    locale               TEXT NOT NULL,
    mechanism_type       TEXT NOT NULL,   -- 封闭/半封闭枚举 key；不匹配任何 key 时填 'other'
    raw_mechanism_label  TEXT,            -- 仅 mechanism_type='other' 时填，保留 LLM 原始描述
    core_summary         TEXT,
    key_mechanics        TEXT,
    reward_form          TEXT,
    reward_amount        TEXT,
    reward_token         TEXT,
    target_users         TEXT,
    entry_threshold      TEXT,
    start_date           TEXT,            -- campaign
    end_date             TEXT,            -- campaign
    main_feature         TEXT,            -- product
    supported_market     TEXT,            -- product，JSON 数组文本
    supported_token      TEXT,            -- product，JSON 数组文本
    supported_platform   TEXT,            -- product，JSON 数组文本
    supported_user_tier  TEXT,            -- product，JSON 数组文本
    content_hash         TEXT NOT NULL,   -- 提取时对应的 announcements.content_hash，增量判断用
    is_locale_derived    BOOLEAN NOT NULL DEFAULT 0,
    derived_from_uid     TEXT REFERENCES announcements (uid),
    prompt_version       TEXT NOT NULL,   -- 如 "zmx-catalog-extract-v1"
    llm_tokens_used      INTEGER,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zmx_summary_cat_locale
    ON zmx_summary (category, locale);
CREATE INDEX IF NOT EXISTS idx_zmx_summary_mechanism
    ON zmx_summary (category, mechanism_type);

CREATE TABLE IF NOT EXISTS zmx_catalog_entry (
    id              TEXT PRIMARY KEY,  -- SHA256(category || "_" || mechanism_type)
    category        TEXT NOT NULL CHECK (category IN ('campaign', 'product')),
    mechanism_type  TEXT NOT NULL,
    exists_flag     TEXT NOT NULL CHECK (exists_flag IN ('yes', 'no', 'partial')),
    capability_desc TEXT,
    example_uids    TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，2-3 个 zmx_summary.source_uid
    typical_reward  TEXT,               -- campaign only
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zmx_catalog_entry_cat
    ON zmx_catalog_entry (category);

-- ============================================================
-- crawl_state（采集水位线）
-- 每个 source × locale 一行；若该源在同一 locale 下有多个互相独立翻页的子分类
-- （如 Zoomex 的多个 menu_id、Phase 2 批次 2 起），category 区分之，单分类源留空
-- 字符串 ''（不是 NULL，NULL 在 SQLite 里参与唯一约束比较的语义容易出岔子）。
-- ============================================================
CREATE TABLE IF NOT EXISTS crawl_state (
    source           TEXT NOT NULL,
    locale           TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT '',   -- 多分类源各分类独立维护水位线；单分类源恒为 ''
    high_watermark   TEXT,   -- 上轮最大 update_time，UTC ISO8601
    strategy         TEXT NOT NULL DEFAULT 'watermark'
                    CHECK (strategy IN ('watermark', 'full_scan')),
    updated_at       TEXT,
    PRIMARY KEY (source, locale, category)
);

-- ============================================================
-- sync_log（飞书同步日志）
-- 记录每一次向飞书多维表 / 群机器人同步或推送的结果，便于排障和幂等校验。
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target       TEXT NOT NULL,   -- bitable / bot_EN / bot_FR / bot_VN / bot_ID / bot_EN-Asia
    record_id    TEXT,            -- uid 或 insight_id
    action       TEXT
                CHECK (action IS NULL OR action IN ('create', 'update', 'skip')),
    status       TEXT
                CHECK (status IS NULL OR status IN ('success', 'failed')),
    error        TEXT,
    synced_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_log_target_record
    ON sync_log (target, record_id);
