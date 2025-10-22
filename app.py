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
