# ...existing code...
import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash

try:
    from queue_utils import add_to_queue, get_queue
except Exception:
    def add_to_queue(order_data):
        print("Order added (fallback):", order_data)
        return True
    def get_queue():
        return []

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs(os.path.dirname(PROFILE_PATH) or ".", exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    # 1) explicit env var
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            resolved = shutil.which(env)
            if resolved:
                return resolved

    # 2) reasonable deployment / dev locations
    candidates = [
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

    # 3) on PATH
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    if which:
        return which

    return None

# prefer environment or installed location
SUPERSLICER_PATH = os.environ.get("SUPERSLICER_PATH", None) or find_superslicer()

def create_default_profile():
    if not os.path.exists(PROFILE_PATH):
        default = """[print]
layer_height = 0.2
perimeters = 3
fill_density = 20
"""
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write(default)
            print("Created default profile:", PROFILE_PATH)
        except Exception as e:
            print("Failed to create profile:", e)

def extract_gcode_time(gcode_path):
    if not os.path.exists(gcode_path):
        return None, None
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 400:  # metadata usually near top
                    break
                l = line.strip().lower()
                if "estimated" in l and "time" in l or l.startswith("; estimated") or l.startswith("; time:"):
                    # handle H M S, M S, H:MM:SS, MM:SS, seconds
                    m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", l)
                    if m:
                        h, mm, s = map(int, m.groups()); secs = h*3600+mm*60+s
                        return _format_time(secs), secs
                    m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", l)
                    if m:
                        mm, s = map(int, m.groups()); secs = mm*60+s
                        return _format_time(secs), secs
                    m = re.search(r"(\d+):(\d+):(\d+)", l)
                    if m:
                        h, mm, s = map(int, m.groups()); secs = h*3600+mm*60+s
                        return _format_time(secs), secs
                    m = re.search(r"(\d+):(\d+)", l)
                    if m:
                        a, b = map(int, m.groups())
                        if a > 12:
                            secs = a*3600 + b*60
                        else:
                            secs = a*60 + b
                        return _format_time(secs), secs
                    m = re.search(r"(\d+)", l)
                    if m and ("time:" in l or "time =" in l):
                        secs = int(m.group(1))
                        # if value large assume seconds
                        return _format_time(secs), secs
    except Exception as e:
        print("Error reading gcode:", e)
    return None, None

def _format_time(secs):
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{int(h)}h {int(m)}m {int(s)}s"
    return f"{int(m)}m {int(s)}s"

def estimate_time(infill, wall_thickness, filename):
    try:
        size_kb = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename)) / 1024.0
        factor = max(0.5, min(3.0, size_kb / 100.0))
    except Exception:
        factor = 1.0
    base = 1.25 * factor
    infill_factor = 1 + (infill / 100.0) * 0.8
    wall_factor = 1 + max(0.0, (wall_thickness - 0.4) / 0.4 * 0.3)
    hours = base * infill_factor * wall_factor
    secs = int(hours * 3600)
    return _format_time(secs), f"${round(hours * COST_PER_HOUR, 2)}"

def run_superslicer(cmds, timeout=600):
    """Try list of cmd variations until one works. cmds is list of lists."""
    last_err = None
    for cmd in cmds:
        try:
            print("Running:", " ".join(cmd))
            r = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
            return True, r
        except subprocess.CalledProcessError as e:
            last_err = f"CalledProcessError: {e.stderr or e.stdout or str(e)}"
            print(last_err)
        except subprocess.TimeoutExpired as e:
            last_err = "TimeoutExpired"
            print(last_err)
    return False, last_err

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file selected", "error"); return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected", "error"); return redirect(request.url)
        if not allowed_file(file.filename):
            flash("Invalid file type", "error"); return redirect(request.url)

        # parse inputs
        try:
            infill = int(request.form.get("infill", 20))
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
            customer_name = request.form.get("customer_name", "").strip()
            customer_email = request.form.get("customer_email", "").strip()
        except Exception:
            flash("Invalid inputs", "error"); return redirect(request.url)

        filename = file.filename
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        if not SUPERSLICER_PATH:
            flash("SuperSlicer not found — running in estimate/demo mode", "warning")
            time_str, cost = estimate_time(infill, wall_thickness, filename)
            is_estimate = True
        else:
            create_default_profile()
            perimeters = max(1, int(wall_thickness / 0.4))
            # primary cmd (common form)
            cmds = [
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), filepath, "--export-gcode", "-o", output_gcode],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill}%", "--perimeters", str(perimeters), "--output", output_gcode, filepath],
                [SUPERSLICER_PATH, "--load", PROFILE_PATH, filepath, "--export-gcode", "-o", output_gcode]
            ]
            ok, res = run_superslicer(cmds, timeout=int(os.environ.get("SLICE_TIMEOUT", 600)))
            if not ok:
                flash("Slicing failed — showing estimate", "warning")
                print("Slicer errors:", res)
                time_str, cost = estimate_time(infill, wall_thickness, filename)
                is_estimate = True
            else:
                # verify gcode
                if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 100:
                    tstr, secs = extract_gcode_time(output_gcode)
                    if tstr and secs:
                        time_str = tstr
                        cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                        is_estimate = False
                    else:
                        time_str, cost = estimate_time(infill, wall_thickness, filename)
                        is_estimate = True
                else:
                    flash("G-code not produced — showing estimate", "warning")
                    time_str, cost = estimate_time(infill, wall_thickness, filename)
                    is_estimate = True

        # order handling
        if request.form.get("order_attempt"):
            if not customer_name or not customer_email:
                flash("Name and email required to place order", "error")
            else:
                order = {
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "file": filename,
                    "infill": infill,
                    "wall_thickness": wall_thickness,
                    "time": time_str,
                    "cost": cost,
                    "is_estimate": is_estimate,
                    "gcode_path": output_gcode if not is_estimate else None
                }
                add_to_queue(order)
                flash("Order submitted", "success")
                return render_template("order_success.html", order=order)

        return render_template("results.html",
                               print_time=time_str,
                               cost=cost,
                               filename=filename,
                               infill=infill,
                               wall_thickness=wall_thickness,
                               is_estimate=is_estimate,
                               gcode_path=(output_gcode if not is_estimate else None))

    return render_template("index.html")

@app.route("/queue")
def view_queue():
    try:
        queue = get_queue()
        return render_template("queue.html", queue=queue)
    except Exception as e:
        flash(f"Error loading queue: {e}", "error")
        return redirect(url_for("index"))

@app.route("/admin")
def admin():
    try:
        orders = get_queue()
        return render_template("admin.html", orders=orders)
    except Exception as e:
        flash(f"Error loading admin: {e}", "error")
        return redirect(url_for("index"))

@app.route("/contact", methods=["POST"])
def contact():
    flash("Message received", "success")
    return redirect(url_for("index"))

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
    port = int(os.environ.get("PORT", 5000))
    print("Starting app — SuperSlicer:", SUPERSLICER_PATH or "Not found")
    create_default_profile()
    app.run(host="0.0.0.0", port=port, debug=debug)
# ...existing code...
