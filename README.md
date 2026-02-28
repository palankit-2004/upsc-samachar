# UPSC Samachar

Daily current affairs aggregator for UPSC aspirants.
Sources: PIB + The Hindu + Indian Express + Economic Times.

## Architecture

PIB News  →  scrape_pib.py (Python)  →  public/data/pib_index.json  →  Browser reads static JSON
Hindu/IE/ET →  netlify/functions/news.js (live RSS)  →  Browser fetches /api/news

WHY this split? PIB blocks server-to-server requests from Netlify serverless functions,
but allows requests during Netlify's BUILD phase. Same approach as pibdigest.netlify.app.

## First Time Setup

1. pip install -r requirements.txt
2. python scrape_pib.py      ← creates public/data/pib_index.json
3. Drag upsc-news/ folder to netlify.com → Deploy manually

Netlify auto-runs scrape_pib.py on every deploy via netlify.toml build command.

## Keep PIB Fresh (daily updates)

Option A: Run python scrape_pib.py locally, redeploy to Netlify.

Option B: Netlify Build Hook + cron-job.org
  - Netlify site settings → Build hooks → create hook URL
  - cron-job.org → hit that URL daily at 8:30am IST

Option C: GitHub Actions (best for automation)
  Push to GitHub, add .github/workflows/refresh.yml:
  ---
  on:
    schedule:
      - cron: '0 3 * * *'   # 3am UTC = 8:30am IST
  jobs:
    scrape:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v3
        - run: pip install -r requirements.txt
        - run: python scrape_pib.py
        - run: |
            git config user.email "bot@example.com"
            git config user.name "PIB Bot"
            git add public/data/
            git commit -m "PIB refresh $(date -u +%Y-%m-%d)" && git push || true
  ---
  Then connect Netlify to your GitHub repo for auto-deploy on push.
