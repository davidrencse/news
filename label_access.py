#!/usr/bin/env python
"""Label the whole current library catalogue free vs member-only.

Fills each article's ``locked`` field (True = member-only, False = free, None =
Medium refused the page) by reading its post page through the project's shared,
self-throttling limiter -- the same ``post_meta`` the curator's paywall-check
uses. It only reads existing articles; it never adds new ones.

Resumable by design: it touches only articles whose status is still unknown,
saves ``library.json`` periodically with the same atomic write the app uses, and
can be stopped (Ctrl-C) and rerun. Pages Medium refuses are left as None and
retried on a later run.

At ~1 request/second against medium.com (polite.py's base gap, slower under
pushback) a full ~510k-article pass takes days of wall-clock time, so run it
repeatedly -- or just leave the app running, whose paywall-check does the same.

Do NOT run while the app is running: both write library.json. The script checks
the app's port and refuses unless --force is given.

Usage:
  python label_access.py                 # label everything unknown, oldest first
  python label_access.py --limit 500     # stop after 500 newly labeled
  python label_access.py --dry-run -n 5  # fetch 5, print verdicts, write nothing
  python label_access.py --data DIR      # explicit data dir (else env / marker)
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
from datetime import date

import polite
from curator import post_meta

ROOT = os.path.dirname(os.path.abspath(__file__))


def resolve_data_dir(explicit):
    """Match app.py: explicit arg, then env, then the data-location.txt marker, then the app dir."""
    if explicit:
        return explicit
    env = os.environ.get("MEDIUM_LIBRARY_DATA")
    if env:
        return env
    marker = os.path.join(ROOT, "data-location.txt")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            found = f.read().strip()
            if found:
                return found
    return ROOT


def app_is_running(port=8765):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="data directory (contains Medium-Library/library.json)")
    ap.add_argument("--limit", "-n", type=int, default=0, help="stop after N newly labeled (0 = all)")
    ap.add_argument("--save-every", type=int, default=200, help="write library.json every N labels")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, but never write")
    ap.add_argument("--force", action="store_true", help="run even if the app appears to be running")
    args = ap.parse_args()

    if app_is_running() and not args.force:
        sys.exit("The app appears to be running on :8765 (both would write library.json). "
                 "Stop it first, or pass --force if you are sure it is something else.")

    data_dir = resolve_data_dir(args.data)
    lib_path = os.path.join(data_dir, "Medium-Library", "library.json")
    if not os.path.exists(lib_path):
        sys.exit(f"library.json not found at {lib_path}")

    print(f"library : {lib_path}")
    with open(lib_path, encoding="utf-8") as f:
        data = json.load(f)
    arts = data["articles"]

    # oldest first -- the stored list is newest-first, matching the curator's backfill order
    todo = [a for a in reversed(arts) if "locked" not in a or a.get("locked") is None]
    total = len(todo)
    print(f"{len(arts):,} articles; {total:,} still unlabeled"
          + ("  [DRY RUN -- no writes]" if args.dry_run else ""))
    if not total:
        print("nothing to do.")
        return

    def save():
        if args.dry_run:
            return
        blob = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = f"{lib_path}.label.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, lib_path)

    done = changed = member = free = unknown = 0
    t0 = time.time()
    try:
        for a in todo:
            waiting = polite.status().get("medium.com", {}).get("waiting_s", 0)
            if waiting > 30:  # Medium asked us to back off; wait it out rather than spinning
                time.sleep(waiting)
            try:
                meta = post_meta(a["url"])
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):  # the limiter is already slowing down; wait, retry next run
                    time.sleep(max(5, polite.status().get("medium.com", {}).get("waiting_s", 0)))
                    continue
                meta = None
            except Exception:
                meta = None

            locked = meta.get("locked") if meta else None
            if args.dry_run:
                verdict = "member-only" if locked is True else "free" if locked is False else "unknown"
                print(f"  [{verdict:11}] {a['url']}")
            else:
                a["locked"] = locked
                a["locked_checked"] = date.today().isoformat()
                if meta and a.get("claps") is not None and meta.get("claps") is not None:
                    a["claps"] = max(a["claps"], meta["claps"])
            changed += 1
            done += 1
            member += locked is True
            free += locked is False
            unknown += locked is None

            if changed >= args.save_every:
                save()
                changed = 0
                rate = done / max(1e-9, time.time() - t0) * 60
                print(f"  labeled {done:,}/{total:,}  member={member:,} free={free:,} "
                      f"unknown={unknown:,}  {rate:.0f}/min")
            if args.limit and done >= args.limit:
                break
    except KeyboardInterrupt:
        print("\nstopping (progress so far is saved)…")
    finally:
        save()

    newly_settled = member + free
    print(f"\nthis run: {done:,} checked -- member={member:,} free={free:,} "
          f"unknown(refused)={unknown:,}")
    print(f"still unlabeled after this run: {total - newly_settled:,} "
          f"(unknown pages are retried next run)")


if __name__ == "__main__":
    main()
