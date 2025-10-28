# ...existing code...
import os
import re
import shutil
import subprocess
import logging
from flask import Flask, render_template, request, redirect, flash
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")
PROFILE_PATH = os.path.join(BASE_DIR, "profiles", "my_config.ini")
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(os.path.dirname(PROFILE_PATH) or BASE_DIR, exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", "3.0"))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "600"))

def find_superslicer():
    # Prefer explicit env var, then PATH, then known deploy location
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        if os.path.isabs(env) and os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        resolved = shutil.which(env)
        if resolved:
            return resolved
    for name in ("superslicer_console", "superslicer"):
        p = shutil.which(name)
        if p:
            return p
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None

SUPERSLICER_PATH = find_superslicer()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    if not text:
        return None, None
    for line in text.splitlines():
        ln = line.strip()
        m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*60 + int(m.group(2))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        m = re.search(r"(\d+):(\d+):(\d+)", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        m = re.search(r"TIME[:=]\s*(\d+)", ln, re.I)
        if m:
            secs = int(m.group(1))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
    return None, None

def parse_gcode_stats(gcode_path: str, superslicer_output: str = None):
    if superslicer_output:
        p, c = parse_time_from_text(superslicer_output)
        if p:
            return p, c
    if not os.path.exists(gcode_path):
        return "Error", "$Error"
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                if line.lstrip().startswith(";") or "estimated" in line.lower() or "print time" in line.lower():
                    p, c = parse_time_from_text(line)
                    if p:
                        return p, c
    except Exception:
        log.exception("Failed reading gcode")
        return "Error", "$Error"
    return "Error", "$Error"

def ensure_profile():
    if not os.path.exists(PROFILE_PATH):
        os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as pf:
                pf.write("[print]\nlayer_height=0.2\nperimeters=3\nfill_density=20\n")
        except Exception:
            log.exception("Could not write default profile")

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part"); return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No selected file"); return redirect(request.url)
        if not allowed_file(file.filename):
            flash("Invalid file type"); return redirect(request.url)

        filename = secure_filename(file.filename)
        uploaded = os.path.join(UPLOAD_FOLDER, filename)
        try:
            file.save(uploaded)
        except Exception:
            flash("Failed to save upload"); return redirect(request.url)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")
        ensure_profile()

        # parse form inputs
        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall = 0.8

        superslicer = SUPERSLICER_PATH or find_superslicer()
        if not superslicer:
            log.warning("SuperSlicer binary not found - returning estimate")
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            # simple estimate fallback
            pretty, cost = estimate = (lambda kb, inf, w: (lambda secs, cost: (secs_to_pretty(secs), cost))(
                int((1.25 * max(0.5, min(3.0, kb/100.0)) * (1 + (inf/100.0)*0.8) * (1 + max(0.0, (w-0.4)/0.4*0.3))) * 3600),
                f"${round((1.25 * max(0.5, min(3.0, kb/100.0)) * (1 + (inf/100.0)*0.8) * (1 + max(0.0, (w-0.4)/0.4*0.3))) * COST_PER_HOUR, 2)}"
            ))(os.path.getsize(uploaded)/1024.0 if os.path.exists(uploaded) else 100.0, infill, wall)
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, is_estimate=True)

        perimeters = max(1, int(wall / 0.4))
        variants = [
            [superslicer, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), uploaded, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, uploaded, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, "--output", output_gcode, uploaded],
        ]

        combined_output = ""
        ok = False
        last_err = None
        for cmd in variants:
            log.info("Running slicer: %s", " ".join(cmd))
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
                combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok = True
                break
            except FileNotFoundError as e:
                last_err = str(e)
                log.exception("Binary missing")
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")
                log.warning("Slicer returned non-zero")
            except subprocess.TimeoutExpired as e:
                last_err = "timeout"
                combined_output = (getattr(e, "stdout", "") or "") + "\n" + (getattr(e, "stderr", "") or "")
                log.warning("Slicer timeout")

        # even on failure try to extract printed estimate
        p, c = parse_time_from_text(combined_output)
        if not ok and p:
            return render_template("results.html", print_time=p, cost=c, filename=filename, is_estimate=False)

        if not ok:
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        pretty, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        is_estimate = (pretty == "Error")
        if is_estimate:
            p2, c2 = parse_time_from_text(combined_output)
            if p2:
                pretty, cost = p2, c2
                is_estimate = False
            else:
                pretty, cost = secs_to_pretty(0), "$0.00"
                is_estimate = True

        return render_template("results.html", print_time=pretty, cost=cost, filename=filename, is_estimate=is_estimate, gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/health")
def health():
    return {
        "status": "ok",
        "superslicer_path": SUPERSLICER_PATH or find_superslicer() or "Not found",
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    log.info("Starting app; superslicer: %s", SUPERSLICER_PATH or find_superslicer())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("DEBUG", "False").lower() == "true")
# ...existing code...
    log.info("Resolved SuperSlicer: %s", SUPERSLICER_PATH)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("DEBUG", "False").lower() == "true")
