import os
import re
import math
import json
import uuid
import time
import threading
from functools import wraps
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Prefer an env var in real deployments; keep a dev fallback so `python app.py` still works.
app.secret_key = os.environ.get("SECRET_KEY", "smart_civic_secret_key_2026")

# Simple hardcoded agent credentials for this prototype. Fine for a demo/hackathon build;
# swap for a real user table with hashed passwords (e.g. werkzeug.security.generate_password_hash)
# before using this anywhere beyond local testing.
AGENT_USERNAME = os.environ.get("AGENT_USERNAME", "agent1")
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "sha123")

# Anchor every file path to the folder this script lives in -- NOT the current working
# directory. Without this, running the app from a different working directory (common on
# Windows via VS Code's "Run" button, or double-clicking a shortcut) saves uploads to one
# location while Flask's static file server looks in another, so images silently 404.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_FOLDER = os.path.join(STATIC_DIR, "uploads")
RESOLVED_FOLDER = os.path.join(STATIC_DIR, "resolved")
# All complaints live in this one JSON file. Override with the DATA_FILE env var if you want it
# somewhere else (e.g. a mounted disk on a hosting service).
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(BASE_DIR, "data.json"))
data_lock = threading.RLock()  # one writer at a time so two requests can't corrupt the JSON file

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload cap
# Keep the citizen's "My Complaints" cookie for a year instead of losing it when the browser closes.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESOLVED_FOLDER, exist_ok=True)

# Persisted to DATA_FILE after every change and reloaded on startup -- see
# load_data()/save_data() below. Swap for the real schema in the blueprint
# (Postgres/MySQL) if you outgrow a single JSON file.
master_issues_db = []
complaints_db = []
next_master_id = 1
next_complaint_id = 1

SEVERITY_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}
AREA_RANK = {"Low": 1, "High": 3, "Very High": 4}
OPEN_STATUSES_EXCLUDING = ("RESOLVED", "CLOSED")

# Buckets every granular workflow status into the 3 groups the public status
# chart shows. Read-only for citizens -- the underlying status can only be
# changed from the agent-only /dashboard route.
STATUS_BUCKETS = {
    "REPORTED": "Unsolved",
    "VERIFIED": "Unsolved",
    "ASSIGNED": "Unsolved",
    "IN_PROGRESS": "In Progress",
    "RESOLVED": "Solved",
    "CLOSED": "Solved",
}


def get_status_summary():
    """Counts every master issue into Solved / Unsolved / In Progress for the
    dynamic status chart on the public dashboard."""
    summary = {"Unsolved": 0, "In Progress": 0, "Solved": 0}
    for master in master_issues_db:
        bucket = STATUS_BUCKETS.get(master["status"], "Unsolved")
        summary[bucket] += 1
    return summary


def save_data():
    """Write the current in-memory state to DATA_FILE so it survives a restart."""
    with data_lock:
        serializable_masters = []
        for m in master_issues_db:
            m_copy = dict(m)
            m_copy["created_at"] = m["created_at"].isoformat()
            serializable_masters.append(m_copy)

        payload = {
            "master_issues": serializable_masters,
            "complaints": complaints_db,
            "next_master_id": next_master_id,
            "next_complaint_id": next_complaint_id,
        }
        tmp_path = DATA_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # make sure the bytes are really on disk, not just in a buffer
        os.replace(tmp_path, DATA_FILE)  # atomic swap -- never leaves a half-written file


def load_data():
    """Restore state from DATA_FILE on startup. If the file is missing, create an empty one.
    If it is unreadable, it is moved aside to a .corrupt-<timestamp> backup -- never silently
    overwritten -- so a bad write can't wipe out every complaint."""
    global master_issues_db, complaints_db, next_master_id, next_complaint_id
    if not os.path.exists(DATA_FILE):
        save_data()
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        masters = payload.get("master_issues", [])
        for m in masters:
            created = datetime.fromisoformat(m["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            m["created_at"] = created
        complaints = payload.get("complaints", [])
        n_master = payload.get("next_master_id", 1)
        n_complaint = payload.get("next_complaint_id", 1)
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError, AttributeError):
        backup = f"{DATA_FILE}.corrupt-{int(time.time())}"
        try:
            os.replace(DATA_FILE, backup)
            print(f"[warning] {DATA_FILE} could not be read; saved a copy as {backup} and starting empty.")
        except OSError:
            print(f"[warning] {DATA_FILE} could not be read and could not be backed up.")
        save_data()  # start a fresh, valid data file
        return

    master_issues_db, complaints_db = masters, complaints
    next_master_id, next_complaint_id = n_master, n_complaint


def parse_issue_id(raw):
    """Accepts '3', 'MI3' or 'MI003' and returns the integer ID (or None)."""
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file_storage, folder):
    """Validate and save an uploaded image with a collision-proof filename.
    Always returns a forward-slash relative path (e.g. "uploads/abc123_photo.png")
    since that's what url_for('static', filename=...) needs to build a correct URL --
    a raw OS path (which uses backslashes on Windows) would silently produce a broken
    image link."""
    if not file_storage or file_storage.filename == "":
        return ""
    if not allowed_file(file_storage.filename):
        flash(f"'{file_storage.filename}' isn't a supported image type and was skipped.", "danger")
        return ""
    safe_name = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    file_storage.save(os.path.join(folder, unique_name))
    relative_folder = os.path.relpath(folder, STATIC_DIR).replace(os.sep, "/")
    return f"{relative_folder}/{unique_name}"


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def text_similarity(a, b):
    """Simple, dependency-free similarity score (0-100) used as the >70% duplicate check."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio() * 100


def waiting_days_for(created_at):
    return (datetime.now(timezone.utc) - created_at).days


def calculate_priority(complaint_count, severity, area_impact, waiting_days):
    if complaint_count <= 2:
        count_score = 1
    elif complaint_count <= 5:
        count_score = 2
    elif complaint_count <= 10:
        count_score = 3
    elif complaint_count <= 20:
        count_score = 4
    else:
        count_score = 5

    severity_score = SEVERITY_RANK.get(severity, 2)
    area_score = AREA_RANK.get(area_impact, 2)

    if waiting_days <= 3:
        waiting_score = 1
    elif waiting_days <= 7:
        waiting_score = 2
    elif waiting_days <= 15:
        waiting_score = 3
    else:
        waiting_score = 4

    total_score = count_score + severity_score + area_score + waiting_score

    if total_score <= 4:
        level = "LOW"
    elif total_score <= 8:
        level = "MEDIUM"
    elif total_score <= 12:
        level = "HIGH"
    else:
        level = "CRITICAL"

    return total_score, level


def recalc_all_priorities():
    """Dynamic Priority Recalculation step from the workflow: ages every open
    master issue on each read instead of freezing its score at creation time."""
    for master in master_issues_db:
        if master["status"] in OPEN_STATUSES_EXCLUDING:
            continue
        score, level = calculate_priority(
            master["complaint_count"],
            master["severity"],
            master["area_impact"],
            waiting_days_for(master["created_at"]),
        )
        master["priority_score"] = score
        master["priority_level"] = level


def find_duplicate_master(issue_type, latitude, longitude, description):
    """Module 4: same category, within 100m, AND description similarity > 70%
    against the closest-matching existing report in that cluster."""
    best_match = None
    best_score = -1
    for master in master_issues_db:
        if master["issue_type"] != issue_type or master["status"] in OPEN_STATUSES_EXCLUDING:
            continue
        dist = haversine_distance(latitude, longitude, master["latitude"], master["longitude"])
        if dist > 100:
            continue

        siblings = [c["description"] for c in complaints_db if c["master_issue_id"] == master["master_issue_id"]]
        if not siblings:
            similarity = 100.0  # no text to compare against yet; type+distance is enough
        else:
            similarity = max(text_similarity(description, s) for s in siblings)

        if similarity >= 70 and similarity > best_score:
            best_match, best_score = master, similarity

    return best_match


def login_required(view_func):
    """Restricts a route to authenticated agents only. Unauthenticated visitors
    are redirected to /login with a flash message, and sent back to the page
    they originally wanted once they log in."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_agent"):
            flash("Please log in as an authorized agent to access this page.", "danger")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == AGENT_USERNAME and password == AGENT_PASSWORD:
            session["is_agent"] = True
            session["agent_username"] = username
            flash("Logged in successfully.", "success")
            next_page = request.args.get("next")
            # only follow local paths -- never bounce the agent to an outside website
            if not next_page or not next_page.startswith("/") or next_page.startswith("//"):
                next_page = url_for("dashboard")
            return redirect(next_page)
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("is_agent", None)
    session.pop("agent_username", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/")
def index():
    recalc_all_priorities()
    # Citizens only ever see aggregate status counts here. The priority-ranked list of actual
    # complaints is shown to logged-in agents only.
    recent_master_issues = []
    if session.get("is_agent"):
        recent_master_issues = sorted(master_issues_db, key=lambda m: m["priority_score"], reverse=True)[:5]
    return render_template(
        "index.html",
        master_issues=recent_master_issues,
        complaints=complaints_db,
        summary=get_status_summary(),
    )


@app.route("/report", methods=["GET", "POST"])
def report():
    global next_master_id, next_complaint_id
    if request.method == "POST":
        try:
            user_id = int(request.form.get("user_id", 1))
        except ValueError:
            user_id = 1
        issue_type = request.form.get("issue_type")
        description = request.form.get("description", "")
        area = request.form.get("area", "Main Street")
        severity = request.form.get("severity", "Medium")
        area_impact = request.form.get("area_impact", "High")

        try:
            latitude = float(request.form.get("latitude", ""))
            longitude = float(request.form.get("longitude", ""))
        except ValueError:
            flash("Latitude/longitude looked invalid, so we couldn't place your report on the map. Please try capturing GPS again.", "danger")
            return render_template("report.html")

        image_path = save_upload(request.files.get("image"), UPLOAD_FOLDER)

        with data_lock:  # one request at a time touches the data + JSON file
            matched_master = find_duplicate_master(issue_type, latitude, longitude, description)

            if matched_master:
                matched_master["complaint_count"] += 1
                # Escalate to the worse of the two ratings -- one report calling it "Critical"
                # shouldn't get diluted by earlier reports that called it "Medium".
                if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(matched_master["severity"], 0):
                    matched_master["severity"] = severity
                if AREA_RANK.get(area_impact, 0) > AREA_RANK.get(matched_master["area_impact"], 0):
                    matched_master["area_impact"] = area_impact

                score, level = calculate_priority(
                    matched_master["complaint_count"],
                    matched_master["severity"],
                    matched_master["area_impact"],
                    waiting_days_for(matched_master["created_at"]),
                )
                matched_master["priority_score"] = score
                matched_master["priority_level"] = level
                master_id = matched_master["master_issue_id"]
                flash(f"Duplicate detected! Grouped into existing Master Issue MI00{master_id}. Use this ID to track its status.", "info")
            else:
                score, level = calculate_priority(1, severity, area_impact, waiting_days=0)
                matched_master = {
                    "master_issue_id": next_master_id,
                    "issue_type": issue_type,
                    "latitude": latitude,
                    "longitude": longitude,
                    "area": area,
                    "severity": severity,
                    "area_impact": area_impact,
                    "complaint_count": 1,
                    "priority_score": score,
                    "priority_level": level,
                    "status": "REPORTED",
                    "created_at": datetime.now(timezone.utc),
                    "resolution_image": "",
                }
                master_issues_db.append(matched_master)
                master_id = next_master_id
                next_master_id += 1
                flash(f"New complaint registered successfully! Your Issue ID is MI00{master_id} -- use it to track the status.", "success")

            complaint = {
                "complaint_id": next_complaint_id,
                "user_id": user_id,
                "master_issue_id": master_id,
                "issue_type": issue_type,
                "description": description,
                "image_path": image_path,
                "area": area,
                "status": matched_master["status"],
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            complaints_db.append(complaint)
            next_complaint_id += 1

            # Track which master issues this browser has personally reported, so the
            # citizen can look them up later on "My Complaints" without needing a login.
            my_master_ids = session.get("my_master_ids", [])
            if master_id not in my_master_ids:
                my_master_ids.append(master_id)
            session["my_master_ids"] = my_master_ids
            session.permanent = True  # keep "My Complaints" after the browser is closed

            save_data()

        return redirect(url_for("index"))

    return render_template("report.html")


@app.route("/my-complaints")
def my_complaints():
    """Citizen-facing, STATUS-ONLY view. Shows just the issue ID, type and current status for
    (a) the issues this browser reported, and (b) any issue looked up by its ID. No locations,
    descriptions, priority scores or photos -- those are for logged-in authorities only.
    Citizens cannot change anything from here."""
    my_master_ids = session.get("my_master_ids", [])
    my_issues = [m for m in master_issues_db if m["master_issue_id"] in my_master_ids]
    my_issues.sort(key=lambda m: m["master_issue_id"], reverse=True)

    lookup_raw = request.args.get("id", "").strip()
    tracked, lookup_failed = None, False
    if lookup_raw:
        wanted = parse_issue_id(lookup_raw)
        tracked = next((m for m in master_issues_db if m["master_issue_id"] == wanted), None)
        lookup_failed = tracked is None

    return render_template(
        "my_complaints.html",
        master_issues=my_issues,
        buckets=STATUS_BUCKETS,
        tracked=tracked,
        lookup_raw=lookup_raw,
        lookup_failed=lookup_failed,
    )


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    """Authority-only (login required for BOTH viewing and editing)."""
    if request.method == "POST":
        try:
            master_id = int(request.form.get("master_issue_id", ""))
        except ValueError:
            flash("That update request looked malformed and was ignored.", "danger")
            return redirect(url_for("dashboard"))
        new_status = request.form.get("status")
        if new_status not in STATUS_BUCKETS:
            flash("Unknown status -- nothing was changed.", "danger")
            return redirect(url_for("dashboard"))
        with data_lock:
            for master in master_issues_db:
                if master["master_issue_id"] == master_id:
                    master["status"] = new_status
                    # keep every individual complaint in the group in sync with the master issue
                    for c in complaints_db:
                        if c["master_issue_id"] == master_id:
                            c["status"] = new_status
                    if new_status == "RESOLVED":
                        res_path = save_upload(request.files.get("resolution_image"), RESOLVED_FOLDER)
                        if res_path:
                            master["resolution_image"] = res_path
                    flash(f"Master Issue MI00{master_id} status updated to {new_status}.", "success")
                    break
            save_data()
        return redirect(url_for("dashboard"))

    recalc_all_priorities()
    sorted_issues = sorted(master_issues_db, key=lambda m: m["priority_score"], reverse=True)
    return render_template("dashboard.html", master_issues=sorted_issues, complaints=complaints_db)


@app.route("/status")
def status_dashboard():
    """Public, read-only status chart. Any citizen can view this -- no login
    required -- but it has no POST handler, so nothing here can change a
    status. Actually editing status stays confined to the agent-only
    /dashboard route above."""
    recalc_all_priorities()
    summary = get_status_summary()
    return render_template(
        "status.html",
        summary=summary,
        total=sum(summary.values()),
    )


@app.route("/wall")
def wall():
    resolved_issues = [m for m in master_issues_db if m["status"] == "RESOLVED"]
    return render_template("wall.html", resolved_issues=resolved_issues, complaints=complaints_db)


load_data()  # restore any data saved from a previous run, before the app starts serving requests

def find_free_port(preferred=5000):
    """Port 5000 is often taken (e.g. macOS AirPlay Receiver, or a previous run still open).
    Try it first, then the next few ports."""
    import socket
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:  # nothing is listening -> free
                return port
    return preferred


if __name__ == "__main__":
    import webbrowser
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 0)) or find_free_port(5000)
    url = f"http://127.0.0.1:{port}"
    # In debug mode Flask starts the app twice (reloader); only open the browser once.
    if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        print("\n" + "=" * 60)
        print(f"  Smart Civic Reporter is running -> open  {url}")
        print("  (press CTRL+C to stop)")
        print("=" * 60 + "\n", flush=True)
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=debug_mode)
