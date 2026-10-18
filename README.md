# JobScout

**Fresh job leads every morning, straight from company career sites. Free and open source.**

JobScout checks the public job boards of thousands of companies every day, keeps the US jobs posted
in the last 30 days, and publishes them as a searchable page. Filter by role, level, location and
salary, apply directly on the employer's site, and copy any job description with one click. If you
use ChatGPT or Claude in your job search, *Export for AI* downloads your matches with full
descriptions so your assistant can rank them against your resume.

**Use it:** [datasorcerer.net/jobs](https://datasorcerer.net/jobs). Nothing to install, no account. Your
filters and saved jobs stay in your own browser.

## Where the jobs come from

Companies post their openings through applicant-tracking systems. JobScout reads the public job
boards of five of them:

| System | Typical employers |
|---|---|
| Greenhouse, Ashby, Lever | Startups and growth-stage tech companies |
| Workday | Large enterprises: telecom, finance, health care, retail, tech |
| SmartRecruiters | Mid-size and large companies |

The company list (`registry/companies.json`) is built automatically and refreshed weekly from:
- links in Hacker News "Who is hiring?" threads
- Common Crawl's index of public web pages
- hand-picked additions in `seeds.txt`

It doesn't cover companies that run their own careers site (Amazon, Google, Microsoft, Apple, and
others), so check those directly.

## How it works

```
GitHub Action, every morning (free)
  python -m jobscout publish     scan every board -> public/data/jobs.json + desc/*.json
  deploy to GitHub Pages         page + data at https://<owner>.github.io/<repo>/
```

- **`jobs.json`** is a compact index: title, company, location, remote flag, salary, posting date,
  the date JobScout first saw the job, and links.
- **`desc/<company>.json`** holds the full descriptions, which the page loads only when needed.
- **Detail cache:** Workday and SmartRecruiters need one request per job for details, so those are
  cached between runs.

Python 3.11+, standard library only. Nothing to `pip install`.

## Run your own copy

1. Fork this repo.
2. In your fork, go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Go to **Actions → Daily scan** and click **Run workflow**. The first run takes a while because it
   fetches every job's details once. After that, your copy updates itself every morning at
   `https://<you>.github.io/<repo>/`.

Want different companies? Add them to `seeds.txt` (the format is explained at the top of the
file). To change the page, edit `web/index.html`. It's a single file, so it's easy to hand to Claude
Code, Cursor or Codex with a request like "add a filter for part-time jobs".

## Personal daily report (optional)

The project also has a personal mode that runs on your own computer. It applies the filters in
`config.toml` to the published data and writes two files to `output/`:
- **an HTML report** of matching jobs you haven't seen yet
- **a JSON file** of the same jobs with full descriptions, for scoring with an AI assistant

```
python -m jobscout            # write output/latest.html and output/latest.json
python -m jobscout --open     # same, then open the report
```

On the first run, `config.toml` is created from `config.example.toml`. Edit it to set your roles,
places and salary floor. On Windows, `run_daily.bat` can be scheduled with Task Scheduler:

```
schtasks /create /tn JobScout /sc weekly /d MON,TUE,WED,THU,FRI /st 07:30 /tr "\"C:\path\to\JobScout\run_daily.bat\""
```

## Other commands

```
python -m jobscout discover        # refresh the company registry
python -m jobscout publish [DIR]   # scan everything and write the dataset locally (default: public/)
```

## License

MIT. Use it, change it, share it. Job listings belong to the employers who posted them; JobScout
links to the original posting for every job.
