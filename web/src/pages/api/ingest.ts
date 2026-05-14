import type { APIRoute } from 'astro';

export const prerender = false;

interface UserRow {
  discord_id: string;
  display_name: string;
  avatar_url?: string | null;
  riot_game_name?: string | null;
  riot_tag_line?: string | null;
  tier?: string | null;
  rank?: string | null;
  league_points?: number | null;
}

interface SortingHatRow {
  discord_id: string;
  house_id: string;
  rate_bracket: string;
  tier: string;
  sorted_at: string;
}

interface TotalsRow {
  discord_id: string;
  total_xp: number;
  vc_seconds: number;
  text_messages: number;
  updated_at: string;
}

interface MonthlyRow {
  year_month: string;
  discord_id: string;
  points: number;
  vc_seconds: number;
  text_messages: number;
}

interface IngestPayload {
  bot_version?: string;
  users: UserRow[];
  sorting_hat: SortingHatRow[];
  contribution_totals: TotalsRow[];
  contribution_monthly: MonthlyRow[];
}

export const POST: APIRoute = async ({ request, locals }) => {
  const env = (locals as { runtime: { env: { DB: D1Database; INGEST_TOKEN: string } } }).runtime.env;

  // 認証: Bearer トークン
  const auth = request.headers.get('authorization') ?? '';
  const expected = `Bearer ${env.INGEST_TOKEN}`;
  if (!env.INGEST_TOKEN || auth !== expected) {
    return new Response(JSON.stringify({ error: 'unauthorized' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    });
  }

  let payload: IngestPayload;
  try {
    payload = (await request.json()) as IngestPayload;
  } catch {
    return new Response(JSON.stringify({ error: 'invalid_json' }), {
      status: 400,
      headers: { 'content-type': 'application/json' },
    });
  }

  const { users = [], sorting_hat = [], contribution_totals = [], contribution_monthly = [] } = payload;

  const statements: D1PreparedStatement[] = [];

  for (const u of users) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO users (discord_id, display_name, avatar_url, riot_game_name, riot_tag_line, tier, rank, league_points, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(discord_id) DO UPDATE SET
           display_name   = excluded.display_name,
           avatar_url     = excluded.avatar_url,
           riot_game_name = excluded.riot_game_name,
           riot_tag_line  = excluded.riot_tag_line,
           tier           = excluded.tier,
           rank           = excluded.rank,
           league_points  = excluded.league_points,
           updated_at     = excluded.updated_at`,
      ).bind(
        u.discord_id,
        u.display_name,
        u.avatar_url ?? null,
        u.riot_game_name ?? null,
        u.riot_tag_line ?? null,
        u.tier ?? null,
        u.rank ?? null,
        u.league_points ?? null,
        new Date().toISOString(),
      ),
    );
  }

  for (const s of sorting_hat) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO sorting_hat (discord_id, house_id, rate_bracket, tier, sorted_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(discord_id) DO UPDATE SET
           house_id     = excluded.house_id,
           rate_bracket = excluded.rate_bracket,
           tier         = excluded.tier,
           sorted_at    = excluded.sorted_at`,
      ).bind(s.discord_id, s.house_id, s.rate_bracket, s.tier, s.sorted_at),
    );
  }

  for (const t of contribution_totals) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO contribution_totals (discord_id, total_xp, vc_seconds, text_messages, updated_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(discord_id) DO UPDATE SET
           total_xp      = excluded.total_xp,
           vc_seconds    = excluded.vc_seconds,
           text_messages = excluded.text_messages,
           updated_at    = excluded.updated_at`,
      ).bind(t.discord_id, t.total_xp, t.vc_seconds, t.text_messages, t.updated_at),
    );
  }

  for (const m of contribution_monthly) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO contribution_monthly (year_month, discord_id, points, vc_seconds, text_messages)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(year_month, discord_id) DO UPDATE SET
           points        = excluded.points,
           vc_seconds    = excluded.vc_seconds,
           text_messages = excluded.text_messages`,
      ).bind(m.year_month, m.discord_id, m.points, m.vc_seconds, m.text_messages),
    );
  }

  statements.push(
    env.DB.prepare(
      `INSERT INTO ingest_log (ingested_at, users_count, sorting_count, totals_count, monthly_count, bot_version)
       VALUES (?, ?, ?, ?, ?, ?)`,
    ).bind(
      new Date().toISOString(),
      users.length,
      sorting_hat.length,
      contribution_totals.length,
      contribution_monthly.length,
      payload.bot_version ?? null,
    ),
  );

  await env.DB.batch(statements);

  return new Response(
    JSON.stringify({
      ok: true,
      counts: {
        users: users.length,
        sorting_hat: sorting_hat.length,
        contribution_totals: contribution_totals.length,
        contribution_monthly: contribution_monthly.length,
      },
    }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  );
};
