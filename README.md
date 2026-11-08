# Scout Ledger

A football scouting web app for your phone. It filters players across the Premier League, La Liga, Bundesliga, Serie A and Ligue 1, refreshes its data every morning from free sources, and tags every figure with where it came from. Estimates are marked as estimates.

## Set it up (about 10 minutes, free)

1. Create a new **public** repository on GitHub, e.g. `scout`.
2. Upload everything in this folder, keeping the structure (`.github/workflows/refresh.yml` included).
3. **Settings → Actions → General → Workflow permissions**: choose *Read and write permissions*, save.
4. **Settings → Pages**: Source *Deploy from a branch*, branch `main`, folder `/ (root)`, save.
5. **Actions → Refresh player data → Run workflow.** The first run takes 30–60 minutes (it reads every player's shot log once); later daily runs only fetch players whose numbers changed.
6. Open `https://<your-username>.github.io/scout/` on your phone and use **Share → Add to Home Screen** (iPhone) or **⋮ → Install app** (Android). It opens full screen and works offline with the last data it saw.

`index.html` also works on its own: the workflow embeds the latest data in it, so you can AirDrop or save that single file and open it without hosting.

## Where each number comes from

| Tag | Source | What it provides |
|---|---|---|
| US | Understat | Top-5 leagues: minutes, appearances, goals, xG, npxG, assists, xA, shots, key passes. From each player's shot log: headed goals, set-piece goals, set-play xG, penalties taken and scored, shots inside and outside the box. |
| FPL | Fantasy Premier League API (Opta data) | Premier League only: date of birth, starts, Opta xG, tackles, recoveries, set-piece duties, injury news. |
| TM | Transfermarkt | Market value, contract expiry, date of birth, detailed position, nationality. |
| ECB | European Central Bank | EUR→GBP rate for the £ view. |
| EST | Estimates | Market value is Transfermarkt's crowd valuation. Wage is a modelled band: annual pay of 8–16% of market value. Neither is a reported figure. |

**Not available free anywhere:** average rating, aerial duels won, successful dribbles, distance covered, sprint distance, number of sprints. FBref dropped its Opta feed in January 2026, and running data only comes from paid tracking providers (SkillCorner, Second Spectrum, Opta). The app keeps these columns and fills them from a CSV you import under **Data & sources** (header: `name,team,rating,aerials_won,dribbles,distance_km_p90,sprint_distance_km_p90,sprints_p90,wage_week_gbp`). A `wage_week_gbp` column replaces the wage estimate with your figure.

## Notes

- If a source fails on a given day, the app keeps that source's previous data and the dot in the header turns amber. The Actions log shows which source failed.
- Understat and Transfermarkt pages are read the way a browser reads them. If either site changes its layout, that source goes stale until `scripts/build_data.py` is updated; the rest keeps working.
- These sites publish their data for personal use; keep the repository for your own scouting rather than republishing the data.
- To change the wage model, edit `WAGE_SHARE_LOW` / `WAGE_SHARE_HIGH` in `scripts/build_data.py`.
