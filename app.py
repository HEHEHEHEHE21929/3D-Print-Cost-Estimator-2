# ...existing code...
import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs(os.path.dirname(PROFILE_PATH) or ".", exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", 3.0))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", 600))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    """Resolve SuperSlicer executable from env, common locations, or PATH."""
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # If env is an absolute path, verify it; if it's a name, resolve on PATH
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            w = shutil.which(env)
            if w:
                return w

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
            return p

    return shutil.which("superslicer_console") or shutil.which("superslicer")

SUPERSLICER_PATH = find_superslicer()

def _secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    """Extract seconds from SuperSlicer stdout/stderr or gcode comment text."""
    if not text:
        return None, None
    for line in text.splitlines():
        ln = line.strip().lower()
        # H M S
        m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # M S
        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", ln)
        if m:
            secs = int(m.group(1))*60 + int(m.group(2))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # H:MM:SS or MM:SS
        m = re.search(r"(\d+):(\d+):(\d+)", ln)
        if m:
            secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        m = re.search(r"(\d+):(\d+)", ln)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            secs = a*3600 + b*60 if a > 12 else a*60 + b
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
        # TIME: seconds
        m = re.search(r"time[:=]\s*(\d+)", ln)
        if m:
            secs = int(m.group(1))
            return _secs_to_pretty(secs), f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
    return None, None

def parse_gcode_stats(gcode_path, superslicer_output=None):
    """Try to extract time from superslicer output first, then G-code comments."""
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
                if ";" in line or "estimated" in line.lower() or "print time" in line.lower():
                    p, c = parse_time_from_text(line)
                    if p:
                        return p, c
    except Exception:
        return "Error", "$Error"

    return "Error", "$Error"
# ...existing code...

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
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        try:
            file.save(filepath)
        except Exception as e:
            flash("Failed to save upload"); return redirect(request.url)

        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        # Confirm superslicer availability
        superslicer_ok = bool(SUPERSLICER_PATH and (shutil.which(SUPERSLICER_PATH) or (os.path.isfile(SUPERSLICER_PATH) and os.access(SUPERSLICER_PATH, os.X_OK))))
        if not superslicer_ok:
            flash("SuperSlicer not found — running in demo/estimate mode", "warning")
            # lightweight estimate fallback
            try:
                kb = os.path.getsize(filepath) / 1024.0
                factor = max(0.5, min(3.0, kb / 100.0))
            except Exception:
                factor = 1.0
            base_hours = 1.25 * factor
            infill = int(request.form.get("infill", 20))
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
            infill_factor = 1 + (infill / 100.0) * 0.8
            wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
            hours = base_hours * infill_factor * wall_factor
            secs = int(hours * 3600)
            pretty = _secs_to_pretty(secs)
            cost = f"${round(hours * COST_PER_HOUR, 2)}"
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        # ensure a minimal profile exists
        if not os.path.exists(PROFILE_PATH):
            os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
            with open(PROFILE_PATH, "w", encoding="utf-8") as pf:
                pf.write("[print]\nlayer_height=0.2\nperimeters=3\nfill_density=20\n")

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        perimeters = max(1, int(wall_thickness / 0.4))

        variants = [
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), filepath, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, filepath, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--output", output_gcode, filepath],
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
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")

        if not ok:
            # try to extract time from whatever SuperSlicer printed even on failure
            p, c = parse_time_from_text(combined_output)
            if p:
                return render_template("results.html", print_time=p, cost=c, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=False)
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        print_time, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        is_estimate = (print_time == "Error")
        if is_estimate:
            # fallback: try direct parse of combined output
            p, c = parse_time_from_text(combined_output)
            if p:
                print_time, cost = p, c
                is_estimate = False
            else:
                # fallback estimate
                try:
                    kb = os.path.getsize(filepath) / 1024.0
                    factor = max(0.5, min(3.0, kb / 100.0))
                except Exception:
                    factor = 1.0
                base_hours = 1.25 * factor
                infill_factor = 1 + (infill / 100.0) * 0.8
                wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
                hours = base_hours * infill_factor * wall_factor
                secs = int(hours * 3600)
                print_time = _secs_to_pretty(secs)
                cost = f"${round(hours * COST_PER_HOUR, 2)}"
                is_estimate = True

        return render_template("results.html", print_time=print_time, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=is_estimate, gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    print("Starting app; SuperSlicer path:", SUPERSLICER_PATH or "Not found")
    app.run(host="0.0.0.0", port=port, debug=debug)
# ...existing code...
