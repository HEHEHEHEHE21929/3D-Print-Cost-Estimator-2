import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, flash

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
    # 1) explicit env var
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # if absolute path given, verify file + exec bit
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            # treat as name -> resolve on PATH
            resolved = shutil.which(env)
            if resolved:
                return resolved
    # 2) common extracted or moved locations (check both release folder and moved location)
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
    # not found
    return None

SUPERSLICER_PATH = find_superslicer()

def parse_gcode_stats(gcode_path):
    """
    Parse common SuperSlicer estimated time comment lines.
    Returns (pretty_time, cost) or ("Error", "$Error").
    """
    def pretty_from_secs(secs):
        h = secs // 3600
        m = (secs % 3600) // 60
        s = secs % 60
        if h:
            return f"{int(h)}h {int(m)}m {int(s)}s"
        return f"{int(m)}m {int(s)}s"

    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        return "Error", "$Error"

    # look for lines containing estimated/time and parse several formats
    for line in content.splitlines():
        lower = line.lower()
        if "estimated" in lower and "time" in lower:
            # try H M S
            m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", line, re.IGNORECASE)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            # try M S
            m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", line, re.IGNORECASE)
            if m:
                secs = int(m.group(1)) * 60 + int(m.group(2))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            # try H:MM:SS or MM:SS
            m = re.search(r"(\d+):(\d+):(\d+)", line)
            if m:
                secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            m = re.search(r"(\d+):(\d+)", line)
            if m:
                a, b = map(int, m.groups())
                # heuristic: if first > 12 treat as H:MM else M:SS
                if a > 12:
                    secs = a*3600 + b*60
                else:
                    secs = a*60 + b
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"

    # fallback: any time-like match in file
    m = re.search(r"(\d+)\s*h", content, re.IGNORECASE)
    if m:
        secs = int(m.group(1)) * 3600
        return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"

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

        # Ensure SuperSlicer binary is available
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
