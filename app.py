# ...existing code...
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
    # 1) explicit env var
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        if shutil.which(env) or (os.path.isfile(env) and os.access(env, os.X_OK)):
            return env
    # 2) common extracted locations (check both exact release folder and moved path)
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
                lower = line.lower()
                if "estimated" in lower and "time" in lower:
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
        # Validate file upload
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

        # Get form data
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

        # Check SuperSlicer binary
        if not SUPERSLICER_PATH or not os.path.isfile(SUPERSLICER_PATH):
            candidates_checked = "\n".join([
                f"ENV SUPERSLICER_PATH={os.environ.get('SUPERSLICER_PATH')}",
                "/opt/render/project/src/superslicer_console",
                "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
                "/usr/local/bin/superslicer_console",
                "on PATH (superslicer_console or superslicer)"
            ])
            flash(f"SuperSlicer not found. Checked SUPERSLICER_PATH env and common locations. Set SUPERSLICER_PATH to the binary path. Candidates checked:\n{candidates_checked}")
            return redirect(request.url)

        cmd = [
            SUPERSLICER_PATH,
            "--load", PROFILE_PATH,
            "--fill-density", str(infill) + "%",
            "--perimeters", str(max(1, int(wall_thickness / 0.4))),
            filepath,
            "--export-gcode",
            "-o", output_gcode
        ]

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
            print_time, cost = parse_gcode_stats(output_gcode)

            order_data = {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "file": filename,
                "infill": infill,
                "wall_thickness": wall_thickness,
                "time": print_time,
                "cost": cost,
                "gcode_path": output_gcode
            }

            if request.form.get("order_attempt"):
                if not customer_name or not customer_email:
                    flash("Please enter your name and email to place an order.")
                    return render_template("results.html",
                                           print_time=print_time,
                                           cost=cost,
                                           filename=filename,
                                           infill=infill,
                                           wall_thickness=wall_thickness)
                add_to_queue(order_data)
                flash("Order submitted successfully!")
                return render_template("order_success.html", order=order_data)

            return render_template("results.html",
                                   print_time=print_time,
                                   cost=cost,
                                   filename=filename,
                                   infill=infill,
                                   wall_thickness=wall_thickness)
        except subprocess.TimeoutExpired:
            flash("Slicing timed out. The file may be too complex.")
            return redirect(request.url)
        except subprocess.CalledProcessError as e:
            error_msg = (e.stderr or str(e)).strip()
            flash(f"Slicing failed: {error_msg}")
            return redirect(request.url)
        except Exception as e:
            flash(f"An error occurred: {str(e)}")
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
# ...existing code...
