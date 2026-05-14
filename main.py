import os
import sqlite3
import datetime
import time
import json
import random
import string
from typing import Any
import aiohttp
import discord
from discord.ext import tasks
from riotwatcher import RiotWatcher, LolWatcher, ApiError


# --- 設定項目 ---
DISCORD_TOKEN: str | None = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY: str | None = os.getenv('RIOT_API_KEY')
DISCORD_GUILD_ID: int = int(os.getenv('DISCORD_GUILD_ID'))
DB_PATH: str = '/data/lol_bot.db'
CLOUDFLARE_INGEST_URL: str | None = os.getenv('CLOUDFLARE_INGEST_URL')
INGEST_TOKEN: str | None = os.getenv('INGEST_TOKEN')
BOT_VERSION: str = "phase3-2"
NOTIFICATION_CHANNEL_ID: int = 1401719055643312219 # 通知用チャンネルID
HONOR_CHANNEL_ID: int = 1447166222591594607 # 名誉用チャンネルID
VOICE_CREATE_CHANNEL_ID: int = 1469467862358823125
RANK_GAME_CHANNEL_ID: int = 1470346492895166566
RANK_ROLES: dict[str, str] = {
    "IRON": "LoL Iron(Solo/Duo)", "BRONZE": "LoL Bronze(Solo/Duo)", "SILVER": "LoL Silver(Solo/Duo)",
    "GOLD": "LoL Gold(Solo/Duo)", "PLATINUM": "LoL Platinum(Solo/Duo)", "EMERALD": "LoL Emerald(Solo/Duo)",
    "DIAMOND": "LoL Diamond(Solo/Duo)", "MASTER": "LoL Master(Solo/Duo)",
    "GRANDMASTER": "LoL Grandmaster(Solo/Duo)", "CHALLENGER": "LoL Challenger(Solo/Duo)"
}

# --- 組分け帽子機能の設定 ---
# 組（ハウス）定義: (内部ID, 表示名, 絵文字, ロール名)
HOUSES: list[tuple[str, str, str, str]] = [
    ("raptor", "ラプター", "🦖", "ラプター"),
    ("krug", "クルーグ", "🪨", "クルーグ"),
    ("wolf", "ウルフ", "🐺", "ウルフ"),
    ("gromp", "グロンプ", "🐸", "グロンプ"),
]

# プルダウン用のTier一覧（最高レート選択肢）
SORTING_TIERS: list[tuple[str, str]] = [
    ("UNRANKED", "Unranked"),
    ("IRON", "Iron"),
    ("BRONZE", "Bronze"),
    ("SILVER", "Silver"),
    ("GOLD", "Gold"),
    ("PLATINUM", "Platinum"),
    ("EMERALD", "Emerald"),
    ("DIAMOND", "Diamond"),
    ("MASTER", "Master"),
    ("GRANDMASTER", "Grandmaster"),
    ("CHALLENGER", "Challenger"),
]

# Tier → レート帯
RATE_BRACKET_OF_TIER: dict[str, str] = {
    "UNRANKED": "low", "IRON": "low", "BRONZE": "low", "SILVER": "low",
    "GOLD": "mid", "PLATINUM": "mid",
    "EMERALD": "high", "DIAMOND": "high",
    "MASTER": "apex", "GRANDMASTER": "apex", "CHALLENGER": "apex",
}
# ----------------

# --- コントリビューション機能の設定 ---
# ポイント発生源（バランス調整時はここを変更）
CONTRIBUTION_VC_PT_PER_MINUTE: int = 1
CONTRIBUTION_TEXT_PT_PER_MESSAGE: int = 2
CONTRIBUTION_TEXT_COOLDOWN_SECONDS: int = 60

# レベル式: 累積XP = COEF * level^EXP に達したら次のレベル
# 個人:  Lv N 必要累積XP = 20 * N^2   （Lv50 ≈ 50,000 XP）
# 寮:    Lv N 必要累積XP = 300 * N^2  （個人式の15倍重い、特典設計用に長期目標化）
CONTRIBUTION_USER_LEVEL_COEF: int = 20
CONTRIBUTION_USER_LEVEL_EXP: float = 2.0
CONTRIBUTION_HOUSE_LEVEL_COEF: int = 300
CONTRIBUTION_HOUSE_LEVEL_EXP: float = 2.0
# -----------------------------

# JST（モジュール全体で共用）
jst: datetime.timezone = datetime.timezone(datetime.timedelta(hours=9))

# --- データベースの初期設定 ---
def setup_database() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            discord_id INTEGER PRIMARY KEY,
            riot_puuid TEXT NOT NULL UNIQUE,
            game_name TEXT,
            tag_line TEXT,
            tier TEXT,
            rank TEXT,
            league_points INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sections (
            role_id INTEGER PRIMARY KEY,
            section_name TEXT NOT NULL UNIQUE,
            notification_channel_id INTEGER NOT NULL
        )
    ''')
    # 組分け帽子: ユーザーごとに所属組とその時のレート帯を記録
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sorting_hat (
            discord_id INTEGER PRIMARY KEY,
            house_id TEXT NOT NULL,
            rate_bracket TEXT NOT NULL,
            tier TEXT NOT NULL,
            sorted_at TEXT NOT NULL
        )
    ''')
    # コントリビューション: 個人累積（永続）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS contribution_totals (
            discord_id INTEGER PRIMARY KEY,
            total_xp INTEGER NOT NULL DEFAULT 0,
            vc_seconds INTEGER NOT NULL DEFAULT 0,
            text_messages INTEGER NOT NULL DEFAULT 0,
            last_text_at TEXT,
            updated_at TEXT NOT NULL
        )
    ''')
    # コントリビューション: 月次集計（JST月初リセット相当、年月キーで分離）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS contribution_monthly (
            year_month TEXT NOT NULL,
            discord_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            vc_seconds INTEGER NOT NULL DEFAULT 0,
            text_messages INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (year_month, discord_id)
        )
    ''')
    # コントリビューション: VC在室セッション（joinで記録、leaveで秒数確定→totals/monthly加算）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS vc_sessions (
            discord_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL
        )
    ''')
    # 寮ごとのDiscordチャンネルID（/setup_house_channels で作成・記録）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS house_channels (
            house_id     TEXT NOT NULL,
            channel_type TEXT NOT NULL,
            channel_id   INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (house_id, channel_type)
        )
    ''')
    # 週次スナップショット: 週次ダイジェストの「先週比」算定に使用
    cur.execute('''
        CREATE TABLE IF NOT EXISTS weekly_snapshots (
            snapshot_date  TEXT NOT NULL,
            discord_id     INTEGER NOT NULL,
            total_xp       INTEGER NOT NULL DEFAULT 0,
            vc_seconds     INTEGER NOT NULL DEFAULT 0,
            text_messages  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (snapshot_date, discord_id)
        )
    ''')
    # 寮長: 管理者が手動で任命。D1への同期も同形式
    cur.execute('''
        CREATE TABLE IF NOT EXISTS house_leaders (
            house_id   TEXT PRIMARY KEY,
            discord_id INTEGER NOT NULL,
            set_at     TEXT NOT NULL,
            set_by     INTEGER
        )
    ''')
    con.commit()
    con.close()
# -----------------------------

# --- コントリビューション機能のヘルパー ---
def is_sorted(discord_id: int) -> bool:
    """組分け帽子を被ったメンバーかどうか"""
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT 1 FROM sorting_hat WHERE discord_id = ?", (discord_id,))
    found: bool = cur.fetchone() is not None
    con.close()
    return found


def current_year_month_jst() -> str:
    """JST基準の '%Y-%m' 文字列。月次集計のキーに使用"""
    return datetime.datetime.now(jst).strftime("%Y-%m")


def required_xp_for_level(level: int, coef: int = CONTRIBUTION_USER_LEVEL_COEF, exp: float = CONTRIBUTION_USER_LEVEL_EXP) -> int:
    """指定レベルに到達するために必要な累積XP（Lv1は0XP）"""
    if level <= 1:
        return 0
    return int(coef * (level ** exp))


def level_from_xp(xp: int, coef: int = CONTRIBUTION_USER_LEVEL_COEF, exp: float = CONTRIBUTION_USER_LEVEL_EXP) -> int:
    """累積XPからレベルを算出。Lv N 必要XP = coef * N^exp の逆算"""
    if xp <= 0:
        return 1
    raw_level: float = (xp / coef) ** (1.0 / exp)
    return max(1, int(raw_level))


def add_contribution(discord_id: int, xp: int, vc_seconds: int, text_messages: int, last_text_at_iso: str | None = None) -> tuple[int, int]:
    """totals と monthly に同時加算。組分け済みであることは呼び出し側で保証する。

    返り値: (old_level, new_level) - 加算前後の個人レベル。Lvアップ判定に使う。
    """
    if xp == 0 and vc_seconds == 0 and text_messages == 0:
        return (0, 0)
    now_iso: str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ym: str = current_year_month_jst()
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    # 加算前の累積XPを取得（Lvアップ判定用）
    cur.execute("SELECT total_xp FROM contribution_totals WHERE discord_id = ?", (discord_id,))
    row: tuple[int] | None = cur.fetchone()
    old_xp: int = row[0] if row else 0
    old_level: int = level_from_xp(old_xp)
    # totals
    cur.execute(
        """
        INSERT INTO contribution_totals (discord_id, total_xp, vc_seconds, text_messages, last_text_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            total_xp = total_xp + excluded.total_xp,
            vc_seconds = vc_seconds + excluded.vc_seconds,
            text_messages = text_messages + excluded.text_messages,
            last_text_at = COALESCE(excluded.last_text_at, contribution_totals.last_text_at),
            updated_at = excluded.updated_at
        """,
        (discord_id, xp, vc_seconds, text_messages, last_text_at_iso, now_iso),
    )
    # monthly
    cur.execute(
        """
        INSERT INTO contribution_monthly (year_month, discord_id, points, vc_seconds, text_messages)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(year_month, discord_id) DO UPDATE SET
            points = points + excluded.points,
            vc_seconds = vc_seconds + excluded.vc_seconds,
            text_messages = text_messages + excluded.text_messages
        """,
        (ym, discord_id, xp, vc_seconds, text_messages),
    )
    con.commit()
    con.close()
    new_level: int = level_from_xp(old_xp + xp)
    return (old_level, new_level)


def get_user_house_id(discord_id: int) -> str | None:
    """sorting_hat から所属寮IDを取得"""
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT house_id FROM sorting_hat WHERE discord_id = ?", (discord_id,))
    row: tuple[str] | None = cur.fetchone()
    con.close()
    return row[0] if row else None


def get_house_channel_id(house_id: str, channel_type: str) -> int | None:
    """house_channels から指定寮・チャンネル種別のIDを取得"""
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute(
        "SELECT channel_id FROM house_channels WHERE house_id = ? AND channel_type = ?",
        (house_id, channel_type),
    )
    row: tuple[int] | None = cur.fetchone()
    con.close()
    return row[0] if row else None


async def notify_level_up(discord_id: int, old_level: int, new_level: int) -> None:
    """所属寮の リーダーボード チャンネルに Lvアップ祝福を投稿する。
    1Lv上昇でも 2Lv以上上昇でも一回だけ最終Lvを祝う（連続Lvアップは合算表現）。
    """
    if new_level <= old_level:
        return
    house_id: str | None = get_user_house_id(discord_id)
    if house_id is None:
        return
    channel_id: int | None = get_house_channel_id(house_id, "leaderboard")
    if channel_id is None:
        return
    channel: discord.abc.GuildChannel | None = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    house_info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == house_id), None)
    house_emoji: str = house_info[2] if house_info else "🏰"
    house_name_jp: str = house_info[1] if house_info else "?"

    # 次Lvまでの必要XP
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT total_xp FROM contribution_totals WHERE discord_id = ?", (discord_id,))
    row: tuple[int] | None = cur.fetchone()
    con.close()
    total_xp: int = row[0] if row else 0
    next_required: int = required_xp_for_level(new_level + 1)
    cur_required: int = required_xp_for_level(new_level)
    remaining: int = max(0, next_required - total_xp)
    in_lv: int = max(0, total_xp - cur_required)
    span: int = max(1, next_required - cur_required)

    title: str = "🎉 Lv UP!"
    if new_level - old_level >= 2:
        title = f"🎉🎉 {new_level - old_level}連Lv UP!"

    # 個人ページURL（display_name 経由）。失敗時はトップにフォールバック
    user_page_url: str = "https://pubview-dashboard.pages.dev/"
    try:
        guild_for_member: discord.Guild | None = bot.get_guild(DISCORD_GUILD_ID)
        member_obj: discord.Member | None = None
        if guild_for_member is not None:
            member_obj = guild_for_member.get_member(discord_id)
            if member_obj is None:
                member_obj = await guild_for_member.fetch_member(discord_id)
        if member_obj is not None:
            import urllib.parse as _up
            user_page_url = f"https://pubview-dashboard.pages.dev/u/{_up.quote(member_obj.display_name, safe='')}"
    except Exception:
        pass

    embed: discord.Embed = discord.Embed(
        title=title,
        url=user_page_url,
        description=f"<@{discord_id}> さんが **Lv {new_level}** に到達しました！",
        color=discord.Color.gold(),
    )
    embed.add_field(name="所属", value=f"{house_emoji} {house_name_jp}", inline=True)
    embed.add_field(name="現在", value=f"Lv {new_level}", inline=True)
    embed.add_field(name=f"次Lv {new_level + 1} まで", value=f"あと **{remaining:,} XP**（{in_lv:,}/{span:,}）", inline=False)
    embed.add_field(name="​", value=f"[📊 ダッシュボードで詳細を見る →]({user_page_url})", inline=False)
    try:
        await channel.send(
            embed=embed,
            # 個人メンションは embed の description 内に <@id> として含めて発火させる
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=discord_id)]),
        )
    except Exception as e:
        print(f"!!! notify_level_up: failed to send to channel {channel_id}: {e}")


def vc_session_start(discord_id: int, channel_id: int) -> None:
    """VC参加を記録。組分け済みのみ実行する想定（呼び出し側でフィルタ）"""
    now_iso: str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO vc_sessions (discord_id, channel_id, joined_at) VALUES (?, ?, ?)",
        (discord_id, channel_id, now_iso),
    )
    con.commit()
    con.close()


def vc_session_end(discord_id: int) -> tuple[int, int]:
    """VC退出時に在室秒数を確定し、totals/monthly へ加算。

    返り値: (old_level, new_level) - Lvアップ判定用。何も加算されなかった場合は (0, 0)。
    """
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT joined_at FROM vc_sessions WHERE discord_id = ?", (discord_id,))
    row: tuple[str] | None = cur.fetchone()
    if not row:
        con.close()
        return (0, 0)
    try:
        joined_at: datetime.datetime = datetime.datetime.fromisoformat(row[0])
    except ValueError:
        cur.execute("DELETE FROM vc_sessions WHERE discord_id = ?", (discord_id,))
        con.commit()
        con.close()
        return (0, 0)
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    seconds: int = max(0, int((now_utc - joined_at).total_seconds()))
    cur.execute("DELETE FROM vc_sessions WHERE discord_id = ?", (discord_id,))
    con.commit()
    con.close()
    if seconds <= 0:
        return (0, 0)
    # 分単位で切り捨て、端数秒は次回まわし（vc_secondsは秒で記録）
    added_xp: int = (seconds // 60) * CONTRIBUTION_VC_PT_PER_MINUTE
    return add_contribution(discord_id, xp=added_xp, vc_seconds=seconds, text_messages=0)
# -----------------------------

# --- Botの初期設定 ---
intents: discord.Intents = discord.Intents.default()
intents.members = True
bot: discord.Bot = discord.Bot(intents=intents)

riot_watcher: RiotWatcher = RiotWatcher(RIOT_API_KEY)
lol_watcher: LolWatcher = LolWatcher(RIOT_API_KEY)

my_region_for_account: str = 'asia'
my_region_for_summoner: str = 'jp1'
# -----------------------------

# --- UIコンポーネント (View) ---
class DashboardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Riot IDの登録", style=discord.ButtonStyle.success, custom_id="dashboard:register")
    async def register_button(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RegisterModal())

    @discord.ui.button(label="Riot IDの登録解除", style=discord.ButtonStyle.danger, custom_id="dashboard:unregister")
    async def unregister_button(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            con: sqlite3.Connection = sqlite3.connect(DB_PATH)
            cur: sqlite3.Cursor = con.cursor()
            cur.execute("DELETE FROM users WHERE discord_id = ?", (interaction.user.id,))
            con.commit()

            if con.total_changes > 0:
                await interaction.followup.send("あなたの登録情報を削除しました。", ephemeral=True, delete_after=30.0)
                # ランク連動ロール削除処理
                guild: discord.Guild | None = interaction.guild
                if guild:
                    member: discord.Member | None = await guild.fetch_member(interaction.user.id)
                    if member:
                        role_names_to_remove: list[discord.Role | None] = [discord.utils.get(guild.roles, name=role_name) for role_name in RANK_ROLES.values()]
                        await member.remove_roles(*[role for role in role_names_to_remove if role is not None and role in member.roles])
            else:
                await interaction.followup.send("あなたはまだ登録されていません。", ephemeral=True, delete_after=30.0)

            con.close()
        except Exception as e:
            print(f"!!! An unexpected error occurred in 'unregister_button': {e}")
            await interaction.followup.send("登録解除中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=30.0)

    @discord.ui.button(label="🎩 組分け帽子を被る", style=discord.ButtonStyle.primary, custom_id="dashboard:sorting_hat")
    async def sorting_hat_button(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        # 既に組分け済みかチェック
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("SELECT house_id FROM sorting_hat WHERE discord_id = ?", (interaction.user.id,))
        existing: tuple[str] | None = cur.fetchone()
        con.close()

        if existing:
            existing_house_id: str = existing[0]
            house_info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == existing_house_id), None)
            if house_info:
                await interaction.response.send_message(
                    f"あなたは既に **{house_info[2]} {house_info[1]}** に組分け済みです。再組分けは現在サポートされていません。",
                    ephemeral=True,
                    delete_after=30.0,
                )
                return

        await interaction.response.send_message(
            content="🎩 帽子があなたの最高レートを尋ねています…\nプルダウンから最高到達Tierを選んでください。",
            view=SortingHatTierSelectView(),
            ephemeral=True,
            delete_after=180,
        )

# --- 組分け帽子: UI と振り分けロジック ---
class SortingHatTierSelectView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.add_item(SortingHatTierSelect())


class SortingHatTierSelect(discord.ui.Select):
    def __init__(self) -> None:
        options: list[discord.SelectOption] = [
            discord.SelectOption(label=label, value=tier_id) for tier_id, label in SORTING_TIERS
        ]
        super().__init__(
            placeholder="最高到達Tierを選択してください",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="sorting_hat:tier_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_tier: str = self.values[0]
        bracket: str = RATE_BRACKET_OF_TIER[selected_tier]
        user: discord.User | discord.Member = interaction.user
        guild: discord.Guild | None = interaction.guild
        if not guild:
            await interaction.followup.send("ギルド情報が取得できませんでした。", ephemeral=True, delete_after=30.0)
            return

        # 二重組分け防止（プルダウン操作中に他で組分けが完了した場合のレース対策）
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("SELECT house_id FROM sorting_hat WHERE discord_id = ?", (user.id,))
        if cur.fetchone():
            con.close()
            await interaction.followup.send("既に組分け済みです。", ephemeral=True, delete_after=30.0)
            return

        # 対象レート帯における各組の現在人数をカウント
        cur.execute(
            "SELECT house_id, COUNT(*) FROM sorting_hat WHERE rate_bracket = ? GROUP BY house_id",
            (bracket,),
        )
        counts: dict[str, int] = {row[0]: row[1] for row in cur.fetchall()}

        # 最少人数の組を選ぶ（同数はランダム）
        house_ids: list[str] = [h[0] for h in HOUSES]
        min_count: int = min(counts.get(hid, 0) for hid in house_ids)
        candidates: list[str] = [hid for hid in house_ids if counts.get(hid, 0) == min_count]
        chosen_house_id: str = random.choice(candidates)

        # DB保存
        sorted_at: str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO sorting_hat (discord_id, house_id, rate_bracket, tier, sorted_at) VALUES (?, ?, ?, ?, ?)",
            (user.id, chosen_house_id, bracket, selected_tier, sorted_at),
        )
        con.commit()
        con.close()

        # 組情報取得
        house_info: tuple[str, str, str, str] = next(h for h in HOUSES if h[0] == chosen_house_id)
        _, house_name_jp, house_emoji, role_name = house_info

        # ロール付与
        member: discord.Member | None = guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except Exception as e:
                print(f"!!! sorting_hat: failed to fetch member: {e}")
        if member is not None:
            role: discord.Role | None = discord.utils.get(guild.roles, name=role_name)
            if role is not None:
                try:
                    await member.add_roles(role, reason="組分け帽子による組分け")
                except Exception as e:
                    print(f"!!! sorting_hat: failed to add role '{role_name}': {e}")
            else:
                print(f"!!! sorting_hat: role '{role_name}' not found in guild")

        # 本人へのephemeral返答
        await interaction.followup.send(
            f"🎩 帽子はあなたを **{house_emoji} {house_name_jp}** に振り分けました！",
            ephemeral=True,
            delete_after=30.0,
        )

        # グローバルメッセージ投稿
        channel: discord.abc.Messageable | None = interaction.channel
        if channel is not None:
            embed: discord.Embed = discord.Embed(
                title="🎩 組分け帽子",
                description=f"{user.mention} は **{house_emoji} {house_name_jp}** に組分けされました！",
                color=discord.Color.purple(),
            )
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"!!! sorting_hat: failed to send global message: {e}")


class GiveHonorModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="名誉を贈る")
        self.add_item(discord.ui.InputText(label="名誉を贈りたいユーザー", required=True))
        self.add_item(discord.ui.InputText(label="名誉を贈りたい理由", required=True))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = bot.get_channel(HONOR_CHANNEL_ID)
        if not channel:
            return
        embed: discord.Embed = discord.Embed(title=f"名誉投票が行われました", color=discord.Color.gold())
        embed.description = f"{interaction.user.mention}が名誉を贈りました"
        embed.add_field(name="名誉を贈りたいユーザー", value=self.children[0].value, inline=False)
        embed.add_field(name="名誉を贈りたい理由", value=self.children[1].value, inline=False)
        await channel.send(embed=embed)
        await interaction.followup.send(f"「{self.children[0].value}」に名誉を贈りました！", ephemeral=True, delete_after=30.0)

class RegisterModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="Riot ID 登録")
        self.add_item(discord.ui.InputText(label="Riot ID (例: TaroYamada)", required=True))
        self.add_item(discord.ui.InputText(label="Tagline (例: JP1) ※#は不要", required=True))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        game_name: str = self.children[0].value
        tag_line: str = self.children[1].value

        if tag_line.startswith("#"):
            tag_line = tag_line[1:]
        tag_line = tag_line.upper()

        try:
            account_info: dict[str, Any] = riot_watcher.account.by_riot_id(my_region_for_account, game_name, tag_line)
            puuid: str = account_info['puuid']
            rank_info: dict[str, Any] | None = get_rank_by_puuid(puuid)

            con: sqlite3.Connection = sqlite3.connect(DB_PATH)
            cur: sqlite3.Cursor = con.cursor()
            if rank_info:
                cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (interaction.user.id, puuid, game_name, tag_line, rank_info['tier'], rank_info['rank'], rank_info['leaguePoints']))
            else:
                cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
                            (interaction.user.id, puuid, game_name, tag_line))
            con.commit()
            con.close()
            await interaction.followup.send(f"Riot ID「{game_name}#{tag_line}」を登録しました！", ephemeral=True, delete_after=30.0)
        except ApiError as err:
            if err.response.status_code == 404:
                await interaction.followup.send(f"Riot ID「{game_name}#{tag_line}」が見つかりませんでした。", ephemeral=True, delete_after=30.0)
            else:
                await interaction.followup.send("Riot APIでエラーが発生しました。", ephemeral=True, delete_after=30.0)
        except Exception as e:
            print(f"!!! An unexpected error occurred in 'RegisterModal' callback: {e}")
            await interaction.followup.send("登録中に予期せぬエラーが発生しました。", ephemeral=True, delete_after=30.0)


class SectionSelectView(discord.ui.View):
    def __init__(self, available_sections: list[tuple[int, str]]) -> None:
        super().__init__(timeout=180)
        self.add_item(SectionSelect(available_sections))

class SectionSelect(discord.ui.Select):
    def __init__(self, available_sections: list[tuple[int, str]]) -> None:
        options: list[discord.SelectOption] = [
            discord.SelectOption(label=section_name, value=str(role_id)) for role_id, section_name in available_sections
        ]
        if not options:
            options.append(discord.SelectOption(label="参加可能なセクションがありません", value="no_sections", default=True))

        super().__init__(placeholder="参加したいセクションを選択してください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "no_sections":
            await interaction.response.edit_message(content="現在参加できるセクションはありません。", view=None)
            return

        role_id: int = int(self.values[0])
        guild: discord.Guild | None = interaction.guild
        if not guild:
            return
        section_role: discord.Role | None = guild.get_role(role_id)

        if not section_role:
            await interaction.response.edit_message(content="指定されたセクション（ロール）が見つかりませんでした。", view=None)
            return

        member: discord.Member = await guild.fetch_member(interaction.user.id)
        if section_role in member.roles:
            await interaction.response.edit_message(content=f"あなたは既にセクション「{section_role.name}」に参加しています。", view=None)
            return

        try:
            await member.add_roles(section_role)

            con: sqlite3.Connection = sqlite3.connect(DB_PATH)
            cur: sqlite3.Cursor = con.cursor()
            cur.execute("SELECT notification_channel_id FROM sections WHERE role_id = ?", (role_id,))
            result: tuple[int] | None = cur.fetchone()
            con.close()

            if result:
                channel_id: int = result[0]
                channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = bot.get_channel(channel_id)
                if channel:
                    await channel.send(f"{member.mention}さんがセクション「{section_role.name}」に参加しました！")

            await interaction.response.edit_message(content=f"セクション「{section_role.name}」に参加しました！", view=None)
        except Exception as e:
            print(f"!!! An unexpected error occurred in 'SectionSelect' callback: {e}")
            await interaction.response.edit_message(content="セクションへの参加中にエラーが発生しました。", view=None)

class RemoveSectionView(discord.ui.View):
    def __init__(self, user_roles: list[discord.Role]):
        super().__init__(timeout=180)
        self.add_item(RemoveSectionSelect(user_roles))

class RemoveSectionSelect(discord.ui.Select):
    def __init__(self, user_roles: list[discord.Role]) -> None:
        options: list[discord.SelectOption] = [
            discord.SelectOption(label=role.name, value=str(role.id)) for role in user_roles
        ]
        super().__init__(placeholder="退出したいセクションを選択してください", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        member: discord.Member | discord.User = interaction.user
        if not isinstance(member, discord.Member):
            return
        role_id: int = int(self.values[0])
        role_to_remove: discord.Role | None = interaction.guild.get_role(role_id) if interaction.guild else None

        if not role_to_remove or role_to_remove not in member.roles:
            await interaction.response.edit_message(content="エラー: 対象のセクション（ロール）が見つからないか、参加していません。", view=None)
            return

        try:
            await member.remove_roles(role_to_remove)
            await interaction.response.edit_message(content=f"セクション「{role_to_remove.name}」から退出しました。", view=None)
        except Exception as e:
            print(f"!!! An unexpected error occurred in 'RemoveSectionSelect' callback: {e}")
            await interaction.response.edit_message(content="セクションからの退出中にエラーが発生しました。", view=None)

# --- ヘルパー関数 ---
def get_rank_by_puuid(puuid: str) -> dict[str, Any] | None:
    max_retries: int = 3
    for attempt in range(max_retries):
        try:
            # LEAGUE-V4のby-puuidエンドポイントを直接呼び出す
            ranked_stats: list[dict[str, Any]] = lol_watcher.league.by_puuid(my_region_for_summoner, puuid)

            # ranked_statsはリスト形式であるため、ループで処理する
            for queue in ranked_stats:
                if queue.get("queueType") == "RANKED_SOLO_5x5":
                    # Solo/Duoランク情報が見つかった場合
                    return {
                        "tier": queue.get("tier"),
                        "rank": queue.get("rank"),
                        "leaguePoints": queue.get("leaguePoints")
                    }

            # リスト内にSolo/Duoランク情報がなかった場合
            return None

        except ApiError as err:
            if err.response.status_code == 429:
                retry_after: int = int(err.response.headers.get('Retry-After', 1))
                print(f"Rate limit exceeded. Retrying after {retry_after} seconds... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_after)
                continue
            elif err.response.status_code == 404:
                # ユーザーにランク情報がない場合
                return None
            else:
                # 400 Bad Requestなど、その他のAPIエラー
                print(f"API Error in get_rank_by_puuid for PUUID {puuid}: {err}")
                raise
        except Exception as e:
            # 予期せぬエラー
            print(f"An unexpected error occurred in get_rank_by_puuid for PUUID {puuid}: {e}")
            raise

    # リトライにすべて失敗した場合
    print(f"Failed to get rank for PUUID {puuid} after {max_retries} retries.")
    return None

def rank_to_value(tier: str, rank: str, lp: int) -> int:
    tier_values: dict[str, int] = {"CHALLENGER": 9, "GRANDMASTER": 8, "MASTER": 7, "DIAMOND": 6, "EMERALD": 5, "PLATINUM": 4, "GOLD": 3, "SILVER": 2, "BRONZE": 1, "IRON": 0}
    rank_values: dict[str, int] = {"I": 4, "II": 3, "III": 2, "IV": 1}
    tier_val: int = tier_values.get(tier.upper(), 0) * 1000
    rank_val: int = rank_values.get(rank.upper(), 0) * 100
    return tier_val + rank_val + lp

# --- ランキング作成ロジックを共通関数化 ---
async def create_ranking_embed() -> discord.Embed:
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    # DBからランク情報がNULLでないユーザーのみを取得
    cur.execute("SELECT discord_id, game_name, tag_line, tier, rank, league_points FROM users WHERE tier IS NOT NULL AND rank IS NOT NULL")
    registered_users_with_rank: list[tuple[int, str, str, str, str, int]] = cur.fetchall()
    con.close()

    embed: discord.Embed = discord.Embed(title="🏆 ぱぶびゅ！内LoL(Solo/Duo)ランキング 🏆", color=discord.Color.gold())

    description_footer: str = "\n\n**`/register` コマンドであなたもランキングに参加しよう！**"
    description_update_time: str = "（ランキングは毎日正午に自動更新されます）"

    if not registered_users_with_rank:
        embed.description = f"現在ランク情報を取得できるユーザーがいません。\n{description_update_time}{description_footer}"
        return embed

    player_ranks: list[dict[str, Any]] = []
    for discord_id, game_name, tag_line, tier, rank, lp in registered_users_with_rank:
        player_ranks.append({
            "discord_id": discord_id, "game_name": game_name, "tag_line": tag_line,
            "tier": tier, "rank": rank, "lp": lp,
            "value": rank_to_value(tier, rank, lp)
        })

    sorted_ranks: list[dict[str, Any]] = sorted(player_ranks, key=lambda x: x['value'], reverse=True)

    embed.description = f"現在登録されているメンバーのランクです。\n{description_update_time}{description_footer}"

    previous_tier: str = ""
    role_emojis: dict[str, str] = {
        "CHALLENGER": "<:challenger:1407917898445357107>",
        "GRANDMASTER": "<:grandmaster:1407917001401434234>",
        "MASTER": "<:master:1407917005524176948>",
        "DIAMOND": "<:diamond:1407916987518156901>",
        "EMERALD": "<:emerald:1407916989581754458>",
        "PLATINUM": "<:plat:1407917008611184762>",
        "GOLD": "<:gold:1407916997303603303>",
        "SILVER": "<:silver:1407917015884103851>",
        "BRONZE": "<:bronze:1407917860763992167>",
        "IRON": "<:iron:1407917003397795901>",
    }

    # ティアごとにプレイヤーをグループ化
    players_by_tier: dict[str, list[dict[str, Any]]] = {}
    for player in sorted_ranks:
        tier: str = player['tier']
        if tier not in players_by_tier:
            players_by_tier[tier] = []
        players_by_tier[tier].append(player)

    # ティアの順序を定義
    tier_order: list[str] = ["CHALLENGER", "GRANDMASTER", "MASTER", "DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON"]

    # ティアごとにフィールドを追加
    rank_counter: int = 1
    for tier in tier_order:
        if tier in players_by_tier:
            tier_players: list[dict[str, Any]] = players_by_tier[tier]
            field_value: str = ""
            for player in tier_players:
                try:
                    user: discord.User = await bot.fetch_user(player['discord_id'])
                    mention_name: str = user.mention
                except discord.NotFound:
                    # サーバーにいないユーザーは display_name を使う（取得できない場合は'N/A'）
                    try:
                        user: discord.User = await bot.fetch_user(player['discord_id'])
                        mention_name: str = user.display_name
                    except:
                        mention_name: str = "N/A"


                riot_id_full: str = f"{player['game_name']}#{player['tag_line'].upper()}"
                # ランク情報の太字を解除
                field_value += f"{rank_counter}. {mention_name} ({riot_id_full})\n{player['tier']} {player['rank']} / {player['lp']}LP\n"
                rank_counter += 1

            if field_value:
                # フィールドのvalue上限(1024文字)を超えないように調整
                if len(field_value) > 1024:
                    field_value = field_value[:1020] + "..."

                # Tierヘッダーのデザインを調整
                # Tier名の長さに応じて罫線の数を変え、全体の長さを揃える
                base_length: int = 28
                header_core_length: int = len(tier) + 4 # 太字化の** **分
                padding_count: int = max(0, base_length - header_core_length)
                padding: str = "─" * padding_count

                header_text: str = f"{role_emojis[tier]} {tier} {role_emojis[tier]} {padding}"

                embed.add_field(
                    name=f"**{header_text}**",
                    value=field_value,
                    inline=False
                )

    return embed

# --- イベント ---
_startup_done: bool = False

@bot.event
async def on_ready() -> None:
    global _startup_done
    print(f"Bot logged in as {bot.user}")

    # on_readyは再接続のたびに発火するため、初回起動時のみ初期化処理を実行
    if _startup_done:
        print("--- Reconnected (skipping initial setup) ---")
        return
    _startup_done = True

    # Bot起動時に永続Viewを登録
    bot.add_view(DashboardView())

    # --- コントリビューション: 起動時のVC在室者スキャン ---
    # 再起動を跨いで在室している組分け済みメンバーを vc_sessions に再登録する
    # （再起動中の時間はロストする。今この瞬間からの計測再開）
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for vc_member in vc.members:
                if vc_member.bot:
                    continue
                if is_sorted(vc_member.id):
                    vc_session_start(vc_member.id, vc.id)
    print("--- VC sessions re-initialized for sorted members ---")

    # 起動時ランキング速報は一旦停止（再開する場合は下記ブロックを有効化）
    # print("--- Posting initial ranking on startup ---")
    # channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    # if channel:
    #     ranking_embed: discord.Embed = await create_ranking_embed()
    #     if ranking_embed:
    #         await channel.send("【起動時ランキング速報】", embed=ranking_embed)

    if not check_ranks_periodically.is_running():
        check_ranks_periodically.start()
    if not weekly_digest_task.is_running():
        weekly_digest_task.start()
    if not monthly_summary_task.is_running():
        monthly_summary_task.start()
    if CLOUDFLARE_INGEST_URL and INGEST_TOKEN and not sync_to_d1_task.is_running():
        sync_to_d1_task.start()
        print("--- D1 sync task started (5min interval) ---")
    elif not (CLOUDFLARE_INGEST_URL and INGEST_TOKEN):
        print("--- D1 sync task SKIPPED: CLOUDFLARE_INGEST_URL / INGEST_TOKEN not set ---")

# --- コマンド ---
@bot.slash_command(name="register", description="あなたのRiot IDをボットに登録します。", guild_ids=[DISCORD_GUILD_ID])
async def register(ctx: discord.ApplicationContext, game_name: str, tag_line: str) -> None:
    await ctx.defer()
    if tag_line.startswith("#"):
        tag_line = tag_line[1:]
    tag_line = tag_line.upper()
    try:
        account_info: dict[str, Any] = riot_watcher.account.by_riot_id(my_region_for_account, game_name, tag_line)
        puuid: str = account_info['puuid']
        rank_info: dict[str, Any] | None = get_rank_by_puuid(puuid)

        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        if rank_info:
            cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (ctx.author.id, puuid, game_name, tag_line, rank_info['tier'], rank_info['rank'], rank_info['leaguePoints']))
        else:
            cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
                        (ctx.author.id, puuid, game_name, tag_line))
        con.commit()
        con.close()
        await ctx.respond(f"Riot ID「{game_name}#{tag_line}」を登録しました！")
    except ApiError as err:
        if err.response.status_code == 404:
            await ctx.respond(f"Riot ID「{game_name}#{tag_line}」が見つかりませんでした。")
        else:
            await ctx.respond("Riot APIでエラーが発生しました。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'register' command: {e}")
        await ctx.respond("登録中に予期せぬエラーが発生しました。")

@bot.slash_command(name="register_by_other", description="指定したユーザーのRiot IDをボットに登録します。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def register_by_other(ctx: discord.ApplicationContext, user: discord.Member, game_name: str, tag_line: str) -> None:
    await ctx.defer(ephemeral=True) # コマンド結果は実行者のみに見える
    if tag_line.startswith("#"):
        tag_line = tag_line[1:]
    tag_line = tag_line.upper()
    try:
        account_info: dict[str, Any] = riot_watcher.account.by_riot_id(my_region_for_account, game_name, tag_line)
        puuid: str = account_info['puuid']
        rank_info: dict[str, Any] | None = get_rank_by_puuid(puuid)

        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        target_discord_id: int = user.id
        if rank_info:
            cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (target_discord_id, puuid, game_name, tag_line, rank_info['tier'], rank_info['rank'], rank_info['leaguePoints']))
        else:
            cur.execute("INSERT OR REPLACE INTO users (discord_id, riot_puuid, game_name, tag_line, tier, rank, league_points) VALUES (?, ?, ?, ?, NULL, NULL, NULL)",
                        (target_discord_id, puuid, game_name, tag_line))
        con.commit()
        con.close()
        await ctx.respond(f"ユーザー「{user.display_name}」にRiot ID「{game_name}#{tag_line}」を登録しました！")
    except ApiError as err:
        if err.response.status_code == 404:
            await ctx.respond(f"Riot ID「{game_name}#{tag_line}」が見つかりませんでした。")
        else:
            await ctx.respond(f"Riot APIでエラーが発生しました。詳細はログを確認してください。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'register_by_other' command: {e}")
        await ctx.respond("登録中に予期せぬエラーが発生しました。")

@bot.slash_command(name="unregister", description="ボットからあなたの登録情報を削除します。", guild_ids=[DISCORD_GUILD_ID])
async def unregister(ctx: discord.ApplicationContext) -> None:
    await ctx.defer()
    try:
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("DELETE FROM users WHERE discord_id = ?", (ctx.author.id,))
        con.commit()
        if con.total_changes > 0:
            await ctx.respond("あなたの登録情報を削除しました。")
        else:
            await ctx.respond("あなたはまだ登録されていません。")
        con.close()

        # --- ランク連動ロール削除処理 ---
        guild: discord.Guild | None = ctx.guild
        if guild:
            member: discord.Member = await guild.fetch_member(ctx.author.id)
            role_names_to_remove: list[discord.Role | None] = [discord.utils.get(guild.roles, name=role_name) for role_name in RANK_ROLES.values()]
            await member.remove_roles(*[role for role in role_names_to_remove if role is not None and role in member.roles])

    except Exception as e:
        await ctx.respond("登録解除中に予期せぬエラーが発生しました。")

@bot.slash_command(name="score", description="あなたの貢献度・レベル・今月のポイントを表示します。", guild_ids=[DISCORD_GUILD_ID])
async def score(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    discord_id: int = ctx.author.id

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT house_id FROM sorting_hat WHERE discord_id = ?", (discord_id,))
    house_row: tuple[str] | None = cur.fetchone()
    if not house_row:
        con.close()
        await ctx.respond(
            "まだ組分け帽子を被っていません。ダッシュボードから組分けすると貢献度が貯まり始めます。",
            ephemeral=True,
        )
        return
    house_info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == house_row[0]), None)

    cur.execute(
        "SELECT total_xp, vc_seconds, text_messages FROM contribution_totals WHERE discord_id = ?",
        (discord_id,),
    )
    totals_row: tuple[int, int, int] | None = cur.fetchone()
    total_xp: int = totals_row[0] if totals_row else 0
    vc_seconds: int = totals_row[1] if totals_row else 0
    text_messages: int = totals_row[2] if totals_row else 0

    ym: str = current_year_month_jst()
    cur.execute(
        "SELECT points, vc_seconds, text_messages FROM contribution_monthly WHERE year_month = ? AND discord_id = ?",
        (ym, discord_id),
    )
    monthly_row: tuple[int, int, int] | None = cur.fetchone()
    monthly_pt: int = monthly_row[0] if monthly_row else 0
    monthly_vc: int = monthly_row[1] if monthly_row else 0
    monthly_text: int = monthly_row[2] if monthly_row else 0
    con.close()

    level: int = level_from_xp(total_xp)
    current_level_xp: int = required_xp_for_level(level)
    next_level_xp: int = required_xp_for_level(level + 1)
    xp_into_current: int = total_xp - current_level_xp
    xp_needed: int = next_level_xp - current_level_xp

    embed: discord.Embed = discord.Embed(
        title=f"📊 {ctx.author.display_name} の貢献度",
        color=discord.Color.blurple(),
    )
    if house_info:
        embed.add_field(name="所属", value=f"{house_info[2]} {house_info[1]}", inline=False)
    embed.add_field(name="個人レベル", value=f"Lv {level}", inline=True)
    embed.add_field(name="累積XP", value=f"{total_xp:,}", inline=True)
    embed.add_field(name="次Lvまで", value=f"{xp_into_current:,} / {xp_needed:,} XP", inline=True)
    embed.add_field(
        name=f"今月（{ym}）",
        value=(
            f"**{monthly_pt:,} pt**\n"
            f"VC {monthly_vc // 3600}時間{(monthly_vc % 3600) // 60}分 / "
            f"投稿 {monthly_text:,}件"
        ),
        inline=False,
    )
    embed.add_field(
        name="累積アクティビティ",
        value=(
            f"VC {vc_seconds // 3600}時間{(vc_seconds % 3600) // 60}分 / "
            f"投稿 {text_messages:,}件"
        ),
        inline=False,
    )
    await ctx.respond(embed=embed, ephemeral=True)


# --- /leaderboard / /house_standings: 貢献度ランキング系 ---
HOUSE_BY_ID: dict[str, tuple[str, str, str, str]] = {h[0]: h for h in HOUSES}


def _house_emoji(house_id: str) -> str:
    h: tuple[str, str, str, str] | None = HOUSE_BY_ID.get(house_id)
    return h[2] if h else "❔"


def _house_name(house_id: str) -> str:
    h: tuple[str, str, str, str] | None = HOUSE_BY_ID.get(house_id)
    return h[1] if h else "?"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


@bot.slash_command(
    name="leaderboard",
    description="個人のコントリビューションランキング（Top10）を表示します。",
    guild_ids=[DISCORD_GUILD_ID],
)
async def leaderboard(
    ctx: discord.ApplicationContext,
    mode: discord.Option(  # type: ignore[valid-type]
        str,
        "並び替え基準",
        choices=["monthly", "total"],
        default="monthly",
    ),
) -> None:
    """組分け済みメンバーの貢献度Top10。mode=monthly: 今月pt順 / mode=total: 累積XP順"""
    await ctx.defer()

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    ym: str = current_year_month_jst()

    if mode == "total":
        cur.execute(
            """
            SELECT s.discord_id, s.house_id,
                   COALESCE(t.total_xp, 0)   AS total_xp,
                   COALESCE(m.points, 0)     AS month_pt
            FROM sorting_hat s
            LEFT JOIN contribution_totals  t ON s.discord_id = t.discord_id
            LEFT JOIN contribution_monthly m ON s.discord_id = m.discord_id AND m.year_month = ?
            ORDER BY total_xp DESC
            LIMIT 10
            """,
            (ym,),
        )
        title: str = "🏆 累積XPランキング（Top 10）"
        primary_label: str = "XP"
    else:
        cur.execute(
            """
            SELECT s.discord_id, s.house_id,
                   COALESCE(t.total_xp, 0)   AS total_xp,
                   COALESCE(m.points, 0)     AS month_pt
            FROM sorting_hat s
            LEFT JOIN contribution_totals  t ON s.discord_id = t.discord_id
            LEFT JOIN contribution_monthly m ON s.discord_id = m.discord_id AND m.year_month = ?
            ORDER BY month_pt DESC, total_xp DESC
            LIMIT 10
            """,
            (ym,),
        )
        title: str = f"🏆 今月の貢献度ランキング（{ym} / Top 10）"
        primary_label: str = "pt"

    rows: list[tuple[int, str, int, int]] = cur.fetchall()
    con.close()

    embed: discord.Embed = discord.Embed(title=title, color=discord.Color.gold())
    if not rows:
        embed.description = "まだ組分けされたメンバーがいないか、貢献度の記録がありません。"
        await ctx.respond(embed=embed)
        return

    lines: list[str] = []
    guild: discord.Guild | None = ctx.guild
    for i, (discord_id, house_id, total_xp, month_pt) in enumerate(rows, start=1):
        member_name: str = f"<@{discord_id}>"
        # ユーザー削除済みのフォールバック
        if guild is not None:
            member: discord.Member | None = guild.get_member(discord_id)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_id)
                except Exception:
                    member = None
            if member is not None:
                member_name = member.mention
        lv: int = level_from_xp(total_xp)
        value: int = total_xp if mode == "total" else month_pt
        lines.append(
            f"`#{i:>2}` {_house_emoji(house_id)} {member_name} ・ Lv {lv} ・ **{_fmt_int(value)} {primary_label}**"
        )

    dashboard_lb_url: str = f"https://pubview-dashboard.pages.dev/leaderboard?mode={mode}"
    embed.description = "\n".join(lines)
    embed.url = dashboard_lb_url
    embed.add_field(
        name="​",
        value=f"[📊 Webダッシュボードのランキングを開く →]({dashboard_lb_url})",
        inline=False,
    )
    embed.set_footer(text="mode=total: 累積XP順 / mode=monthly: 今月pt順")
    await ctx.respond(embed=embed)


@bot.slash_command(
    name="house_standings",
    description="4寮のコントリビューション対抗状況を表示します。",
    guild_ids=[DISCORD_GUILD_ID],
)
async def house_standings(ctx: discord.ApplicationContext) -> None:
    """寮ごとの 寮Lv / 累積XP / 今月pt / メンバー数 を1枚で表示。"""
    await ctx.defer()

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    ym: str = current_year_month_jst()

    cur.execute(
        """
        SELECT s.house_id,
               COUNT(DISTINCT s.discord_id)   AS member_count,
               COALESCE(SUM(t.total_xp), 0)    AS total_xp,
               COALESCE(SUM(m.points),  0)    AS month_pt
        FROM sorting_hat s
        LEFT JOIN contribution_totals  t ON s.discord_id = t.discord_id
        LEFT JOIN contribution_monthly m ON s.discord_id = m.discord_id AND m.year_month = ?
        GROUP BY s.house_id
        """,
        (ym,),
    )
    stats_by_house: dict[str, tuple[int, int, int]] = {
        row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()
    }
    con.close()

    embed: discord.Embed = discord.Embed(
        title=f"🏰 寮対抗スタンディング（{ym}）",
        color=discord.Color.dark_purple(),
    )

    # 寮Lv 順、同点は今月pt降順
    ordered: list[tuple[str, int, int, int, int]] = []
    for house_id, name_jp, emoji, _role in HOUSES:
        member_count, total_xp, month_pt = stats_by_house.get(house_id, (0, 0, 0))
        house_lv: int = level_from_xp(total_xp, CONTRIBUTION_HOUSE_LEVEL_COEF, CONTRIBUTION_HOUSE_LEVEL_EXP)
        ordered.append((house_id, member_count, total_xp, month_pt, house_lv))
    ordered.sort(key=lambda x: (-x[4], -x[3]))

    for i, (house_id, member_count, total_xp, month_pt, house_lv) in enumerate(ordered, start=1):
        h_name: str = _house_name(house_id)
        h_emoji: str = _house_emoji(house_id)
        cur_xp: int = required_xp_for_level(house_lv, CONTRIBUTION_HOUSE_LEVEL_COEF, CONTRIBUTION_HOUSE_LEVEL_EXP)
        next_xp: int = required_xp_for_level(house_lv + 1, CONTRIBUTION_HOUSE_LEVEL_COEF, CONTRIBUTION_HOUSE_LEVEL_EXP)
        in_lv: int = total_xp - cur_xp
        span: int = max(1, next_xp - cur_xp)
        bar_filled: int = max(0, min(10, round((in_lv / span) * 10)))
        bar: str = "▰" * bar_filled + "▱" * (10 - bar_filled)
        embed.add_field(
            name=f"`#{i}` {h_emoji} {h_name}  ・ 寮Lv {house_lv}",
            value=(
                f"{bar} {_fmt_int(in_lv)}/{_fmt_int(next_xp - cur_xp)} XP\n"
                f"累積 **{_fmt_int(total_xp)} XP** ・ 今月 **{_fmt_int(month_pt)} pt** ・ メンバー {member_count}人"
            ),
            inline=False,
        )

    embed.url = "https://pubview-dashboard.pages.dev/"
    embed.add_field(
        name="​",
        value="[📊 Webダッシュボードを開く →](https://pubview-dashboard.pages.dev/)",
        inline=False,
    )
    await ctx.respond(embed=embed)
# -----------------------------


@bot.slash_command(name="ranking", description="サーバー内のLoLランクランキングを表示します。", guild_ids=[DISCORD_GUILD_ID])
async def ranking(ctx: discord.ApplicationContext) -> None:
    await ctx.defer()
    try:
        ranking_embed: discord.Embed = await create_ranking_embed()
        if ranking_embed:
            await ctx.respond(embed=ranking_embed)
        else:
            await ctx.respond("まだ誰も登録されていないか、ランク情報を取得できるユーザーがいません。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'ranking' command: {e}")
        await ctx.respond("ランキングの作成中にエラーが発生しました。")

# --- 管理者向けコマンド ---
@bot.slash_command(name="dashboard", description="登録・登録解除用のダッシュボードを送信します。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def dashboard(ctx: discord.ApplicationContext, channel: discord.TextChannel | None = None) -> None:
    """
    ダッシュボードメッセージを送信します。
    """
    target_channel: discord.TextChannel | discord.VoiceChannel | discord.Thread = channel or ctx.channel
    embed: discord.Embed = discord.Embed(
        title="# ダッシュボード", # 絵文字は適当なものに置き換えてください
        description=(
            "## Riot IDの登録\n"
            "あなたのRiot IDをサーバーに登録しましょう！\n"
            f"このボタンからあなたのRiot IDを登録すると、あなたのSolo/Duoランクが24時間ごとに自動でチェックされ、サーバー内のラダーランキング(<#{NOTIFICATION_CHANNEL_ID}>)に反映されます。\n"
            "## Riot IDの登録解除\n"
            "ボットからあなたのRiot ID情報を削除します。\n"
            "## 🎩 組分け帽子を被る\n"
            "あなたの最高到達Tierを申告すると、4つの組のいずれかに振り分けられ、対応するロールが付与されます。\n"
            "各レート帯ごとに人数が均等になるよう自動で振り分けられます。\n"
        ),
        color=discord.Color.blue()
    )

    await target_channel.send(embed=embed, view=DashboardView())
    await ctx.respond("ダッシュボードを送信しました。", ephemeral=True)

@bot.slash_command(name="setup_house_channels", description="4寮ぶんのカテゴリ・チャンネルを一括作成します。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def setup_house_channels(ctx: discord.ApplicationContext) -> None:
    """寮ごとのカテゴリと配下チャンネル（雑談/リーダーボード/VC）を一括作成する。

    権限設計:
      - カテゴリ: @everyone view denied / 寮ロール view allowed
      - 雑談 / VC: カテゴリから継承
      - リーダーボード: カテゴリから継承 + 寮ロール send_messages denied（Botのみ投稿可）

    idempotent: 既に作成済みのチャンネルがあればスキップ、無いものだけ作る。
    作成結果は house_channels テーブルに記録する。
    """
    await ctx.defer(ephemeral=True)
    guild: discord.Guild | None = ctx.guild
    if guild is None:
        await ctx.respond("ギルド情報が取得できませんでした。", ephemeral=True)
        return

    me: discord.Member | None = guild.me
    bot_member: discord.Member = me if me is not None else await guild.fetch_member(bot.user.id)

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    now_iso: str = datetime.datetime.now(datetime.timezone.utc).isoformat()

    summary_lines: list[str] = []

    for house_id, house_name_jp, house_emoji, role_name in HOUSES:
        house_role: discord.Role | None = discord.utils.get(guild.roles, name=role_name)
        if house_role is None:
            summary_lines.append(f"⚠️ {house_emoji} {house_name_jp}: ロール「{role_name}」が見つかりません。先にロール作成が必要。")
            continue

        # カテゴリ権限: @everyone view denied / 寮ロール view allowed / Bot view allowed
        category_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            house_role:          discord.PermissionOverwrite(view_channel=True, connect=True),
            bot_member:          discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, connect=True),
        }

        category_name: str = f"{house_emoji}｜{house_name_jp}"
        category: discord.CategoryChannel | None = discord.utils.get(guild.categories, name=category_name)
        category_created: bool = False
        if category is None:
            try:
                category = await guild.create_category(name=category_name, overwrites=category_overwrites, reason=f"setup_house_channels: {house_id}")
                category_created = True
            except Exception as e:
                summary_lines.append(f"❌ {house_emoji} {house_name_jp}: カテゴリ作成失敗 ({e})")
                continue
        else:
            # 既存カテゴリの権限を念のため上書き（誤設定リカバリ）
            try:
                for target, ow in category_overwrites.items():
                    await category.set_permissions(target, overwrite=ow, reason="setup_house_channels: enforce overwrites")
            except Exception as e:
                print(f"!!! setup_house_channels: failed to enforce category overwrites for {house_id}: {e}")

        cur.execute(
            "INSERT OR REPLACE INTO house_channels (house_id, channel_type, channel_id, created_at) VALUES (?, ?, ?, ?)",
            (house_id, "category", category.id, now_iso),
        )

        # 配下チャンネル定義: (channel_type, name, kind, extra_overwrites)
        # kind: "text" | "voice"
        # extra_overwrites: リーダーボードは寮ロールに send_messages = False を上書き
        chat_name: str = "雑談"
        board_name: str = "リーダーボード"
        vc_name: str = f"{house_name_jp}VC"

        # リーダーボード用 overwrite（カテゴリ継承 + 寮ロールの send 拒否）
        board_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            house_role:          discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=True),
            bot_member:          discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        }

        sub_channels: list[tuple[str, str, str, dict[discord.Role | discord.Member, discord.PermissionOverwrite] | None]] = [
            ("chat",        chat_name,  "text",  None),
            ("leaderboard", board_name, "text",  board_overwrites),
            ("vc",          vc_name,    "voice", None),
        ]

        for channel_type, name, kind, extra_ow in sub_channels:
            existing: discord.abc.GuildChannel | None
            if kind == "text":
                existing = discord.utils.get(category.text_channels, name=name)
            else:
                existing = discord.utils.get(category.voice_channels, name=name)
            if existing is not None:
                cur.execute(
                    "INSERT OR REPLACE INTO house_channels (house_id, channel_type, channel_id, created_at) VALUES (?, ?, ?, ?)",
                    (house_id, channel_type, existing.id, now_iso),
                )
                continue
            try:
                if kind == "text":
                    created = await guild.create_text_channel(
                        name=name, category=category,
                        overwrites=extra_ow if extra_ow is not None else category_overwrites,
                        reason=f"setup_house_channels: {house_id}/{channel_type}",
                    )
                else:
                    created = await guild.create_voice_channel(
                        name=name, category=category, user_limit=0,
                        reason=f"setup_house_channels: {house_id}/{channel_type}",
                    )
                cur.execute(
                    "INSERT OR REPLACE INTO house_channels (house_id, channel_type, channel_id, created_at) VALUES (?, ?, ?, ?)",
                    (house_id, channel_type, created.id, now_iso),
                )
            except Exception as e:
                summary_lines.append(f"❌ {house_emoji} {house_name_jp}/{name}: 作成失敗 ({e})")

        summary_lines.append(
            f"{'🆕' if category_created else '♻️'} {house_emoji} {house_name_jp}: カテゴリ + 雑談 + リーダーボード + VC OK"
        )

    con.commit()
    con.close()

    msg: str = "**寮チャンネルセットアップ完了**\n" + "\n".join(summary_lines)
    await ctx.respond(msg, ephemeral=True)


@bot.slash_command(
    name="set_house_leader",
    description="指定ユーザーをその寮の寮長に任命します。（管理者向け）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def set_house_leader(
    ctx: discord.ApplicationContext,
    house: discord.Option(  # type: ignore[valid-type]
        str,
        "対象寮",
        choices=["raptor", "krug", "wolf", "gromp"],
    ),
    user: discord.Member,
) -> None:
    """対象ユーザーを寮長に任命。対象は同寮所属のメンバーであることを推奨（ただし制約はかけない）。"""
    await ctx.defer(ephemeral=True)
    user_house: str | None = get_user_house_id(user.id)
    info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == house), None)
    if info is None:
        await ctx.respond(f"unknown house: {house}", ephemeral=True)
        return
    now_iso: str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO house_leaders (house_id, discord_id, set_at, set_by) VALUES (?, ?, ?, ?)",
        (house, user.id, now_iso, ctx.author.id),
    )
    con.commit()
    con.close()
    warn: str = ""
    if user_house is None:
        warn = "\n⚠️ 対象ユーザーはまだ組分け帽子を被っていません。"
    elif user_house != house:
        warn = f"\n⚠️ 対象ユーザーは別の寮 ({_house_name(user_house)}) に所属しています。"
    await ctx.respond(
        f"✅ {info[2]} {info[1]} の寮長に {user.mention} を任命しました。{warn}\n"
        "次回 D1 同期 (5分以内) でダッシュボードに反映されます。",
        ephemeral=True,
    )


@bot.slash_command(
    name="clear_house_leader",
    description="指定寮の寮長任命を解除します。（管理者向け）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def clear_house_leader(
    ctx: discord.ApplicationContext,
    house: discord.Option(  # type: ignore[valid-type]
        str,
        "対象寮",
        choices=["raptor", "krug", "wolf", "gromp"],
    ),
) -> None:
    await ctx.defer(ephemeral=True)
    info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == house), None)
    if info is None:
        await ctx.respond(f"unknown house: {house}", ephemeral=True)
        return
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("DELETE FROM house_leaders WHERE house_id = ?", (house,))
    changed: int = cur.rowcount
    con.commit()
    con.close()
    if changed == 0:
        await ctx.respond(f"{info[2]} {info[1]} には寮長が任命されていません。", ephemeral=True)
    else:
        await ctx.respond(
            f"✅ {info[2]} {info[1]} の寮長任命を解除しました。次回 D1 同期で反映されます。",
            ephemeral=True,
        )


@bot.slash_command(name="add_section", description="参加可能なセクションを登録します。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def add_section(ctx: discord.ApplicationContext, section_role: discord.Role, notification_channel: discord.TextChannel) -> None:
    await ctx.defer(ephemeral=True)
    try:
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("INSERT OR REPLACE INTO sections (role_id, section_name, notification_channel_id) VALUES (?, ?, ?)",
                    (section_role.id, section_role.name, notification_channel.id))
        con.commit()
        con.close()
        await ctx.respond(f"セクション（ロール「{section_role.name}」）を、通知チャンネル「{notification_channel.name}」と紐付けて登録しました。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'add_section' command: {e}")
        await ctx.respond("セクションの登録中に予期せぬエラーが発生しました。")

@bot.slash_command(name="remove_section", description="参加可能なセクションを削除します。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def remove_section(ctx: discord.ApplicationContext, section_role: discord.Role) -> None:
    await ctx.defer(ephemeral=True)
    try:
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("DELETE FROM sections WHERE role_id = ?", (section_role.id,))
        con.commit()

        if con.total_changes > 0:
            await ctx.respond(f"セクション（ロール「{section_role.name}」）をDBから削除しました。")
        else:
            await ctx.respond(f"指定されたセクション（ロール）はDBに登録されていません。")

        con.close()
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'remove_section' command: {e}")
        await ctx.respond("セクションの削除中に予期せぬエラーが発生しました。")


@bot.slash_command(name="remove_user_from_section", description="指定したユーザーをセクションから退出させます。（管理者向け）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def remove_user_from_section(ctx: discord.ApplicationContext, user: discord.Member, section_role: discord.Role) -> None:
    await ctx.defer(ephemeral=True)

    # 指定されたロールがセクションとして登録されているか確認
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT 1 FROM sections WHERE role_id = ?", (section_role.id,))
    is_section: tuple[int] | None = cur.fetchone()
    con.close()

    if not is_section:
        await ctx.respond(f"エラー: ロール「{section_role.name}」はセクションとして登録されていません。")
        return

    if section_role not in user.roles:
        await ctx.respond(f"ユーザー「{user.display_name}」はセクション「{section_role.name}」に参加していません。")
        return

    try:
        await user.remove_roles(section_role)
        await ctx.respond(f"ユーザー「{user.display_name}」をセクション「{section_role.name}」から退出させました。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'remove_user_from_section' command: {e}")
        await ctx.respond("セクションからの退出処理中に予期せぬエラーが発生しました。")


# --- デバッグ用コマンド ---
@bot.slash_command(
    name="setup_dev_channel",
    description="開発者用のテストチャンネルを作成します（実行者と Bot のみ閲覧可）。",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def setup_dev_channel(
    ctx: discord.ApplicationContext,
    channel_name: discord.Option(str, "作成するチャンネル名", default="🧪｜bot-test"),  # type: ignore[valid-type]
) -> None:
    """テスト用のプライベートチャンネルを作成する。

    権限: @everyone view denied / 実行者 view+send allow / Bot view+send+manage allow
    既存があればそのまま流用してIDだけ返す（idempotent）。
    """
    await ctx.defer(ephemeral=True)
    guild: discord.Guild | None = ctx.guild
    if guild is None:
        await ctx.respond("ギルド情報が取得できませんでした。", ephemeral=True)
        return

    existing: discord.TextChannel | None = discord.utils.get(guild.text_channels, name=channel_name)
    if existing is not None:
        await ctx.respond(
            f"既に同名のチャンネルが存在します: {existing.mention} (ID: `{existing.id}`)",
            ephemeral=True,
        )
        return

    me: discord.Member = guild.me if guild.me is not None else await guild.fetch_member(bot.user.id)
    author: discord.User | discord.Member = ctx.author
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        author:             discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        me:                 discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, manage_channels=True),
    }
    try:
        created: discord.TextChannel = await guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            reason=f"setup_dev_channel by {author}",
        )
    except Exception as e:
        await ctx.respond(f"作成失敗: `{e}`", ephemeral=True)
        return
    await ctx.respond(
        f"✅ テストチャンネル {created.mention} を作成しました（ID: `{created.id}`）。\n"
        "あなたと Bot のみ閲覧可能です。他の管理者を追加したい場合は手動で permission を編集してください。",
        ephemeral=True,
    )


@bot.slash_command(name="debug_check_ranks_periodically", description="定期的なランクチェックを手動で実行します。（デバッグ用）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def debug_check_ranks_periodically(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    try:
        await ctx.respond("定期ランクチェック処理を開始します...")
        await check_ranks_periodically()
        await ctx.followup.send("定期ランクチェック処理が完了しました。")
    except Exception as e:
        await ctx.followup.send(f"処理中にエラーが発生しました: {e}")

@bot.slash_command(name="debug_rank_all_iron", description="登録者全員のランクをIron IVに設定します。（デバッグ用）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def debug_rank_all_iron(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    try:
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        # 全ユーザーのランク情報を更新
        cur.execute("UPDATE users SET tier = 'IRON', rank = 'IV', league_points = 0")
        count: int = cur.rowcount
        con.commit()
        con.close()
        await ctx.respond(f"{count}人のユーザーのランクをIron IVに設定しました。")
    except Exception as e:
        await ctx.respond(f"処理中にエラーが発生しました: {e}")

@bot.slash_command(name="debug_modify_rank", description="特定のユーザーのランクを強制的に変更します。（デバッグ用）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def debug_modify_rank(ctx: discord.ApplicationContext, user: discord.Member, tier: str, rank: str, league_points: int) -> None:
    await ctx.defer(ephemeral=True)
    TIERS: list[str] = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    RANKS: list[str] = ["I", "II", "III", "IV"]

    if tier.upper() not in TIERS or rank.upper() not in RANKS:
        await ctx.respond(f"無効なTierまたはRankです。\nTier: {', '.join(TIERS)}\nRank: {', '.join(RANKS)}")
        return

    try:
        con: sqlite3.Connection = sqlite3.connect(DB_PATH)
        cur: sqlite3.Cursor = con.cursor()
        cur.execute("UPDATE users SET tier = ?, rank = ?, league_points = ? WHERE discord_id = ?",
                    (tier.upper(), rank.upper(), league_points, user.id))

        count: int = cur.rowcount
        con.commit()
        con.close()

        if count > 0:
            await ctx.respond(f"ユーザー「{user.display_name}」のランクを {tier.upper()} {rank.upper()} {league_points}LP に設定しました。")
        else:
            await ctx.respond(f"ユーザー「{user.display_name}」は見つかりませんでした。先に/registerで登録してください。")

    except Exception as e:
        await ctx.respond(f"処理中にエラーが発生しました: {e}")


@bot.slash_command(
    name="debug_grant_xp",
    description="指定ユーザーにXPを付与してLvアップを強制発火させます。（デバッグ用）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def debug_grant_xp(
    ctx: discord.ApplicationContext,
    user: discord.Member,
    amount: int,
) -> None:
    """組分け済みユーザーに任意のXPを加算する。Lvが上がれば祝福通知も発火する。"""
    await ctx.defer(ephemeral=True)
    if not is_sorted(user.id):
        await ctx.respond(f"{user.display_name} はまだ組分け帽子を被っていません。", ephemeral=True)
        return
    if amount == 0:
        await ctx.respond("amount は 0 以外を指定してください。", ephemeral=True)
        return
    old_level, new_level = add_contribution(user.id, xp=amount, vc_seconds=0, text_messages=0)
    msg: str = f"✅ {user.display_name} に {amount:+,} XP 加算しました（Lv {old_level} → Lv {new_level}）。"
    if new_level > old_level:
        msg += " Lvアップ通知が所属寮のリーダーボードに投稿されます。"
        await notify_level_up(user.id, old_level, new_level)
    await ctx.respond(msg, ephemeral=True)


# --- 週次ダイジェスト / 月次サマリ ---
DASHBOARD_URL: str = "https://pubview-dashboard.pages.dev/"


def _bar10(progress: float) -> str:
    """0.0〜1.0 を10段階のバー文字列に。"""
    filled: int = max(0, min(10, round(progress * 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _this_jst_monday(now_jst: datetime.datetime | None = None) -> datetime.date:
    """今週月曜日のJST日付（土曜だったら今週月曜＝5日前）。"""
    if now_jst is None:
        now_jst = datetime.datetime.now(jst)
    return (now_jst.date() - datetime.timedelta(days=now_jst.weekday()))


def save_weekly_snapshot(snapshot_date: datetime.date) -> int:
    """全組分け済みメンバーの現在累積をスナップショット保存。書き込んだ行数を返す。"""
    iso_date: str = snapshot_date.isoformat()
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO weekly_snapshots (snapshot_date, discord_id, total_xp, vc_seconds, text_messages)
        SELECT ?, s.discord_id,
               COALESCE(t.total_xp, 0),
               COALESCE(t.vc_seconds, 0),
               COALESCE(t.text_messages, 0)
        FROM sorting_hat s
        LEFT JOIN contribution_totals t ON s.discord_id = t.discord_id
        """,
        (iso_date,),
    )
    written: int = cur.rowcount
    con.commit()
    con.close()
    return written


async def post_weekly_digest_for_house(house_id: str) -> str:
    """指定寮の週次ダイジェストを 該当寮 リーダーボード へ投稿。

    返り値: 結果サマリ文字列（デバッグコマンドが拾って表示）。
    """
    info: tuple[str, str, str, str] | None = next((h for h in HOUSES if h[0] == house_id), None)
    if info is None:
        return f"unknown house: {house_id}"
    _, house_name_jp, house_emoji, _ = info

    channel_id: int | None = get_house_channel_id(house_id, "leaderboard")
    if channel_id is None:
        return f"no leaderboard channel registered for {house_id}"
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return f"channel {channel_id} not text channel"

    now_jst: datetime.datetime = datetime.datetime.now(jst)
    this_monday: datetime.date = _this_jst_monday(now_jst)
    last_monday: datetime.date = this_monday - datetime.timedelta(days=7)
    this_sunday: datetime.date = this_monday + datetime.timedelta(days=6)

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    # 寮メンバー + 現在の累積 + 先週のスナップショット
    cur.execute(
        """
        SELECT s.discord_id,
               COALESCE(t.total_xp,     0) AS total_xp,
               COALESCE(t.vc_seconds,   0) AS vc_seconds,
               COALESCE(t.text_messages,0) AS text_messages,
               COALESCE(ws.total_xp,    0) AS prev_xp,
               COALESCE(ws.vc_seconds,  0) AS prev_vc,
               COALESCE(ws.text_messages,0) AS prev_text,
               (ws.discord_id IS NOT NULL) AS has_prev
        FROM sorting_hat s
        LEFT JOIN contribution_totals t ON s.discord_id = t.discord_id
        LEFT JOIN weekly_snapshots   ws ON s.discord_id = ws.discord_id AND ws.snapshot_date = ?
        WHERE s.house_id = ?
        """,
        (last_monday.isoformat(), house_id),
    )
    rows: list[tuple[int, int, int, int, int, int, int, int]] = cur.fetchall()
    con.close()

    if not rows:
        try:
            await channel.send(f"今週のダイジェスト: {house_emoji} {house_name_jp} はまだメンバーがいません。")
        except Exception as e:
            return f"send failed: {e}"
        return f"{house_id}: no members"

    has_baseline: bool = any(r[7] for r in rows)
    members: list[dict[str, Any]] = []
    for did, total_xp, vc_sec, txt, prev_xp, prev_vc, prev_text, has_prev in rows:
        gained_xp: int = total_xp - prev_xp if has_prev else 0  # 初回はゼロ表示（先週比なし）
        gained_vc: int = vc_sec - prev_vc if has_prev else 0
        gained_text: int = txt - prev_text if has_prev else 0
        members.append({
            "discord_id": did,
            "total_xp": total_xp,
            "gained_xp": gained_xp,
            "gained_vc": gained_vc,
            "gained_text": gained_text,
            "level": level_from_xp(total_xp),
        })

    members.sort(key=lambda m: m["gained_xp"], reverse=True)

    house_gained: int = sum(m["gained_xp"] for m in members)
    active_count: int = sum(1 for m in members if m["gained_xp"] > 0)
    member_count: int = len(members)
    total_vc: int = sum(m["gained_vc"] for m in members)
    total_text: int = sum(m["gained_text"] for m in members)

    # KPIs
    kpi_lines: list[str] = [
        f"├ 今週の獲得pt: **+{house_gained:,} pt**",
        f"├ 参加メンバー: **{active_count}/{member_count}** ({(active_count/max(1,member_count)*100):.1f}%)",
        f"├ VC在室時間: **{total_vc//3600}h {(total_vc%3600)//60}m**",
        f"└ テキスト投稿: **{total_text:,}件**",
    ]

    # トップコントリビューター（上位5）
    top_lines: list[str] = []
    for i, m in enumerate(members[:5], start=1):
        lv: int = m["level"]
        cur_xp: int = required_xp_for_level(lv)
        next_xp: int = required_xp_for_level(lv + 1)
        in_lv: int = m["total_xp"] - cur_xp
        span: int = max(1, next_xp - cur_xp)
        share: float = (m["gained_xp"] / house_gained * 100) if house_gained > 0 else 0.0
        bar: str = _bar10(in_lv / span)
        top_lines.append(
            f"<@{m['discord_id']}> **Lv.{lv}** ({in_lv:,}/{span:,} XP)\n"
            f"{bar} **+{m['gained_xp']:,} pt** ({share:.1f}%)"
        )

    # 寮間順位算出
    standings: list[tuple[str, int]] = []
    con2: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur2: sqlite3.Cursor = con2.cursor()
    for h in HOUSES:
        cur2.execute(
            """
            SELECT COALESCE(SUM(t.total_xp - COALESCE(ws.total_xp, 0)), 0)
            FROM sorting_hat s
            LEFT JOIN contribution_totals t ON s.discord_id = t.discord_id
            LEFT JOIN weekly_snapshots   ws ON s.discord_id = ws.discord_id AND ws.snapshot_date = ?
            WHERE s.house_id = ?
            """,
            (last_monday.isoformat(), h[0]),
        )
        result: tuple[int] = cur2.fetchone()
        standings.append((h[0], result[0]))
    con2.close()
    standings.sort(key=lambda x: x[1], reverse=True)
    rank: int = next((i for i, (hid, _) in enumerate(standings, start=1) if hid == house_id), 0)
    rank_line: str = ""
    if rank == 1 and len(standings) > 1:
        runner_up_pt: int = standings[1][1]
        rank_line = f"🏃 順位: **4寮中 1位** / 2位{HOUSE_BY_ID[standings[1][0]][1]}との差 +{house_gained - runner_up_pt:,} pt"
    elif rank >= 2:
        leader_pt: int = standings[0][1]
        rank_line = f"🏃 順位: 4寮中 {rank}位 / 1位{HOUSE_BY_ID[standings[0][0]][1]}まで +{leader_pt - house_gained:,} pt"

    desc: str = "\n".join(kpi_lines)
    house_page_url: str = f"https://pubview-dashboard.pages.dev/houses/{house_id}"
    embed: discord.Embed = discord.Embed(
        title=f"{house_emoji} {house_name_jp} 週次ダイジェスト",
        url=house_page_url,
        description=(
            f"📅 {this_monday.strftime('%Y年%m月%d日')} 〜 {this_sunday.strftime('%Y年%m月%d日')}\n\n"
            f"📊 主要指標 (KPIs)\n{desc}"
        ),
        color=discord.Color.dark_purple(),
    )
    if not has_baseline:
        embed.add_field(name="📌 注記", value="初回計測（先週比なし）", inline=False)
    if top_lines:
        embed.add_field(name="🏆 トップコントリビューター", value="\n\n".join(top_lines)[:1024], inline=False)
    if rank_line:
        embed.add_field(name="📈 トレンド", value=rank_line, inline=False)
    embed.add_field(
        name="​",
        value=f"[📊 {house_name_jp} の寮ページを見る →]({house_page_url})",
        inline=False,
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
        )
    except Exception as e:
        return f"send failed: {e}"
    return f"{house_id}: posted ({house_gained:,} pt, {active_count}/{member_count} active)"


async def post_weekly_digest_all() -> list[str]:
    """4寮の週次ダイジェストを投稿し、最後に新スナップショットを保存。"""
    summaries: list[str] = []
    for h in HOUSES:
        summary: str = await post_weekly_digest_for_house(h[0])
        summaries.append(summary)
    today: datetime.date = _this_jst_monday()
    save_weekly_snapshot(today)
    return summaries


async def post_monthly_summary(year_month_override: str | None = None) -> str:
    """月次サマリを NOTIFICATION_CHANNEL_ID へ投稿。

    year_month_override が None の場合は「先月」を対象（毎月1日0:00発火想定）。
    """
    now_jst: datetime.datetime = datetime.datetime.now(jst)
    if year_month_override is not None:
        target_ym: str = year_month_override
    else:
        prev_month_last_day: datetime.date = now_jst.date().replace(day=1) - datetime.timedelta(days=1)
        target_ym = prev_month_last_day.strftime("%Y-%m")

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return f"NOTIFICATION_CHANNEL_ID {NOTIFICATION_CHANNEL_ID} not text channel"

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()

    # 寮別合計
    cur.execute(
        """
        SELECT s.house_id, COALESCE(SUM(m.points), 0) AS pt, COUNT(DISTINCT s.discord_id) AS members
        FROM sorting_hat s
        LEFT JOIN contribution_monthly m ON s.discord_id = m.discord_id AND m.year_month = ?
        GROUP BY s.house_id
        """,
        (target_ym,),
    )
    house_rows: list[tuple[str, int, int]] = cur.fetchall()
    house_rows.sort(key=lambda r: r[1], reverse=True)

    # 個人MVP Top5
    cur.execute(
        """
        SELECT m.discord_id, s.house_id, m.points
        FROM contribution_monthly m
        JOIN sorting_hat s ON m.discord_id = s.discord_id
        WHERE m.year_month = ?
        ORDER BY m.points DESC
        LIMIT 5
        """,
        (target_ym,),
    )
    mvp_rows: list[tuple[int, str, int]] = cur.fetchall()
    con.close()

    medals: list[str] = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    house_lines: list[str] = []
    for i, (house_id, pt, members) in enumerate(house_rows):
        info: tuple[str, str, str, str] = HOUSE_BY_ID[house_id]
        medal: str = medals[i] if i < len(medals) else f"{i+1}."
        house_lines.append(f"{medal} {info[2]} {info[1]} — **{pt:,} pt** ({members}人)")
    mvp_lines: list[str] = []
    if mvp_rows:
        for i, (did, hid, pt) in enumerate(mvp_rows, start=1):
            house_emoji: str = HOUSE_BY_ID[hid][2]
            mvp_lines.append(f"`#{i}` {house_emoji} <@{did}> — **{pt:,} pt**")

    monthly_page_url: str = f"https://pubview-dashboard.pages.dev/monthly/{target_ym}"
    embed: discord.Embed = discord.Embed(
        title=f"📅 {target_ym} 月次サマリ",
        url=monthly_page_url,
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🏰 寮対抗ランキング",
        value="\n".join(house_lines) if house_lines else "（記録なし）",
        inline=False,
    )
    embed.add_field(
        name="🌟 個人MVP Top 5",
        value="\n".join(mvp_lines) if mvp_lines else "（記録なし）",
        inline=False,
    )
    embed.add_field(
        name="​",
        value=f"[📊 {target_ym} のアーカイブを見る →]({monthly_page_url})",
        inline=False,
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
        )
    except Exception as e:
        return f"send failed: {e}"
    return f"posted monthly summary for {target_ym}"
# -----------------------------


# --- Bot → D1 同期 ---
async def sync_to_d1() -> dict[str, Any]:
    """Bot SQLite の全テーブルを取得して /api/ingest へ POST する。

    返り値: {ok: bool, status, counts, error?}
    """
    if not CLOUDFLARE_INGEST_URL or not INGEST_TOKEN:
        return {"ok": False, "error": "CLOUDFLARE_INGEST_URL / INGEST_TOKEN unset"}

    guild: discord.Guild | None = bot.get_guild(DISCORD_GUILD_ID)
    if guild is None:
        return {"ok": False, "error": f"guild {DISCORD_GUILD_ID} not in cache"}

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()

    # users: Bot DB の Riot 情報 + Discord プロフィール（display_name/avatar）を結合
    cur.execute("SELECT discord_id, game_name, tag_line, tier, rank, league_points FROM users")
    riot_users: dict[int, tuple[str | None, str | None, str | None, str | None, int | None]] = {
        row[0]: (row[1], row[2], row[3], row[4], row[5]) for row in cur.fetchall()
    }

    # sorting_hat 配下の discord_id も含めて users 配列に入れる
    cur.execute("SELECT discord_id FROM sorting_hat")
    sorted_ids: set[int] = {row[0] for row in cur.fetchall()}
    all_discord_ids: set[int] = set(riot_users.keys()) | sorted_ids
    # house_leaders, contribution_totals/monthly に居るが他に無いユーザーもDiscordプロファイル必要
    cur.execute("SELECT discord_id FROM contribution_totals")
    for r in cur.fetchall(): all_discord_ids.add(r[0])
    cur.execute("SELECT discord_id FROM house_leaders")
    for r in cur.fetchall(): all_discord_ids.add(r[0])

    users_payload: list[dict[str, Any]] = []
    for did in all_discord_ids:
        member: discord.Member | None = guild.get_member(did)
        if member is None:
            try:
                member = await guild.fetch_member(did)
            except Exception:
                member = None
        display_name: str = member.display_name if member is not None else f"user_{did}"
        avatar_url: str | None = str(member.display_avatar.url) if member is not None else None
        game_name, tag_line, tier, rank_, lp = riot_users.get(did, (None, None, None, None, None))
        users_payload.append({
            "discord_id": str(did),
            "display_name": display_name,
            "avatar_url": avatar_url,
            "riot_game_name": game_name,
            "riot_tag_line": tag_line,
            "tier": tier,
            "rank": rank_,
            "league_points": lp,
        })

    cur.execute("SELECT discord_id, house_id, rate_bracket, tier, sorted_at FROM sorting_hat")
    sorting_payload: list[dict[str, Any]] = [
        {"discord_id": str(r[0]), "house_id": r[1], "rate_bracket": r[2], "tier": r[3], "sorted_at": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute("SELECT discord_id, total_xp, vc_seconds, text_messages, updated_at FROM contribution_totals")
    totals_payload: list[dict[str, Any]] = [
        {"discord_id": str(r[0]), "total_xp": r[1], "vc_seconds": r[2], "text_messages": r[3], "updated_at": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute("SELECT year_month, discord_id, points, vc_seconds, text_messages FROM contribution_monthly")
    monthly_payload: list[dict[str, Any]] = [
        {"year_month": r[0], "discord_id": str(r[1]), "points": r[2], "vc_seconds": r[3], "text_messages": r[4]}
        for r in cur.fetchall()
    ]

    cur.execute("SELECT house_id, discord_id, set_at, set_by FROM house_leaders")
    leaders_payload: list[dict[str, Any]] = [
        {"house_id": r[0], "discord_id": str(r[1]), "set_at": r[2], "set_by": str(r[3]) if r[3] else None}
        for r in cur.fetchall()
    ]
    con.close()

    body: dict[str, Any] = {
        "bot_version": BOT_VERSION,
        "users": users_payload,
        "sorting_hat": sorting_payload,
        "contribution_totals": totals_payload,
        "contribution_monthly": monthly_payload,
        "house_leaders": leaders_payload,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CLOUDFLARE_INGEST_URL,
                headers={"Authorization": f"Bearer {INGEST_TOKEN}", "Content-Type": "application/json"},
                data=json.dumps(body),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status: int = resp.status
                text: str = await resp.text()
    except Exception as e:
        return {"ok": False, "error": f"request failed: {e}"}

    if status != 200:
        return {"ok": False, "status": status, "error": text[:300]}
    try:
        result: dict[str, Any] = json.loads(text)
    except Exception:
        result = {"raw": text}
    return {"ok": True, "status": status, "counts": result.get("counts", {})}
# -----------------------------


# --- デバッグ用: ダイジェスト/サマリ手動投稿 ---
@bot.slash_command(
    name="debug_post_weekly_digest",
    description="週次寮ダイジェストを手動投稿します。house 指定なしで全寮。（デバッグ用）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def debug_post_weekly_digest(
    ctx: discord.ApplicationContext,
    house: discord.Option(  # type: ignore[valid-type]
        str,
        "対象寮（省略時は全寮）",
        choices=["raptor", "krug", "wolf", "gromp", "all"],
        default="all",
    ),
) -> None:
    await ctx.defer(ephemeral=True)
    if house == "all":
        summaries: list[str] = await post_weekly_digest_all()
        await ctx.respond("✅ 全寮ダイジェスト投稿\n" + "\n".join(f"- {s}" for s in summaries), ephemeral=True)
    else:
        summary: str = await post_weekly_digest_for_house(house)
        await ctx.respond(f"✅ ダイジェスト投稿: {summary}", ephemeral=True)


@bot.slash_command(
    name="debug_post_monthly_summary",
    description="月次サマリを手動投稿します。（デバッグ用）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def debug_post_monthly_summary(
    ctx: discord.ApplicationContext,
    year_month: discord.Option(  # type: ignore[valid-type]
        str,
        "対象年月 (例: 2026-05)。省略時は先月。",
        default="",
    ) = "",
) -> None:
    await ctx.defer(ephemeral=True)
    ym_arg: str | None = year_month if year_month else None
    result: str = await post_monthly_summary(ym_arg)
    await ctx.respond(f"✅ {result}", ephemeral=True)


@bot.slash_command(
    name="debug_save_weekly_snapshot",
    description="今この瞬間の週次スナップショットを保存します。（デバッグ用）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def debug_save_weekly_snapshot(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    today: datetime.date = _this_jst_monday()
    n: int = save_weekly_snapshot(today)
    await ctx.respond(f"✅ snapshot saved: date={today.isoformat()} rows={n}", ephemeral=True)


@bot.slash_command(
    name="debug_force_sync",
    description="Cloudflare D1 への同期を即時実行します。（デバッグ用）",
    guild_ids=[DISCORD_GUILD_ID],
)
@discord.default_permissions(administrator=True)
async def debug_force_sync(ctx: discord.ApplicationContext) -> None:
    await ctx.defer(ephemeral=True)
    result: dict[str, Any] = await sync_to_d1()
    if result.get("ok"):
        counts: dict[str, int] = result.get("counts", {})
        await ctx.respond(
            f"✅ 同期成功\n```json\n{json.dumps(counts, indent=2, ensure_ascii=False)}\n```",
            ephemeral=True,
        )
    else:
        await ctx.respond(f"❌ 同期失敗\nstatus={result.get('status')} error={result.get('error')}", ephemeral=True)


# --- バックグラウンドタスク ---
@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=jst))
async def check_ranks_periodically() -> None:
    print("--- Starting periodic rank check ---")

    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = bot.get_channel(NOTIFICATION_CHANNEL_ID)

    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT discord_id, riot_puuid, tier, rank, game_name, tag_line FROM users")
    registered_users: list[tuple[int, str, str | None, str | None, str, str]] = cur.fetchall()
    if not registered_users:
        con.close()
        return

    if not channel:
        print(f"Error: Notification channel with ID {NOTIFICATION_CHANNEL_ID} not found.")
        con.close()
        return

    promoted_users: list[dict[str, Any]] = []
    for discord_id, puuid, old_tier, old_rank, game_name, tag_line in registered_users:
        try:
            new_rank_info: dict[str, Any] | None = get_rank_by_puuid(puuid)
            guild: discord.Guild | None = channel.guild
            if not guild:
                continue
            member: discord.Member | None = await guild.fetch_member(discord_id)
            if not member: continue

            # --- データベース更新 ---
            if new_rank_info:
                cur.execute("UPDATE users SET tier = ?, rank = ?, league_points = ? WHERE discord_id = ?",
                            (new_rank_info['tier'], new_rank_info['rank'], new_rank_info['leaguePoints'], discord_id))
            else:
                cur.execute("UPDATE users SET tier = NULL, rank = NULL, league_points = NULL WHERE discord_id = ?", (discord_id,))

            # --- ランクアップ判定 ---
            if new_rank_info and old_tier and old_rank:
                old_value: int = rank_to_value(old_tier, old_rank, 0)
                new_value: int = rank_to_value(new_rank_info['tier'], new_rank_info['rank'], 0)
                if new_value > old_value:
                    promoted_users.append({
                        "member": member,
                        "game_name": game_name,
                        "tag_line": tag_line,
                        "old_tier": old_tier,
                        "old_rank": old_rank,
                        "new_tier": new_rank_info['tier'],
                        "new_rank": new_rank_info['rank']
                    })

            # --- ランク連動ロール処理 ---
            current_rank_tier: str | None = new_rank_info['tier'].upper() if new_rank_info else None

            # 現在のユーザーが持っているランクロールを確認
            current_rank_role: discord.Role | None = None
            for role_name in RANK_ROLES.values():
                role: discord.Role | None = discord.utils.get(guild.roles, name=role_name)
                if role and role in member.roles:
                    current_rank_role = role
                    break

            # 新しいランクに対応するロールを取得
            new_rank_role: discord.Role | None = None
            if current_rank_tier and current_rank_tier in RANK_ROLES:
                new_rank_role = discord.utils.get(guild.roles, name=RANK_ROLES[current_rank_tier])

            # ロールの変更が必要な場合のみ処理
            if current_rank_role != new_rank_role:
                # 古いランクロールを削除（存在する場合）
                if current_rank_role:
                    await member.remove_roles(current_rank_role)

                # 新しいランクロールを追加（存在する場合）
                if new_rank_role:
                    await member.add_roles(new_rank_role)

        except discord.NotFound:
             print(f"User with ID {discord_id} not found in the server. Skipping.")
             continue
        except Exception as e:
            print(f"Error processing user {discord_id}: {e}")
            continue

    con.commit()
    con.close()

    # --- 定期ランキング速報処理 ---
    if channel:
        ranking_embed: discord.Embed = await create_ranking_embed()
        if ranking_embed:
            await channel.send("【定期ランキング速報】", embed=ranking_embed)

    # --- ランクアップ通知処理 ---
    if channel and promoted_users:
        for user_data in promoted_users:
            riot_id_full: str = f"{user_data['game_name']}#{user_data['tag_line'].upper()}"
            await channel.send(f"🎉 **ランクアップ！** 🎉\nおめでとうございます、{user_data['member'].mention}さん ({riot_id_full})！\n**{user_data['old_tier']} {user_data['old_rank']}** → **{user_data['new_tier']} {user_data['new_rank']}** に昇格しました！")

    print("--- Periodic rank check finished ---")


# 週次寮ダイジェスト: 毎日 10:00 JST に発火 → 月曜だけ実投稿
@tasks.loop(time=datetime.time(hour=10, minute=0, tzinfo=jst))
async def weekly_digest_task() -> None:
    now_jst: datetime.datetime = datetime.datetime.now(jst)
    if now_jst.weekday() != 0:  # 0 = Monday
        return
    print(f"--- weekly_digest_task firing on {now_jst.isoformat()} ---")
    summaries: list[str] = await post_weekly_digest_all()
    for s in summaries:
        print(f"  weekly_digest: {s}")


# 月次サマリ: 毎日 0:00 JST に発火 → 1日だけ実投稿
@tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=jst))
async def monthly_summary_task() -> None:
    now_jst: datetime.datetime = datetime.datetime.now(jst)
    if now_jst.day != 1:
        return
    print(f"--- monthly_summary_task firing on {now_jst.isoformat()} ---")
    result: str = await post_monthly_summary()
    print(f"  monthly_summary: {result}")


# Bot → Cloudflare D1: 5分おきに全件同期
@tasks.loop(minutes=5)
async def sync_to_d1_task() -> None:
    if not CLOUDFLARE_INGEST_URL or not INGEST_TOKEN:
        return  # 環境変数なしのときは何もしない
    result: dict[str, Any] = await sync_to_d1()
    if result.get("ok"):
        counts: dict[str, int] = result.get("counts", {})
        print(f"--- sync_to_d1 OK: {counts} ---")
    else:
        print(f"!!! sync_to_d1 FAILED: status={result.get('status')} error={result.get('error')}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Bot自身・DM・システムメッセージは除外
    if message.author.bot or message.guild is None:
        return
    discord_id: int = message.author.id
    if not is_sorted(discord_id):
        return
    # 60秒クールダウン判定
    now_utc: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    con: sqlite3.Connection = sqlite3.connect(DB_PATH)
    cur: sqlite3.Cursor = con.cursor()
    cur.execute("SELECT last_text_at FROM contribution_totals WHERE discord_id = ?", (discord_id,))
    row: tuple[str | None] | None = cur.fetchone()
    con.close()
    if row and row[0]:
        try:
            last_at: datetime.datetime = datetime.datetime.fromisoformat(row[0])
            if (now_utc - last_at).total_seconds() < CONTRIBUTION_TEXT_COOLDOWN_SECONDS:
                return
        except ValueError:
            pass
    old_level, new_level = add_contribution(
        discord_id,
        xp=CONTRIBUTION_TEXT_PT_PER_MESSAGE,
        vc_seconds=0,
        text_messages=1,
        last_text_at_iso=now_utc.isoformat(),
    )
    if new_level > old_level:
        await notify_level_up(discord_id, old_level, new_level)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    # --- コントリビューション: VC在室秒数の計測 ---
    # チャンネルが変わったとき（join / leave / move）のみ処理
    if not member.bot and before.channel != after.channel:
        # 旧セッションを確定（退出 or 移動元）
        if before.channel is not None:
            old_level, new_level = vc_session_end(member.id)
            if new_level > old_level:
                await notify_level_up(member.id, old_level, new_level)
        # 新セッション開始（入室 or 移動先）。組分け済みのみ計測対象
        if after.channel is not None and is_sorted(member.id):
            vc_session_start(member.id, after.channel.id)

    guild: discord.Guild = member.guild
    category: discord.CategoryChannel | None = discord.utils.get(guild.categories, id=1469467787356410030)

    # 新規ボイスチャンネル作成（指定チャンネルに入室した場合）
    if after.channel and (after.channel.id == VOICE_CREATE_CHANNEL_ID or after.channel.id == RANK_GAME_CHANNEL_ID):
        if not category:
            return
        try:
            channel_name: str = "👀｜ランク戦見守り部屋" if after.channel.id == RANK_GAME_CHANNEL_ID else "".join(random.choices(string.ascii_letters + string.digits, k=5))
            new_channel: discord.VoiceChannel = await guild.create_voice_channel(
                name=channel_name,
                category=category,
                user_limit=0,  # 0=制限なし
            )
            if after.channel.id == RANK_GAME_CHANNEL_ID:
                await new_channel.set_permissions(guild.default_role, stream=False)
                await new_channel.set_permissions(member, stream=True)
            await member.move_to(new_channel)
        except Exception as e:
            print(f"!!! ボイスチャンネル作成エラー: {e}")

    # 空チャンネル削除（退出したチャンネルが空になった場合）
    if before.channel and category and before.channel.category_id == category.id:
        if before.channel.id == VOICE_CREATE_CHANNEL_ID or before.channel.id == RANK_GAME_CHANNEL_ID:
            return
        if len(before.channel.members) == 0:
            try:
                await before.channel.delete()
            except Exception as e:
                print(f"!!! 空チャンネル削除エラー: {e}")

# --- Botの起動 ---
if __name__ == '__main__':
    setup_database()
    bot.run(DISCORD_TOKEN)
