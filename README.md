# Schedule Manager

A Windows desktop app that turns a work-schedule email into calendar events and keeps an eye on the rest of your week.

It reads schedule emails from Gmail (written for Domino's "your schedule is..." emails), adds the shifts to Google Calendar, and emails you a weekly summary that covers everything on your calendar: jobs, classes, appointments, and any conflicts between them.

## What it does

- Imports a week's shifts, shows what will change before you confirm, and never adds the same shift twice.
- Notices when a schedule is updated and can remove shifts that were dropped.
- Week and month views. Drag events to move or resize them, or drag on empty space to add one.
- Weekly report by email (styled HTML with a plain-text copy): hours and pay per job, conflicts, tight turnarounds, short rest between a late close and an early class.
- Earnings by week, month or pay period, with an optional take-home estimate and next payday.
- A study planner that finds free time around your classes and shifts.
- Search, a command palette (Ctrl+K), CSV and .ics export, undo for deletes and moves, offline mode, daily backups.

Jobs are matched by words in the event title (for example `staples, work`), and classes are recognised by course code (`CIS 111`). Both are configurable in Settings.

## Setup

1. Install Python 3.10 or newer.
2. Create a Google Cloud project, enable the Gmail API and the Google Calendar API, and create an OAuth client ID of type "Desktop app". Add your own address as a test user on the consent screen. Save the downloaded file as `credentials.json` next to the app.
3. Copy `config.example.json` to `config.json` and fill in your email, timezone and pay rates (or use the Settings page).
4. Double-click `Schedule Manager.vbs`. The first launch builds a private Python environment in `.venv` and asks you to sign in to Google.

`Create Desktop Shortcut.bat` adds Desktop and Start menu shortcuts. The whole folder can be copied to another computer; `.venv` is rebuilt automatically.

`token.json`, `credentials.json`, `config.json` and `state.json` stay on your machine and are not part of the repository.

## Command line

```
python dominos_schedule.py            # import the newest schedule
python dominos_schedule.py report     # email the weekly report
python dominos_schedule.py search dentist
python dominos_schedule.py -h
```

## Tests

```
.venv\Scripts\python.exe -m unittest tests.test_core
.venv\Scripts\python.exe -m unittest tests.test_startup
.venv\Scripts\python.exe -m unittest tests.test_gui
```

The tests use in-memory fakes for Gmail and Calendar and never touch a real account. `run_tests.bat` runs all of them. `tests/live_smoke.py` is a read-only check against your real account.

Not affiliated with Domino's.
