import os
import re
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
from queue_utils import add_to_queue

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
SUPERSLICER_PATH = "/opt/render/project/src/superslicer_console"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = 3.0  # $3 per hour

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_gcode_stats(gcode_path):
    print_time = None
    with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "estimated printing time" in line.lower():
                # Example: "; estimated printing time (normal mode) : 3h 54m 52s"
                match = re.search(r"(\d+)h\s*(\d+)m\s*(\d+)s", line)
                if match:
                    h, m, s = map(int, match.groups())
                    print_time = f"{h}h {m}m {s}s"
                    hours = h + m / 60 + s / 3600
                    cost = round(hours * COST_PER_HOUR, 2)
                    return print_time, f"${cost}"
                # If only minutes and seconds
                match = re.search(r"(\d+)m\s*(\d+)s", line)
                if match:
                    m, s = map(int, match.groups())
                    print_time = f"{m}m {s}s"
                    hours = m / 60 + s / 3600
                    cost = round(hours * COST_PER_HOUR, 2)
                    return print_time, f"${cost}"
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
            "--perimeters", str(max(1, int(wall_thickness / 0.4))),  # Convert thickness to perimeter count
            filepath,
            "--export-gcode",
            "-o", output_gcode
        ]
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            print_time, cost = parse_gcode_stats(output_gcode)
            
            # Create order data
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
            
            # Add to queue if customer info provided
            if request.form.get("order_attempt"):
                if not customer_name or not customer_email:
                    flash("Please enter your name and email to place an order.")
                    return render_template("results.html", 
                                      print_time=print_time, 
                                      cost=cost, 
                                      filename=filename,
                                      infill=infill,
                                      wall_thickness=wall_thickness)
                else:
                    add_to_queue(order_data)
                    flash("Order submitted successfully!")
                    return render_template("order_success.html", order=order_data)
            else:
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
            error_msg = e.stderr if e.stderr else str(e)
            flash(f"Slicing failed: {error_msg}")
            return redirect(request.url)
        except Exception as e:
            flash(f"An error occurred: {str(e)}")
            return redirect(request.url)
    
    return render_template("index.html")

@app.route("/queue")
def view_queue():
    """View the current print queue"""
    from queue_utils import get_queue
    queue = get_queue()
    return render_template("queue.html", queue=queue)

@app.route("/admin")
def admin():
    """Simple admin page to view orders"""
    from queue_utils import get_queue
    orders = get_queue()
    return render_template("admin.html", orders=orders)

@app.route("/contact", methods=["POST"])
def contact():
    # handle form submission
    return "Order received!"

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    app.run(host="0.0.0.0", port=port, debug=debug)