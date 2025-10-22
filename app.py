import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash
try:
    from queue_utils import add_to_queue, get_queue
except ImportError:
    # Fallback functions if queue_utils doesn't exist
    def add_to_queue(order_data):
        print(f"Order added: {order_data}")
        return True
    def get_queue():
        return []

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs("profiles", exist_ok=True)

COST_PER_HOUR = float(os.environ.get('COST_PER_HOUR', 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    """Find SuperSlicer executable"""
    # 1) Environment variable first
    env = os.environ.get("SUPERSLICER_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    
    # 2) Render deployment paths
    render_candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
    ]
    
    # 3) Local development paths
    local_candidates = [
        # Windows
        r"C:\Users\Zeti\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe",
        r"C:\Program Files\SuperSlicer\superslicer_console.exe",
        r"C:\SuperSlicer\superslicer_console.exe",
        # Linux/Mac
        "/usr/local/bin/superslicer_console",
        "/usr/bin/superslicer_console",
        "/opt/superslicer/superslicer_console",
        "./superslicer_console",
    ]
    
    # Check appropriate paths based on environment
    if os.path.exists('/opt/render') or os.environ.get('RENDER'):
        candidates = render_candidates
    else:
        candidates = local_candidates
    
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    
    # Try PATH resolution
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    return which

SUPERSLICER_PATH = find_superslicer()

def create_default_profile():
    """Create a basic SuperSlicer profile if none exists"""
    if not os.path.exists(PROFILE_PATH):
        default_config = """# Basic SuperSlicer Configuration
# Generated automatically - customize as needed

[printer:Generic]
printer_technology = FFF
bed_shape = 0x0,220x0,220x220,0x220
max_print_height = 250
nozzle_diameter = 0.4
printer_model = Generic
printer_variant = 0.4
default_print_profile = 0.20mm QUALITY
default_filament_profile = Generic PLA

[filament:Generic PLA]
filament_type = PLA
filament_density = 1.24
filament_cost = 25
temperature = 200
bed_temperature = 60
first_layer_temperature = 210
first_layer_bed_temperature = 60

[print:0.20mm QUALITY]
layer_height = 0.2
first_layer_height = 0.2
perimeters = 3
top_solid_layers = 5
bottom_solid_layers = 4
fill_density = 20%
infill_pattern = grid
support_material = 0
support_material_threshold = 45
"""
        try:
            with open(PROFILE_PATH, 'w') as f:
                f.write(default_config)
            print(f"✅ Created default profile: {PROFILE_PATH}")
        except Exception as e:
            print(f"⚠️  Could not create default profile: {e}")

def extract_gcode_time(gcode_path):
    """Extract actual print time from SuperSlicer G-code comments"""
    if not os.path.exists(gcode_path):
        return None, None
    
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            # Read first 200 lines where SuperSlicer puts metadata
            lines = []
            for i, line in enumerate(f):
                if i > 200:
                    break
                lines.append(line.strip())
    except Exception as e:
        print(f"❌ Error reading G-code: {e}")
        return None, None
    
    # SuperSlicer time estimation patterns
    patterns = [
        r";\s*estimated printing time.*?=\s*(\d+)h\s*(\d+)m\s*(\d+)s",
        r";\s*estimated printing time.*?:\s*(\d+)h\s*(\d+)m\s*(\d+)s",
        r";\s*print time.*?:\s*(\d+)h\s*(\d+)m\s*(\d+)s",
        r";\s*TIME:\s*(\d+)",
        r";\s*total print time.*?:\s*(\d+)m\s*(\d+)s",
        r";\s*print time.*?:\s*(\d+)m\s*(\d+)s",
    ]
    
    for line in lines:
        if not line.startswith(';'):
            continue
        
        line_lower = line.lower()
        
        for pattern in patterns:
            match = re.search(pattern, line_lower)
            if match:
                groups = match.groups()
                
                if len(groups) == 1:
                    # TIME:seconds format
                    seconds = int(groups[0])
                elif len(groups) == 2:
                    # minutes and seconds
                    minutes, secs = map(int, groups)
                    seconds = minutes * 60 + secs
                elif len(groups) == 3:
                    # hours, minutes, seconds
                    hours, minutes, secs = map(int, groups)
                    seconds = hours * 3600 + minutes * 60 + secs
                else:
                    continue
                
                # Format time string
                hours = seconds // 3600
                mins = (seconds % 3600) // 60
                secs = seconds % 60
                
                if hours > 0:
                    time_str = f"{hours}h {mins}m {secs}s"
                else:
                    time_str = f"{mins}m {secs}s"
                
                print(f"📊 Found print time in G-code: {time_str} ({seconds} seconds)")
                return time_str, seconds
    
    print("⚠️  No print time found in G-code comments")
    return None, None

def calculate_estimate(infill, wall_thickness, filename):
    """Calculate estimates when SuperSlicer unavailable"""
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
            flash("No file selected", "error")
            return redirect(request.url)

        file = request.files["file"]
        if file.filename == "":
            flash("No file selected", "error")
            return redirect(request.url)

        if not file or not allowed_file(file.filename):
            flash("Invalid file type. Please upload STL, 3MF, or OBJ files only.", "error")
            return redirect(request.url)

        # Get form parameters with validation
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
        print(f"📁 File saved: {filepath}")

        # Generate G-code output path
        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")
        
        # Initialize variables
        print_time = None
        cost = None
        is_demo = False

        if not SUPERSLICER_PATH:
            # SuperSlicer not available - demo mode
            print_time, cost = calculate_estimate(infill, wall_thickness, filename)
            is_demo = True
            flash("Demo mode: SuperSlicer not available. Showing estimated values.", "info")
            print("⚠️  Demo mode: SuperSlicer not found")
        else:
            # SuperSlicer available - generate real G-code
            print(f"🔧 SuperSlicer found at: {SUPERSLICER_PATH}")
            
            # Create default profile if needed
            create_default_profile()
            
            # Build SuperSlicer command
            cmd = [
                SUPERSLICER_PATH,
                "--load", PROFILE_PATH,
                "--fill-density", f"{infill}%",
                "--perimeters", str(max(1, int(wall_thickness / 0.4))),
                "--output", output_gcode,
                filepath
            ]
            
            print(f"🚀 Running SuperSlicer: {' '.join(cmd)}")
            
            try:
                # Run SuperSlicer with timeout
                result = subprocess.run(
                    cmd, 
                    check=True, 
                    capture_output=True, 
                    text=True, 
                    timeout=300,
                    cwd=os.getcwd()
                )
                
                print(f"✅ SuperSlicer completed successfully")
                if result.stdout:
                    print(f"📋 SuperSlicer output: {result.stdout[:200]}...")
                
                # Verify G-code file was created
                if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 100:
                    file_size = os.path.getsize(output_gcode)
                    print(f"✅ G-code generated: {output_gcode} ({file_size} bytes)")
                    
                    # Extract actual print time from G-code
                    print_time, time_seconds = extract_gcode_time(output_gcode)
                    
                    if print_time and time_seconds:
                        # Calculate cost from actual time
                        hours = time_seconds / 3600
                        cost = f"${round(hours * COST_PER_HOUR, 2)}"
                        flash(f"✅ G-code generated successfully! Actual print time: {print_time}", "success")
                        print(f"💰 Calculated cost: {cost} (${COST_PER_HOUR}/hour × {hours:.2f}h)")
                    else:
                        # Fallback to estimate if time extraction fails
                        print_time, cost = calculate_estimate(infill, wall_thickness, filename)
                        is_demo = True
                        flash("G-code generated but time extraction failed. Showing estimated values.", "warning")
                        print("⚠️  Using estimate as fallback")
                else:
                    raise Exception(f"G-code file not created or too small: {output_gcode}")
                    
            except subprocess.TimeoutExpired:
                print("⏱️  SuperSlicer timed out")
                print_time, cost = calculate_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash("Slicing timed out. Showing estimated values.", "warning")
                
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.strip() if e.stderr else str(e)
                print(f"❌ SuperSlicer failed: {error_msg}")
                print_time, cost = calculate_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash(f"Slicing failed: {error_msg}. Showing estimated values.", "warning")
                
            except Exception as e:
                print(f"❌ Processing error: {e}")
                print_time, cost = calculate_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash(f"Error: {e}. Showing estimated values.", "warning")

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
                    "gcode_path": output_gcode if not is_demo else None
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
                             gcode_path=output_gcode if not is_demo else None)

    return render_template("index.html")

@app.route("/queue")
def view_queue():
    """View the current print queue"""
    try:
        queue = get_queue()
        return render_template("queue.html", queue=queue)
    except Exception as e:
        flash(f"Error loading queue: {str(e)}", "error")
        return redirect(url_for('index'))

@app.route("/admin")
def admin():
    """Admin panel for order overview"""
    try:
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
    """Health check endpoint"""
    return {
        "status": "healthy", 
        "superslicer_available": bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH)),
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "cost_per_hour": COST_PER_HOUR,
        "profile_exists": os.path.exists(PROFILE_PATH)
    }

if __name__ == "__main__":
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    
    # Startup information
    print(f"🖨️  3D Print Cost Estimator Starting...")
    print(f"📁 Upload folder: {UPLOAD_FOLDER}")
    print(f"📁 Output folder: output")
    print(f"🔧 SuperSlicer path: {SUPERSLICER_PATH or 'Not found'}")
    print(f"✅ SuperSlicer available: {bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH))}")
    print(f"📋 Profile path: {PROFILE_PATH}")
    print(f"📋 Profile exists: {os.path.exists(PROFILE_PATH)}")
    print(f"💰 Cost per hour: ${COST_PER_HOUR}")
    print(f"🔍 Debug mode: {debug}")
    print(f"🌐 Port: {port}")
    
    if not SUPERSLICER_PATH:
        print("⚠️  SuperSlicer not found - app will run in demo mode")
        print("   Install SuperSlicer and set SUPERSLICER_PATH environment variable")
    else:
        print("🚀 Ready to generate real G-code!")
    
    # Create default profile if needed
    create_default_profile()
    
    app.run(host="0.0.0.0", port=port, debug=debug)
