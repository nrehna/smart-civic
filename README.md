# Smart Civic Issue Reporter

An AI-assisted civic issue reporting system featuring automated duplicate complaint detection, spatial clustering, and dynamic priority calculation.

## Features
- **Duplicate Complaint Detection:** Groups reports within a 100-meter radius for the same issue category.
- **Dynamic Priority Scoring:** Computes urgency based on complaint frequency, severity, area impact, and waiting time.
- **Authority Dashboard:** Enables officials to filter, assign workers, and update issue status.
- **Public Transparency Wall:** Showcases before-and-after resolutions for public trust.

## How to Run
1. Install Python 3.8+
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the application:
   ```bash
   python app.py
   ```
4. Open your browser and navigate to: `http://127.0.0.1:5000`

## Notes on this version
- Duplicate detection now checks description similarity (>70%, via `difflib`) in addition to category + 100m radius, so nearby-but-unrelated reports of the same issue type no longer get merged by mistake.
- Priority scores are recalculated dynamically from each master issue's real age every time the dashboard/home page loads, instead of being frozen at creation time.
- When a duplicate report carries a worse severity or area-impact rating than the existing one, the master issue is escalated to match.
- Uploaded images are validated by extension and saved with a random prefix to avoid overwriting files with the same name; uploads are capped at 8MB.
- `FLASK_DEBUG=1` enables the Werkzeug debugger (off by default); set `SECRET_KEY` in the environment for anything beyond local testing.
- **Data now persists across restarts.** All complaints and master issues are saved to `data.json` (created automatically in the project folder on first report) after every change, and reloaded automatically the next time you run `python app.py`. Delete `data.json` if you want to reset the app back to empty. Uploaded images in `static/uploads/` and `static/resolved/` are unaffected by restarts either way, since they're already files on disk.
- **Fixed broken before/after images on Windows.** Uploaded photos are now saved to an absolute path anchored to the app's own folder (not the current working directory), and the stored filename always uses forward slashes. Previously, running the app from a different working directory than `app.py` (which happens easily via some IDE "Run" buttons on Windows) caused uploads to save correctly but the image URLs to come out malformed, showing broken image icons on the Transparency Wall.
- **Before/after photos now visible from the Dashboard, not just the public Wall.** Each row on the Authority Dashboard now has a "View Photos" button (next to "Update") that opens a modal showing the citizen's original "before" photo alongside the "after" resolution photo once one has been uploaded. Previously the dashboard never received the complaint data needed to show the before photo at all, so there was no way to see it outside the public Transparency Wall page.

## Access control (new)
- **Authority Dashboard is now login-protected.** Only an authorized agent can access `/dashboard` (view all issues, update status, upload resolution photos). Default credentials for this prototype:
  - Username: `agent1`
  - Password: `sha123`

  These can be overridden without touching code by setting the `AGENT_USERNAME` / `AGENT_PASSWORD` environment variables before running the app. **This is a single hardcoded account suitable for a demo/prototype only** — before using this anywhere beyond local testing, replace it with a real user table and hashed passwords (e.g. `werkzeug.security.generate_password_hash`).
- **Citizens do not need to log in.** Anyone can browse the homepage, submit a report (`/report`), and view the public Transparency Wall (`/wall`) without any account.
- **"My Complaints" (`/my-complaints`)** is a new citizen-facing page showing only the issues that browser has personally reported — not everyone else's. It's tracked via the browser's session cookie the moment a report is submitted, so no login or manually-entered ID is needed. Note: since this uses a session cookie rather than a real account, switching browsers or clearing cookies will show an empty list even for the same person — that's expected behavior for this lightweight, no-signup approach.
- Attempting to open `/dashboard` without logging in redirects to `/login` with a message, and returns you to the dashboard automatically after a successful login.

## Update: permanent storage + strict citizen / authority access
- **Storage:** every complaint is written to `data.json` (next to `app.py`, or wherever the `DATA_FILE` env var points) after each change, using an atomic, fsync'd write behind a lock. The file is created automatically on first start. If it ever becomes unreadable it is moved to `data.json.corrupt-<timestamp>` instead of being overwritten. **Keep `data.json` (and `static/uploads`, `static/resolved`) when you copy or re-zip the project, and always run the app from the same folder** -- a fresh copy of the project starts with an empty database.
- **Citizens (no login, read-only):** can submit reports and see *status only* -- the home page shows Unsolved / In Progress / Solved counts, `/status` shows the chart, and `/my-complaints` shows only ID, issue type and status for their own reports (remembered for a year) plus a "track by Issue ID" box. No locations, descriptions, priority scores or photos, and no way to edit.
- **Authorities (login required):** only a logged-in agent can open `/dashboard` to view every complaint's details (description, area, photos, priority) and edit the status / upload the resolution photo. Status changes are also written onto each individual complaint.

## Troubleshooting: the link doesn't open
- `python app.py` now prints a clear `open http://127.0.0.1:<port>` line and opens your browser automatically. If port 5000 is busy it picks the next free one, so use the port shown in the terminal.
- `requirements.txt` no longer lists `numpy` (the app never used it, and its old pinned version fails to install on newer Python). If `pip install -r requirements.txt` failed before, Flask was never installed -- run it again.
- Use `python app.py` from inside the `smart_civic_reporter` folder, and open the link in a browser on the same computer (`127.0.0.1` only works locally).
