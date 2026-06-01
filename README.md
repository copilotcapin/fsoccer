# Flashscore Soccer HTTP Test API

Unofficial FastAPI/Vercel test wrapper for Flashscore soccer HTTP/xfeed.

This is meant to test the same kind of browser-free verifier path you can call
from `propose.py`, without Playwright/Chromium.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

Open:

```text
http://127.0.0.1:3003/
```

## Key endpoints

```bash
curl "http://127.0.0.1:3003/v2/health"

curl "http://127.0.0.1:3003/v2/soccer/details?date=2026-05-31&tournament=FIFA%20Friendly&home=Poland&away=Ukraine&proposed=Ukraine&domain=both&exhaustive=true"

curl "http://127.0.0.1:3003/v2/soccer/details?date=2026-05-31&home=Poland&away=Ukraine&question=Will%20Poland%20vs%20Ukraine%20end%20in%20a%20draw%3F&proposed=No&market_type=auto&domain=both&exhaustive=true"

curl "http://127.0.0.1:3003/v2/debug/fetch?date=2026-05-31&source=feed&domain=both&exhaustive=true"

curl "http://127.0.0.1:3003/v2/debug/search?date=2026-05-31&q=Ukraine&domain=both"
```

## What changed from the tennis version

- Uses Flashscore soccer/football xfeed candidates: `f_1_0_X_en_1`.
- Uses `/football/` referer for `flashscore.com` and `/soccer/` for `flashscoreusa.com`.
- Replaces tennis player matching with soccer team matching.
- Final score winner logic supports home win, away win, and draw.
- `/v2/soccer/details` supports:
  - `market_type=moneyline`
  - `market_type=draw_binary`
  - `market_type=home_win_binary`
  - `market_type=away_win_binary`
  - `market_type=auto` with optional `question=` for Yes/No draw markets.

## Deploy to Vercel

Upload/import this folder to Vercel. `vercel.json` points Vercel to `app.py`, which imports the FastAPI app from `main.py`.
