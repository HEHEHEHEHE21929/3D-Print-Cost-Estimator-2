import os
import subprocess
from flask import Flask, render_template, request, redirect, url_for
from queue_utils import add_to_queue

app = Flask(__name__)

# Paths
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
OUTPUT_FOLDER = os.path.join(os.getcwd(), "output")
PROFILE_PATH = os.path.join(os.getcwd(), "profiles", "my_config.ini")
SUPERSLICER_PATH = r"C:\Users\Zeti\Downloads\SuperSlicer_2.7.61.1_win64_250407\SuperSlicer_2.7.61.1_win64_250407\superslicer_console.exe"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_gcode_stats(gcode_path):
    """Extract estimated print time, filament used, and cost from G-code"""
    print_time = "Unknown"
    filament_used = "Unknown"
    cost = "Error"

    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if "estimated printing time" in line.lower():
                    print_time = line.split("=")[-1].strip()
                elif "filament used" in line.lower():
                    filament_used = line.split("=")[-1].strip()

        # Convert print_time to minutes and calculate cost = 3$/hour
        if print_time != "Unknown":
            total_minutes = 0
            # Example formats: '1h 20m', '20m', '1h'
            h = 0
            m = 0
            import re
            match_h = re.search(r"(\d+)h", print_time)
            match_m = re.search(r"(\d+)m", print_time)
            if match_h:
                h = int(match_h.group(1))
            if match_m:
                m = int(match_m.group(1))
            total_minutes = h * 60 + m
            cost = f"${(total_minutes/60)*3:.2f}"
    except Exception as e:
        print(f"G-code parsing error: {e}")

    return print_time, filament_used, cost

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        return redirect(url_for("index"))

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    # Get infill and wall thickness from form
    infill = request.form.get("infill", "20")
    wall_thickness = request.form.get("wall_thickness", "0.8")

    gcode_filename = os.path.splitext(file.filename)[0] + ".gcode"
    gcode_path = os.path.join(OUTPUT_FOLDER, gcode_filename)

    # Run SuperSlicer with custom infill and wall thickness
    try:
        if not os.path.isfile(SUPERSLICER_PATH):
            raise FileNotFoundError(f"SuperSlicer executable not found at: {SUPERSLICER_PATH}")
        slicer_args = [
            SUPERSLICER_PATH,
            "--load", PROFILE_PATH,
            filepath,
            "--export-gcode",
            "-o", gcode_path,
            "--fill-density", str(infill),
            "--extrusion-width", str(wall_thickness)
        ]
        subprocess.run(
            slicer_args,
            check=True
        )
    except FileNotFoundError as e:
        print(f"Slicing failed: {e}")
        return render_template("results.html", time="Error: Slicer not found", filament="Error", cost="Error")
    except subprocess.CalledProcessError as e:
        print(f"Slicing failed: {e}")
        return render_template("results.html", time="Error", filament="Error", cost="Error")

    print_time, filament_used, cost = parse_gcode_stats(gcode_path)
    # Pass all info needed for order form
    return render_template(
        "results.html",
        time=print_time,
        cost=cost,
        file=file.filename,
        infill=infill,
        wall_thickness=wall_thickness,
        gcode_path=gcode_path
    )
@app.route("/order", methods=["POST"])
def order():
    # Collect order info
    order = {
        "file": request.form.get("file"),
        "infill": request.form.get("infill"),
        "wall_thickness": request.form.get("wall_thickness"),
        "gcode_path": request.form.get("gcode_path"),
        "time": request.form.get("time"),
        "cost": request.form.get("cost"),
        "customer_name": request.form.get("customer_name"),
        "customer_email": request.form.get("customer_email")
    }
    add_to_queue(order)
    # Email sending removed; Shopify will notify the business owner via its own system
    return render_template("order_success.html", order=order)

if __name__ == "__main__":
    # Run on all interfaces for server access
    app.run(host="0.0.0.0", port=5000, debug=True)












