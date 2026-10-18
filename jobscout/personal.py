"""Personal daily run: filter the published dataset with config.toml and write a report.

Reads the dataset published by the daily GitHub Action (seconds). If that's unavailable or stale,
falls back to a full local scan via publish.py (slow, but your morning report never depends on the
website being up).

Output: output/latest.html (to read) and output/latest.json (new jobs with full descriptions,
for scoring with an AI assistant), plus dated copies.
"""

import html
import json
import re
import shutil
import sqlite3
import tomllib
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

from .core import default_urls, http_json
from .publish import publish
from .sources import fetch_description

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.toml"
CONFIG_EXAMPLE = ROOT / "config.example.toml"
DB = ROOT / "jobscout.db"
OUT = ROOT / "output"
LOCAL_PUBLIC = ROOT / "public"


def compile_any(patterns):
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None


def load_config():
    if not CONFIG.exists():
        shutil.copy(CONFIG_EXAMPLE, CONFIG)
        print(f"Created {CONFIG.name} from the example; edit it to tune filters.")
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


class Filters:
    def __init__(self, cfg):
        r, s, l = cfg["roles"], cfg["seniority"], cfg["location"]
        self.role_in = compile_any(r["include"])
        self.role_out = compile_any(r["exclude"])
        self.levels = [(lv, re.compile(rf"\b{re.escape(lv)}\b\.?", re.I)) for lv in s["levels"]]
        self.require_level = s.get("require_level", False)
        self.local = compile_any(l["local"])
        self.local_label = l.get("local_label", "Local")
        self.allow_remote = l.get("remote", True)
        self.min_max_salary = cfg["salary"]["min_max_salary"]

    def level(self, title):
        for name, rx in self.levels:
            if rx.search(title):
                return "senior" if name == "sr" else name
        return ""

    def title_ok(self, title):
        if not self.role_in.search(title) or (self.role_out and self.role_out.search(title)):
            return False
        return bool(self.level(title)) or not self.require_level

    def location(self, job):
        """A short label like 'Chicago · Hybrid' or 'Remote (US)', or None to reject."""
        if self.local and self.local.search(job["location"]):
            return self.local_label + (f" · {job['work']}" if job["work"] and job["work"] != "Remote" else "")
        if self.allow_remote and job["remote"]:
            return "Remote (US)"
        return None

    def salary_ok(self, job):
        return job["salary_max"] is None or job["salary_max"] > self.min_max_salary


# ---------------------------------------------------------------- dataset

def load_dataset(cfg):
    """Return (index, base) where base is a URL or local Path that holds desc/ files."""
    src = cfg.get("source", {})
    url = src.get("data_url", "").rstrip("/")
    max_age_h = src.get("max_age_hours", 30)
    if url:
        try:
            index = http_json(f"{url}/jobs.json", timeout=120)
            age = datetime.now().astimezone() - datetime.fromisoformat(index["generated"])
            if age < timedelta(hours=max_age_h):
                print(f"Using published data from {index['generated']} ({len(index['jobs']):,} US jobs)")
                return index, url
            print(f"Published data is {age} old; falling back to a local scan")
        except Exception as e:
            print(f"Published data unavailable ({e}); falling back to a local scan")
    print("Running a full local scan (this can take a while)...")
    index = publish(LOCAL_PUBLIC)
    return index, LOCAL_PUBLIC / "data"


def rows_to_jobs(index):
    f = {name: i for i, name in enumerate(index["fields"])}
    comps = index["companies"]
    for r in index["jobs"]:
        name, ats, slug, dfile, host = comps[r[f["c"]]]
        std_url, std_apply = default_urls(ats, slug, r[f["id"]], host)
        url = r[f["url"]] or std_url
        yield {"key": f"{ats}:{slug}:{r[f['id']]}", "id": r[f["id"]], "company": name, "ats": ats,
               "slug": slug, "desc_file": dfile, "title": r[f["title"]], "location": r[f["loc"]],
               "remote": bool(r[f["remote"]]), "work": r[f["work"]],
               "salary_min": r[f["smin"]], "salary_max": r[f["smax"]], "posted": r[f["posted"]],
               "seen": r[f["seen"]], "url": url,
               "apply_url": r[f["apply"]] or (std_apply if not r[f["url"]] else url)}


def attach_descriptions(jobs, base):
    """Workday descriptions come from the dataset's desc/ files; the rest straight from each board."""
    by_file = {}
    for j in jobs:
        if j["desc_file"]:
            by_file.setdefault(j["desc_file"], []).append(j)
    for dfile, group in by_file.items():
        try:
            if isinstance(base, Path):
                descs = json.loads((base / "desc" / dfile).read_text(encoding="utf-8"))
            else:
                descs = http_json(f"{base}/desc/{dfile}", timeout=60) or {}
        except Exception:
            descs = {}
        for j in group:
            j["description"] = descs.get(j["id"], "")

    def live(j):
        try:
            j["description"] = fetch_description(j["ats"], j["slug"], j["id"])
        except Exception:
            j["description"] = ""
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(live, [j for j in jobs if not j["desc_file"]]))


# ---------------------------------------------------------------- output

def fmt_money(v):
    return f"${v / 1000:.0f}K" if v else ""


def salary_label(job):
    lo, hi = job["salary_min"], job["salary_max"]
    if hi is None:
        return ""
    return f"{fmt_money(lo)}–{fmt_money(hi)}" if lo and lo != hi else fmt_money(hi)


def job_record(job, desc_chars):
    d = job.get("description", "")
    return {
        "key": job["key"], "company": job["company"], "title": job["title"], "level": job["level"],
        "location": job["loc_label"], "locations_raw": job["location"],
        "salary_min": job["salary_min"], "salary_max": job["salary_max"], "posted": job["posted"],
        "url": job["url"], "apply_url": job["apply_url"],
        "source": job["ats"], "description": d[:desc_chars] if desc_chars else d,
    }


PAGE_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#0b6bcb;--chip:#eef3fa;--money:#137333}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d20;--fg:#ececee;--muted:#9a9aa2;--line:#2c2c31;--accent:#6aa9ff;--chip:#232a36;--money:#5fd38a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}
.filter{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
.job{display:grid;grid-template-columns:1fr auto;gap:2px 12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.job a{color:var(--accent);font-weight:600;text-decoration:none}.job a:hover{text-decoration:underline}
.co{font-weight:600}.meta{color:var(--muted);font-size:13px}.right{text-align:right;white-space:nowrap}
.money{color:var(--money);font-weight:600;font-size:13px}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;font-size:12px;margin-right:4px}
details summary{cursor:pointer;color:var(--muted)}footer{color:var(--muted);font-size:12px;margin-top:32px}
@media (max-width:600px){.job{grid-template-columns:1fr}.right{text-align:left}}
"""


def job_html(job):
    e = html.escape
    posted = datetime.fromisoformat(job["posted"]).strftime("%b %d") if job["posted"] else ""
    chips = "".join(f'<span class="chip">{e(c)}</span>' for c in (job["level"], job["loc_label"]) if c)
    search = e(f'{job["company"]} {job["title"]} {job["loc_label"]}'.lower())
    return (f'<div class="job" data-s="{search}"><div><a href="{e(job["url"] or "#")}" target="_blank">{e(job["title"])}</a>'
            f'<div><span class="co">{e(job["company"])}</span> <span class="meta">· {e(job["location"][:120])}</span></div>'
            f'<div>{chips}</div></div>'
            f'<div class="right"><div class="money">{e(salary_label(job))}</div><div class="meta">{e(posted)}</div>'
            f'<div class="meta">{e(job["ats"])}</div></div></div>')


def write_html(path, new, still_open, stats):
    today = date.today().strftime("%A, %B %d, %Y")
    body = "\n".join(job_html(j) for j in new) or '<p class="sub">No new matches today.</p>'
    older = "\n".join(job_html(j) for j in still_open)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobScout {date.today().isoformat()}</title>
<style>{PAGE_CSS}</style></head><body><main>
<h1>JobScout · {len(new)} new matches</h1>
<div class="sub">{today} · {stats['companies']:,} companies · {stats['scanned']:,} US jobs · {stats['matched']} match your filters</div>
<input class="filter" id="q" placeholder="Filter by company, title, location…" autofocus>
<h2>New today ({len(new)})</h2>{body}
<details><summary>Older matches, still open ({len(still_open)})</summary>{older}</details>
<footer>Generated by JobScout.</footer>
</main><script>
document.getElementById('q').addEventListener('input',e=>{{const q=e.target.value.toLowerCase();
document.querySelectorAll('.job').forEach(el=>el.style.display=el.dataset.s.includes(q)?'':'none')}});
</script></body></html>"""
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- main

def run(open_report=False):
    cfg = load_config()
    filters = Filters(cfg)
    index, base = load_dataset(cfg)
    all_jobs = list(rows_to_jobs(index))

    matched, dedupe = [], set()
    for job in all_jobs:
        if not filters.title_ok(job["title"]) or not filters.salary_ok(job):
            continue
        loc = filters.location(job)
        if not loc:
            continue
        sig = (job["company"].lower(), job["title"].lower(), loc)
        if sig in dedupe:
            continue
        dedupe.add(sig)
        job["loc_label"], job["level"] = loc, filters.level(job["title"])
        matched.append(job)

    # Each job is reported as "new" exactly once; re-running the same day reproduces the report.
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=cfg["output"]["max_age_days"])).isoformat()
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, reported TEXT)")
    db.executemany("""INSERT INTO seen(key, first_seen, last_seen) VALUES(?, ?, ?)
                      ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen""",
                   [(j["key"], today, today) for j in matched])
    reported = dict(db.execute("SELECT key, reported FROM seen WHERE reported IS NOT NULL"))
    new, still_open = [], []
    for job in matched:
        when = reported.get(job["key"])
        if when == today or (not when and (job["posted"] or job["seen"]) >= cutoff):
            new.append(job)
            if not when:
                db.execute("UPDATE seen SET reported=? WHERE key=?", (today, job["key"]))
        else:
            still_open.append(job)
    db.commit()
    db.close()
    for lst in (new, still_open):
        lst.sort(key=lambda j: j["posted"] or "", reverse=True)

    attach_descriptions(new, base)
    OUT.mkdir(exist_ok=True)
    stats = {"companies": len(index["companies"]), "scanned": len(all_jobs), "matched": len(matched)}
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_generated": index["generated"],
        "criteria": cfg.get("summary", {}),
        "stats": stats,
        "new_jobs": [job_record(j, cfg["output"].get("description_chars", 0)) for j in new],
    }
    for name in (f"jobs_{today}", "latest"):
        (OUT / f"{name}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        write_html(OUT / f"{name}.html", new, still_open, stats)
    print(f"Done: {len(all_jobs):,} US jobs, {len(matched)} match, {len(new)} new.")
    print(f"  {OUT / 'latest.html'}")
    if open_report:
        webbrowser.open((OUT / "latest.html").as_uri())
