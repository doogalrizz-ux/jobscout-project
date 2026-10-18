"""JobScout command line.

    python -m jobscout                  personal run: filter the daily dataset with config.toml
    python -m jobscout --open           same, then open the HTML report
    python -m jobscout publish [DIR]    scan every board and write the public dataset (default: public/)
    python -m jobscout discover         refresh registry/companies.json (HN + Common Crawl + seeds.txt)
    python -m jobscout site DEST URL    copy web/index.html to DEST, reading its data from URL
"""

import argparse
import json
from pathlib import Path

from . import discover, personal, publish


def main():
    ap = argparse.ArgumentParser(prog="jobscout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")

    p = sub.add_parser("publish", help="scan every board and write the public dataset")
    p.add_argument("out_dir", nargs="?", default=str(publish.ROOT / "public"))
    p.add_argument("--prev-url", help="URL of the last published jobs.json (for first-seen dates)")
    p.add_argument("--max-age-days", type=int, default=14)
    p.add_argument("--limit", type=int, help="only scan the first N companies (for testing)")

    d = sub.add_parser("discover", help="refresh the company registry")
    d.add_argument("--no-common-crawl", action="store_true")
    d.add_argument("--hn-months", type=int, default=24)

    w = sub.add_parser("site", help="copy the page for hosting elsewhere (e.g. datasorcerer.net/jobs)")
    w.add_argument("dest", help="output file, e.g. ../datasorcerer/jobs/index.html")
    w.add_argument("data_url", help="published data folder, e.g. https://you.github.io/repo/data")

    args = ap.parse_args()
    if args.cmd == "publish":
        publish.publish(args.out_dir, prev_url=args.prev_url, max_age_days=args.max_age_days, limit=args.limit)
    elif args.cmd == "site":
        page = (publish.ROOT / "web" / "index.html").read_text(encoding="utf-8")
        marker = "<script>\n  // Where the daily dataset lives."
        assert marker in page, "data URL marker missing from web/index.html"
        setting = f"<script>window.JOBSCOUT_DATA_URL = {json.dumps(args.data_url)};</script>\n"
        page = page.replace(marker, setting + marker, 1)
        dest = Path(args.dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page, encoding="utf-8")
        print(f"Wrote {dest} (data from {args.data_url})")
    elif args.cmd == "discover":
        discover.discover(hn_months=args.hn_months, use_common_crawl=not args.no_common_crawl)
    else:
        personal.run(open_report=args.open)


if __name__ == "__main__":
    main()
