"""Build the company registry (registry/companies.json) from three sources:

- Common Crawl's URL index: every Ashby / Greenhouse / Lever / SmartRecruiters / Workday board it has seen
- Hacker News "Who is hiring?" threads: startup boards linked from job posts
- seeds.txt: hand-picked boards

Every candidate is checked against the live API before it's added. Boards that fail are remembered
in "rejected" so they aren't re-checked every week.
"""

import html
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from .core import USER_AGENT, clean_company_name, http_json, pretty_slug
from .sources import probe

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "registry" / "companies.json"
SEEDS = ROOT / "seeds.txt"

ATS_LINK_RE = re.compile(
    r"(jobs\.ashbyhq\.com|(?:job-)?boards\.greenhouse\.io|jobs\.lever\.co|jobs\.smartrecruiters\.com)/"
    r"(?:embed/job_board\?for=)?([A-Za-z0-9_.%\-]+)")
WORKDAY_RE = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com(?::\d+)?/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")
NOT_SLUGS = {"embed", "job_board", "jobs", "v1", "api", "oneclick-ui", "robots", "external-referrals",
             "sitemap", "robots.txt", "wday", "job", "login"}
# Workday sites that aren't the public job board
WORKDAY_SKIP_SITE = re.compile(
    r"intern|early|event|noncompet|agency|alumni|referral|contingent|employee|student|campus|college|universit|graduate"
    r"|nonpublic|canada|mexico|india|china|japan|europe|emea|apac|latam|brazil|germany|france"
    r"|_uk\b|^uk|(^|[-_])(eu|lac|meisa|apac)([-_]|$)|robots"
    , re.I)
LOCALE_PATH = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")  # "es", "en-us": language paths, not sites (case-sensitive)


def key_of(c):
    return f"{c['ats']}:{c['slug'].lower()}"


def _ats_from_host(host):
    return ("greenhouse" if "greenhouse" in host else "ashby" if "ashby" in host
            else "lever" if "lever" in host else "smartrecruiters")


# ---------------------------------------------------------------- candidate sources

def from_hn(months):
    hits = http_json("https://hn.algolia.com/api/v1/search_by_date"
                     "?tags=story,author_whoishiring&query=hiring&hitsPerPage=100") or {"hits": []}
    threads = [h["objectID"] for h in hits["hits"] if "who is hiring" in h["title"].lower()][:months]
    found = {}

    def scan(tid):
        try:
            item = http_json(f"https://hn.algolia.com/api/v1/items/{tid}", timeout=120) or {}
        except Exception as e:
            print(f"  HN thread {tid} failed: {e}")
            return
        for c in item.get("children") or []:
            text = html.unescape(c.get("text") or "")
            name = re.sub(r"<[^>]+>", "", text.split("|")[0]).strip()
            for host, slug in ATS_LINK_RE.findall(text):
                if slug.lower() in NOT_SLUGS:
                    continue
                cand = {"ats": _ats_from_host(host), "slug": slug,
                        "name": name if 0 < len(name) <= 40 else None}
                found.setdefault(key_of(cand), cand)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(scan, threads))
    print(f"  HN: {len(threads)} threads, {len(found)} boards")
    return list(found.values())


def _cc_get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            return urllib.request.urlopen(req, timeout=240).read().decode("utf-8", "replace")
        except Exception:
            time.sleep(10 * (i + 1))
    return None


def from_common_crawl(n_crawls=1):
    """URLs for each ATS host from the newest few crawls. Partial results are fine: the index is flaky."""
    try:
        crawls = [c["id"] for c in json.loads(_cc_get("https://index.commoncrawl.org/collinfo.json"))][:n_crawls]
    except Exception as e:
        print(f"  Common Crawl unavailable: {e}")
        return [], {}
    patterns = ["jobs.ashbyhq.com/*", "job-boards.greenhouse.io/*", "boards.greenhouse.io/*",
                "jobs.lever.co/*", "jobs.smartrecruiters.com/*", "*.myworkdayjobs.com"]
    boards = {}
    workday = defaultdict(Counter)  # (tenant, wd) -> Counter(site)
    for crawl in crawls:
        for pat in patterns:
            base = (f"https://index.commoncrawl.org/{crawl}-index?url={urllib.parse.quote(pat, safe='*/')}"
                    "&output=json&fl=url")
            meta = _cc_get(base + "&showNumPages=true")
            try:
                pages = json.loads(meta)["pages"]
            except Exception:
                print(f"  CC {crawl} {pat}: unavailable")
                continue
            got = 0
            for p in range(pages):
                txt = _cc_get(f"{base}&page={p}")
                for line in (txt or "").splitlines():
                    try:
                        url = json.loads(line)["url"]
                    except Exception:
                        continue
                    got += 1
                    if m := WORKDAY_RE.match(url):
                        if m.group(3) not in NOT_SLUGS:
                            workday[(m.group(1), m.group(2))][m.group(3)] += 1
                    elif m := ATS_LINK_RE.search(url):
                        if m.group(2).lower() not in NOT_SLUGS:
                            cand = {"ats": _ats_from_host(m.group(1)), "slug": m.group(2), "name": None}
                            boards.setdefault(key_of(cand), cand)
            print(f"  CC {crawl} {pat}: {got} urls")
    return list(boards.values()), workday


def from_seeds():
    out, workday = [], defaultdict(Counter)
    for line in SEEDS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        if line.startswith("workday:"):  # workday:tenant.wd5/Site
            host, site = line[len("workday:"):].split("/", 1)
            tenant, wd = host.split(".")
            workday[(tenant, wd)][site] += 10_000  # seeds win over crawled site names
        elif ":" in line:
            ats, slug = line.split(":", 1)
            out.append({"ats": ats.strip(), "slug": slug.strip(), "name": None})
        else:
            out.append({"ats": None, "slug": line, "name": None})
    return out, workday


# ---------------------------------------------------------------- validation

def check_board(cand):
    """Return a registry entry, or None. Seeds without an ATS are tried against each."""
    for ats in ([cand["ats"]] if cand["ats"] else ["ashby", "greenhouse", "lever"]):
        try:
            res = probe(ats, cand["slug"])
        except Exception:
            continue
        if res is not None and res[0] > 0:
            count, name = res
            return {"ats": ats, "slug": cand["slug"],
                    "name": clean_company_name(name) or cand.get("name") or pretty_slug(cand["slug"])}
    return None


WORKDAY_DATACENTERS = ["wd1", "wd3", "wd5", "wd12", "wd103", "wd501", "wd10", "wd108"]
# Well-known tenants whose Workday names don't contain the company's everyday name
WORKDAY_NAMES = {"jj": "Johnson & Johnson", "ms": "Morgan Stanley", "walmart": "Walmart", "humana": "Humana",
                 "att": "AT&T", "citi": "Citi", "capitalone": "Capital One", "fedex": "FedEx","bms": "Bristol Myers Squibb", "jci": "Johnson Controls", "gevernova": "GE Vernova",
                 "bristolmyerssquibb": "Bristol Myers Squibb", "3m": "3M", "hp": "HP", "hpe": "HPE"}


def workday_name(tenant, org):
    """Best everyday name from the tenant id plus the legal entity on a job posting.

    'cisco' + 'Cisco Systems Slovakia spol. s.r.o.' -> 'Cisco'; 'capitalone' + 'Capital One, N.A.' -> 'Capital One'.
    """
    if tenant in WORKDAY_NAMES:
        return WORKDAY_NAMES[tenant]
    org = clean_company_name(org or "") or ""
    # Strip internal codes: "7Z4 Pfizer", "(USA) Ecolab", "001_BCBSA ...", "ADUS-Adobe", "US: Mastercard", "CAN FedEx"
    org = re.sub(r"^\s*(\([^)]*\)\s*|[A-Za-z0-9]*\d[A-Za-z0-9]*\s*[-_:]?\s*|[A-Z]{3,6}-(?=[A-Z][a-z])|[A-Z]{2,3}:\s*"
                 r"|(CAN|USA|US|UK|GBR|MEX|IND)\s+)", "", org).strip()
    target = re.sub(r"[^a-z0-9]", "", tenant.lower())
    norm = lambda ws: re.sub(r"[^a-z0-9]", "", " ".join(ws).lower().replace("&", "and"))
    words = org.split()
    # Shortest run of words that spells out the tenant id: "CCB iHeartMedia" + "iheartmedia" -> "iHeartMedia"
    best = None
    for i in range(len(words)):
        for k in range(i + 1, len(words) + 1):
            if target in norm(words[i:k]) or target in re.sub(r"[^a-z0-9]", "", " ".join(words[i:k]).lower()):
                if best is None or k - i < best[1] - best[0]:
                    best = (i, k)
                break
    if best:
        return " ".join(words[best[0]:best[1]]).strip(" ,-")
    if org and len(words) <= 6 and not re.search(r"\d", org):
        return org  # e.g. "The Options Clearing Corporation" for tenant "theocc"
    return tenant.upper() if len(tenant) <= 4 else pretty_slug(tenant)


def check_workday(item):
    """Pick the tenant's main public job site (the one with the most jobs) and name it."""
    (tenant, wd), sites = item
    candidates = [s for s, _ in sites.most_common(8) if not WORKDAY_SKIP_SITE.search(s) and not LOCALE_PATH.match(s)]
    best = None
    for dc in [wd] + [d for d in WORKDAY_DATACENTERS if d != wd]:
        host = f"{tenant}.{dc}.myworkdayjobs.com"
        for site in candidates:
            try:
                res = probe("workday", f"{tenant}/{site}", host)
            except Exception:
                continue
            if res and res[0] > 0 and (best is None or res[0] > best[0]):
                best = (res[0], host, site, res[1])
        if best:
            break  # only try other data centers if the crawled one is dead
    if not best:
        return None
    _, host, site, org = best
    return {"ats": "workday", "slug": f"{tenant}/{site}", "host": host, "name": workday_name(tenant, org)}


# ---------------------------------------------------------------- main

def load_registry():
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"updated": None, "companies": [], "rejected": []}


def save_registry(reg):
    REGISTRY.parent.mkdir(exist_ok=True)
    # One entry per board (Workday: one per tenant, since a tenant can be found under several sites)
    unique = {}
    for c in reg["companies"]:
        unique.setdefault(f"workday:{c['host']}" if c["ats"] == "workday" else key_of(c), c)
    reg["companies"] = sorted(unique.values(), key=lambda c: (c["name"].lower(), c["ats"]))
    reg["rejected"] = sorted(set(reg["rejected"]))
    REGISTRY.write_text(json.dumps(reg, indent=1, ensure_ascii=False), encoding="utf-8")


def discover(hn_months=24, use_common_crawl=True, workers=16):
    reg = load_registry()
    have = {key_of(c) for c in reg["companies"]}
    have_tenants = {c["host"] for c in reg["companies"] if c["ats"] == "workday"}
    rejected = set(reg["rejected"])
    print("Discovering companies...")

    boards, workday = from_seeds()
    try:
        boards += from_hn(hn_months)
    except Exception as e:
        print(f"  HN failed: {e}")
    if use_common_crawl:
        cc_boards, cc_workday = from_common_crawl()
        boards += cc_boards
        for k, v in cc_workday.items():
            workday[k].update(v)

    seen, todo = set(), []
    for c in boards:
        k = key_of(c) if c["ats"] else f"any:{c['slug'].lower()}"
        known = k in have or (not c["ats"] and any(f"{a}:{c['slug'].lower()}" in have
                                                   for a in ("ashby", "greenhouse", "lever")))
        if k in seen or k in rejected or known:
            continue
        seen.add(k)
        todo.append((k, c))
    wd_todo = [(k, v) for k, v in workday.items()
               if f"{k[0]}.{k[1]}.myworkdayjobs.com" not in have_tenants and f"workday:{k[0]}.{k[1]}" not in rejected]
    print(f"  checking {len(todo)} boards and {len(wd_todo)} Workday tenants...")

    added = 0
    with ThreadPoolExecutor(workers) as pool:
        for (k, _), entry in zip(todo, pool.map(lambda kc: check_board(kc[1]), todo)):
            if entry and key_of(entry) not in have:
                reg["companies"].append(entry)
                have.add(key_of(entry))
                added += 1
            elif not entry:
                rejected.add(k)
        for (k, _), entry in zip(wd_todo, pool.map(check_workday, wd_todo)):
            if entry:
                reg["companies"].append(entry)
                added += 1
            else:
                rejected.add(f"workday:{k[0]}.{k[1]}")

    reg["rejected"] = list(rejected)
    reg["updated"] = date.today().isoformat()
    save_registry(reg)
    counts = Counter(c["ats"] for c in reg["companies"])
    print(f"  {len(reg['companies'])} companies ({added} new): " + ", ".join(f"{a} {n}" for a, n in counts.most_common()))
    return reg
