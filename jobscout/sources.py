"""One adapter per applicant-tracking system. Each turns a company's job board into normalized dicts.

Normalized job:
    key, ats, slug, company, id, title, url, apply_url, locations (list[str]), workplace,
    remote_flag, posted (YYYY-MM-DD or None), department, salary_min, salary_max, description

Greenhouse, Ashby and Lever return a whole board (descriptions included) in one call. Workday and
SmartRecruiters list jobs without descriptions, so details are fetched per job only for jobs that
look US-based, and cached between runs (see `detail_cache`).
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from .core import (classify_location, html_to_text, http_json, iso_day, parse_salary_text,
                   pretty_slug)


def _job(company, **fields):
    job = {"ats": company["ats"], "slug": company["slug"], "company": company["name"],
           "apply_url": None, "locations": [], "workplace": "", "remote_flag": False, "posted": None,
           "department": "", "salary_min": None, "salary_max": None, "description": ""}
    job.update(fields)
    job["id"] = str(job["id"])
    job["key"] = f"{company['ats']}:{company['slug']}:{job['id']}"
    job["locations"] = [l.strip() for l in dict.fromkeys(job["locations"]) if l and l.strip()]
    if job["salary_max"] is None:
        job["salary_min"], job["salary_max"] = parse_salary_text(job["description"])
    return job


# ---------------------------------------------------------------- Greenhouse

def greenhouse(company, ctx):
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs?content=true",
                     timeout=60)
    for j in (data or {}).get("jobs", []):
        work = ""
        for m in j.get("metadata") or []:
            if re.search(r"location type|workplace|remote", m.get("name") or "", re.I):
                v = m.get("value")
                work = " ".join(v) if isinstance(v, list) else str(v or "")
        offices = j.get("offices") or []
        yield _job(company,
                   id=j["id"], title=(j.get("title") or "").strip(),
                   url=j.get("absolute_url"),
                   locations=[(j.get("location") or {}).get("name") or ""]
                   + [o.get("location") or o.get("name") or "" for o in offices],
                   workplace=work, remote_flag="remote" in work.lower(),
                   posted=iso_day(j.get("first_published") or j.get("updated_at")),
                   department=", ".join(d.get("name", "") for d in j.get("departments") or []),
                   description=html_to_text(j.get("content")))


# ---------------------------------------------------------------- Ashby

def ashby(company, ctx):
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}"
                     "?includeCompensation=true", timeout=60)
    for j in (data or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        work = j.get("workplaceType") or ""
        lo = hi = None
        for c in (j.get("compensation") or {}).get("summaryComponents") or []:
            if c.get("compensationType") == "Salary" and c.get("currencyCode") in ("USD", None):
                mult = 2080 if "HOUR" in (c.get("interval") or "") else 1
                lo = (c.get("minValue") or 0) * mult or None
                hi = (c.get("maxValue") or 0) * mult or None
        yield _job(company,
                   id=j["id"], title=(j.get("title") or "").strip(),
                   url=j.get("jobUrl"), apply_url=j.get("applyUrl"),
                   locations=[j.get("location") or ""]
                   + [s.get("location") or "" for s in j.get("secondaryLocations") or []],
                   workplace=work,
                   # isRemote is set when *any* location is remote; trust it only if the type agrees.
                   remote_flag=work.lower() == "remote" or (not work and bool(j.get("isRemote"))),
                   posted=iso_day(j.get("publishedAt")),
                   department=j.get("department") or "",
                   salary_min=lo, salary_max=hi,
                   description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")))


# ---------------------------------------------------------------- Lever

def lever(company, ctx):
    data = http_json(f"https://api.lever.co/v0/postings/{company['slug']}?mode=json", timeout=60)
    for j in data or []:
        cat = j.get("categories") or {}
        work = j.get("workplaceType") or ""
        locs = [cat.get("location") or ""] + list(cat.get("allLocations") or [])
        if work == "remote" and j.get("country") == "US":
            locs.append("Remote (US)")
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        sr = j.get("salaryRange") or {}
        lo = hi = None
        if sr.get("max") and sr.get("currency", "USD") == "USD":
            mult = 2080 if "hour" in (sr.get("interval") or "").lower() else 1
            lo, hi = (sr.get("min") or 0) * mult or None, sr["max"] * mult
        yield _job(company,
                   id=j["id"], title=(j.get("text") or "").strip(),
                   url=j.get("hostedUrl"), apply_url=j.get("applyUrl"),
                   locations=locs, workplace=work, remote_flag=work.lower() == "remote",
                   posted=iso_day(j.get("createdAt")),
                   department=cat.get("department") or cat.get("team") or "",
                   salary_min=lo, salary_max=hi,
                   description="\n\n".join(p for p in parts if p).strip())


# ---------------------------------------------------------------- Workday

_POSTED_RE = re.compile(r"(today|yesterday|(\d+)\+?\s*days?)", re.I)


def _workday_age(posted_on):
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 3 Days Ago' -> 3, '30+' -> 30."""
    m = _POSTED_RE.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    return 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))


def _fill_details(needed, fetch, ctx, deadline):
    """Fetch missing per-job details a few at a time and store them in ctx["detail_cache"].

    needed: {cache_key: argument for fetch}. Failures, and anything left when the board's time
    budget runs out, are skipped; the next run picks them up (finished ones stay cached).
    """
    cache = ctx["detail_cache"]
    todo = [(k, a) for k, a in needed.items() if k not in cache]
    if not todo:
        return

    def one(item):
        key, arg = item
        if time.monotonic() > deadline:
            return
        try:
            cache[key] = fetch(arg)
        except Exception:
            pass

    with ThreadPoolExecutor(ctx.get("detail_workers", 4)) as pool:
        list(pool.map(one, todo))


def workday(company, ctx):
    # slug is "tenant/site"; host is e.g. "nvidia.wd5.myworkdayjobs.com"
    tenant, site = company["slug"].split("/", 1)
    base = f"https://{company['host']}/wday/cxs/{tenant}/{site}"
    today = date.today()
    deadline = time.monotonic() + ctx.get("board_budget_s", 600)

    # 1. Page through the newest-first listing until postings are older than max_age_days.
    listed, offset, page_size = [], 0, 20
    while offset < ctx["workday_max_jobs"] and time.monotonic() < deadline:
        data = http_json(f"{base}/jobs", body={"appliedFacets": {}, "limit": page_size,
                                               "offset": offset, "searchText": ""}, timeout=45)
        postings = (data or {}).get("jobPostings") or []
        too_old = 0
        for p in postings:
            age = _workday_age(p.get("postedOn"))
            if age is not None and age >= ctx["max_age_days"]:
                too_old += 1
                continue
            path, loc_text = p.get("externalPath"), p.get("locationsText") or ""
            if not path or not p.get("title"):
                continue
            multi = bool(re.match(r"\d+\s+locations?$", loc_text, re.I))
            if not multi and not classify_location([loc_text])[0] and not re.search(r"remote", loc_text, re.I):
                continue  # clearly outside the US; skip the detail call
            listed.append((p, age, f"workday:{company['slug']}:{path}"))
        if not postings or too_old == len(postings) or len(postings) < page_size:
            break
        offset += page_size

    # 2. Details (description, all locations, exact date) for jobs we haven't seen before.
    def fetch(path):
        info = (http_json(f"{base}{path}", timeout=45) or {}).get("jobPostingInfo") or {}
        return {
            "locations": [info.get("location") or ""] + list(info.get("additionalLocations") or []),
            "country": (info.get("country") or {}).get("descriptor") or "",
            "remote": info.get("remoteType") or "",
            "posted": info.get("startDate") or info.get("postedOnDate"),
            "url": info.get("externalUrl"),
            "description": html_to_text(info.get("jobDescription"))[:15_000],
            "time_type": info.get("timeType") or "",
        }
    _fill_details({key: p["externalPath"] for p, _, key in listed}, fetch, ctx, deadline)

    for p, age, key in listed:
        detail = ctx["detail_cache"].get(key)
        if detail is None:
            continue
        locs = [l for l in detail["locations"] if l] or [p.get("locationsText") or ""]
        if detail["country"] == "United States of America" and not any(classify_location([l])[0] for l in locs):
            locs.append("United States")
        yield _job(company,
                   id=p["externalPath"], title=p["title"].strip(),
                   url=detail["url"] or f"https://{company['host']}/{site}{p['externalPath']}",
                   locations=locs,
                   workplace=detail["remote"], remote_flag=bool(re.search(r"remote", detail["remote"], re.I)),
                   posted=iso_day(detail["posted"]) or (today - timedelta(days=age or 0)).isoformat(),
                   department=detail["time_type"],
                   description=detail["description"])


# ---------------------------------------------------------------- SmartRecruiters

def smartrecruiters(company, ctx):
    base = f"https://api.smartrecruiters.com/v1/companies/{company['slug']}/postings"
    cutoff = (date.today() - timedelta(days=ctx["max_age_days"])).isoformat()
    deadline = time.monotonic() + ctx.get("board_budget_s", 600)

    listed, offset = [], 0
    while True:
        data = http_json(f"{base}?limit=100&offset={offset}", timeout=45) or {}
        content = data.get("content") or []
        for p in content:
            posted = iso_day(p.get("releasedDate"))
            if ((p.get("location") or {}).get("country") or "").lower() == "us" and not (posted and posted < cutoff):
                listed.append((p, posted, f"smartrecruiters:{company['slug']}:{p['id']}"))
        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            break

    def fetch(job_id):
        d = http_json(f"{base}/{job_id}", timeout=45) or {}
        sections = (d.get("jobAd") or {}).get("sections") or {}
        text = "\n\n".join(
            f"{s.get('title', '')}\n{html_to_text(s.get('text'))}".strip()
            for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
            if (s := sections.get(k)) and s.get("text"))
        # Only salary and links are kept; readers fetch SmartRecruiters descriptions directly.
        lo, hi = parse_salary_text(text)
        return {"url": d.get("postingUrl"), "apply_url": d.get("applyUrl"), "smin": lo, "smax": hi}
    _fill_details({key: p["id"] for p, _, key in listed}, fetch, ctx, deadline)

    for p, posted, key in listed:
        detail = ctx["detail_cache"].get(key) or {}
        loc = p.get("location") or {}
        city = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
        remote = bool(loc.get("remote"))
        yield _job(company,
                   id=p["id"], title=(p.get("name") or "").strip(),
                   url=detail.get("url") or f"https://jobs.smartrecruiters.com/{company['slug']}/{p['id']}",
                   apply_url=detail.get("apply_url"),
                   locations=[f"{city}, US" if city else "United States"] + (["Remote (US)"] if remote else []),
                   workplace="Remote" if remote else "", remote_flag=remote,
                   posted=posted,
                   department=(p.get("department") or {}).get("label") or "",
                   salary_min=detail.get("smin"), salary_max=detail.get("smax"))


ADAPTERS = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever,
            "workday": workday, "smartrecruiters": smartrecruiters}


# ---------------------------------------------------------------- single-job descriptions
# The published dataset hosts Workday descriptions only; the others are fetched on demand.
# Keep in sync with descriptionFor() in web/index.html.

def fetch_description(ats, slug, job_id, _ashby_boards={}):
    if ats == "greenhouse":
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}") or {}
        return html_to_text(d.get("content"))
    if ats == "lever":
        d = http_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}") or {}
        parts = [d.get("descriptionPlain") or ""]
        parts += [f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in d.get("lists") or []]
        parts.append(d.get("additionalPlain") or "")
        return "\n\n".join(p for p in parts if p).strip()
    if ats == "ashby":  # no single-job endpoint; fetch the board once per company
        if slug not in _ashby_boards:
            board = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}") or {}
            _ashby_boards[slug] = {j["id"]: j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
                                   for j in board.get("jobs", [])}
        return _ashby_boards[slug].get(job_id, "")
    if ats == "smartrecruiters":
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}") or {}
        sections = (d.get("jobAd") or {}).get("sections") or {}
        return "\n\n".join(f"{s.get('title', '')}\n{html_to_text(s.get('text'))}".strip()
                           for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
                           if (s := sections.get(k)) and s.get("text"))
    return ""


# ---------------------------------------------------------------- board validation (for discovery)

def probe(ats, slug, host=None):
    """Check a board exists. Returns (job_count, company_name) or None if not found."""
    if ats == "greenhouse":
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if d is None:
            return None
        jobs = d.get("jobs", [])
        return len(jobs), (jobs[0].get("company_name") if jobs else None)
    if ats == "ashby":
        d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        return None if d is None else (len(d.get("jobs", [])), None)
    if ats == "lever":
        d = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=200")
        return None if d is None else (len(d), None)
    if ats == "smartrecruiters":
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
        if not d or not d.get("totalFound"):
            return None  # SmartRecruiters returns an empty 200 for unknown companies
        return d["totalFound"], ((d.get("content") or [{}])[0].get("company") or {}).get("name")
    if ats == "workday":
        tenant, site = slug.split("/", 1)
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        d = http_json(f"{base}/jobs", body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        if d is None:
            return None
        name = None
        if d.get("jobPostings"):  # the employer's display name is only on a job's detail page
            info = http_json(base + d["jobPostings"][0]["externalPath"]) or {}
            name = (info.get("hiringOrganization") or {}).get("name")
        return d.get("total") or 0, name
    raise ValueError(ats)


def default_name(ats, slug):
    return pretty_slug(slug.split("/")[0])
