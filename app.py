import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
from queue_utils import add_to_queue, get_queue

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = 3.0  # $3 per hour

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    # 1) explicit env var (if set to name, try which; if path, verify)
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            which_env = shutil.which(env)
            if which_env:
                return which_env
    # 2) common extracted locations
    candidates = [
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 3) on PATH
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    if which:
        return which
    return None

SUPERSLICER_PATH = find_superslicer()

def parse_gcode_stats(gcode_path):
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                lower = line.lower()
                if "estimated" in lower and "time" in lower:
                    m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", line)
                    if m:
                        h, mm, s = map(int, m.groups())
                        secs = h*3600 + mm*60 + s
                    else:
                        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", line)
                        if m:
                            mm, s = map(int, m.groups())
                            secs = mm*60 + s
                        else:
                            # fallback: any H:M:S or M:S anywhere
                            m = re.search(r"(\d+):(\d+):(\d+)", line)
                            if m:
                                h, mm, s = map(int, m.groups())
                                secs = h*3600 + mm*60 + s
                            else:
                                m = re.search(r"(\d+):(\d+)", line)
                                if m:
                                    a, b = map(int, m.groups())
                                    # prefer H:M if a>12 else M:S
                                    if a > 12:
                                        secs = a*3600 + b*60
                                    else:
                                        secs = a*60 + b
                                else:
                                    continue
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    s = secs % 60
                    pretty = f"{int(h)}h {int(m)}m {int(s)}s" if h else f"{int(m)}m {int(s)}s"
                    cost = round((secs/3600.0) * COST_PER_HOUR, 2)
                    return pretty, f"${cost}"
    except FileNotFoundError:
        pass
    return "Error", "$Error"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file selected")
            return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected")
            return redirect(request.url)
        if not file or not allowed_file(file.filename):
            flash("Invalid file type. Please upload STL, 3MF, or OBJ files only.")
            return redirect(request.url)

        try:
            infill = int(request.form.get("infill", 20))
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
            customer_name = request.form.get("customer_name", "")
            customer_email = request.form.get("customer_email", "")
        except (ValueError, TypeError):
            flash("Invalid form data. Please check your inputs.")
            return redirect(request.url)

        if not (0 <= infill <= 99):
            flash("Infill must be between 0 and 99%")
            return redirect(request.url)
        if not (0.1 <= wall_thickness <= 10.0):
            flash("Wall thickness must be between 0.1 and 10.0 mm")
            return redirect(request.url)

        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        if not SUPERSLICER_PATH or not os.path.isfile(SUPERSLICER_PATH):
            checked = [
                f"ENV SUPERSLICER_PATH={os.environ.get('SUPERSLICER_PATH')}",
                "/opt/render/project/src/superslicer_console",
                "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
                "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
                "/usr/local/bin/superslicer_console",
                "on PATH (superslicer_console or superslicer)"
            ]
            flash("SuperSlicer not found. Checked locations:\n" + "\n".join(checked))
            return redirect(request.url)

        cmd = [
            SUPERSLICER_PATH,
            "--load", PROFILE_PATH,
            "--fill-density", f"{infill}%",
            "--perimeters", str(max(1, int(wall_thickness / 0.4))),
            filepath,
            "--export-gcode",
            "-o", output_gcode
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
            print_time, cost = parse_gcode_stats(output_gcode)
            return render_template("results.html", print_time=print_time, cost=cost, filename=filename)
        except subprocess.TimeoutExpired:
            flash("Slicing timed out.")
            return redirect(request.url)
        except subprocess.CalledProcessError as e:
            flash(f"Slicing failed: {(e.stderr or str(e)).strip()}")
            return redirect(request.url)
        except Exception as e:
            flash(f"An error occurred: {e}")
            return redirect(request.url)

    return render_template("index.html")

@app.route("/queue")
def view_queue():
    queue = get_queue()
    return render_template("queue.html", queue=queue)

@app.route("/admin")
def admin():
    orders = get_queue()
    return render_template("admin.html", orders=orders)

@app.route("/contact", methods=["POST"])
def contact():
    return "Order received!"
