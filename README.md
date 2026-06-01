# Flashscore Soccer HTTP API

Unofficial FastAPI/Vercel wrapper for Flashscore soccer xfeed.

## Endpoints

- `/v2/health`
- `/v2/soccer`
- `/v2/soccer/details`
- `/v2/debug/fetch`
- `/v2/debug/raw`
- `/v2/debug/search`

## Main verifier endpoint

Use one endpoint for winner, draw, total goals/points, spread/handicap, and halftime-leading markets:

```bash
/v2/soccer/details?date=2026-05-31&home=CS%20Cienciano&away=CS%20Cristal&proposed=Over&question=CS%20Cienciano%20vs.%20CS%20Cristal:%20O/U%202.5
```

`market_type=auto` is the default. The API infers market type from the `question` text.

Supported auto-detected titles include examples like:

- `Team A vs Team B`
- `Will Team A win?`
- `Will Team A vs Team B end in a draw?`
- `Team A vs Team B: O/U 2.5`
- `Total Goals Over 2.5`
- `Spread: Team A (-1.5)`
- `Handicap: Team A -1.5`
- `Team A leading at halftime?`

For totals and spreads, the API only needs the Flashscore final score. For halftime-leading markets, it needs first-half period score fields from Flashscore HTTP.
