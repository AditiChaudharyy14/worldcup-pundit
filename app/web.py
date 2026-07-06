"""FastAPI status page + health check, served alongside the bot in the same
process (see main.py's `serve` mode) -- this is the judges' entry point:
a public link showing the bot is alive, plus a "Join the Discord" button
since that's where the actual pundit commentary and Hi-Lo game happen.

No login, no write endpoints -- purely a read-only window onto storage.py
(events processed / recent events / leaderboard) and app.state's in-process
bot-readiness flag.
"""

from __future__ import annotations

import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import AppConfig
from app.state import STATE
from app.storage import Storage


def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def _render_page(
    *,
    online: bool,
    uptime_text: str,
    events_processed: int,
    recent_events: list[dict],
    leaderboard: list[tuple[str, str | None, int, int]],
    invite_url: str | None,
    github_url: str,
) -> str:
    status_label = "Online" if online else "Starting up"
    status_class = "online" if online else "offline"

    if recent_events:
        events_html = "\n".join(
            f'<li><span class="mono">#{html.escape(str(e["fixture_id"]))}</span> '
            f'<span class="tag">{html.escape(e["event_type"])}</span> '
            f'<span class="muted">{html.escape(e["created_at"])}</span></li>'
            for e in recent_events
        )
    else:
        events_html = '<li class="muted">No events yet -- check back once a match kicks off.</li>'

    if leaderboard:
        board_html = "\n".join(
            f"<li><span class=\"rank\">{i}.</span> "
            f'<span class="mono">{html.escape(display_name or external_id)}</span> '
            f'<span class="muted">best streak {best}</span></li>'
            for i, (external_id, display_name, _count, best) in enumerate(leaderboard, start=1)
        )
    else:
        board_html = '<li class="muted">No Hi-Lo streaks yet -- play a round in Discord!</li>'

    invite_button = (
        f'<a class="btn btn-primary" href="{html.escape(invite_url)}">Join the Discord</a>'
        if invite_url
        else '<span class="btn btn-primary btn-disabled">Discord invite not configured</span>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WC Pundit -- status</title>
<style>
  :root {{
    --bg: #0b0d12; --card: #12151c; --text: #e7e9ee; --muted: #8b92a3;
    --accent: #5865f2; --green: #3ba55c; --amber: #d9a441; --border: #232733;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #f5f6f8; --card: #ffffff; --text: #14161a; --muted: #5b6270;
      --accent: #5865f2; --green: #1f8b4c; --amber: #b8790f; --border: #e3e5ea;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 24px 16px 48px; }}
  header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  h1 {{ font-size: 1.4rem; margin: 0; }}
  .badge {{
    display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px;
    border-radius: 999px; font-size: 0.85rem; font-weight: 600; border: 1px solid var(--border);
  }}
  .badge .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .badge.online .dot {{ background: var(--green); }}
  .badge.offline .dot {{ background: var(--amber); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
  .card .value {{ font-size: 1.6rem; font-weight: 700; }}
  .card .label {{ color: var(--muted); font-size: 0.85rem; margin-top: 2px; }}
  section {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
  section h2 {{ font-size: 1rem; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ padding: 8px 0; border-top: 1px solid var(--border); display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }}
  li:first-child {{ border-top: none; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .tag {{ background: var(--accent); color: #fff; padding: 1px 8px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; }}
  .muted {{ color: var(--muted); font-size: 0.85rem; }}
  .rank {{ color: var(--muted); min-width: 1.5em; }}
  .cta {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 20px 0; }}
  .btn {{
    display: inline-block; padding: 12px 20px; border-radius: 10px; font-weight: 600;
    text-decoration: none; border: 1px solid var(--border);
  }}
  .btn-primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .btn-secondary {{ background: transparent; color: var(--text); }}
  .btn-disabled {{ opacity: 0.5; }}
  footer {{ text-align: center; color: var(--muted); font-size: 0.8rem; margin-top: 24px; }}
  footer a {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>&#9917; WC Pundit</h1>
    <span class="badge {status_class}"><span class="dot"></span>{status_label}</span>
  </header>

  <div class="cards">
    <div class="card"><div class="value">{uptime_text}</div><div class="label">Uptime</div></div>
    <div class="card"><div class="value">{events_processed}</div><div class="label">Events processed</div></div>
  </div>

  <div class="cta">
    {invite_button}
    <a class="btn btn-secondary" href="{html.escape(github_url)}">View on GitHub</a>
  </div>

  <section>
    <h2>Recent events</h2>
    <ul>{events_html}</ul>
  </section>

  <section>
    <h2>Hi-Lo leaderboard</h2>
    <ul>{board_html}</ul>
  </section>

  <footer>
    AI pundit commentary (English / Nepali / Hindi) + a Hi-Lo streak game, live in Discord, built on TxLINE World Cup data.
    <br><a href="/health">/health</a>
  </footer>
</div>
</body>
</html>"""


def create_app(storage: Storage, config: AppConfig) -> FastAPI:
    app = FastAPI(title="WC Pundit status", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def status_page() -> HTMLResponse:
        events_processed = await storage.count_processed_events()
        recent_events = await storage.list_recent_events(limit=5)
        board = await storage.leaderboard(limit=5)
        return HTMLResponse(
            _render_page(
                online=STATE.discord_online,
                uptime_text=_format_uptime(STATE.uptime_seconds),
                events_processed=events_processed,
                recent_events=recent_events,
                leaderboard=board,
                invite_url=config.settings.discord_invite_url,
                github_url="https://github.com/AditiChaudharyy14/worldcup-pundit",
            )
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "discord_online": STATE.discord_online,
                "uptime_seconds": STATE.uptime_seconds,
            }
        )

    return app
