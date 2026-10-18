"""Scan every registered board and publish the public dataset.

Output (in `public/data/`):
    jobs.json         compact index of every US job posted in the last `max_age_days`
    desc/<file>.json  Workday descriptions, one file per company: {job id: text}. Other boards
                      allow browsers to fetch descriptions directly, so those aren't hosted.

State between runs:
    first-seen dates come from the previous jobs.json (local copy, or the published one via --prev-url)
    Workday/SmartRecruiters details are cached in state/details.sqlite so each job is fetched once

Memory: each board is reduced to slim records the moment it finishes, and hosted descriptions are
written to disk right away, so a full run over thousands of boards stays small.
"""

import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from .cache import DetailCache
from .core import classify_location, default_urls, http_json
from .discover import load_registry
from .sources import ADAPTERS

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state" / "details.sqlite"
FIELDS = ["c", "id", "title", "loc", "remote", "work", "smin", "smax", "posted", "seen", "url", "apply"]
MAX_DESC = 15_000
HOSTED_DESC = {"workday"}  # boards whose descriptions browsers can't fetch themselves


def desc_file(company):
    return f"{company['ats']}-{re.sub(r'[^A-Za-z0-9_-]', '_', company['slug'])}.json"


def work_label(workplace, remote):
    w = (workplace or "").lower().replace("_", "-")
    if remote or "remote" in w:
        return "Remote"
    return "Hybrid" if "hybrid" in w else "On-site" if re.search(r"on-?site|office", w) else ""


def load_previous(out_dir, prev_url):
    """Map job key -> first-seen date from the last published index."""
    path = out_dir / "data" / "jobs.json"
    data = None
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    elif prev_url:
        try:
            data = http_json(prev_url, timeout=120)
        except Exception as e:
            print(f"  previous index unavailable ({e}); every job will look new today")
    if not data:
        return {}
    comps = data["companies"]
    i_c, i_id, i_seen = (data["fields"].index(f) for f in ("c", "id", "seen"))
    return {f"{comps[r[i_c]][1]}:{comps[r[i_c]][2]}:{r[i_id]}": r[i_seen] for r in data["jobs"]}


def publish(out_dir, prev_url=None, max_age_days=14, workers=24, limit=None):
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    reg = load_registry()
    companies = reg["companies"][:limit] if limit else reg["companies"]
    previous = load_previous(out_dir, prev_url)
    cache = DetailCache(STATE)
    # board_budget_s: one huge board can't hold up the run; unfinished details resume next time.
    ctx = {"max_age_days": max_age_days, "workday_max_jobs": 1000, "board_budget_s": 600, "detail_cache": cache}
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()

    (data_dir / "desc").mkdir(parents=True, exist_ok=True)
    for old in (data_dir / "desc").glob("*.json"):
        old.unlink()

    print(f"Scanning {len(companies)} boards ({len(previous)} jobs from last run, {len(cache)} cached details)...")
    started = time.time()

    def run(i):
        """Scan one board; return (jobs scanned, slim records for US jobs in the date window)."""
        c = companies[i]
        t0 = time.time()
        records, descs, seen_sig, scanned = [], {}, set(), 0
        for job in ADAPTERS[c["ats"]](c, ctx):
            scanned += 1
            is_us, remote = classify_location(job["locations"], job["remote_flag"])
            first_seen = previous.get(job["key"], today)
            if not is_us or (job["posted"] or first_seen) < cutoff:
                continue
            loc = "; ".join(job["locations"][:5])[:160]
            sig = (job["title"].lower(), loc.lower())
            if sig in seen_sig:
                continue  # same role re-posted under several requisitions
            seen_sig.add(sig)
            # Store links only when they differ from the board's standard pattern.
            std_url, std_apply = default_urls(c["ats"], c["slug"], job["id"], c.get("host"))
            url = job["url"] if job["url"] != std_url else None
            apply = job["apply_url"] if job["apply_url"] and job["apply_url"] not in (std_apply, job["url"]) else None
            records.append([job["id"], job["title"], loc, int(remote), work_label(job["workplace"], remote),
                            job["salary_min"], job["salary_max"], job["posted"], first_seen, url, apply])
            if c["ats"] in HOSTED_DESC:
                descs[job["id"]] = job["description"][:MAX_DESC]
        if descs:
            (data_dir / "desc" / desc_file(c)).write_text(json.dumps(descs, ensure_ascii=False), encoding="utf-8")
        if time.time() - t0 > 180:
            print(f"  slow board: {c['name']} ({c['ats']}) {time.time() - t0:.0f}s, {len(records)} US jobs")
        return scanned, records

    # Workday boards are the slowest (paged + per-job details), so start them first.
    order = sorted(range(len(companies)), key=lambda i: companies[i]["ats"] != "workday")
    comp_rows, rows, errors = [], [], Counter()
    scanned_total = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, i): i for i in order}
        for n, fut in enumerate(as_completed(futures), 1):
            i = futures.pop(fut)
            try:
                scanned, records = fut.result()
            except Exception:
                errors[companies[i]["ats"]] += 1
                scanned, records = 0, []
            del fut
            scanned_total += scanned
            if records:
                c = companies[i]
                ci = len(comp_rows)
                comp_rows.append([c["name"], c["ats"], c["slug"],
                                  desc_file(c) if c["ats"] in HOSTED_DESC else None, c.get("host")])
                rows.extend([ci] + r for r in records)
            if n % 250 == 0:
                print(f"  {n}/{len(companies)} boards, {len(rows):,} US jobs, {time.time() - started:.0f}s")

    new_today = sum(1 for r in rows if r[9] == today)
    index = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "max_age_days": max_age_days,
        "stats": {"boards": len(companies), "boards_failed": sum(errors.values()),
                  "jobs_scanned": scanned_total, "us_jobs": len(rows), "new_today": new_today,
                  "by_ats": dict(Counter(c[1] for c in comp_rows))},
        "fields": FIELDS,
        "companies": comp_rows,
        "jobs": rows,
    }
    with open(data_dir / "jobs.json", "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))  # streams to disk
    cache.close()
    mins = (time.time() - started) / 60
    print(f"Published {len(rows):,} US jobs from {len(comp_rows):,} companies "
          f"({scanned_total:,} scanned, {new_today:,} new, {sum(errors.values())} boards failed) in {mins:.1f} min")
    return index


if __name__ == "__main__":
    publish(sys.argv[1] if len(sys.argv) > 1 else ROOT / "public")
