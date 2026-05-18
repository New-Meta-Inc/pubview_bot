-- 寮長: 管理者が手動で任命する。Bot DBで一意に管理し同期される
CREATE TABLE IF NOT EXISTS house_leaders (
  house_id   TEXT PRIMARY KEY,
  discord_id TEXT NOT NULL,
  set_at     TEXT NOT NULL,
  set_by     TEXT
);
