import os
import re
import shutil
import subprocess
import tempfile
from flask import Flask, render_template, request, redirect, url_for, flash

# Import utilities with fallback
try:
    from queue_utils import add_to_queue, get_queue
except ImportError:
    def add_to_queue(order_data):
        print(f"Order added: {order_data}")
        return True
    def get_queue():
        return []

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "output"
PROFILE_PATH = "profiles/my_config.ini"
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs("profiles", exist_ok=True)

COST_PER_HOUR = float(os.environ.get('COST_PER_HOUR', 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def find_superslicer():
    """Find SuperSlicer executable with comprehensive search"""
    print("🔍 Searching for SuperSlicer...")
    
    # 1) Environment variable first
    env = os.environ.get("SUPERSLICER_PATH")
    if env and os.path.isfile(env):
        if os.access(env, os.X_OK):
            print(f"✅ Found SuperSlicer via SUPERSLICER_PATH: {env}")
            return env
        else:
            print(f"⚠️  SuperSlicer found but not executable: {env}")
    
    # 2) Render deployment paths
    render_candidates = [
        "/opt/render/superslicer/superslicer_console",
        "/opt/render/project/src/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/bin/superslicer_console",
        "/opt/render/project/src/SuperSlicer-2.7.61.1-linux/SuperSlicer",
    ]
    
    # 3) Local development paths (updated with your actual path)
    local_candidates = [
        # Windows - your specific path
        r"C:\Users\Zeti\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe",
        # Other common Windows paths
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
            print(f"✅ Found SuperSlicer at: {path}")
            return path
        elif os.path.isfile(path):
            print(f"⚠️  Found SuperSlicer but not executable: {path}")
    
    # Try PATH resolution
    which = shutil.which("superslicer_console") or shutil.which("superslicer")
    if which:
        print(f"✅ Found SuperSlicer on PATH: {which}")
        return which
    
    print("❌ SuperSlicer not found in any location")
    return None

SUPERSLICER_PATH = find_superslicer()

def create_superslicer_profile():
    """Create a working SuperSlicer profile for actual G-code generation"""
    if not os.path.exists(PROFILE_PATH):
        # Create a comprehensive SuperSlicer configuration
        config_content = """# Generated SuperSlicer Configuration for G-code generation
# version = 2.5.59.13
# min_slic3r_version = 2.4.0-alpha0

[printer:Generic Printer]
printer_technology = FFF
bed_shape = 0x0,200x0,200x200,0x200
bed_temperature = 60
between_objects_gcode = 
deretract_speed = 40
extruder_clearance_height = 20
extruder_clearance_radius = 20
extruder_colour = ""
extruder_offset = 0x0
gcode_flavor = marlin
silent_mode = 0
use_relative_e_distances = 0
machine_max_acceleration_e = 10000,5000
machine_max_acceleration_extruding = 1500,1250
machine_max_acceleration_retracting = 1500,1250
machine_max_acceleration_travel = 1500,1250
machine_max_acceleration_x = 2000,1000
machine_max_acceleration_y = 2000,1000
machine_max_acceleration_z = 500,200
machine_max_feedrate_e = 120,120
machine_max_feedrate_x = 500,200
machine_max_feedrate_y = 500,200
machine_max_feedrate_z = 12,12
machine_max_jerk_e = 2.5,2.5
machine_max_jerk_x = 10,10
machine_max_jerk_y = 10,10
machine_max_jerk_z = 0.2,0.4
machine_min_extruding_rate = 0,0
machine_min_travel_rate = 0,0
layer_gcode = 
max_layer_height = 0.32
max_print_height = 200
min_layer_height = 0.07
nozzle_diameter = 0.4
printer_model = Generic
printer_variant = 0.4
retract_before_travel = 2
retract_before_wipe = 0%
retract_layer_change = 0
retract_length = 2
retract_length_toolchange = 10
retract_lift = 0
retract_lift_above = 0
retract_lift_below = 0
retract_restart_extra = 0
retract_restart_extra_toolchange = 0
retract_speed = 40
single_extruder_multi_material = 0
start_gcode = G28 ; home all axes\\nG1 Z5 F5000 ; lift nozzle
end_gcode = M104 S0 ; turn off temperature\\nG28 X0 ; home X axis\\nM84 ; disable motors
toolchange_gcode = 
use_firmware_retraction = 0
use_volumetric_e = 0
variable_layer_height = 1
wipe = 0
z_offset = 0
printer_settings_id = Generic Printer
default_print_profile = Generic Print
default_filament_profile = Generic PLA

[print:Generic Print]
avoid_crossing_perimeters = 1
avoid_crossing_perimeters_max_detour = 0
bottom_fill_pattern = monotonic
bottom_solid_layers = 3
bottom_solid_min_thickness = 0
bridge_acceleration = 0
bridge_angle = 0
bridge_flow_ratio = 1
bridge_speed = 60
brim_width = 0
clip_multipart_objects = 1
compatible_printers = 
compatible_printers_condition = 
complete_objects = 0
default_acceleration = 0
dont_support_bridges = 1
elefant_foot_compensation = 0
ensure_vertical_shell_thickness = 0
external_perimeter_extrusion_width = 0
external_perimeter_speed = 50%
external_perimeters_first = 0
extra_perimeters = 1
extruder_clearance_height = 20
extruder_clearance_radius = 20
extrusion_width = 0
fill_angle = 45
fill_density = 20%
fill_pattern = grid
first_layer_acceleration = 0
first_layer_extrusion_width = 200%
first_layer_height = 0.35
first_layer_speed = 30
gap_fill_speed = 20
gcode_comments = 0
gcode_label_objects = 0
infill_acceleration = 0
infill_every_layers = 1
infill_extruder = 1
infill_extrusion_width = 0
infill_first = 0
infill_only_where_needed = 0
infill_overlap = 25%
infill_speed = 80
interface_shells = 0
layer_height = 0.2
max_print_speed = 80
max_volumetric_speed = 0
min_skirt_length = 0
notes = 
only_retract_when_crossing_perimeters = 1
ooze_prevention = 0
output_filename_format = [input_filename_base].gcode
overhangs = 1
perimeter_acceleration = 0
perimeter_extruder = 1
perimeter_extrusion_width = 0
perimeter_speed = 60
perimeters = 3
post_process = 
print_settings_id = Generic Print
raft_layers = 0
resolution = 0
seam_position = aligned
skirt_distance = 6
skirt_height = 1
skirts = 1
slice_closing_radius = 0.049
small_perimeter_speed = 15
solid_infill_below_area = 70
solid_infill_every_layers = 0
solid_infill_extruder = 1
solid_infill_extrusion_width = 0
solid_infill_speed = 20
spiral_vase = 0
standby_temperature_delta = -5
support_material = 0
support_material_angle = 0
support_material_auto = 1
support_material_buildplate_only = 0
support_material_contact_distance = 0.2
support_material_enforce_layers = 0
support_material_extruder = 1
support_material_extrusion_width = 0
support_material_interface_contact_loops = 0
support_material_interface_extruder = 1
support_material_interface_layers = 3
support_material_interface_spacing = 0
support_material_interface_speed = 100%
support_material_pattern = pillars
support_material_spacing = 2.5
support_material_speed = 60
support_material_synchronize_layers = 0
support_material_threshold = 0
support_material_with_sheath = 1
support_material_xy_spacing = 0.6
thin_walls = 1
threads = 8
top_fill_pattern = monotonic
top_infill_extrusion_width = 0
top_solid_infill_speed = 15
top_solid_layers = 3
top_solid_min_thickness = 0
travel_speed = 130
wipe_tower = 0
wipe_tower_bridging = 10
wipe_tower_rotation_angle = 0
wipe_tower_width = 60
wipe_tower_x = 180
wipe_tower_y = 140
xy_size_compensation = 0

[filament:Generic PLA]
bed_temperature = 60
bridge_fan_speed = 100
compatible_printers = 
compatible_printers_condition = 
compatible_prints = 
compatible_prints_condition = 
cooling = 1
disable_fan_first_layers = 3
extrusion_multiplier = 1
fan_always_on = 1
fan_below_layer_time = 60
filament_colour = #29B2B2
filament_cost = 25
filament_density = 1.24
filament_diameter = 1.75
filament_max_volumetric_speed = 0
filament_minimal_purge_on_wipe_tower = 15
filament_notes = ""
filament_ramming_parameters = "120 100 6.6 6.8 7.2 7.6 7.9 8.2 8.7 9.4 9.9 10.0| 0.05 6.6 0.45 6.8 0.95 7.8 1.45 8.3 1.95 9.7 2.45 10 2.95 7.6 3.45 7.6 3.95 7.6 4.45 7.6 4.95 7.6"
filament_settings_id = Generic PLA
filament_soluble = 0
filament_toolchange_delay = 0
filament_type = PLA
filament_unload_time = 0
filament_unloading_speed = 90
filament_unloading_speed_start = 100
first_layer_bed_temperature = 60
first_layer_temperature = 215
inherits = 
max_fan_speed = 100
min_fan_speed = 35
min_print_speed = 10
slowdown_below_layer_time = 5
temperature = 200
"""
        
        try:
            with open(PROFILE_PATH, 'w', encoding='utf-8') as f:
                f.write(config_content)
            print(f"✅ Created SuperSlicer profile: {PROFILE_PATH}")
            return True
        except Exception as e:
            print(f"❌ Failed to create SuperSlicer profile: {e}")
            return False
    else:
        print(f"✅ SuperSlicer profile exists: {PROFILE_PATH}")
        return True

def run_superslicer_slicing(input_file, output_file, infill_percent, wall_thickness):
    """Run SuperSlicer to generate actual G-code with real time estimates"""
    if not SUPERSLICER_PATH:
        return False, "SuperSlicer not found"
    
    if not create_superslicer_profile():
        return False, "Failed to create SuperSlicer profile"
    
    # Calculate perimeters from wall thickness (assuming 0.4mm nozzle)
    perimeters = max(1, int(wall_thickness / 0.4))
    
    # Build SuperSlicer command for actual slicing
    cmd = [
        SUPERSLICER_PATH,
        "--load", PROFILE_PATH,
        "--fill-density", f"{infill_percent}%",
        "--perimeters", str(perimeters),
        "--layer-height", "0.2",
        "--first-layer-height", "0.2",
        "--nozzle-diameter", "0.4",
        "--filament-diameter", "1.75",
        "--temperature", "200",
        "--bed-temperature", "60",
        "--print-center", "100,100",
        "--gcode-comments",  # Enable comments for time extraction
        "--output", output_file,
        input_file
    ]
    
    print(f"🚀 Running SuperSlicer command:")
    print(f"   {' '.join(cmd)}")
    
    try:
        # Run SuperSlicer with proper working directory
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=os.getcwd(),
            check=False  # Don't raise exception on non-zero exit
        )
        
        print(f"📋 SuperSlicer exit code: {result.returncode}")
        if result.stdout:
            print(f"📋 SuperSlicer stdout: {result.stdout[:500]}...")
        if result.stderr:
            print(f"📋 SuperSlicer stderr: {result.stderr[:500]}...")
        
        # Check if G-code file was created successfully
        if os.path.exists(output_file) and os.path.getsize(output_file) > 100:
            file_size = os.path.getsize(output_file)
            print(f"✅ G-code generated successfully: {output_file} ({file_size} bytes)")
            return True, "Success"
        else:
            error_msg = result.stderr if result.stderr else "Unknown error"
            return False, f"G-code generation failed: {error_msg}"
            
    except subprocess.TimeoutExpired:
        return False, "SuperSlicer timed out (>5 minutes)"
    except Exception as e:
        return False, f"SuperSlicer execution error: {str(e)}"

def extract_superslicer_time_from_gcode(gcode_path):
    """Extract actual print time from SuperSlicer generated G-code"""
    if not os.path.exists(gcode_path):
        print(f"❌ G-code file not found: {gcode_path}")
        return None, None
    
    try:
        with open(gcode_path, 'r', encoding='utf-8', errors='ignore') as f:
            # Read first 300 lines where SuperSlicer metadata is located
            lines = []
            for i, line in enumerate(f):
                if i > 300:
                    break
                lines.append(line.strip())
                
        print(f"📖 Reading G-code file: {gcode_path} ({len(lines)} header lines)")
        
    except Exception as e:
        print(f"❌ Error reading G-code file: {e}")
        return None, None
    
    # SuperSlicer time patterns (multiple formats)
    time_patterns = [
        # Most common SuperSlicer format
        r';\s*estimated\s+printing\s+time.*?=\s*(\d+)h\s*(\d+)m\s*(\d+)s',
        r';\s*estimated\s+printing\s+time.*?:\s*(\d+)h\s*(\d+)m\s*(\d+)s',
        # Alternative formats
        r';\s*print\s+time.*?:\s*(\d+)h\s*(\d+)m\s*(\d+)s',
        r';\s*total\s+print\s+time.*?:\s*(\d+)h\s*(\d+)m\s*(\d+)s',
        # Minutes only format
        r';\s*estimated\s+printing\s+time.*?:\s*(\d+)m\s*(\d+)s',
        r';\s*print\s+time.*?:\s*(\d+)m\s*(\d+)s',
        # Seconds only format
        r';\s*TIME:\s*(\d+)',
        # Duration format
        r';\s*printing\s+time.*?(\d+):(\d+):(\d+)',
    ]
    
    for line_num, line in enumerate(lines):
        if not line.startswith(';'):
            continue
            
        line_lower = line.lower()
        
        # Debug: Print time-related comments
        if any(keyword in line_lower for keyword in ['time', 'duration', 'print']):
            print(f"📝 Line {line_num}: {line}")
        
        for pattern_num, pattern in enumerate(time_patterns):
            match = re.search(pattern, line_lower, re.IGNORECASE)
            if match:
                groups = match.groups()
                print(f"✅ Found time match with pattern {pattern_num}: {groups}")
                
                try:
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
                    
                    print(f"🎯 FOUND ACTUAL SUPERSLICER TIME: {time_str} ({seconds} seconds)")
                    return time_str, seconds
                    
                except ValueError as e:
                    print(f"⚠️  Error parsing time groups {groups}: {e}")
                    continue
    
    print("❌ No SuperSlicer time estimate found in G-code")
    return None, None

def calculate_demo_estimate(infill, wall_thickness, filename):
    """Calculate estimates when SuperSlicer unavailable"""
    try:
        file_size_kb = os.path.getsize(os.path.join(UPLOAD_FOLDER, filename)) / 1024
        size_factor = max(0.5, min(3.0, file_size_kb / 100))
    except Exception:
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
        print(f"📁 File saved: {filepath} ({os.path.getsize(filepath)} bytes)")

        # Generate G-code output path
        base_name = filename.rsplit(".", 1)[0]
        output_gcode = os.path.join(OUTPUT_FOLDER, f"{base_name}.gcode")
        
        # Initialize variables
        print_time = None
        cost = None
        is_demo = False
        actual_gcode_generated = False

        if not SUPERSLICER_PATH:
            # SuperSlicer not available - demo mode
            print("⚠️  SuperSlicer not found - using demo mode")
            print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
            is_demo = True
            flash("Demo mode: SuperSlicer not available. Showing estimated values based on your settings.", "info")
        else:
            # SuperSlicer available - generate ACTUAL G-code
            print(f"🔧 SuperSlicer found at: {SUPERSLICER_PATH}")
            print(f"🎯 Generating ACTUAL G-code with SuperSlicer...")
            print(f"   Settings: {infill}% infill, {wall_thickness}mm walls")
            
            # Run SuperSlicer to generate G-code
            success, error_msg = run_superslicer_slicing(filepath, output_gcode, infill, wall_thickness)
            
            if success:
                print("✅ SuperSlicer G-code generation successful!")
                actual_gcode_generated = True
                
                # Extract ACTUAL time from SuperSlicer G-code
                print("🔍 Extracting actual print time from G-code...")
                print_time, time_seconds = extract_superslicer_time_from_gcode(output_gcode)
                
                if print_time and time_seconds:
                    # Calculate cost from ACTUAL time
                    hours = time_seconds / 3600
                    cost = f"${round(hours * COST_PER_HOUR, 2)}"
                    flash(f"✅ ACTUAL G-code generated! Real print time: {print_time}", "success")
                    print(f"💰 Cost calculated from ACTUAL time: {cost} (${COST_PER_HOUR}/hour × {hours:.2f}h)")
                else:
                    # G-code generated but time extraction failed
                    print("⚠️  G-code generated but couldn't extract time - using estimate")
                    print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                    is_demo = True
                    flash("G-code generated but time extraction failed. Showing estimated values.", "warning")
            else:
                # SuperSlicer failed - use demo mode
                print(f"❌ SuperSlicer failed: {error_msg}")
                print_time, cost = calculate_demo_estimate(infill, wall_thickness, filename)
                is_demo = True
                flash(f"SuperSlicer failed: {error_msg}. Showing estimated values.", "warning")

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
                    "actual_gcode": actual_gcode_generated,
                    "gcode_path": output_gcode if actual_gcode_generated else None
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
                             actual_gcode=actual_gcode_generated,
                             gcode_path=output_gcode if actual_gcode_generated else None)

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

@app.route("/health")
def health():
    """Health check endpoint with SuperSlicer status"""
    superslicer_available = bool(SUPERSLICER_PATH and os.path.exists(SUPERSLICER_PATH))
    profile_exists = os.path.exists(PROFILE_PATH)
    
    return {
        "status": "healthy", 
        "superslicer_available": superslicer_available,
        "superslicer_path": SUPERSLICER_PATH or "Not found",
        "cost_per_hour": COST_PER_HOUR,
        "profile_exists": profile_exists,
        "upload_folder": UPLOAD_FOLDER,
        "output_folder": OUTPUT_FOLDER
    }

if __name__ == "__main__":
    debug = os.environ.get('DEBUG', 'True').lower() == 'true'
    port = int(os.environ.get('PORT', 5000))
    
    # Startup information with SuperSlicer status
    print("🖨️  3D Print Cost Estimator Starting...")
    print(f"📁 Upload folder: {UPLOAD_FOLDER}")
    print(f"📁 Output folder: {OUTPUT_FOLDER}")
    print(f"📋 Profile path: {PROFILE_PATH}")
    print(f"💰 Cost per hour: ${COST_PER_HOUR}")
    print(f"🔍 Debug mode: {debug}")
    print(f"🌐 Port: {port}")
    print()
    
    # SuperSlicer status check
    if SUPERSLICER_PATH:
        print(f"✅ SuperSlicer found at: {SUPERSLICER_PATH}")
        if os.path.exists(SUPERSLICER_PATH):
            print("✅ SuperSlicer executable exists")
            if os.access(SUPERSLICER_PATH, os.X_OK):
                print("✅ SuperSlicer is executable")
                print("🚀 READY TO GENERATE ACTUAL G-CODE!")
            else:
                print("⚠️  SuperSlicer exists but not executable")
        else:
            print("❌ SuperSlicer path set but file doesn't exist")
    else:
        print("❌ SuperSlicer not found")
        print("⚠️  App will run in DEMO MODE with estimates only")
        print("   To get actual G-code analysis:")
        print("   1. Install SuperSlicer")
        print("   2. Set SUPERSLICER_PATH environment variable")
    
    print()
    
    # Create SuperSlicer profile
    create_superslicer_profile()
    
    app.run(host="0.0.0.0", port=port, debug=debug)
