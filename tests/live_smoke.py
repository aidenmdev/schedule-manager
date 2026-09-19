"""READ-ONLY check of the real Gmail/Calendar connection. Creates, edits and sends nothing.
Run:  .venv\\Scripts\\python.exe -m tests.live_smoke"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dominos_schedule as core


def timed(label, fn):
    t = time.time()
    result = fn()
    print(f"  {label:<38} {time.time() - t:5.2f}s")
    return result


def main():
    cfg = core.load_config()
    state = core.StateStore(core.STATE_PATH)
    print("Connecting...")
    gmail, cal = timed("build services", core.build_services)
    profile = timed("gmail profile", lambda: gmail.users().getProfile(userId="me").execute())
    print(f"  signed in as {profile['emailAddress']}")

    emails = timed("find schedule emails (cold or cached)", lambda: core.find_schedule_emails(gmail, cfg, 15))
    emails2 = timed("find schedule emails (cached)", lambda: core.find_schedule_emails(gmail, cfg, 15))
    print(f"  {len(emails)} schedule email(s) parsed")
    for p in emails[:5]:
        print(f"    week of {core.fmt_date(p.week_start)}  {p.total_hours:>6.2f}h  {len(p.shifts)} shifts  "
              f"status={core.schedule_status(state, p)}")
    assert [p.email_id for p in emails] == [p.email_id for p in emails2]

    if emails:
        a = timed("analyze newest email (calendar read)", lambda: core.analyze_import(cal, cfg, state, emails[0]))
        d = a["diff"]
        print(f"    would add {len(d['new'])}, already there {len(d['already'])}, dropped {len(d['removed'])}, "
              f"{len(a['conflicts'])} conflict(s)")

    ws, we = core.this_week_bounds()
    rep = timed("this week's report", lambda: core.generate_weekly_report(cal, cfg, ws, we))
    hours, pay = core.paid_totals(rep["category_hours"], cfg)
    print(f"    {hours:.2f}h paid, ${pay:.2f}, {sum(len(v) for v in rep['conflicts'].values())} conflict(s)")

    found = timed("search 'staples'", lambda: core.search_events(cal, cfg, "staples"))
    print(f"    {len(found)} match(es)")
    months = timed("6-month earnings", lambda: core.compute_monthly_earnings(cal, cfg, 6))
    print("    " + "  ".join(f"{r['label']} ${r['pay']:.0f}" for r in months))
    weeks = timed("8-week earnings", lambda: core.compute_weekly_earnings(cal, cfg, 8))
    print(f"    total ${sum(r['pay'] for r in weeks):.0f} over {len(weeks)} weeks")
    print("\nAll read-only checks passed.")


if __name__ == "__main__":
    main()
