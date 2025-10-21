import os
import re
import subprocess
from flask import Flask, render_template, request, redirect, flash
from queue_utils import add_to_queue, get_queue

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
SUPERSLICER_PATH = os.environ.get("SUPERSLICER_PATH", "/opt/render/project/src/superslicer_console")
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = 3.0  # $3 per hour

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_gcode_stats(gcode_path):
    """
    Robust parsing of SuperSlicer gcode comment lines that contain an
    estimated print time. Returns (pretty_string, cost_string) or ("Error","$Error").
    """
    def seconds_to_pretty(sec):
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        parts = []
        if h:
            parts.append(f"{int(h)}h")
        if m:
            parts.append(f"{int(m)}m")
        if s:
            parts.append(f"{int(s)}s")
        return " ".join(parts) or "0s"

    patterns = [
        (r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", lambda m: int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))),
        (r"(\d+)\s*h\s*(\d+)\s*m", lambda m: int(m.group(1))*3600 + int(m.group(2))*60),
        (r"(\d+)\s*m\s*(\d+)\s*s", lambda m: int(m.group(1))*60 + int(m.group(2))),
        (r"(\d+):(\d+):(\d+)", lambda m: int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))),
        (r"(\d+):(\d+)", lambda m: int(m.group(1))*60 + int(m.group(2))),
        (r"([0-9]+(?:\.[0-9]+)?)\s*hours?", lambda m: int(float(m.group(1)) * 3600)),
        (r"([0-9]+(?:\.[0-9]+)?)\s*h\b", lambda m: int(float(m.group(1)) * 3600)),
        (r"(\d+)\s*sec", lambda m: int(m.group(1))),
        (r"(\d+)\s*s\b", lambda m: int(m.group(1))),
    ]

    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                lower = line.lower()
                if ("estimated" in lower and "time" in lower) or "estimated printing time" in lower or lower.strip().startswith("; estimated"):
                    for pat, fn in patterns:
                        m = re.search(pat, line, re.IGNORECASE)
                        if m:
                            secs = fn(m)
                            pretty = seconds_to_pretty(secs)
                            cost = round(secs / 3600.0 * COST_PER_HOUR, 2)
                            return pretty, f"${cost}"
                    nums = re.findall(r"(\d+)", line)
                    if len(nums) == 3:
                        secs = int(nums[0]) * 3600 + int(nums[1]) * 60 + int(nums[2])
                        pretty = seconds_to_pretty(secs)
                        cost = round(secs / 3600.0 * COST_PER_HOUR, 2)
                        return pretty, f"${cost}"
                    if len(nums) == 2:
                        a, b = map(int, nums)
                        secs = a * 3600 + b * 60
                        pretty = seconds_to_pretty(secs)
                        cost = round(secs / 3600.0 * COST_PER_HOUR, 2)
                        return pretty, f"${cost}"
        # fallback: scan whole file for any time-like match
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            for pat, fn in patterns:
                m = re.search(pat, content, re.IGNORECASE)
                if m:
                    secs = fn(m)
                    pretty = seconds_to_pretty(secs)
                    cost = round(secs / 3600.0 * COST_PER_HOUR, 2)
                    return pretty, f"${cost}"
    except FileNotFoundError:
        return "Error", "$Error"
    except Exception:
        return "Error", "$Error"

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

        # Validate inputs
        if not (0 <= infill <= 99):
            flash("Infill must be between 0 and 99%")
            return redirect(request.url)

        if not (0.1 <= wall_thickness <= 10.0):
            flash("Wall thickness must be between 0.1 and 10.0 mm")
            return redirect(request.url)

        # Save uploaded file
        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        # Generate output path
        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        # Check if SuperSlicer exists
        if not os.path.exists(SUPERSLICER_PATH):
            flash(f"SuperSlicer not found at: {SUPERSLICER_PATH}")
            return redirect(request.url)

        # Build command with parameters
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

            # If user attempted to place an order include hidden input "order_attempt" in form
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
