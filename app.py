import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
from queue_utils import add_to_queue

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = float(os.environ.get('COST_PER_HOUR', 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    """Find SuperSlicer executable in various locations"""
    # 1) Explicit environment variable
    env = os.environ.get("SUPERSLICER_PATH")
    if env:
        # If absolute path given, verify file + exec bit
        if os.path.isabs(env):
            if os.path.isfile(env) and os.access(env, os.X_OK):
                return env
        else:
            # Treat as name -> resolve on PATH
            resolved = shutil.which(env)
            if resolved:
                return resolved
    
    # 2) Common extracted or moved locations (check both release folder and moved location)
    candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        # Windows paths for local development
        r"C:\Users\zetil\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe",
        r"C:\Program Files\SuperSlicer\superslicer_console.exe",
    ]
    
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    
    # 3) On PATH
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    if which:
        return which
    
    # Not found
    return None

SUPERSLICER_PATH = find_superslicer()

def calculate_demo_estimate(infill, wall_thickness, filename):
    """Calculate realistic estimates when SuperSlicer is unavailable"""
    try:
        file_size_kb = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename)) / 1024
        size_factor = max(0.5, min(3.0, file_size_kb / 100))
    except:
        size_factor = 1.0
    
    # Base print time (hours)
    base_hours = 1.5 * size_factor
    
    # Adjust for infill
    infill_factor = 1 + (infill / 100 * 0.8)
    
    # Adjust for wall thickness
    wall_factor = 1 + ((wall_thickness - 0.4) / 0.4 * 0.3)
    
    total_hours = base_hours * infill_factor * wall_factor
    
    # Format time
    hours = int(total_hours)
    minutes = int((total_hours - hours) * 60)
    seconds = int(((total_hours - hours) * 60 - minutes) * 60)
    
    if hours > 0:
        time_str = f"{hours}h {minutes}m {seconds}s"
    else:
        time_str = f"{minutes}m {seconds}s"
    
    cost = f"${round(total_hours * COST_PER_HOUR, 2)}"
    return time_str, cost

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

    # Look for lines containing estimated/time and parse several formats
    for line in content.splitlines():
        lower = line.lower()
        if "estimated" in lower and "time" in lower:
            # Try H M S
            m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", line, re.IGNORECASE)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            
            # Try M S
            m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", line, re.IGNORECASE)
            if m:
                secs = int(m.group(1)) * 60 + int(m.group(2))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            
            # Try H:MM:SS or MM:SS
            m = re.search(r"(\d+):(\d+):(\d+)", line)
            if m:
                secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"
            
            m = re.search(r"(\d+):(\d+)", line)
            if m:
                a, b = map(int, m.groups())
                # Heuristic: if first > 12 treat as H:MM else M:SS
                if a > 12:
                    secs = a*3600 + b*60
                else:
                    secs = a*60 + b
                return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"

    # Fallback: any time-like match in file
    m = re.search(r"(\d+)\s*h", content, re.IGNORECASE)
    if m:
        secs = int(m.group(1)) * 3600
        return pretty_from_secs(secs), f"${round(secs/3600.0*COST_PER_HOUR,2)}"

    return "Error", "$Error"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # File validation
        if "file" not in request.files:
            flash("No file part", "error")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "error")
            return redirect(request.url)

        if not file or not allowed_file(file.filename):
            flash("Invalid file type. Please upload STL, 3MF, or OBJ files only.", "error")
            return redirect(request.url)

        # Get form parameters
        try:
            infill = int(request.form.get("infill", 20))
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
            customer_name = request.form.get("customer_name", "").strip()
            customer_email = request.form.get("customer_email", "").strip()
        except (ValueError, TypeError):
            flash("Invalid form data. Please check your inputs.", "error")
            return redirect(request.url)

        # Validate ranges
        if not (0 <= infill <= 99):
            flash("Infill must be between 0 and 99%", "error")
            return redirect(request.url)

        if not (0.1 <= wall_thickness <= 10.0):
            flash("Wall thickness must be between 0.1 and 10.0 mm", "error")
            return redirect(request.url)

        # Save file
        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        # Check SuperSlicer availability
        print_time = None
        cost = None
        is_demo = False

        if not SUPERSLICER_PATH or not os.path.isfile(SUPERSLICER_PATH):
            # SuperSlicer not available - use demo mode
            print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
            is_demo = True
            flash("Demo mode: SuperSlicer not available. Showing estimated values based on your settings.", "info")
        else:
            # SuperSlicer available - attempt real slicing
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
                
                if print_time == "Error":
                    # G-code parsing failed, use demo estimate
                    print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                    is_demo = True
                    flash("Slicing completed but estimation failed. Showing calculated estimate.", "warning")
                    
            except subprocess.TimeoutExpired:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash("Slicing timed out. Showing estimated values.", "warning")
            except subprocess.CalledProcessError as e:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                error_msg = (e.stderr or str(e)).strip()
                flash(f"Slicing failed: {error_msg}. Showing estimated values.", "warning")
            except Exception as e:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash(f"An error occurred: {e}. Showing estimated values.", "warning")

        # Handle order submission
        if request.form.get("order_attempt"):
            if not customer_name or not customer_email:
                flash("Please enter your name and email to place an order.", "error")
            else:
                order_data = {
                    "customer_name": customer_name,
                    "customer_email": customer_email,
                    "file": filename,
                    "infill": infill,
                    "wall_thickness": wall_thickness,
                    "time": print_time,
                    "cost": cost,
                    "is_estimate": is_demo
                }
                
                try:
                    add_to_queue(order_data)
                    flash("Order submitted successfully! We'll contact you soon.", "success")
                    return render_template("order_success.html", order=order_data)
                except Exception as e:
                    flash(f"Failed to submit order: {str(e)}", "error")

        return render_template("results.html", 
                             print_time=print_time, 
                             cost=cost, 
                             filename=filename,
                             infill=infill,
                             wall_thickness=wall_thickness,
                             is_estimate=is_demo)

    return render_template("index.html")

@app.route("/queue")
def view_queue():
    """View the current print queue"""
    try:
        from queue_utils import get_queue
        queue = get_queue()
        return render_template("queue.html", queue=queue)
    except Exception as e:
        flash(f"Error loading queue: {str(e)}", "error")
        return redirect(url_for('index'))

@app.route("/admin")
def admin():
    """Admin page to view all orders"""
    try:
        from queue_utils import get_queue
        orders = get_queue()
        return render_template("admin.html", orders=orders)
    except Exception as e:
        flash(f"Error loading admin panel: {str(e)}", "error")
        return redirect(url_for('index'))

@app.route("/contact", methods=["POST"])
def contact():
    """Handle contact form submissions"""
    try:
        customer_name = request.form.get("customer_name", "").strip()
        customer_email = request.form.get("customer_email", "").strip()
        message = request.form.get("message", "").strip()
        
        if not all([customer_name, customer_email, message]):
            flash("Please fill in all required fields.", "error")
        else:
            flash("Thank you for your message! We'll get back to you soon.", "success")
    except Exception as e:
        flash(f"Error processing contact form: {str(e)}", "error")
    
    return redirect(url_for('index'))

@app.route("/health")
def health():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy", 
        "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)),
        "superslicer_path": SUPERSLICER_PATH or "Not found"
    }

if __name__ == "__main__":
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # Print startup info
    print(f"🖨️  3D Print Cost Estimator Starting...")
    print(f"📁 Upload folder: {UPLOAD_FOLDER}")
    print(f"🔧 SuperSlicer path: {SUPERSLICER_PATH or 'Not found'}")
    print(f"✅ SuperSlicer available: {bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH))}")
    print(f"💰 Cost per hour: ${COST_PER_HOUR}")
    print(f"🔍 Debug mode: {debug}")
    
    if not SUPERSLICER_PATH:
        print("⚠️  SuperSlicer not found - app will run in demo mode")
        print("   Set SUPERSLICER_PATH environment variable or install SuperSlicer")
    
    app.run(host="0.0.0.0", debug=debug)
