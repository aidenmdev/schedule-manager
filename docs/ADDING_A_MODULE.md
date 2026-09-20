# Adding a tool to the app

The app is a small platform. The schedule is one tool (a *module*); receipt logging, a TrueNAS storage browser and
anything else can be added the same way without touching the schedule code.

## What a module is

A `hub.Module` (see `hub.py`) says:

- `key`: short and permanent, for example `"receipts"`. It names the module's data folder.
- `name` and `description`: shown on Home.
- `section`: the small heading above its pages in the sidebar.
- `pages`: a list of `hub.PageSpec(key, title, icon, page_class)`. Page keys must be unique across the whole app.
- `settings_tabs` (optional): `[(tab title, build)]` for extra tabs in Settings.
- `home_card` (optional): a function that builds the module's card on Home.

Register it once, before the app starts:

```python
import hub
hub.register(hub.Module("receipts", "Receipts", "Log and search receipts", section="RECEIPTS",
                        pages=[hub.PageSpec("receipts", "Receipts", "\ue8a5", ReceiptsPage)]))
```

`App.NAV`, the sidebar, Ctrl+number shortcuts, lazy page creation and the icon table all come from the registry.

## A page

A page is a `Page` subclass (see `schedule_gui.py`): build widgets in `__init__`, fill them in `on_show()`, and use
`self.app.run_job(...)` for anything slow so the window never freezes. Use the helpers in `schedule_gui.py`
(`card`, `label`, `button`, `entry`, `segmented`, `option_menu`, `switch`, `scroll_frame`) so it matches the rest.

## Data and settings

- Files: `hub.module_dir("receipts")` gives a folder under the app data folder (`modules/receipts`), created on demand.
- Settings: keep them under `config["modules"]["receipts"]` (`hub.module_settings(config, "receipts")`), so a module
  never collides with the schedule's own keys.
- Secrets (passwords, API keys for a NAS): don't put them in `config.json`, which is synced to your other computers.
  Add the key name to `LOCAL_ONLY_CONFIG` in `sync.py`, or keep them in a file inside the module's folder.

## Talking to other machines

Long calls (a TrueNAS API, an upload) must run off the main thread: `app.run_async(work, on_success, on_error)`.
Network errors that mean "offline" are `OSError`s; `core.is_network_error(e)` recognises them and the app treats them quietly.

## Sync and updates

- Settings under `config["modules"]` sync between computers automatically (everything in `config.json` does, except
  `LOCAL_ONLY_CONFIG`). Module files in `modules/<key>` do not sync yet; add them in `sync.py` when a module needs it.
- Updates need nothing extra: new module files are part of the program, so `publish_update.bat` ships them.
- If a module needs a new Python package, add it to `requirements.txt` and build a fresh installer once
  (`build_installer.bat`); updates only replace files the installer already contains.
