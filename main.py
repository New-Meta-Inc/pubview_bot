import os
import sqlite3
import datetime
import time
import random
import string
from typing import Any
import discord
from discord.ext import tasks
from riotwatcher import RiotWatcher, LolWatcher, ApiError


# --- 設定項目 ---
DISCORD_TOKEN: str | None = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY: str | None = os.getenv('RIOT_API_KEY')
DISCORD_GUILD_ID: int = int(os.getenv('DISCORD_GUILD_ID'))
DB_PATH: str = '/data/lol_bot.db'
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
    ("gryffindor", "グリフィンドール", "🦁", "グリフィンドール"),
    ("slytherin", "スリザリン", "🐍", "スリザリン"),
    ("ravenclaw", "レイヴンクロー", "🦅", "レイヴンクロー"),
    ("hufflepuff", "ハッフルパフ", "🦡", "ハッフルパフ"),
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
    con.commit()
    con.close()
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
    # ▼▼▼ 起動時にランキングを投稿する処理を追加 ▼▼▼
    print("--- Posting initial ranking on startup ---")
    channel: discord.TextChannel | discord.VoiceChannel | discord.Thread | None = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if channel:
        ranking_embed: discord.Embed = await create_ranking_embed()
        if ranking_embed:
            await channel.send("【起動時ランキング速報】", embed=ranking_embed)

    if not check_ranks_periodically.is_running():
        check_ranks_periodically.start()

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

# --- バックグラウンドタスク ---
jst: datetime.timezone = datetime.timezone(datetime.timedelta(hours=9))
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

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
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
