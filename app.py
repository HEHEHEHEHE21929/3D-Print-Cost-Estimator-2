import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
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
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "./superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    return which

SUPERSLICER_PATH = find_superslicer()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def create_default_profile():
    if not os.path.exists(PROFILE_PATH):
        default = """[printer:Generic]
nozzle_diameter = 0.4

[print:default]
layer_height = 0.2
perimeters = 3
fill_density = 20
"""
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write(default)
        except Exception:
            pass

def parse_gcode_stats(gcode_path):
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            chunk = fh.read(8192)
    except Exception:
        return "Error", "$Error"

    patterns = [
        (r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", lambda g: int(g[0])*3600 + int(g[1])*60 + int(g[2])),
        (r"(\d+)\s*m\s*(\d+)\s*s", lambda g: int(g[0])*60 + int(g[1])),
        (r"(\d+):(\d+):(\d+)", lambda g: int(g[0])*3600 + int(g[1])*60 + int(g[2])),
        (r"(\d+):(\d+)", lambda g: int(g[0])*60 + int(g[1])),
        (r"TIME:\s*(\d+)", lambda g: int(g[0])),
    ]

    for line in chunk.splitlines():
        lower = line.lower()
        if ("estimated" in lower and "time" in lower) or lower.strip().startswith("; estimated") or "print time" in lower:
            for pat, fn in patterns:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    secs = fn(m.groups())
                    h = secs // 3600
                    m_ = (secs % 3600) // 60
                    s = secs % 60
                    pretty = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
                    cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                    return pretty, cost

    for pat, fn in patterns:
        m = re.search(pat, chunk, re.IGNORECASE)
        if m:
            secs = fn(m.groups())
            h = secs // 3600
            m_ = (secs % 3600) // 60
            s = secs % 60
            pretty = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
            cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
            return pretty, cost

    return "Error", "$Error"

def estimate_time(filepath, infill, wall_thickness):
    try:
        kb = os.path.getsize(filepath) / 1024.0
        factor = max(0.5, min(3.0, kb / 100.0))
    except Exception:
        factor = 1.0
    base_hours = 1.25 * factor
    infill_factor = 1 + (infill/100.0) * 0.8
    wall_factor = 1 + max(0.0, (wall_thickness - 0.4)/0.4 * 0.3)
    hours = base_hours * infill_factor * wall_factor
    secs = int(hours * 3600)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    pretty = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    cost = f"${round(hours * COST_PER_HOUR, 2)}"
    return pretty, cost

def run_slicer(cmd, timeout=SLICE_TIMEOUT):
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        return True, proc
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))
    except subprocess.TimeoutExpired as e:
        return False, "timeout"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part", "error"); return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "error"); return redirect(request.url)
        if not allowed_file(file.filename):
            flash("Invalid file type", "error"); return redirect(request.url)

        filename = secure_filename(file.filename)
        upload_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(upload_path)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        is_estimate = False
        print_time = None
        cost = None

        if not SUPERSLICER_PATH:
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            print_time, cost = estimate_time(upload_path, infill, wall_thickness)
            is_estimate = True
        else:
            create_default_profile()
            perimeters = max(1, int(wall_thickness / 0.4))
            variants = [
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), upload_path, "--export-gcode", "-o", output_gcode],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, upload_path, "--export-gcode", "-o", output_gcode],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--output", output_gcode, upload_path],
            ]
            ok = False
            last_err = None
            for cmd in variants:
                ok, res = run_slicer(cmd)
                if ok:
                    break
                last_err = res

            if not ok:
                flash("Slicing failed — showing estimate", "warning")
                print("Slicer error:", last_err)
                print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                is_estimate = True
            else:
                if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 100:
                    pt, pcost = parse_gcode_stats(output_gcode)
                    if pt != "Error":
                        print_time, cost = pt, pcost
                        is_estimate = False
                    else:
                        print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                        is_estimate = True
                        flash("G-code produced but time extraction failed — showing estimate", "warning")
                else:
                    print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                    is_estimate = True
                    flash("G-code not produced — showing estimate", "warning")

        if request.form.get("order_attempt"):
            customer_name = request.form.get("customer_name", "").strip()
            customer_email = request.form.get("customer_email", "").strip()
            if not customer_name or not customer_email:
                flash("Name and email required to place order", "error")
            else:
                order = {
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "file": filename,
                    "print_time": print_time,
                    "cost": cost,
                    "is_estimate": is_estimate,
                    "gcode_path": output_gcode if not is_estimate else None
                }
                try:
                    # optional queue_utils
                    try:
                        from queue_utils import add_to_queue
                    except Exception:
                        def add_to_queue(o): print("Order queued (fallback):", o)
                    add_to_queue(order)
                    flash("Order submitted", "success")
                    return render_template("order_success.html", order=order)
                except Exception as e:
                    flash(f"Failed to submit order: {e}", "error")

        return render_template("results.html",
                               print_time=print_time,
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
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)),
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", "5000"))
    print("Starting app; SuperSlicer:", SUPERSLICER_PATH or "Not found")
    create_default_profile()
    app.run(host="0.0.0.0", port=port, debug=debug)
```# filepath: c:\Users\Zeti\Desktop\bambu_lab_test\app.py
import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
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
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "./superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    return which

SUPERSLICER_PATH = find_superslicer()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def create_default_profile():
    if not os.path.exists(PROFILE_PATH):
        default = """[printer:Generic]
nozzle_diameter = 0.4

[print:default]
layer_height = 0.2
perimeters = 3
fill_density = 20
"""
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write(default)
        except Exception:
            pass

def parse_gcode_stats(gcode_path):
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            chunk = fh.read(8192)
    except Exception:
        return "Error", "$Error"

    patterns = [
        (r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", lambda g: int(g[0])*3600 + int(g[1])*60 + int(g[2])),
        (r"(\d+)\s*m\s*(\d+)\s*s", lambda g: int(g[0])*60 + int(g[1])),
        (r"(\d+):(\d+):(\d+)", lambda g: int(g[0])*3600 + int(g[1])*60 + int(g[2])),
        (r"(\d+):(\d+)", lambda g: int(g[0])*60 + int(g[1])),
        (r"TIME:\s*(\d+)", lambda g: int(g[0])),
    ]

    for line in chunk.splitlines():
        lower = line.lower()
        if ("estimated" in lower and "time" in lower) or lower.strip().startswith("; estimated") or "print time" in lower:
            for pat, fn in patterns:
                m = re.search(pat, line, re.IGNORECASE)
                if m:
                    secs = fn(m.groups())
                    h = secs // 3600
                    m_ = (secs % 3600) // 60
                    s = secs % 60
                    pretty = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
                    cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                    return pretty, cost

    for pat, fn in patterns:
        m = re.search(pat, chunk, re.IGNORECASE)
        if m:
            secs = fn(m.groups())
            h = secs // 3600
            m_ = (secs % 3600) // 60
            s = secs % 60
            pretty = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
            cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
            return pretty, cost

    return "Error", "$Error"

def estimate_time(filepath, infill, wall_thickness):
    try:
        kb = os.path.getsize(filepath) / 1024.0
        factor = max(0.5, min(3.0, kb / 100.0))
    except Exception:
        factor = 1.0
    base_hours = 1.25 * factor
    infill_factor = 1 + (infill/100.0) * 0.8
    wall_factor = 1 + max(0.0, (wall_thickness - 0.4)/0.4 * 0.3)
    hours = base_hours * infill_factor * wall_factor
    secs = int(hours * 3600)
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    pretty = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    cost = f"${round(hours * COST_PER_HOUR, 2)}"
    return pretty, cost

def run_slicer(cmd, timeout=SLICE_TIMEOUT):
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        return True, proc
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))
    except subprocess.TimeoutExpired as e:
        return False, "timeout"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part", "error"); return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "error"); return redirect(request.url)
        if not allowed_file(file.filename):
            flash("Invalid file type", "error"); return redirect(request.url)

        filename = secure_filename(file.filename)
        upload_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(upload_path)

        output_gcode = os.path.join(OUTPUT_FOLDER, filename.rsplit(".", 1)[0] + ".gcode")

        try:
            infill = int(request.form.get("infill", 20))
        except Exception:
            infill = 20
        try:
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
        except Exception:
            wall_thickness = 0.8

        is_estimate = False
        print_time = None
        cost = None

        if not SUPERSLICER_PATH:
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            print_time, cost = estimate_time(upload_path, infill, wall_thickness)
            is_estimate = True
        else:
            create_default_profile()
            perimeters = max(1, int(wall_thickness / 0.4))
            variants = [
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), upload_path, "--export-gcode", "-o", output_gcode],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, upload_path, "--export-gcode", "-o", output_gcode],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--output", output_gcode, upload_path],
            ]
            ok = False
            last_err = None
            for cmd in variants:
                ok, res = run_slicer(cmd)
                if ok:
                    break
                last_err = res

            if not ok:
                flash("Slicing failed — showing estimate", "warning")
                print("Slicer error:", last_err)
                print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                is_estimate = True
            else:
                if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 100:
                    pt, pcost = parse_gcode_stats(output_gcode)
                    if pt != "Error":
                        print_time, cost = pt, pcost
                        is_estimate = False
                    else:
                        print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                        is_estimate = True
                        flash("G-code produced but time extraction failed — showing estimate", "warning")
                else:
                    print_time, cost = estimate_time(upload_path, infill, wall_thickness)
                    is_estimate = True
                    flash("G-code not produced — showing estimate", "warning")

        if request.form.get("order_attempt"):
            customer_name = request.form.get("customer_name", "").strip()
            customer_email = request.form.get("customer_email", "").strip()
            if not customer_name or not customer_email:
                flash("Name and email required to place order", "error")
            else:
                order = {
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "file": filename,
                    "print_time": print_time,
                    "cost": cost,
                    "is_estimate": is_estimate,
                    "gcode_path": output_gcode if not is_estimate else None
                }
                try:
                    # optional queue_utils
                    try:
                        from queue_utils import add_to_queue
                    except Exception:
                        def add_to_queue(o): print("Order queued (fallback):", o)
                    add_to_queue(order)
                    flash("Order submitted", "success")
                    return render_template("order_success.html", order=order)
                except Exception as e:
                    flash(f"Failed to submit order: {e}", "error")

        return render_template("results.html",
                               print_time=print_time,
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
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)),
        "profile_exists": os.path.exists(PROFILE_PATH),
        "cost_per_hour": COST_PER_HOUR
    }

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", "5000"))
    print("Starting app; SuperSlicer:", SUPERSLICER_PATH or "Not found")
    create_default_profile()
    app.run(host="0.0.0.0", port=port, debug=debug)
