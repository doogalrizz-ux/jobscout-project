"""Optional, personal add-on: score the morning's new jobs with Claude and build a ranked page.

Unsupported. It's here for anyone curious, but it assumes a particular setup:

- Claude Code is installed and logged in on this machine. Scoring runs `claude -p` with all tools
  disabled, so it uses your Claude subscription and needs no API key.
- Profile files (your work history, stories, targets) sit in `private/` (git-ignored).
- The rubric comes from a Claude skill named in config.toml (`[scoring] skill`), read fresh each
  run from Claude Code's synced-skills folder, so edits made in Claude.ai carry over. Only the
  part between "## Scoring rubric" and the next "## " heading is used.

Enable with `[scoring] enabled = true` in config.toml. If anything fails, the plain report is used.
"""

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = Path.home() / ".claude" / "skills"

SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason": {"type": "string"},
            "flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "score", "reason", "flags"],
    }}},
    "required": ["scores"],
}

SYSTEM = ("You score job postings for one specific job seeker, using their profile and rubric exactly "
          "as given. Read every description in full. Be honest about gaps; never inflate. "
          "Return only the structured output.")


class ScoringError(Exception):
    pass


# ---------------------------------------------------------------- inputs

def find_rubric(skill_name, whole_skill=False):
    """Scoring guidance from the named skill's SKILL.md.

    whole_skill=False: just the '## Scoring rubric' section.
    whole_skill=True: the whole skill minus front matter and any '## Builder script' section
    (the page is built here, not by the model).
    """
    paths = sorted(SKILLS_DIR.rglob(f"{skill_name}/SKILL.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        raise ScoringError(f"skill '{skill_name}' not found under {SKILLS_DIR}")
    text = paths[0].read_text(encoding="utf-8")
    if whole_skill:
        text = re.sub(r"\A---.*?^---\s*", "", text, flags=re.S | re.M)
        text = re.sub(r"^## Builder script.*?(?=^## |\Z)", "", text, flags=re.S | re.M)
        return text.strip()
    m = re.search(r"^## Scoring rubric.*?(?=^## )", text, re.S | re.M)
    if not m:
        raise ScoringError(f"no '## Scoring rubric' section in {paths[0]}")
    return m.group(0).strip()


def load_profile(files):
    parts = []
    for name in files:
        p = ROOT / "private" / name
        if not p.exists():
            raise ScoringError(f"profile file missing: {p}")
        parts.append(f'<file name="{name}">\n{p.read_text(encoding="utf-8")}\n</file>')
    return "\n\n".join(parts)


def dedupe(jobs):
    """One job per (company, title), keeping the first. Returns (unique, duplicates_removed)."""
    seen, out = set(), []
    for j in jobs:
        k = (j["company"].lower(), j["title"].lower())
        if k not in seen:
            seen.add(k)
            out.append(j)
    return out, len(jobs) - len(out)


# ---------------------------------------------------------------- Claude

def _claude_exe():
    exe = shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        raise ScoringError("Claude Code ('claude') isn't on PATH")
    return exe


def score_batch(batch, profile, rubric, model=None, timeout=900):
    """Score a list of jobs (each with an 'id') in one headless Claude call."""
    jobs_text = "\n\n".join(
        f'<job id="{j["id"]}">\nCompany: {j["company"]}\nTitle: {j["title"]}\nLocation: {j["location"]}\n'
        f'Salary: {j["salary_text"]}\nPosted: {j["posted"]}\n\n{j["description"] or "(no description available)"}\n</job>'
        for j in batch)
    prompt = (f"<profile>\n{profile}\n</profile>\n\n<rubric>\n{rubric}\n</rubric>\n\n"
              f"Score each of the {len(batch)} jobs below with the rubric. Return one entry per job id: "
              "score (0-100), reason (1-3 sentences addressed to the job seeker as \"you\": concrete matches, "
              "then honest gaps), and 2-4 snake_case flags. If a description is missing or truncated, say so "
              "and add the flag jd_truncated.\n\n" + jobs_text)
    cmd = [_claude_exe(), "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
           "--output-format", "json", "--system-prompt", SYSTEM, "--json-schema", json.dumps(SCHEMA)]
    if model:
        cmd += ["--model", model]
    # Run from an empty folder so no project CLAUDE.md or settings get pulled into the prompt.
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, cwd=tmp)
    if proc.returncode != 0:
        raise ScoringError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ScoringError(f"unreadable claude output: {proc.stdout[:300]}")
    if out.get("is_error"):
        raise ScoringError(f"claude reported an error: {str(out.get('result'))[:300]}")
    result = out.get("structured_output")
    if result is None:  # older CLI versions put the JSON in "result"
        m = re.search(r"\{.*\}", out.get("result") or "", re.S)
        result = json.loads(m.group(0)) if m else {}
    scores = {s["id"]: s for s in result.get("scores", [])}
    missing = [j["id"] for j in batch if j["id"] not in scores]
    if missing:
        raise ScoringError(f"no score returned for {len(missing)} job(s): {missing[:5]}")
    return scores


def score_jobs(jobs, cfg):
    """Return (unique jobs with score/reason/flags added, duplicates removed)."""
    sc = cfg.get("scoring", {})
    rubric = find_rubric(sc.get("skill", "daily-job-match-scoring"), whole_skill=sc.get("whole_skill", False))
    if sc.get("whole_skill"):
        rubric += ("\n\n(Running as an automated step: the profile files referenced above are provided in "
                   "<profile>; ignore instructions about reading memory, writing files, running scripts, "
                   "building pages or replying in chat. Just return the scores.)")
    profile = load_profile(sc.get("profile_files", ["job-search.md", "career-stories.md"]))
    unique, dupes = dedupe(jobs)
    unique = unique[: sc.get("max_jobs", 60)]
    for i, j in enumerate(unique):
        j["id"] = f"j{i}"
    size = sc.get("batch_size", 15)
    for start in range(0, len(unique), size):
        batch = unique[start:start + size]
        scores = score_batch(batch, profile, rubric, model=sc.get("model") or None)
        for j in batch:
            s = scores[j["id"]]
            j["score"] = max(0, min(100, int(s["score"])))
            j["reason"], j["flags"] = s["reason"].strip(), [f.strip() for f in s["flags"] if f.strip()]
        print(f"  scored {min(start + size, len(unique))}/{len(unique)}")
    return unique, dupes


# ---------------------------------------------------------------- page (same look as the skill's builder)

def tier(n):
    return "strong_fit" if n >= 85 else "reach" if n >= 70 else "skip"


LABELS = {"strong_fit": "Strong Fit", "reach": "Reach", "skip": "Skip"}

CSS = """
:root { --bg:#f6f4ef; --card:#fff; --ink:#1d1d1f; --muted:#5f6368; --line:#e3e0d8;
  --strong:#1f7a4d; --reach:#b7791f; --skip:#8a8f98; --btn:#2e5266; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181b; --card:#1f2226; --ink:#e8e6e1;
  --muted:#a0a4ab; --line:#30343a; --strong:#4fbf86; --reach:#e0a54a; --skip:#7d838c; --btn:#5d8fae; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
main { max-width:900px; margin:0 auto; padding:24px 16px 64px; }
h1 { font:600 26px Georgia,serif; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.stale { background:#fff4d6; color:#6b4a00; border:1px solid #e8c766; border-radius:6px; padding:8px 12px; font-size:14px; }
@media (prefers-color-scheme: dark) { .stale { background:#3a2f12; color:#f3d98b; border-color:#6b5520; } }
.card { display:flex; gap:16px; background:var(--card); border:1px solid var(--line);
  border-left:5px solid var(--skip); border-radius:10px; padding:16px; margin-bottom:12px; }
.card.strong_fit { border-left-color:var(--strong); } .card.reach { border-left-color:var(--reach); }
.card.skip { opacity:.82; }
.score { flex:0 0 64px; text-align:center; }
.num { display:block; font:700 28px Georgia,serif; }
.strong_fit .num, .strong_fit .tier { color:var(--strong); }
.reach .num, .reach .tier { color:var(--reach); }
.skip .num, .skip .tier { color:var(--skip); }
.tier { font-size:11px; text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
.body { flex:1; min-width:0; }
h2 { font-size:17px; margin:0 0 2px; }
.meta { color:var(--muted); font-size:13px; margin:0 0 8px; }
.reason { margin:0 0 8px; }
.flags { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
.flag { font-size:12px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
.actions { display:flex; gap:10px; flex-wrap:wrap; }
button, .apply { font:600 14px system-ui,sans-serif; padding:8px 14px; border-radius:6px; cursor:pointer; text-decoration:none; }
button { background:var(--btn); color:#fff; border:1px solid var(--btn); }
button.done { background:var(--strong); border-color:var(--strong); }
.apply { border:1px solid var(--btn); color:var(--btn); background:transparent; }
@media (max-width:560px) { .card { flex-direction:column; gap:8px; } .score { text-align:left; } .num { display:inline; margin-right:8px; } }
"""

JS = """
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
  ta.remove(); return ok;
}
document.querySelectorAll('button.copy').forEach(btn => btn.addEventListener('click', async () => {
  const text = JDS[+btn.dataset.i];
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { ok = fallbackCopy(text); }
  btn.textContent = ok ? 'Copied ✓' : 'Copy failed';
  btn.classList.toggle('done', ok);
  setTimeout(() => { btn.textContent = 'Copy full JD'; btn.classList.remove('done'); }, 1800);
}));
"""


def write_scored_html(path, jobs, dupes, date_str, stale_html=""):
    e = html.escape
    jobs = sorted(jobs, key=lambda j: -j["score"])
    counts = {t: 0 for t in LABELS}
    cards, jds = [], []
    for i, j in enumerate(jobs):
        t = tier(j["score"])
        counts[t] += 1
        jds.append(f"{j['company']} — {j['title']}\nLocation: {j['location']} | Salary: {j['salary_text']} | "
                   f"Posted: {j['posted'] or ''}\nPosting: {j['url']}\nApply: {j['apply_url']}\n\n{j['description']}")
        flags = "".join(f'<span class="flag">{e(f)}</span>' for f in j["flags"])
        cards.append(f'''<article class="card {t}">
  <div class="score"><span class="num">{j['score']}</span><span class="tier">{LABELS[t]}</span></div>
  <div class="body">
    <h2>{e(j['title'])}</h2>
    <p class="meta"><strong>{e(j['company'])}</strong> · {e(j['location'])} · {e(j['salary_text'])} · posted {e(j['posted'] or '')}</p>
    <p class="reason">{e(j['reason'])}</p>
    <div class="flags">{flags}</div>
    <div class="actions">
      <button type="button" data-i="{i}" class="copy">Copy full JD</button>
      <a class="apply" href="{e(j['apply_url'])}" target="_blank" rel="noopener">Apply ↗</a>
    </div>
  </div>
</article>''')
    body = "\n".join(cards) or '<p class="sub">No new matches to score today.</p>'
    page = (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>Job Matches {date_str}</title>\n<style>{CSS}</style></head>\n<body><main>\n'
            f'<h1>Job Matches — {date_str}</h1>\n'
            f'<p class="sub">{len(jobs)} jobs ({dupes} duplicates removed) · {counts["strong_fit"]} Strong Fit · '
            f'{counts["reach"]} Reach · {counts["skip"]} Skip — best fits first</p>\n{stale_html}\n'
            + body + '\n</main>\n<script>\nconst JDS = ' + json.dumps(jds).replace("</", "<\\/") + ";\n"
            + JS + "</script>\n</body></html>\n")
    Path(path).write_text(page, encoding="utf-8")
    return counts
