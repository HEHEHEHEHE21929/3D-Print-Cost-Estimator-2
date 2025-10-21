import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash

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
    # 1) explicit env var (if path provided, verify; if name provided, resolve via PATH)
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
    print_time = None
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                low = line.lower()
                if "estimated" in low and "time" in low:
                    match = re.search(r"(\d+)h\s*(\d+)m\s*(\d+)s", line)
                    if match:
                        h, m, s = map(int, match.groups())
                        print_time = f"{h}h {m}m {s}s"
                        hours = h + m / 60 + s / 3600
                        cost = round(hours * COST_PER_HOUR, 2)
                        return print_time, f"${cost}"
                    match = re.search(r"(\d+)m\s*(\d+)s", line)
                    if match:
                        m, s = map(int, match.groups())
                        print_time = f"{m}m {s}s"
                        hours = m / 60 + s / 3600
                        cost = round(hours * COST_PER_HOUR, 2)
                        return print_time, f"${cost}"
    except FileNotFoundError:
        pass
    return "Error", "$Error"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file")
            return redirect(request.url)

        if not file or not allowed_file(file.filename):
            flash("Invalid file type")
            return redirect(request.url)

        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        # Ensure binary available
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
