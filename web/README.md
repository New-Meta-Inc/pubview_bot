# pubview-dashboard (Web)

Astro + Cloudflare Pages + D1 で稼働する、ぱぶびゅ！コントリビューションダッシュボード。

## 構成

- **Frontend**: Astro v5 (SSR) + Tailwind v3
- **Hosting**: Cloudflare Pages
- **DB**: Cloudflare D1 (`pubview-dashboard-db`)
- **同期元**: macmini 上の Bot が5分おきに `/api/ingest` へ POST

## ローカル開発

```bash
pnpm install
cp .dev.vars.example .dev.vars   # INGEST_TOKEN を本物に差し替え
pnpm d1:apply:local              # ローカル D1 にスキーマ適用
pnpm dev                         # http://localhost:4321
```

## デプロイ

```bash
# 本番 D1 にマイグレーション適用
pnpm d1:apply

# Pages にデプロイ
pnpm deploy

# シークレット登録（初回のみ）
wrangler pages secret put INGEST_TOKEN --project-name pubview-dashboard
```

## URL（予定）

| パス | 役割 |
|---|---|
| `/` | 4寮の対決ボード |
| `/houses/[house_id]` | 寮詳細（メンバー一覧、推移グラフ） |
| `/leaderboard` | 全体ランキング（累積/月次切替） |
| `/u/[slug]` | 個人ページ |
| `/monthly/[yyyy-mm]` | 月次アーカイブ |
| `/api/ingest` | Bot からの同期受信エンドポイント（POST） |

## アクセス制御

現状はパブリック（`robots.txt` で noindex）。将来 Discord OAuth を入れる際は `feature/oauth` で追加予定。
