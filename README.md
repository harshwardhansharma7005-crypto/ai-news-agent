# ai-news-agent

Every day at 8:00 PM IST, a GitHub Actions cloud runner (your laptop can be off):

1. collects AI news from official company blogs and tech-media RSS feeds
2. removes duplicates, stale and non-AI stories
3. verifies each article and asks Gemini to analyse and rank them
4. picks the 10 most significant, with company diversity
5. writes a Facebook-ready post to `output/daily_post.txt`
   and the stories to `output/daily_news.json`

## Setup
1. Get a free key at https://aistudio.google.com/apikey
2. In your repo: Settings > Secrets and variables > Actions > New repository secret
   Name: GEMINI_API_KEY   Value: your key   (never paste the key into any file)
3. Settings > Actions > General > Workflow permissions > "Read and write permissions" > Save
4. Actions tab > "Daily AI News" > Run workflow (manual test)

## Facebook publishing (optional - needs these)
- Secrets: META_ACCESS_TOKEN, META_PAGE_ID (numeric Page ID)
- Variable (Settings > Secrets and variables > Actions > Variables tab): META_GRAPH_VERSION, e.g. v25.0
- One post per day is enforced. To post again the same day, run the workflow manually and tick "force_publish".

## Notes
- Cron is in UTC: "30 14 * * *" = 20:00 IST. GitHub may start it a few minutes late.
- Some feeds (Anthropic, xAI) are community-generated from the official pages,
  and Mistral is covered through InfoQ's Mistral feed because Mistral has no public RSS. If one fails, the log shows a warning and
  the run continues. Edit the FEEDS list in main.py to fix or add sources.
- To change the Gemini model: add a repository variable GEMINI_MODEL
  (Settings > Secrets and variables > Actions > Variables).
- GitHub can pause scheduled workflows in repos with no activity for 60 days.
  The daily output commit normally counts as activity. If it gets paused,
  re-enable it in the Actions tab.
