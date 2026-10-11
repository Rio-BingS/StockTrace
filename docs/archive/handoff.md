> ⚠️ 已过时（OUTDATED）：v1.01 时代的交接文档，仅供考古。其中的部署方式（Flexible SSL、http 访问等）已废弃，以 `deploy/DEPLOY.md` 与 `CLAUDE.md` 为准。

# StockTrace Project Handoff

## TL;DR

StockTrace is a full-stack A-share stock monitoring dashboard built with Django 6 + DRF backend and Vue 3 + ECharts frontend. It features: stock watchlist with search-by-name, collapsible stock table with real-time price/change data, daily K-line candlestick chart, intraday minute-level trend chart with peak/valley detection and annotations (open/close/high/low/current price with percentage diffs), and click-to-view intraday chart from daily K-line bars. The app has been deployed to a VPS (Ubuntu 22.04, 1H2G) at http://stocktrace.yumeat.cc.cd using Nginx + Gunicorn + systemd, with Cloudflare CDN (Flexible SSL) for HTTPS. The site is currently accessible but has NO access control — anyone can add/delete stocks and trigger data fetches. HTTP Basic Auth via Nginx was chosen as the next step to fix this.

## Files Modified

**Backend:**
- `backend/stocks/models.py` — Added `prev_close`, `change_diff`, `change_pct` fields to DailyQuote; updated `compute_derived_fields()`
- `backend/stocks/services.py` — Added `search_stocks()` with cached A-share list; `fetch_daily_data()` now calculates prev_close/change fields via sorted iteration + DB lookup
- `backend/stocks/views.py` — Added `search` action to StockViewSet; `dashboard` view returns new fields; `create()` override for soft-delete reactivation
- `backend/stocks/serializers.py` — Added `StockSearchSerializer`, `DashboardStockSerializer` extended with prev_close/change fields; `DailyQuoteSerializer` fields updated
- `backend/StockTrace/settings.py` — SECRET_KEY/DEBUG/ALLOWED_HOSTS read from env vars; CORS restricted to domain; added STATIC_ROOT
- `backend/stocks/urls.py` — Unchanged (existing router covers search action)
- `backend/stocks/migrations/0003_*.py` — Auto-generated migration for 3 new DailyQuote fields
- `backend/requirements.txt` — New file: pinned Python dependencies for deployment

**Frontend:**
- `frontend/src/components/StockTable.vue` — Rewritten: collapsible rows (basic info collapsed, expand for amplitude/OHLC), K-line entry button in both states
- `frontend/src/components/IntradayChart.vue` — New: minute-level trend line chart with ECharts, MarkLine/MarkPoint annotations, local extrema detection (1% threshold), data table below
- `frontend/src/components/KlineChart.vue` — Added `date-click` emit on candlestick click, cached sortedData ref
- `frontend/src/components/StockWatchlist.vue` — Rewritten: single search box with debounced dropdown, supports code or name search
- `frontend/src/views/StockDetail.vue` — Integrated IntradayChart, `showIntraday(date)` for switching dates, `onKlineDateClick` handler, responsive layout
- `frontend/src/views/Dashboard.vue` — Minor: uses updated StockTable with collapsible rows

**Deployment:**
- `.gitignore` — New: excludes db, venv, node_modules, dist, .env
- VPS: `/etc/stocktrace/env` — Environment variables (SECRET_KEY, DEBUG=False, ALLOWED_HOSTS)
- VPS: `/opt/stocktrace/backend/gunicorn_config.py` — Gunicorn config (bind 127.0.0.1:8000, 2 workers, no user/group directive)
- VPS: `/etc/systemd/system/stocktrace.service` — systemd service (User=www, EnvironmentFile, Restart=always)
- VPS: `/etc/nginx/sites-available/stocktrace` — Nginx site config (frontend dist + API proxy + static files)

## Key Decisions

1. **Change metric**: User explicitly rejected prev-close-based change. Frontend uses `open_close_pct`/`open_close_diff` (relative to day's open). `prev_close`/`change_pct`/`change_diff` fields kept in DB but not displayed.
2. **Local extrema threshold**: Set to 1% of full-day range (user rejected 5% as too aggressive for A-shares).
3. **Gunicorn user conflict**: Systemd `User=www` must NOT be duplicated in Gunicorn config `user=` directive — causes setuid permission error since systemd already dropped privileges.
4. **Python 3.12 upgrade on VPS**: Django 6.0 requires Python >=3.12; Ubuntu 22.04 ships 3.10. Installed 3.12 via deadsnakes PPA rather than downgrading Django (future-proof, zero code changes).
5. **Node.js 22 upgrade on VPS**: Vite 8 requires Node >=20.19; Ubuntu 22.04 ships v12. Upgraded via `n 22`.
6. **Cloudflare Flexible SSL**: Chosen over Certbot for simplicity — Cloudflare handles HTTPS to users, HTTP back to VPS.
7. **HTTP Basic Auth for access control**: Chosen over Django auth system and IP whitelist for minimal resource usage and simplicity on 1H2G VPS.

## Known Blockers

1. **No access control** — Anyone on the internet can access the site, add/delete stocks, trigger data fetching. HTTP Basic Auth implementation is the immediate next task.
2. **No HTTPS from browser** — Without Cloudflare proxy (orange cloud), browser accesses plain HTTP. Cloudflare Flexible SSL mode must be properly configured.
3. **Browser may force HTTPS redirect** — If Cloudflare proxy was previously enabled with "Always Use HTTPS" rule, browsers may cache HSTS and refuse HTTP even after switching to DNS-only mode. May need to clear browser HSTS cache or fully set up Cloudflare Flexible SSL.

## Next Task

Implement HTTP Basic Auth on Nginx:
1. Install `apache2-utils` on VPS: `sudo apt install -y apache2-utils`
2. Create password file: `sudo htpasswd -c /etc/nginx/.htpasswd <username>`
3. Add auth directives to Nginx site config: `auth_basic "Restricted";` and `auth_basic_user_file /etc/nginx/.htpasswd;` inside the server block
4. Reload Nginx: `sudo systemctl reload nginx`
5. Re-enable Cloudflare orange cloud proxy with SSL/TLS mode set to "Flexible"
