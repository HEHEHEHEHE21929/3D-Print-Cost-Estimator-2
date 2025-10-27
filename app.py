import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "output")
PROFILE_PATH = os.path.join(BASE_DIR, "profiles", "my_config.ini")
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}
COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", "3.0"))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "600"))

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)

def find_superslicer():
    # 1) explicit env var
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # env may be name or absolute path
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            w = shutil.which(env)
            if w:
                return w
    # 2) likely deployment locations
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 3) PATH
    return shutil.which("superslicer_console") or shutil.which("superslicer")

SUPERSLICER_PATH = find_superslicer()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    """
    Scan text (stdout/stderr or a gcode comment line) for common time formats.
    Returns (pretty_str, cost_str) or (None, None).
    """
    if not text:
        return None, None
    # normalize
    for line in text.splitlines():
        ln = line.strip()
        # prioritized patterns
        # 1) "3h 12m 5s" or "3 h 12 m 5 s"
        m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # 2) "192m 30s"
        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", ln, re.I)
        if m:
            secs = int(m.group(1))*60 + int(m.group(2))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # 3) "01:23:45" or "1:23:45"
        m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # 4) "MM:SS" or "H:MM" heuristic
        m = re.search(r"(\d{1,2}):(\d{2})\b", ln)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                secs = a*3600 + b*60
            else:
                secs = a*60 + b
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # 5) "TIME: 1234" seconds
        m = re.search(r"TIME[:=]\s*(\d+)", ln, re.I)
        if m:
            secs = int(m.group(1))
            return secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # 6) phrases like "estimated printing time : 3h 54m 52s"
        m = re.search(r"(estimated.*time|print time|total print time).*?([0-9hms:\s]+)", ln, re.I)
        if m:
            val = m.group(2).strip()
            # try parse the captured value via recursion
            p, c = parse_time_from_text(val)
            if p:
                return p, c
    return None, None

def parse_gcode_stats(gcode_path: str, superslicer_output: str = None):
    """
    Prefer times printed by SuperSlicer (superslicer_output), then G-code comments.
    Returns (pretty, cost) or ("Error","$Error").
    """
    # 1) prefer superslicer output (stdout/stderr)
    if superslicer_output:
        p, c = parse_time_from_text(superslicer_output)
        if p:
            return p, c

    # 2) parse top of gcode file comments
    if not os.path.exists(gcode_path):
        return "Error", "$Error"
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                # only examine comments and lines with time keywords
                if line.lstrip().startswith(";") or "estimated" in line.lower() or "time" in line.lower():
                    p, c = parse_time_from_text(line)
                    if p:
                        return p, c
    except Exception:
        return "Error", "$Error"
    return "Error", "$Error"

def ensure_profile_exists():
    if not os.path.exists(PROFILE_PATH):
        os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
        default = "[print]\nlayer_height = 0.2\nperimeters = 3\nfill_density = 20\n"
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write(default)
        except Exception:
            pass

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
        except Exception:
            flash("Failed to save upload"); return redirect(request.url)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")

        # ensure profile exists
        ensure_profile_exists()

        # validate numeric inputs
        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        # verify superslicer binary
        superslicer_resolved = SUPERSLICER_PATH and (shutil.which(SUPERSLICER_PATH) or (os.path.isfile(SUPERSLICER_PATH) and os.access(SUPERSLICER_PATH, os.X_OK)))
        if not superslicer_resolved:
            # try to find again at runtime
            from importlib import reload
            shutil = reload(shutil)
            alt = find_superslicer()
            if alt:
                superslicer_resolved = alt

        if not superslicer_resolved:
            # fallback estimate
            kb = os.path.getsize(upload_path) / 1024.0 if os.path.exists(upload_path) else 100.0
            factor = max(0.5, min(3.0, kb / 100.0))
            base_hours = 1.25 * factor
            infill_factor = 1 + (infill / 100.0) * 0.8
            wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
            hours = base_hours * infill_factor * wall_factor
            secs = int(hours * 3600)
            pretty = secs_to_pretty(secs)
            cost = f"${round(hours * COST_PER_HOUR, 2)}"
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        # build command variants (different releases use different args)
        perimeters = max(1, int(wall_thickness / 0.4))
        variants = [
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), upload_path, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, upload_path, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--output", output_gcode, upload_path],
        ]

        last_err = None
        combined_output = ""
        ok = False
        for cmd in variants:
            try:
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
                combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok = True
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")
            except subprocess.TimeoutExpired as e:
                last_err = "timeout"
                combined_output = (getattr(e, "stdout", "") or "") + "\n" + (getattr(e, "stderr", "") or "")

        # if run failed, still try to extract time from combined output
        if not ok:
            p, c = parse_time_from_text(combined_output)
            if p:
                return render_template("results.html", print_time=p, cost=c, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=False)
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        # prefer superslicer's printed estimate, otherwise parse gcode
        pretty, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        if pretty == "Error":
            # try direct parse of combined output
            p, c = parse_time_from_text(combined_output)
            if p:
                pretty, cost = p, c
                is_estimate = False
            else:
                # fallback estimate
                kb = os.path.getsize(upload_path) / 1024.0 if os.path.exists(upload_path) else 100.0
                factor = max(0.5, min(3.0, kb / 100.0))
                base_hours = 1.25 * factor
                infill_factor = 1 + (infill / 100.0) * 0.8
                wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
                hours = base_hours * infill_factor * wall_factor
                secs = int(hours * 3600)
                pretty = secs_to_pretty(secs)
                cost = f"${round(hours * COST_PER_HOUR, 2)}"
                is_estimate = True
        else:
            is_estimate = False

        return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=is_estimate, gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/health")
def health():
    return {
        "status": "ok",
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "superslicer_available": bool(SUPERSLICER_PATH and (shutil.which(SUPERSLICER_PATH) or (os.path.isfile(SUPERSLICER_PATH) and os.access(SUPERSLICER_PATH, os.X_OK)))),
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    print("Starting app; SuperSlicer path:", SUPERSLICER_PATH or "Not found")
    app.run(host="0.0.0.0", port=port, debug=debug)
