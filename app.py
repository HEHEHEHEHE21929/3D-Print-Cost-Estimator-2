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
os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", "3.0"))
SLICE_TIMEOUT = int(os.environ.get("SLICE_TIMEOUT", "600"))

def find_superslicer():
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            resolved = shutil.which(env)
            if resolved:
                return resolved
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("superslicer_console") or shutil.which("superslicer")

SUPERSLICER_PATH = find_superslicer()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_time_from_text(text: str):
    if not text:
        return None, None
    # prioritized regexes
    patterns = [
        r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s",
        r"(\d+)\s*m\s*(\d+)\s*s",
        r"(\d+):(\d+):(\d+)",
        r"(\d+):(\d+)",
        r"TIME[:=]\s*(\d+)",
        r"estimated.*time[:\s]*([0-9hms:\s]+)"
    ]
    for line in text.splitlines():
        ln = line.strip()
        for pat in patterns:
            m = re.search(pat, ln, re.IGNORECASE)
            if not m:
                continue
            groups = m.groups()
            try:
                if len(groups) == 3 and groups[0] and groups[1] and groups[2]:
                    secs = int(groups[0]) * 3600 + int(groups[1]) * 60 + int(groups[2])
                elif len(groups) == 2 and groups[0] and groups[1]:
                    a, b = int(groups[0]), int(groups[1])
                    secs = a*3600 + b*60 if a > 12 else a*60 + b
                elif len(groups) == 1 and groups[0]:
                    # TIME: seconds or captured value
                    if groups[0].isdigit():
                        secs = int(groups[0])
                    else:
                        continue
                else:
                    continue
            except Exception:
                continue
            pretty = _secs_to_pretty(secs)
            cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
            return pretty, cost
    return None, None

def create_default_profile():
    if not os.path.exists(PROFILE_PATH):
        try:
            os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write("[print]\nlayer_height = 0.2\nperimeters = 3\nfill_density = 20\n")
            log.info("Created default profile at %s", PROFILE_PATH)
        except Exception:
            log.exception("Failed to create default profile")

def parse_gcode_stats(gcode_path: str, superslicer_output: str = None):
    # prefer superslicer output first
    if superslicer_output:
        p, c = parse_time_from_text(superslicer_output)
        if p:
            return p, c
    # then parse gcode file comments
    if not os.path.exists(gcode_path):
        return "Error", "$Error"
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                if ";" in line or "estimated" in line.lower() or "time" in line.lower():
                    p, c = parse_time_from_text(line)
                    if p:
                        return p, c
    except Exception:
        log.exception("Failed to read gcode file")
        return "Error", "$Error"
    return "Error", "$Error"

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
        saved_path = os.path.join(UPLOAD_FOLDER, filename)
        try:
            file.save(saved_path)
        except Exception:
            flash("Failed to save uploaded file"); return redirect(request.url)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")
        create_default_profile()

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        superslicer = SUPERSLICER_PATH or find_superslicer()
        if not superslicer or not os.path.isfile(superslicer):
            # fallback estimate
            pretty, cost = (lambda p, w: (lambda secs, cost: (_secs_to_pretty(secs), cost))
                            (*((int((1.25 * max(0.5, min(3.0, (os.path.getsize(saved_path)/1024.0)/100.0))) *
                                (1 + (infill/100.0) * 0.8) * (1 + max(0.0, (wall_thickness-0.4)/0.4 * 0.3))) * 3600), f"${round((1.25 * max(0.5, min(3.0, (os.path.getsize(saved_path)/1024.0)/100.0)) * (1 + (infill/100.0) * 0.8) * (1 + max(0.0, (wall_thickness-0.4)/0.4 * 0.3))) * COST_PER_HOUR, 2)}")))(None, None)
            flash("SuperSlicer not found — showing local estimate", "warning")
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        variants = [
            [superslicer, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(max(1, int(wall_thickness/0.4))), saved_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, saved_path, "--export-gcode", "-o", output_gcode],
            [superslicer, "--load", PROFILE_PATH, "--output", output_gcode, saved_path],
        ]

        combined_output = ""
        last_err = None
        ok = False
        for cmd in variants:
            try:
                log.info("Running: %s", " ".join(cmd))
                proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=SLICE_TIMEOUT)
                combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                ok = True
                break
            except FileNotFoundError as e:
                last_err = str(e)
                combined_output = ""
                log.exception("Slicer binary not found: %s", e)
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
                combined_output = (e.stdout or "") + "\n" + (e.stderr or "")
                log.warning("Slicer failed: %s", last_err)
            except subprocess.TimeoutExpired as e:
                last_err = "timeout"
                combined_output = (getattr(e, "stdout", "") or "") + "\n" + (getattr(e, "stderr", "") or "")
                log.warning("Slicer timeout")

        # if failed, still try to extract time SuperSlicer printed
        p, c = parse_time_from_text(combined_output)
        if not ok and p:
            return render_template("results.html", print_time=p, cost=c, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=False)

        if not ok:
            flash(f"Slicing failed: {last_err}", "error")
            return redirect(request.url)

        pretty, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
        is_estimate = (pretty == "Error")
        if is_estimate:
            # final fallback estimate
            pretty, cost = estimate_time := (lambda p, w: (lambda secs, cost: (_secs_to_pretty(secs), cost))
                            (*((int((1.25 * max(0.5, min(3.0, (os.path.getsize(saved_path)/1024.0)/100.0))) *
                                (1 + (infill/100.0) * 0.8) * (1 + max(0.0, (wall_thickness-0.4)/0.4 * 0.3))) * 3600), f"${round((1.25 * max(0.5, min(3.0, (os.path.getsize(saved_path)/1024.0)/100.0)) * (1 + (infill/100.0) * 0.8) * (1 + max(0.0, (wall_thickness-0.4)/0.4 * 0.3))) * COST_PER_HOUR, 2)}")))(None, None)

        return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=is_estimate, gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/health")
def health():
    return {
        "status": "ok",
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "superslicer_available": bool(find_superslicer()),
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    log.info("Resolved SuperSlicer: %s", SUPERSLICER_PATH)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("DEBUG", "False").lower() == "true")
