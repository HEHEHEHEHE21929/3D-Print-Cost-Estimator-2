import os
import re
import shutil
import subprocess
import logging
from flask import Flask, render_template, request, redirect, url_for, flash
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
os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", "3.0"))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "600"))

def find_superslicer():
    """Resolve a usable superslicer_console executable path."""
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # if env is absolute path, verify; if name, resolve on PATH
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                log.info("Using SUPERSLICER_PATH from env: %s", env)
                return env
            log.warning("SUPERSLICER_PATH env set but not executable: %s", env)
        else:
            resolved = shutil.which(env)
            if resolved:
                log.info("Resolved SUPERSLICER_PATH name on PATH: %s -> %s", env, resolved)
                return resolved

    # common locations (Render build copies binary to /opt/render/superslicer)
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "./superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            log.info("Found superslicer at: %s", p)
            return p

    # fallback to names on PATH
    for name in ("superslicer_console", "superslicer"):
        resolved = shutil.which(name)
        if resolved:
            log.info("Found on PATH: %s -> %s", name, resolved)
            return resolved

    log.info("No SuperSlicer binary found")
    return None

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    """Extract time estimate from arbitrary text (stdout/stderr or gcode comment)."""
    if not text:
        return None, None
    for line in text.splitlines():
        ln = line.strip()
        # "3h 12m 5s"
        m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "192m 30s"
        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*60 + int(m.group(2))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "01:23:45"
        m = re.search(r"(\d+):(\d+):(\d+)", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "MM:SS" or "H:MM"
        m = re.search(r"(\d+):(\d+)\b", ln)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            secs = a*3600 + b*60 if a > 12 else a*60 + b
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "TIME: 1234"
        m = re.search(r"time[:=]\s*(\d+)", ln, re.I)
        if m:
            secs = int(m.group(1))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # phrases like "estimated printing time : 3h 54m"
        m = re.search(r"(estimated.*time|print time|total print time).*?([0-9hms:\s]+)", ln, re.I)
        if m:
            val = m.group(2).strip()
            p, c = parse_time_from_text(val)
            if p:
                return p, c
    return None, None

def parse_gcode_stats(gcode_path: str, superslicer_output: str = None):
    """Prefer superslicer output, then G-code comments. Return (pretty,cost) or ('Error','$Error')."""
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
    except Exception as exc:
        log.exception("Error reading gcode: %s", exc)
        return "Error", "$Error"

    return "Error", "$Error"

def estimate_time_from_file(path: str, infill: int, wall_thickness: float):
    try:
        kb = os.path.getsize(path) / 1024.0
    except Exception:
        kb = 100.0
    factor = max(0.5, min(3.0, kb / 100.0))
    base_hours = 1.25 * factor
    infill_factor = 1 + (infill / 100.0) * 0.8
    wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
    hours = base_hours * infill_factor * wall_factor
    secs = int(hours * 3600)
    return _secs_to_pretty(secs), f"${round(hours * COST_PER_HOUR, 2)}"

def ensure_profile_exists():
    if not os.path.exists(PROFILE_PATH):
        os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
        default = "[print]\nlayer_height = 0.2\nperimeters = 3\nfill_density = 20\n"
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as pf:
                pf.write(default)
            log.info("Wrote default profile %s", PROFILE_PATH)
        except Exception:
            log.exception("Failed to write profile")

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
        upload_path = os.path.join(UPLOAD_FOLDER, filename)
        try:
            file.save(upload_path)
        except Exception as exc:
            log.exception("Failed to save upload: %s", exc)
            flash("Failed to save uploaded file"); return redirect(request.url)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")

        ensure_profile_exists()

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        superslicer = find_superslicer()
        if not superslicer:
            log.warning("SuperSlicer not found; returning estimate")
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            pretty, cost = estimate_time_from_file(upload_path, infill, wall_thickness)
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        perimeters = max(1, int(wall_thickness / 0.4))
        variants = [
            [superslicer, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), upload_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, upload_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, "--output", output_gcode, upload_path],
        ]

        last_err = None
        combined_output = ""
        ok = False
        for cmd in variants:
            log.info("Running slicer command: %s", " ".join(cmd))
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
                combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok = True
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")
                log.warning("Slicer returned non-zero: %s", last_err)
            except subprocess.TimeoutExpired as e:
                last_err = "timeout"
                combined_output = (getattr(e, "stdout", "") or "") + "\n" + (getattr(e, "stderr", "") or "")
                log.warning("Slicer timed out")

        # if the process failed, still attempt to parse any estimate it printed
        p, c = parse_time_from_text(combined_output)
        if not ok and p:
            return render_template("results.html", print_time=p, cost=c, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=False)

        if not ok:
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        # prefer superslicer's stdout/stderr, then gcode comments
        pretty, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        if pretty == "Error":
            # try direct parse of combined_output
            p2, c2 = parse_time_from_text(combined_output)
            if p2:
                pretty, cost = p2, c2
                is_estimate = False
            else:
                pretty, cost = estimate_time_from_file(upload_path, infill, wall_thickness)
                is_estimate = True
        else:
            is_estimate = False

        return render_template("results.html",
                               print_time=pretty,
                               cost=cost,
                               filename=filename,
                               infill=infill,
                               wall_thickness=wall_thickness,
                               is_estimate=is_estimate,
                               gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/health")
def health():
    return {
        "status": "ok",
        "superslicer_path": find_superslicer() or "Not found",
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting app; resolved superslicer: %s", find_superslicer())
    app.run(host="0.0.0.0", port=port, debug=debug)
```# filepath: c:\Users\Zeti\Desktop\bambu_lab_test\app.py
import os
import re
import shutil
import subprocess
import logging
from flask import Flask, render_template, request, redirect, url_for, flash
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
os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", "3.0"))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "600"))

def find_superslicer():
    """Resolve a usable superslicer_console executable path."""
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # if env is absolute path, verify; if name, resolve on PATH
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                log.info("Using SUPERSLICER_PATH from env: %s", env)
                return env
            log.warning("SUPERSLICER_PATH env set but not executable: %s", env)
        else:
            resolved = shutil.which(env)
            if resolved:
                log.info("Resolved SUPERSLICER_PATH name on PATH: %s -> %s", env, resolved)
                return resolved

    # common locations (Render build copies binary to /opt/render/superslicer)
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "./superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            log.info("Found superslicer at: %s", p)
            return p

    # fallback to names on PATH
    for name in ("superslicer_console", "superslicer"):
        resolved = shutil.which(name)
        if resolved:
            log.info("Found on PATH: %s -> %s", name, resolved)
            return resolved

    log.info("No SuperSlicer binary found")
    return None

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    """Extract time estimate from arbitrary text (stdout/stderr or gcode comment)."""
    if not text:
        return None, None
    for line in text.splitlines():
        ln = line.strip()
        # "3h 12m 5s"
        m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "192m 30s"
        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*60 + int(m.group(2))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "01:23:45"
        m = re.search(r"(\d+):(\d+):(\d+)", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "MM:SS" or "H:MM"
        m = re.search(r"(\d+):(\d+)\b", ln)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            secs = a*3600 + b*60 if a > 12 else a*60 + b
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # "TIME: 1234"
        m = re.search(r"time[:=]\s*(\d+)", ln, re.I)
        if m:
            secs = int(m.group(1))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # phrases like "estimated printing time : 3h 54m"
        m = re.search(r"(estimated.*time|print time|total print time).*?([0-9hms:\s]+)", ln, re.I)
        if m:
            val = m.group(2).strip()
            p, c = parse_time_from_text(val)
            if p:
                return p, c
    return None, None

def parse_gcode_stats(gcode_path: str, superslicer_output: str = None):
    """Prefer superslicer output, then G-code comments. Return (pretty,cost) or ('Error','$Error')."""
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
    except Exception as exc:
        log.exception("Error reading gcode: %s", exc)
        return "Error", "$Error"

    return "Error", "$Error"

def estimate_time_from_file(path: str, infill: int, wall_thickness: float):
    try:
        kb = os.path.getsize(path) / 1024.0
    except Exception:
        kb = 100.0
    factor = max(0.5, min(3.0, kb / 100.0))
    base_hours = 1.25 * factor
    infill_factor = 1 + (infill / 100.0) * 0.8
    wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
    hours = base_hours * infill_factor * wall_factor
    secs = int(hours * 3600)
    return _secs_to_pretty(secs), f"${round(hours * COST_PER_HOUR, 2)}"

def ensure_profile_exists():
    if not os.path.exists(PROFILE_PATH):
        os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
        default = "[print]\nlayer_height = 0.2\nperimeters = 3\nfill_density = 20\n"
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as pf:
                pf.write(default)
            log.info("Wrote default profile %s", PROFILE_PATH)
        except Exception:
            log.exception("Failed to write profile")

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
        upload_path = os.path.join(UPLOAD_FOLDER, filename)
        try:
            file.save(upload_path)
        except Exception as exc:
            log.exception("Failed to save upload: %s", exc)
            flash("Failed to save uploaded file"); return redirect(request.url)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")

        ensure_profile_exists()

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        superslicer = find_superslicer()
        if not superslicer:
            log.warning("SuperSlicer not found; returning estimate")
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            pretty, cost = estimate_time_from_file(upload_path, infill, wall_thickness)
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        perimeters = max(1, int(wall_thickness / 0.4))
        variants = [
            [superslicer, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), upload_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, upload_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, "--output", output_gcode, upload_path],
        ]

        last_err = None
        combined_output = ""
        ok = False
        for cmd in variants:
            log.info("Running slicer command: %s", " ".join(cmd))
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
                combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok = True
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")
                log.warning("Slicer returned non-zero: %s", last_err)
            except subprocess.TimeoutExpired as e:
                last_err = "timeout"
                combined_output = (getattr(e, "stdout", "") or "") + "\n" + (getattr(e, "stderr", "") or "")
                log.warning("Slicer timed out")

        # if the process failed, still attempt to parse any estimate it printed
        p, c = parse_time_from_text(combined_output)
        if not ok and p:
            return render_template("results.html", print_time=p, cost=c, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=False)

        if not ok:
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        # prefer superslicer's stdout/stderr, then gcode comments
        pretty, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        if pretty == "Error":
            # try direct parse of combined_output
            p2, c2 = parse_time_from_text(combined_output)
            if p2:
                pretty, cost = p2, c2
                is_estimate = False
            else:
                pretty, cost = estimate_time_from_file(upload_path, infill, wall_thickness)
                is_estimate = True
        else:
            is_estimate = False

        return render_template("results.html",
                               print_time=pretty,
                               cost=cost,
                               filename=filename,
                               infill=infill,
                               wall_thickness=wall_thickness,
                               is_estimate=is_estimate,
                               gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/health")
def health():
    return {
        "status": "ok",
        "superslicer_path": find_superslicer() or "Not found",
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting app; resolved superslicer: %s", find_superslicer())
    app.run(host="0.0.0.0", port=port, debug=debug)
