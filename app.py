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
    """Find SuperSlicer executable with correct Render paths"""
    # 1) Explicit environment variable first
    env = os.environ.get("SUPERSLICER_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    
    # 2) Render deployment paths (in priority order)
    render_candidates = [
        "/opt/render/superslicer/superslicer_console",           # Our standard location
        "/opt/render/project/src/superslicer_console",          # Alternative location
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",  # Direct extraction path
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
    ]
    
    # 3) Local development paths (Windows/Mac/Linux)
    local_candidates = [
        # Windows paths
        r"C:\Users\zetil\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe",
        r"C:\Program Files\SuperSlicer\superslicer_console.exe",
        r"C:\SuperSlicer\superslicer_console.exe",
        
        # Linux/Mac paths
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "/opt/superslicer/superslicer_console",
        "./superslicer_console",
    ]
    
    # Check Render paths first (if we're on Render)
    if os.environ.get('RENDER') or os.path.exists('/opt/render'):
        for path in render_candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                print(f"✅ Found SuperSlicer at: {path}")
                return path
    
    # Check local development paths
    for path in local_candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            print(f"✅ Found SuperSlicer at: {path}")
            return path
    
    # 4) Try PATH resolution
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    if which:
        print(f"✅ Found SuperSlicer on PATH: {which}")
        return which
    
    print("❌ SuperSlicer not found in any expected location")
    return None

SUPERSLICER_PATH = find_superslicer()

def analyze_gcode_detailed(gcode_path):
    """Comprehensive G-code analysis to extract actual print data"""
    if not os.path.exists(gcode_path):
        return None
        
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        print(f"❌ Error reading G-code file: {e}")
        return None
    
    analysis = {
        "file_size": os.path.getsize(gcode_path),
        "total_lines": len(content.splitlines()),
        "print_time": None,
        "layer_count": 0,
        "filament_used": 0.0,
        "layer_height": None,
        "infill_percentage": None,
        "print_speed": None,
        "bed_temperature": None,
        "nozzle_temperature": None,
        "total_extrusion": 0.0,
        "movement_commands": 0,
        "estimates": {}
    }
    
    lines = content.splitlines()
    current_z = 0.0
    layers = set()
    
    for line in lines:
        line = line.strip()
        
        # Count movement commands
        if line.startswith(('G0', 'G1')):
            analysis["movement_commands"] += 1
            
            # Extract Z values for layer counting
            z_match = re.search(r'Z([\d.-]+)', line)
            if z_match:
                z_val = float(z_match.group(1))
                if z_val != current_z:
                    layers.add(z_val)
                    current_z = z_val
        
        # Extract temperatures
        if line.startswith('M104') or line.startswith('M109'):  # Nozzle temp
            temp_match = re.search(r'S(\d+)', line)
            if temp_match and not analysis["nozzle_temperature"]:
                analysis["nozzle_temperature"] = int(temp_match.group(1))
        
        if line.startswith('M140') or line.startswith('M190'):  # Bed temp
            temp_match = re.search(r'S(\d+)', line)
            if temp_match and not analysis["bed_temperature"]:
                analysis["bed_temperature"] = int(temp_match.group(1))
        
        # Parse SuperSlicer comments for detailed info
        if line.startswith(';'):
            comment = line[1:].strip().lower()
            
            # Extract print time estimates
            if "estimated printing time" in comment or "print time" in comment:
                time_match = re.search(r"(\d+)h\s*(\d+)m\s*(\d+)s", line, re.IGNORECASE)
                if time_match:
                    h, m, s = map(int, time_match.groups())
                    analysis["print_time"] = f"{h}h {m}m {s}s"
                    analysis["estimates"]["time_seconds"] = h * 3600 + m * 60 + s
                else:
                    time_match = re.search(r"(\d+)m\s*(\d+)s", line, re.IGNORECASE)
                    if time_match:
                        m, s = map(int, time_match.groups())
                        analysis["print_time"] = f"{m}m {s}s"
                        analysis["estimates"]["time_seconds"] = m * 60 + s
            
            # Extract other parameters
            if "filament used" in comment:
                filament_match = re.search(r"([\d.]+)\s*m", line)
                if filament_match:
                    analysis["filament_used"] = float(filament_match.group(1))
            
            if "layer height" in comment:
                height_match = re.search(r"([\d.]+)\s*mm", line)
                if height_match:
                    analysis["layer_height"] = float(height_match.group(1))
            
            if "fill density" in comment or "infill" in comment:
                infill_match = re.search(r"(\d+)%", line)
                if infill_match:
                    analysis["infill_percentage"] = int(infill_match.group(1))
    
    # Calculate layer count
    analysis["layer_count"] = len(layers)
    
    # Calculate cost if we have time
    if analysis["estimates"].get("time_seconds"):
        hours = analysis["estimates"]["time_seconds"] / 3600
        analysis["estimates"]["cost"] = round(hours * COST_PER_HOUR, 2)
        analysis["estimates"]["cost_formatted"] = f"${analysis['estimates']['cost']}"
    
    return analysis

def parse_gcode_stats(gcode_path):
    """Enhanced G-code parsing with fallback"""
    # Try detailed analysis first
    detailed = analyze_gcode_detailed(gcode_path)
    if detailed and detailed["print_time"]:
        cost = detailed["estimates"].get("cost_formatted", "$0.00")
        return detailed["print_time"], cost
    
    # Fallback to basic parsing
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

    # Look for time patterns in comments
    for line in content.splitlines():
        if line.startswith(';') and "time" in line.lower():
            patterns = [
                r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s",
                r"(\d+):(\d+):(\d+)",
                r"(\d+)\s*m\s*(\d+)\s*s",
                r"(\d+):(\d+)",
            ]
            
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    if len(match.groups()) == 3:
                        h, m, s = map(int, match.groups())
                        secs = h * 3600 + m * 60 + s
                    elif len(match.groups()) == 2:
                        a, b = map(int, match.groups())
                        if a > 12:
                            secs = a * 3600 + b * 60
                        else:
                            secs = a * 60 + b
                    
                    time_str = pretty_from_secs(secs)
                    cost = f"${round(secs / 3600.0 * COST_PER_HOUR, 2)}"
                    return time_str, cost

    return "Error", "$Error"

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

        # Initialize variables
        print_time = None
        cost = None
        is_demo = False
        gcode_analysis = None

        if not SUPERSLICER_PATH:
            # SuperSlicer not available - use demo mode
            print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
            is_demo = True
            flash("Demo mode: SuperSlicer not available. Showing estimated values based on your settings.", "info")
        else:
            # SuperSlicer available - generate actual G-code
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
                print(f"🔧 Running SuperSlicer: {' '.join(cmd)}")
                result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
                
                # Verify G-code file was created
                if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 0:
                    print(f"✅ G-code generated successfully: {output_gcode}")
                    
                    # Perform detailed G-code analysis
                    gcode_analysis = analyze_gcode_detailed(output_gcode)
                    
                    if gcode_analysis and gcode_analysis["print_time"]:
                        print_time = gcode_analysis["print_time"]
                        cost = gcode_analysis["estimates"]["cost_formatted"]
                        print(f"📊 G-code analysis complete - Time: {print_time}, Cost: {cost}")
                        flash(f"✅ G-code analyzed: {gcode_analysis['layer_count']} layers, {gcode_analysis['movement_commands']} commands", "success")
                    else:
                        # G-code analysis failed, try basic parsing
                        print_time, cost = parse_gcode_stats(output_gcode)
                        if print_time == "Error":
                            print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                            is_demo = True
                            flash("G-code generated but analysis failed. Showing estimated values.", "warning")
                        else:
                            flash("✅ G-code generated and parsed successfully", "success")
                else:
                    raise Exception("G-code file was not generated or is empty")
                    
            except subprocess.TimeoutExpired:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash("⏱️ Slicing timed out. Showing estimated values.", "warning")
            except subprocess.CalledProcessError as e:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                error_msg = (e.stderr or str(e)).strip()
                flash(f"❌ Slicing failed: {error_msg}. Showing estimated values.", "warning")
                print(f"❌ SuperSlicer error: {error_msg}")
            except Exception as e:
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash(f"❌ Error: {e}. Showing estimated values.", "warning")
                print(f"❌ Processing error: {e}")

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
                    "is_estimate": is_demo,
                    "gcode_path": output_gcode if not is_demo else None,
                    "gcode_analysis": gcode_analysis
                }
                
                try:
                    add_to_queue(order_data)
                    flash("✅ Order submitted successfully! We'll contact you soon.", "success")
                    return render_template("order_success.html", order=order_data)
                except Exception as e:
                    flash(f"❌ Failed to submit order: {str(e)}", "error")

        return render_template("results.html", 
                             print_time=print_time, 
                             cost=cost, 
                             filename=filename,
                             infill=infill,
                             wall_thickness=wall_thickness,
                             is_estimate=is_demo,
                             gcode_analysis=gcode_analysis,
                             gcode_path=output_gcode if not is_demo else None)

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

@app.route("/health")
def health():
    """Health check endpoint for monitoring"""
    return {
        "status": "healthy", 
        "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)),
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "cost_per_hour": COST_PER_HOUR,
        "upload_folder": UPLOAD_FOLDER,
        "profile_path": PROFILE_PATH
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
    print(f"📋 Profile path: {PROFILE_PATH}")
    
    if not SUPERSLICER_PATH:
        print("⚠️  SuperSlicer not found - app will run in demo mode")
        print("   Set SUPERSLICER_PATH environment variable or install SuperSlicer")
    
    app.run(host="0.0.0.0", debug=debug)
