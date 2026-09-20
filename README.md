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

`token.json`, `credentials.json`, `config.json` and `state.json` stay on your machine and are not part of the repository, and neither is the built installer.

## Updating your other computers

Change the code on your main computer, then run `publish_update.bat "what changed"`. It rebuilds the program, works out which files changed since the installer (usually 3 or 4 files, about 9 MB), signs the update and publishes it to the `updates` branch of your GitHub project (`aidenmdev/schedule-manager`, set in `release\repo.txt` if you use another). It pushes with the git that is already signed in on this computer, and each publish replaces the previous one, so the branch stays one commit. GitHub's cache can take up to five minutes to show a new update to the other computers.

Each installed copy checks for an update when it opens (at most twice a day) and under About & help > Updates, where "Check for updates" and "Update now" do it on demand. Nothing installs without you choosing Update now. The app closes, a small helper swaps the files in, and it reopens by itself. If anything goes wrong, the previous version is put back and the app tells you.

Updates are signed with a key made the first time you build (`release\update_signing_key.pem`). Copies of the app only accept updates signed by that key, so anyone who can read or even change the branch still can't install anything on your computers. Keep the `release` folder backed up: if it is lost, build a new installer and reinstall on your other computers once. The same applies whenever you run `build_installer.bat` again, because a new installer starts a new update line and older installs need the new setup file once. Copies that are running from the source folder don't update themselves. The project has to stay public (or the computers need access to it), because they download the update without signing in.

## Installing on another computer

Run `build_installer.bat` once on a computer that has Python. It builds `dist\Schedule Manager Setup.exe` (about 40 MB), a normal installer for any Windows 10 or 11 computer, with no Python needed and no administrator rights. Copy that one file to the other computer and run it. It offers Desktop and Start menu shortcuts and an option to start the tablet display when you sign in, and it adds an entry to Settings > Apps so it can be uninstalled. For a script, `"Schedule Manager Setup.exe" /S` installs silently (`--dir`, `--no-desktop`, `--no-start-menu`, `--tablet-autostart` and `--no-launch` are also accepted). Running a newer setup file updates the program and leaves your data alone.

If `credentials.json` is next to the app when you build, the installer includes it, so the new computer only has to sign in to Google. Treat the setup file as private for that reason. The installed program keeps its files in `%LOCALAPPDATA%\Schedule Manager`, separate from the program folder. Uninstalling asks whether to delete them too.

## Using more than one computer

Your calendar events are shared automatically because they live in Google Calendar. Settings, import history and undo information sync too. They are stored in a private calendar named "Schedule Manager sync data" in the same Google account (hidden from your calendar list; don't delete it).

To add a computer: copy or clone this folder, put `credentials.json` next to the app, and open `Schedule Manager.vbs`. Sign in to Google when asked. The first sync loads your settings and history, so there's no need to copy `config.json`. After that, changes sync by themselves a few seconds after you make them, every few minutes, and whenever you switch back to the window. Settings > Sync shows the status and has a Sync now button. From the command line, `python dominos_schedule.py sync` does the same.

Two computers can change different settings at the same time and both changes are kept. If both change the same setting, the newer edit wins. Deleting a week or undoing an import on one computer shows up on the others. A backup of your files is saved in `backups` before a sync ever replaces them. Turn syncing off per computer in Settings (`sync_enabled`, `tablet_port` and window size are never synced).

## Tablet display

Shows your week (read-only, no pay) on any device on your home Wi-Fi, such as an old tablet used as a wall calendar. The page is plain HTML and CSS so it works in old Android browsers, and it refreshes itself every few minutes.

Open Settings > Tablet. It shows whether the display is running and the address to type on the tablet, something like `http://192.168.1.20:8765`, with Start, Stop and Copy address buttons. It runs in the background with no window and keeps going after you close the app until you press Stop. The same controls exist as Start menu shortcuts in the installed app, and as `Tablet Display.vbs` and `Stop Tablet Display.bat` when running from the folder. `Tablet Auto-Start ON.bat` starts it quietly each time you sign in to Windows (`OFF` undoes that).

Windows asks the first time whether Python may use your network; allow it on private networks. The PC has to be on and awake for the tablet to show anything, and the tablet has to be on the same Wi-Fi. Nothing is exposed to the internet, but anyone on your Wi-Fi can open the page. The port (default 8765) is set on the same tab. If it doesn't start, look in `tablet.log`.

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
.venv\Scripts\python.exe -m unittest tests.test_scroll
.venv\Scripts\python.exe -m unittest tests.test_tablet
.venv\Scripts\python.exe -m unittest tests.test_gui_tablet
.venv\Scripts\python.exe -m unittest tests.test_sync
.venv\Scripts\python.exe -m unittest tests.test_installer
.venv\Scripts\python.exe -m unittest tests.test_updater
.venv\Scripts\python.exe -m unittest tests.test_gui_updates
.venv\Scripts\python.exe -m unittest tests.test_gui_sync
```

The tests use in-memory fakes for Gmail and Calendar and never touch a real account, and their windows are invisible and off screen (`python -m tests.screenshots OUT_DIR` saves a picture of each page the same way). `run_tests.bat` runs all of them. `tests/live_smoke.py` is a read-only check against your real account.

Not affiliated with Domino's.
