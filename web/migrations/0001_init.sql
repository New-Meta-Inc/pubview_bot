-- pubview-dashboard 初期スキーマ
-- Bot側 SQLite (Docker内 /data/lol_bot.db) からの同期先

-- ユーザー: Discordプロフィール + Riot ID（Bot側 users + Discordから補完）
CREATE TABLE IF NOT EXISTS users (
  discord_id     TEXT PRIMARY KEY,
  display_name   TEXT NOT NULL,
  avatar_url     TEXT,
  riot_game_name TEXT,
  riot_tag_line  TEXT,
  tier           TEXT,
  rank           TEXT,
  league_points  INTEGER,
  updated_at     TEXT NOT NULL
);

-- 組分け帽子
CREATE TABLE IF NOT EXISTS sorting_hat (
  discord_id   TEXT PRIMARY KEY,
  house_id     TEXT NOT NULL,
  rate_bracket TEXT NOT NULL,
  tier         TEXT NOT NULL,
  sorted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sorting_hat_house ON sorting_hat(house_id);

-- 個人累積（永続）
CREATE TABLE IF NOT EXISTS contribution_totals (
  discord_id    TEXT PRIMARY KEY,
  total_xp      INTEGER NOT NULL DEFAULT 0,
  vc_seconds    INTEGER NOT NULL DEFAULT 0,
  text_messages INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_totals_xp ON contribution_totals(total_xp DESC);

-- 月次集計
CREATE TABLE IF NOT EXISTS contribution_monthly (
  year_month    TEXT NOT NULL,
  discord_id    TEXT NOT NULL,
  points        INTEGER NOT NULL DEFAULT 0,
  vc_seconds    INTEGER NOT NULL DEFAULT 0,
  text_messages INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (year_month, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_monthly_points ON contribution_monthly(year_month, points DESC);

-- 同期ログ（直近の取り込み状況の可視化用）
CREATE TABLE IF NOT EXISTS ingest_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ingested_at     TEXT NOT NULL,
  users_count     INTEGER NOT NULL,
  sorting_count   INTEGER NOT NULL,
  totals_count    INTEGER NOT NULL,
  monthly_count   INTEGER NOT NULL,
  bot_version     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_log_at ON ingest_log(ingested_at DESC);
