-- 日次スナップショット: 個人ページの「日次成果グラフ」描画に使う
CREATE TABLE IF NOT EXISTS daily_snapshots (
  snapshot_date  TEXT    NOT NULL,
  discord_id     TEXT    NOT NULL,
  total_xp       INTEGER NOT NULL DEFAULT 0,
  vc_seconds     INTEGER NOT NULL DEFAULT 0,
  text_messages  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_date, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_snapshots_user ON daily_snapshots(discord_id, snapshot_date DESC);
