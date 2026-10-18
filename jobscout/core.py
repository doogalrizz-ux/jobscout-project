"""Shared helpers: HTTP, text cleanup, dates, salary parsing, and US-location detection."""

import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

USER_AGENT = "JobScout/2.0 (+https://github.com/doogalrizz-ux/jobscout-project)"


# ---------------------------------------------------------------- HTTP

class NotFound(Exception):
    pass


def http_json(url, body=None, timeout=30, retries=2):
    """GET (or POST when body is given) a URL and parse JSON.
    Returns None on 404/410; raises on other errors after retrying."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            if attempt == retries or e.code in (400, 401, 403, 422):
                raise
            time.sleep(2 * (attempt + 1) if e.code != 429 else 15)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------- text and dates

def html_to_text(s):
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes its content
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|ul|ol|tr)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def pretty_slug(slug):
    return re.sub(r"[-_]+", " ", slug).strip().title()


def clean_company_name(name):
    """Tidy legal-entity names from Workday: '2100 NVIDIA USA' -> 'NVIDIA', 'Acme, Inc.' -> 'Acme'."""
    if not name:
        return name
    name = re.sub(r"^\s*[\d\-_.]+\s+", "", name)
    name = re.sub(r"[\s,]+(inc|llc|l\.l\.c|ltd|corp|corporation|co|usa|us|na|n\.a|group|holdings|company)\.?\s*$",
                  "", name, flags=re.I)
    name = re.sub(r"[\s,]+(inc|llc|ltd|corp|usa|us)\.?\s*$", "", name, flags=re.I)  # "Foo USA, Inc."
    return name.strip(" ,") or None


def parse_date(value):
    """ISO string or epoch-ms -> aware datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_day(value):
    d = parse_date(value) if not isinstance(value, (date, datetime)) else value
    return d.date().isoformat() if isinstance(d, datetime) else (d.isoformat() if d else None)


# ---------------------------------------------------------------- job links
# The published index omits links that follow these patterns; readers rebuild them.
# Keep in sync with urlsFor() in web/index.html.

def default_urls(ats, slug, job_id, host=None):
    """(posting url, apply url) a job gets when its board uses the standard link format."""
    if ats == "ashby":
        u = f"https://jobs.ashbyhq.com/{slug}/{job_id}"
        return u, u + "/application"
    if ats == "lever":
        u = f"https://jobs.lever.co/{slug}/{job_id}"
        return u, u + "/apply"
    if ats == "greenhouse":
        u = f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"
        return u, u
    if ats == "workday":
        u = f"https://{host}/{slug.split('/', 1)[1]}{job_id}"
        return u, u
    if ats == "smartrecruiters":
        u = f"https://jobs.smartrecruiters.com/{slug}/{job_id}"
        return u, u
    return None, None


# ---------------------------------------------------------------- salary

_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK])?"
_SEP = r"\s*(?:-|–|—|to)\s*"
_SALARY_RES = [
    re.compile(r"\$\s?" + _NUM + _SEP + r"(?:USD\s?)?\$?\s?" + _NUM),
    re.compile(r"(?:USD|US\$)\s?" + _NUM + _SEP + r"(?:USD\s?)?" + _NUM),
    re.compile(_NUM + r"\s?(?:USD|US\$)" + _SEP + _NUM + r"\s?(?:USD|US\$)"),  # "184,000 USD - 287,500 USD"
]


def _to_annual(num, k, hourly):
    v = float(num.replace(",", "")) * (1000 if k else 1)
    if hourly or v < 500:
        v *= 2080
    return v


def parse_salary_text(text):
    """Find salary ranges in free text. Returns (min, max) annual USD or (None, None)."""
    lo = hi = None
    for rx in _SALARY_RES:
        for m in rx.finditer(text or ""):
            tail = text[m.end():m.end() + 25].lower()
            hourly = bool(re.match(r"\s*(/|per|an)\s*(hr|hour)", tail))
            a = _to_annual(m.group(1), m.group(2), hourly)
            b = _to_annual(m.group(3), m.group(4) or m.group(2), hourly)
            if not (20_000 <= a <= b <= 2_000_000):
                continue  # funding amounts, monthly figures, etc.
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    return (round(lo) if lo else None), (round(hi) if hi else None)


# ---------------------------------------------------------------- US location detection

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}
US_CITIES = [
    "nyc", "manhattan", "brooklyn", "san francisco", "los angeles", "chicago", "boston", "seattle",
    "austin", "dallas", "houston", "denver", "atlanta", "miami", "philadelphia", "phoenix",
    "san diego", "san jose", "palo alto", "mountain view", "menlo park", "sunnyvale", "redwood city",
    "oakland", "portland", "minneapolis", "detroit", "nashville", "charlotte", "raleigh", "pittsburgh",
    "salt lake city", "las vegas", "baltimore", "st. louis", "kansas city", "columbus", "cincinnati",
    "indianapolis", "tampa", "orlando", "jersey city", "hoboken", "irving", "plano", "cambridge, ma",
    "silicon valley", "bay area", "washington, d.c", "washington dc", "basking ridge", "ashburn",
]
_STATE_ABBRS = "|".join(sorted(set(STATES.values())))
US_RE = re.compile(
    r"\b(united states|u\.s\.a?\.?|usa|us|americas|north america)\b"
    r"|\b(" + "|".join(re.escape(s) for s in list(STATES) + US_CITIES) + r")\b",
    re.I)
# "Austin, TX" / "US-TX-Austin" / "TX - Dallas": a state code next to a separator. Case-sensitive.
US_ABBR_RE = re.compile(r"(?:^|[,\-|/(]\s*)(" + _STATE_ABBRS + r")(?=\s*(?:$|[,\-|/)]))")
EXPLICIT_US_RE = re.compile(r"\b(united states|u\.s\.a?\.?|usa|us)\b", re.I)
NON_US_RE = re.compile(r"\b(" + "|".join([
    "canada", "uk", "united kingdom", "england", "scotland", "wales", "ireland", "europe", "eu",
    "emea", "apac", "latam", "latin america", "asia", "africa", "middle east", "india", "germany",
    "france", "spain", "portugal", "italy", "netherlands", "belgium", "switzerland", "austria",
    "poland", "romania", "czechia", "czech republic", "slovakia", "hungary", "greece", "sweden",
    "norway", "denmark", "finland", "estonia", "lithuania", "latvia", "ukraine", "serbia", "croatia",
    "bulgaria", "turkey", "israel", "uae", "dubai", "saudi arabia", "egypt", "nigeria", "kenya",
    "south africa", "brazil", "mexico city", "argentina", "colombia", "chile", "peru", "uruguay",
    "costa rica", "australia", "new zealand", "singapore", "japan", "china", "hong kong", "taiwan",
    "korea", "philippines", "vietnam", "thailand", "malaysia", "indonesia", "pakistan", "bangladesh",
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "london", "dublin", "berlin", "munich",
    "hamburg", "frankfurt", "paris", "amsterdam", "madrid", "barcelona", "lisbon", "milan", "zurich",
    "stockholm", "copenhagen", "oslo", "helsinki", "warsaw", "krakow", "prague", "bucharest",
    "tel aviv", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai", "gurgaon",
    "gurugram", "noida", "delhi", "sydney", "melbourne", "tokyo", "seoul", "shanghai", "beijing",
    "shenzhen", "manila", "sao paulo", "são paulo", "buenos aires", "bogota", "bogotá", "medellin",
    "santiago", "lima", "cdmx", "guadalajara", "monterrey", "ontario", "quebec", "british columbia",
    "alberta",
]) + r")\b", re.I)
REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh|virtual|telecommute)\b", re.I)
BARE_US_RE = re.compile(r"^\s*(united states( of america)?|usa|us|u\.s\.)\s*$", re.I)


def _is_us_string(s):
    pos = bool(US_RE.search(s) or US_ABBR_RE.search(s))
    if pos and NON_US_RE.search(s) and not EXPLICIT_US_RE.search(s):
        # e.g. "New Mexico" is fine, but "Berlin, DE" or "Toronto, ON, Canada" is not
        return bool(re.search(r"new mexico", s, re.I)) and not re.search(r"mexico city", s, re.I)
    return pos


def classify_location(locations, remote_flag=False):
    """Decide whether a job is open to US candidates and whether it's remote.

    Returns (is_us, is_remote). A remote location with no country at all ("Remote") counts as
    US, since that's what it nearly always means on US-focused boards.
    """
    us_onsite = us_remote = False
    any_remote_string = False
    for s in locations:
        if not s:
            continue
        if REMOTE_RE.search(s) or BARE_US_RE.match(s):
            any_remote_string = True
            if _is_us_string(s) or not NON_US_RE.search(s):
                us_remote = True
        elif _is_us_string(s):
            us_onsite = True
    if remote_flag and not any_remote_string:
        if us_onsite or not any(s for s in locations) or not any(NON_US_RE.search(s) for s in locations if s):
            us_remote = True
    return (us_onsite or us_remote), us_remote
