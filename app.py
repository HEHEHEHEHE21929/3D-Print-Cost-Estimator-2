import os
import re
import shutil
import subprocess
from flask import Flask, render_template, request, redirect, flash, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-in-production")

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
# Prefer environment; fall back to common linux location or name on PATH
SUPERSLICER_PATH = (
    os.environ.get("SUPERSLICER_PATH")
    or shutil.which("superslicer_console")
    or shutil.which("superslicer")
    or "/opt/render/superslicer/superslicer_console"
)
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs(os.path.dirname(PROFILE_PATH) or ".", exist_ok=True)

COST_PER_HOUR = float(os.environ.get("COST_PER_HOUR", 3.0))

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def parse_gcode_stats(gcode_path):
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                l = line.lower()
                if ("estimated" in l and "time" in l) or l.strip().startswith("; estimated") or "print time" in l:
                    m = re.search(r"(\d+)\s*h\s*(\d+)\s*m\s*(\d+)\s*s", line)
                    if m:
                        h, mm, s = map(int, m.groups()); secs = h*3600 + mm*60 + s
                    else:
                        m = re.search(r"(\d+)\s*m\s*(\d+)\s*s", line)
                        if m:
                            mm, s = map(int, m.groups()); secs = mm*60 + s
                        else:
                            m = re.search(r"(\d+):(\d+):(\d+)", line)
                            if m:
                                h, mm, s = map(int, m.groups()); secs = h*3600 + mm*60 + s
                            else:
                                m = re.search(r"TIME:\s*(\d+)", line)
                                if m:
                                    secs = int(m.group(1))
                                else:
                                    continue
                    h = secs // 3600
                    m_ = (secs % 3600) // 60
                    s = secs % 60
                    pretty = f"{h}h {m_}m {s}s" if h else f"{m_}m {s}s"
                    cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                    return pretty, cost
    except FileNotFoundError:
        pass
    return "Error", "$Error"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part"); return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            flash("No selected file"); return redirect(request.url)
        if not allowed_file(file.filename):
            flash("Invalid file type"); return redirect(request.url)

        filename = file.filename
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")

        # If SUPERSLICER_PATH is a path string but not executable, treat as not found
        superslicer_ok = SUPERSLICER_PATH and (shutil.which(SUPERSLICER_PATH) or (os.path.isfile(SUPERSLICER_PATH) and os.access(SUPERSLICER_PATH, os.X_OK)))

        if not superslicer_ok:
            flash("SuperSlicer not found — running in demo/estimate mode", "warning")
            # simple estimate fallback
            try:
                size_kb = os.path.getsize(filepath) / 1024.0
                factor = max(0.5, min(3.0, size_kb / 100.0))
            except Exception:
                factor = 1.0
            base_hours = 1.25 * factor
            infill = int(request.form.get("infill", 20))
            wall_thickness = float(request.form.get("wall_thickness", 0.8))
            infill_factor = 1 + (infill/100.0) * 0.8
            wall_factor = 1 + max(0.0, (wall_thickness - 0.4)/0.4 * 0.3)
            hours = base_hours * infill_factor * wall_factor
            secs = int(hours * 3600)
            h = secs // 3600; m = (secs % 3600)//60; s = secs % 60
            pretty = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            cost = f"${round(hours * COST_PER_HOUR, 2)}"
            return render_template("results.html", print_time=pretty, cost=cost, filename=filename, infill=infill, wall_thickness=wall_thickness, is_estimate=True)

        # Build command variants (some releases use different args)
        create_profile = False
        if not os.path.exists(PROFILE_PATH):
            os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
            with open(PROFILE_PATH, "w", encoding="utf-8") as pf:
                pf.write("[print]\nlayer_height=0.2\nperimeters=3\nfill_density=20\n")
            create_profile = True

        infill = request.form.get("infill", "20")
        wall_thickness = request.form.get("wall_thickness", "0.8")
        try:
            infill_int = int(infill)
            perimeters = max(1, int(float(wall_thickness) / 0.4))
        except Exception:
            infill_int = 20
            perimeters = 2

        variants = [
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--fill-density", f"{infill_int}%", "--perimeters", str(perimeters), filepath, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, filepath, "--export-gcode", "-o", output_gcode],
            [SUPERSLICER_PATH, "--load", PROFILE_PATH, "--output", output_gcode, filepath],
        ]

        last_err = None
        ok = False
        for cmd in variants:
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=int(os.environ.get("SLICE_TIMEOUT", 600)))
                ok = True
                break
            except subprocess.CalledProcessError as e:
                last_err = e.stderr or e.stdout or str(e)
            except subprocess.TimeoutExpired:
                last_err = "timeout"

        if not ok:
            flash(f"Slicing failed: {last_err}. Showing estimate.", "warning")
            return redirect(request.url)

        if os.path.exists(output_gcode) and os.path.getsize(output_gcode) > 100:
            print_time, cost = parse_gcode_stats(output_gcode)
            is_estimate = (print_time == "Error")
            if is_estimate:
                flash("G-code produced but time extraction failed — showing estimate", "warning")
            return render_template("results.html", print_time=print_time, cost=cost, filename=filename, infill=infill_int, wall_thickness=wall_thickness, is_estimate=is_estimate, gcode_path=output_gcode)

        flash("G-code not produced — showing estimate", "warning")
        return redirect(request.url)

    return render_template("index.html")

if __name__ == "__main__":
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    port = int(os.environ.get("PORT", "5000"))
    print("Starting app; SuperSlicer path:", SUPERSLICER_PATH or "Not found")
    app.run(host="0.0.0.0", port=port, debug=debug)
