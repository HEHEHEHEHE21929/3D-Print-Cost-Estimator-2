import os
import re
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
from queue_utils import add_to_queue

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"

# Smart path detection for different environments
def get_superslicer_path():
    # Check if we're on Render (Linux)
    if os.environ.get('RENDER') or os.path.exists('/opt/render'):
        # Render deployment - check multiple possible paths
        possible_paths = [
            "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
            "/opt/render/project/src/superslicer_console",
            os.environ.get('SUPERSLICER_PATH', '')
        ]
        for path in possible_paths:
            if path and os.path.exists(path):
                return path
        return ""  # Return empty to trigger demo mode
    else:
        # Local development (Windows/Mac/Linux)
        return os.environ.get('SUPERSLICER_PATH', 
                            r"C:\Users\zetil\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe")

SUPERSLICER_PATH = get_superslicer_path()
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = float(os.environ.get('COST_PER_HOUR', 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def calculate_demo_estimate(infill, wall_thickness, filename):
    """Calculate realistic estimates based on settings when SuperSlicer unavailable"""
    # Base time estimation based on file size and settings
    try:
        file_size_kb = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename)) / 1024
        size_factor = max(0.5, min(3.0, file_size_kb / 100))  # Scale based on file size
    except:
        size_factor = 1.0
    
    # Base print time (hours)
    base_hours = 1.5 * size_factor
    
    # Adjust for infill (higher infill = longer time)
    infill_factor = 1 + (infill / 100 * 0.8)
    
    # Adjust for wall thickness (thicker = longer time)
    wall_factor = 1 + ((wall_thickness - 0.4) / 0.4 * 0.3)
    
    # Calculate total time
    total_hours = base_hours * infill_factor * wall_factor
    
    # Format time
    hours = int(total_hours)
    minutes = int((total_hours - hours) * 60)
    seconds = int(((total_hours - hours) * 60 - minutes) * 60)
    
    if hours > 0:
        time_str = f"{hours}h {minutes}m {seconds}s"
    else:
        time_str = f"{minutes}m {seconds}s"
    
    # Calculate cost
    cost = f"${round(total_hours * COST_PER_HOUR, 2)}"
    
    return time_str, cost

def parse_gcode_stats(gcode_path):
    """Parse G-code file for print time statistics"""
    try:
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
    except Exception as e:
        print(f"Error parsing G-code: {e}")
    
    return "Error", "$Error"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Validate file upload
        if "file" not in request.files:
            flash("No file selected", "error")
            return redirect(request.url)
        
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected", "error")
            return redirect(request.url)
        
        if not file or not allowed_file(file.filename):
            flash("Invalid file type. Please upload STL, 3MF, or OBJ files only.", "error")
            return redirect(request.url)
        
        # Get form data with validation
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
        
        # Save uploaded file
        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        
        # Try SuperSlicer first, fall back to demo mode
        print_time = None
        cost = None
        is_demo = False
        
        if SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH):
            # SuperSlicer is available - attempt real slicing
            output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")
            
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
                result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
                print_time, cost = parse_gcode_stats(output_gcode)
                
                if print_time == "Error":
                    raise Exception("Failed to parse G-code output")
                    
            except subprocess.TimeoutExpired:
                flash("Slicing timed out. Using estimated values.", "warning")
                is_demo = True
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr if e.stderr else str(e)
                flash(f"Slicing failed: {error_msg}. Using estimated values.", "warning")
                is_demo = True
            except Exception as e:
                flash(f"Slicing error: {str(e)}. Using estimated values.", "warning")
                is_demo = True
        else:
            # SuperSlicer not available
            is_demo = True
        
        # Use demo mode if SuperSlicer failed or unavailable
        if is_demo or not print_time:
            print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
            if not flash:  # Only show demo message if no other error was flashed
                flash("Showing estimated values based on your settings.", "info")
        
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
        
        # Show results
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
            # Here you could add email sending logic
            flash("Thank you for your message! We'll get back to you soon.", "success")
    except Exception as e:
        flash(f"Error processing contact form: {str(e)}", "error")
    
    return redirect(url_for('index'))

@app.route("/health")
def health():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH))}

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", error="Page not found"), 404

@app.errorhandler(500)
def server_error(error):
    return render_template("error.html", error="Internal server error"), 500

if __name__ == "__main__":
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # Print startup info
    print(f"Starting 3D Print Cost Estimator...")
    print(f"SuperSlicer path: {SUPERSLICER_PATH}")
    print(f"SuperSlicer available: {bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH))}")
    print(f"Demo mode: {'Yes' if not (SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)) else 'No'}")
    
    app.run(host="0.0.0.0", debug=debug)

# No app.run() block at the end!
