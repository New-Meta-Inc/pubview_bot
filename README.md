# ぱぶびゅ！ LoL Rank Bot

## 概要

このDiscordボットは、サーバーに参加しているメンバーのLeague of Legends (LoL) のSolo/Duoランク情報を自動で管理し、コミュニティ活動を活性化させるためのツールです。主な機能として、ランクに基づいたランキングの自動生成、ランクに応じたDiscordロールの自動付与、そしてランクアップ時のお祝い通知などがあります。

ユーザーはダッシュボード上のボタンから直感的に自身のRiot IDを登録・解除でき、管理者は簡単なコマンドでボットの管理やデバッグが可能です。

---

## コンポーネント間の連携

### 1. ユーザー
PCやスマートフォンのDiscordアプリを使用し、ボットにコマンドを送信したり、ボタンを操作します。
- **アクション**: `/ranking` などのスラッシュコマンド入力、ダッシュボードのボタン操作。
- **通信先**: Discordサーバー

### 2. Discordサーバー
ユーザーからの操作を受け取り、ボット（バックエンドアプリケーション）にイベントとして転送します。
- **アクション**:
    -   ユーザーからのコマンドやボタン操作を受け付けます。
    -   その内容をボットにイベントとして通知します。
    -   ボットからのレスポンス（ランキングの埋め込みメッセージなど）をユーザーに表示します。
-   **通信先**: ユーザー、およびボットアプリケーション。

### 3. ボットアプリケーション (このリポジトリ)
Dockerコンテナとして動作し、Discordからのイベントを処理します。
-   **アクション**:
    -   Discordサーバーからイベントを受け取ります。
    -   必要に応じて **Riot Games API** にアクセスし、プレイヤーのランク情報を取得します。
    -   取得したデータやコマンドの実行結果を **Discordサーバー** にレスポンスとして返します。
-   **通信先**: Discordサーバー、Riot Games API

---

## 動作方法

### 1. 必要なAPIキーとIDの取得

ボットを動作させるには、以下の情報が必要です。
- **Discord Bot Token**: [Discord Developer Portal](https://discord.com/developers/applications)でアプリケーションを作成し、Botトークンを取得します。
- **Riot Games API Key**: [Riot Games Developer Portal](https://developer.riotgames.com/)からAPIキーを取得します。
- **Discord Guild ID**: ボットを導入するDiscordサーバーのID。
- **Notification Channel ID**: ランキングや通知を投稿するテキストチャンネルのID。

### 2. 環境変数ファイルの準備

プロジェクトのルートディレクトリに `.env` ファイルを作成し、取得した情報を記述します。

```env
DISCORD_TOKEN="YOUR_DISCORD_TOKEN"
RIOT_API_KEY="YOUR_RIOT_API_KEY"
DISCORD_GUILD_ID="YOUR_GUILD_ID"
NOTIFICATION_CHANNEL_ID="YOUR_CHANNEL_ID"
```

### 3. Dockerでの実行

Dockerがインストールされている環境で、以下のコマンドを実行します。

```bash
# 1. Dockerイメージのビルド
docker build -t lol-rank-bot:latest .

# 2. Dockerコンテナの実行
docker run --env-file .env --name lol-bot -d lol-rank-bot:latest
```
これにより、ボットがバックグラウンドで起動します。

---

## 使い方

### プレイヤー情報の登録と削除 (ダッシュボードからの操作)

管理者によって設置されたダッシュボードのボタンから直感的に操作できます。

- **Riot IDの登録**: ボタンを押すと表示されるウィンドウに、あなたのRiot IDとTaglineを入力して登録します。
- **Riot IDの登録解除**: ボタンを押すと、あなたの情報がボットから削除されます。

※操作後の確認メッセージは30秒で自動的に消えます。

### スラッシュコマンド一覧

#### 一般ユーザー向けコマンド
-   `/register [game_name] [tag_line]`: Riot IDをボットに登録します。
-   `/unregister`: 登録情報を削除します。
-   `/ranking`: サーバー内のランクランキングを表示します。

#### 管理者向けコマンド
-   `/dashboard [channel]`: 登録・登録解除用のダッシュボードを指定チャンネルに送信します。
-   `/register_by_other [user] [game_name] [tag_line]`: 他のユーザーに代わってRiot IDを登録します。
-   `/debug_check_ranks_periodically`: 定期ランクチェックを手動で実行します。
-   `/debug_rank_all_iron`: 登録者全員のランクをIron IVに設定します。
-   `/debug_modify_rank [user] [tier] [rank] [league_points]`: 特定ユーザーのランクを強制的に変更します。

---

## 主な機能

### 🏆 定期的なランキング表示

毎日、指定された時刻（デフォルトではJST 12:00）に、全ユーザーの最新のランク情報を取得し、ランキングを自動で投稿します。

### 🎖️ ランク連動ロール

登録したプレイヤーのランクに応じて、Discordサーバー内の対応するランクロール（例: `LoL Gold(Solo/Duo)`）を自動で付与または更新します。

### 🎉 ランクアップ通知

ランクが上昇した際、Discordのチャンネルに自動で通知メッセージを送信し、みんなでお祝いできます。
