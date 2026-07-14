-- 竞品情报平台 SQLite schema
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
    content              TEXT,               -- 清洗后正文（Phase 3 之前可能是原始正文）
    content_hash         TEXT,               -- SHA256(content)，变更检测用
    post_time            TEXT,               -- 发布时间，UTC ISO8601
    update_time          TEXT,               -- 源端更新时间（如有），UTC ISO8601
    fetched_at           TEXT,               -- 本次抓取时间，UTC ISO8601
    status               TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'changed', 'unchanged')),
    category             TEXT
                         CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'other')),
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
-- insights（分析层 / 汇总分析表）
-- Phase 4 产出。一行 = 一次 LLM 分析结论，可回链多个 announcements。
-- ============================================================
CREATE TABLE IF NOT EXISTS insights (
    id             TEXT PRIMARY KEY,
    related_uids   TEXT,   -- JSON 数组，回链 announcements.uid
    source         TEXT,
    category       TEXT
                  CHECK (category IS NULL OR category IN ('campaign', 'product', 'listing', 'other')),
    summary        TEXT,
    zmx_diff       TEXT,
    diff_type      TEXT
                  CHECK (diff_type IS NULL OR diff_type IN ('ZMX已有', 'ZMX缺失', 'ZMX玩法不同', '不适用')),
    priority       TEXT
                  CHECK (priority IS NULL OR priority IN ('高', '中', '低')),
    created_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_insights_source_category
    ON insights (source, category);
CREATE INDEX IF NOT EXISTS idx_insights_diff_type
    ON insights (diff_type);

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
