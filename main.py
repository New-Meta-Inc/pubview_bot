import discord
from discord.ext import tasks
from riotwatcher import RiotWatcher, LolWatcher, ApiError
import os
import sqlite3
import datetime
import time

# --- 設定項目 ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
RIOT_API_KEY = os.getenv('RIOT_API_KEY')
DISCORD_GUILD_ID = int(os.getenv('DISCORD_GUILD_ID'))
DB_PATH = '/data/lol_bot.db'
NOTIFICATION_CHANNEL_ID = 1401719055643312219 # 通知用チャンネルID
RANK_ROLES = {
    "IRON": "LoL Iron(Solo/Duo)", "BRONZE": "LoL Bronze(Solo/Duo)", "SILVER": "LoL Silver(Solo/Duo)",
    "GOLD": "LoL Gold(Solo/Duo)", "PLATINUM": "LoL Platinum(Solo/Duo)", "EMERALD": "LoL Emerald(Solo/Duo)",
    "DIAMOND": "LoL Diamond(Solo/Duo)", "MASTER": "LoL Master(Solo/Duo)",
    "GRANDMASTER": "LoL Grandmaster(Solo/Duo)", "CHALLENGER": "LoL Challenger(Solo/Duo)"
}
# ----------------

# --- データベースの初期設定 ---
def setup_database() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
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
    con.commit()
    con.close()
# -----------------------------

# --- Botの初期設定 ---
intents = discord.Intents.default()
intents.members = True
bot = discord.Bot(intents=intents)

riot_watcher = RiotWatcher(RIOT_API_KEY)
lol_watcher = LolWatcher(RIOT_API_KEY)

my_region_for_account = 'asia'
my_region_for_summoner = 'jp1'
# -----------------------------

# --- ヘルパー関数 ---
def get_rank_by_puuid(puuid: str) -> dict | None:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # LEAGUE-V4のby-puuidエンドポイントを直接呼び出す
            ranked_stats = lol_watcher.league.by_puuid(my_region_for_summoner, puuid)

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
                retry_after = int(err.response.headers.get('Retry-After', 1))
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
    tier_values = {"CHALLENGER": 9, "GRANDMASTER": 8, "MASTER": 7, "DIAMOND": 6, "EMERALD": 5, "PLATINUM": 4, "GOLD": 3, "SILVER": 2, "BRONZE": 1, "IRON": 0}
    rank_values = {"I": 4, "II": 3, "III": 2, "IV": 1}
    tier_val = tier_values.get(tier.upper(), 0) * 1000
    rank_val = rank_values.get(rank.upper(), 0) * 100
    return tier_val + rank_val + lp

# --- ランキング作成ロジックを共通関数化 ---
async def create_ranking_embed() -> discord.Embed:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # DBからランク情報がNULLでないユーザーのみを取得
    cur.execute("SELECT discord_id, game_name, tag_line, tier, rank, league_points FROM users WHERE tier IS NOT NULL AND rank IS NOT NULL")
    registered_users_with_rank = cur.fetchall()
    con.close()

    embed = discord.Embed(title="🏆 ぱぶびゅ！内LoL(Solo/Duo)ランキング 🏆", color=discord.Color.gold())

    description_footer = "\n\n**`/register` コマンドであなたもランキングに参加しよう！**"
    description_update_time = "（ランキングは毎日正午に自動更新されます）"

    if not registered_users_with_rank:
        embed.description = f"現在ランク情報を取得できるユーザーがいません。\n{description_update_time}{description_footer}"
        return embed

    player_ranks = []
    for discord_id, game_name, tag_line, tier, rank, lp in registered_users_with_rank:
        player_ranks.append({
            "discord_id": discord_id, "game_name": game_name, "tag_line": tag_line,
            "tier": tier, "rank": rank, "lp": lp,
            "value": rank_to_value(tier, rank, lp)
        })

    sorted_ranks = sorted(player_ranks, key=lambda x: x['value'], reverse=True)

    embed.description = f"現在登録されているメンバーのランクです。\n{description_update_time}{description_footer}"

    for i, player in enumerate(sorted_ranks[:20]):
        try:
            user = await bot.fetch_user(player['discord_id'])
            # メンション形式でユーザー名を表示
            mention_name = user.mention
        except discord.NotFound:
            mention_name = user.display_name

        riot_id_full = f"{player['game_name']}#{player['tag_line'].upper()}"
        embed.add_field(name=f"", value=f"{i+1}. {mention_name} ({riot_id_full})\n**{player['tier']} {player['rank']} / {player['lp']}LP**", inline=False)

    return embed

# --- イベント ---
@bot.event
async def on_ready() -> None:
    print(f"Bot logged in as {bot.user}")

    # ▼▼▼ 起動時にランキングを投稿する処理を追加 ▼▼▼
    print("--- Posting initial ranking on startup ---")
    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if channel:
        ranking_embed = await create_ranking_embed()
        if ranking_embed:
            await channel.send("【起動時ランキング速報】", embed=ranking_embed)

    check_ranks_periodically.start()

# --- コマンド ---
@bot.slash_command(name="register", description="あなたのRiot IDをボットに登録します。", guild_ids=[DISCORD_GUILD_ID])
async def register(ctx: discord.ApplicationContext, game_name: str, tag_line: str) -> None:
    await ctx.defer()
    if tag_line.startswith("#"):
        tag_line = tag_line[1:]
    tag_line = tag_line.upper()
    try:
        account_info = riot_watcher.account.by_riot_id(my_region_for_account, game_name, tag_line)
        puuid = account_info['puuid']
        rank_info = get_rank_by_puuid(puuid)

        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
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
        account_info = riot_watcher.account.by_riot_id(my_region_for_account, game_name, tag_line)
        puuid = account_info['puuid']
        rank_info = get_rank_by_puuid(puuid)

        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        target_discord_id = user.id
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
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("DELETE FROM users WHERE discord_id = ?", (ctx.author.id,))
        con.commit()
        if con.total_changes > 0:
            await ctx.respond("あなたの登録情報を削除しました。")
        else:
            await ctx.respond("あなたはまだ登録されていません。")
        con.close()

        # --- ランク連動ロール削除処理 ---
        guild = ctx.guild
        member = await guild.fetch_member(ctx.author.id)
        role_names_to_remove = [discord.utils.get(guild.roles, name=role_name) for role_name in RANK_ROLES.values()]
        await member.remove_roles(*[role for role in role_names_to_remove if role is not None and role in member.roles])

    except Exception as e:
        await ctx.respond("登録解除中に予期せぬエラーが発生しました。")

@bot.slash_command(name="ranking", description="サーバー内のLoLランクランキングを表示します。", guild_ids=[DISCORD_GUILD_ID])
async def ranking(ctx: discord.ApplicationContext) -> None:
    await ctx.defer()
    try:
        ranking_embed = await create_ranking_embed()
        if ranking_embed:
            await ctx.respond(embed=ranking_embed)
        else:
            await ctx.respond("まだ誰も登録されていないか、ランク情報を取得できるユーザーがいません。")
    except Exception as e:
        print(f"!!! An unexpected error occurred in 'ranking' command: {e}")
        await ctx.respond("ランキングの作成中にエラーが発生しました。")

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
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        # 全ユーザーのランク情報を更新
        cur.execute("UPDATE users SET tier = 'IRON', rank = 'IV', league_points = 0")
        count = cur.rowcount
        con.commit()
        con.close()
        await ctx.respond(f"{count}人のユーザーのランクをIron IVに設定しました。")
    except Exception as e:
        await ctx.respond(f"処理中にエラーが発生しました: {e}")

@bot.slash_command(name="debug_modify_rank", description="特定のユーザーのランクを強制的に変更します。（デバッグ用）", guild_ids=[DISCORD_GUILD_ID])
@discord.default_permissions(administrator=True)
async def debug_modify_rank(ctx: discord.ApplicationContext, user: discord.Member, tier: str, rank: str, league_points: int) -> None:
    await ctx.defer(ephemeral=True)
    TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    RANKS = ["I", "II", "III", "IV"]

    if tier.upper() not in TIERS or rank.upper() not in RANKS:
        await ctx.respond(f"無効なTierまたはRankです。\nTier: {', '.join(TIERS)}\nRank: {', '.join(RANKS)}")
        return

    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("UPDATE users SET tier = ?, rank = ?, league_points = ? WHERE discord_id = ?",
                    (tier.upper(), rank.upper(), league_points, user.id))

        count = cur.rowcount
        con.commit()
        con.close()

        if count > 0:
            await ctx.respond(f"ユーザー「{user.display_name}」のランクを {tier.upper()} {rank.upper()} {league_points}LP に設定しました。")
        else:
            await ctx.respond(f"ユーザー「{user.display_name}」は見つかりませんでした。先に/registerで登録してください。")

    except Exception as e:
        await ctx.respond(f"処理中にエラーが発生しました: {e}")

# --- バックグラウンドタスク ---
jst = datetime.timezone(datetime.timedelta(hours=9))
@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=jst))
async def check_ranks_periodically() -> None:
    print("--- Starting periodic rank check ---")

    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT discord_id, riot_puuid, tier, rank, game_name, tag_line FROM users")
    registered_users = cur.fetchall()
    if not registered_users:
        con.close()
        return

    if not channel:
        print(f"Error: Notification channel with ID {NOTIFICATION_CHANNEL_ID} not found.")
        con.close()
        return

    for discord_id, puuid, old_tier, old_rank, game_name, tag_line in registered_users:
        try:
            new_rank_info = get_rank_by_puuid(puuid)
            guild = channel.guild
            member = await guild.fetch_member(discord_id)
            if not member: continue

            # --- データベース更新 ---
            if new_rank_info:
                cur.execute("UPDATE users SET tier = ?, rank = ?, league_points = ? WHERE discord_id = ?",
                            (new_rank_info['tier'], new_rank_info['rank'], new_rank_info['leaguePoints'], discord_id))
            else:
                cur.execute("UPDATE users SET tier = NULL, rank = NULL, league_points = NULL WHERE discord_id = ?", (discord_id,))

            # --- ランク連動ロール処理 ---
            current_rank_tier = new_rank_info['tier'].upper() if new_rank_info else None

            # 現在のユーザーが持っているランクロールを確認
            current_rank_role = None
            for role_name in RANK_ROLES.values():
                role = discord.utils.get(guild.roles, name=role_name)
                if role and role in member.roles:
                    current_rank_role = role
                    break

            # 新しいランクに対応するロールを取得
            new_rank_role = None
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

    # --- 定期ランキング速報処理 ---
    if channel:
        ranking_embed = await create_ranking_embed()
    if ranking_embed:
        await channel.send("【定期ランキング速報】", embed=ranking_embed)

    # --- ランクアップ通知処理 ---
    if new_rank_info and old_tier and old_rank:
        old_value = rank_to_value(old_tier, old_rank, 0)
        new_value = rank_to_value(new_rank_info['tier'], new_rank_info['rank'], 0)
        if new_value > old_value:
            riot_id_full = f"{game_name}#{tag_line.upper()}"
            await channel.send(f"🎉 **ランクアップ！** 🎉\nおめでとうございます、{member.mention}さん ({riot_id_full})！\n**{old_tier} {old_rank}** → **{new_rank_info['tier']} {new_rank_info['rank']}** に昇格しました！")

    con.commit()
    con.close()

    print("--- Periodic rank check finished ---")

# --- Botの起動 ---
if __name__ == '__main__':
    setup_database()
    bot.run(DISCORD_TOKEN)
