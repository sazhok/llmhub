#!/usr/bin/env python3
"""The migration valve, on the command line.

Every company starts **disabled**. Seeding does not switch anything on: it records which
companies exist and whether their taskset tree is on this box, so `--list` answers the only
question that matters before a cutover - *can this company be served here at all?*

    python3 scripts/seed_sources.py --import-params   # discover companies from the tree
    python3 scripts/seed_sources.py --list
    python3 scripts/seed_sources.py --enable 1call-203
    python3 scripts/seed_sources.py --disable 1call-203

Enabling is the **third** half of a two-sided switch and must come last: point ochat at
llmhub first (harmless - the queue is empty), then fw, then enable here. Enabling first would
mean orders arriving for a company no consumer is polling.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llmhub import db, sources, tasksets  # noqa: E402
from llmhub.config import settings as load_settings  # noqa: E402


def _companies_on_disk(params_root: str) -> list[str]:
    """Every company directory that actually holds a taskset generation.

    A bare directory is not enough: `data/params/<company>/` also exists for companies whose
    only content is `bg.loc` and similar operational params.
    """
    if not os.path.isdir(params_root):
        return []
    out = []
    for name in sorted(os.listdir(params_root)):
        if tasksets.has_taskset(params_root, name):
            out.append(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import-params", action="store_true",
                        help="record every company whose taskset tree is on this box (disabled)")
    parser.add_argument("--list", action="store_true", help="what is known, and what is served")
    parser.add_argument("--enable", metavar="COMPANY")
    parser.add_argument("--disable", metavar="COMPANY")
    parser.add_argument("--force", action="store_true",
                        help="enable even without a local taskset (you will get an idle queue)")
    parser.add_argument("--priority", type=int, metavar="N", help="with --enable/--disable")
    args = parser.parse_args()

    settings = load_settings()
    db.init_pool()
    db.ensure_schema()
    try:
        return _run(args, settings)
    finally:
        db.close_pool()


def _run(args, settings) -> int:

    if args.import_params:
        found = _companies_on_disk(settings.params_root)
        for company in found:
            generations = tasksets.generations(settings.params_root, company)
            sources.upsert(company, note=f"taskset generations: {','.join(generations)}")
        print(f"recorded {len(found)} company/companies from {settings.params_root}, "
              f"all disabled")

    if args.enable or args.disable:
        company = args.enable or args.disable
        try:
            row = sources.set_enabled(company, bool(args.enable),
                                      params_root=settings.params_root, force=args.force)
        except sources.NoTaskset as e:
            print(f"refusing to enable {company}: {e}", file=sys.stderr)
            return 2
        if args.priority is not None:
            sources.upsert(company, priority=args.priority,
                           params_dir=row.get("params_dir") or "",
                           note=row.get("note") or "")
        print(f"{company}: enabled={row['enabled']}")

    if args.list or not (args.import_params or args.enable or args.disable):
        rows = sources.list_sources()
        if not rows:
            print("no sources recorded yet - run with --import-params")
            return 0
        print(f"{'company':<20} {'served':<7} {'taskset':<24} {'ready':>6} {'parked':>7}")
        for row in rows:
            root = row["params_dir"] or settings.params_root
            generations = tasksets.generations(root, row["company_id"])
            local = ",".join(generations) if generations else "-- not on this box --"
            print(f"{row['company_id']:<20} {str(bool(row['enabled'])):<7} {local:<24} "
                  f"{row['ready']:>6} {row['parked']:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
