# ...existing code...
import os
import re
import subprocess
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

UPLOAD_FOLDER = "uploads"
PROFILE_PATH = "profiles/my_config.ini"
SUPERSLICER_PATH = os.environ.get('SUPERSLICER_PATH', r"C:\Users\zetil\Downloads\SuperSlicer_2.5.59.13_win64_240701\SuperSlicer_2.5.59.13_win64_240701\superslicer_console.exe")
ALLOWED_EXTENSIONS = {"stl", "3mf", "obj"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("output", exist_ok=True)

COST_PER_HOUR = 3.0  # $3 per hour
# ...existing code...

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _secs_to_pretty(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"

def parse_time_from_text(text: str):
    """
    Scan given text (superslicer stdout/stderr or gcode) for time estimates and return (pretty, cost)
    or (None, None) if nothing matched.
    """
    if not text:
        return None, None

    text = text.strip()
    # Normalize common separators
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Patterns to try (ordered).
    # Group functions return seconds.
    patterns = [
        (r"estimated printing time[:\s]*([0-9hms\:\s]+)", None),
        (r"estimated time[:\s]*([0-9hms\:\s]+)", None),
        (r"print time[:\s]*([0-9hms\:\s]+)", None),
        (r"total print time[:\s]*([0-9hms\:\s]+)", None),
        (r"total time[:\s]*([0-9hms\:\s]+)", None),
        (r"time[:\s]*([0-9hms\:\s]+)", None),
        (r"TIME[:=]\s*(\d+)", None),  # seconds reported as TIME:1234
    ]

    # Helper to parse a value like "3h 12m 5s" or "192m 30s" or "01:23:45" or "1234"
    def parse_value(val: str):
        val = val.strip()
        # H M S textual
        m = re.search(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", val, re.IGNORECASE)
        if m and any(m.groups()):
            h = int(m.group(1) or 0)
            mm = int(m.group(2) or 0)
            s = int(m.group(3) or 0)
            return h*3600 + mm*60 + s
        # H:MM:SS or MM:SS
        m = re.match(r"^(\d+):(\d+):(\d+)$", val)
        if m:
            return int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
        m = re.match(r"^(\d+):(\d+)$", val)
        if m:
            a = int(m.group(1)); b = int(m.group(2))
            # heuristic: if a>12 treat as H:MM else M:SS
            if a > 12:
                return a*3600 + b*60
            return a*60 + b
        # plain integer -> seconds
        m = re.match(r"^\d+$", val)
        if m:
            return int(val)
        # fallback: find any H M S in the string
        m = re.search(r"(\d+)\s*h", val)
        if m:
            h = int(m.group(1))
        else:
            h = 0
        m2 = re.search(r"(\d+)\s*m", val)
        mm = int(m2.group(1)) if m2 else 0
        m3 = re.search(r"(\d+)\s*s", val)
        s = int(m3.group(1)) if m3 else 0
        if h or mm or s:
            return h*3600 + mm*60 + s
        return None

    # Scan each line for patterns
    for line in lines:
        low = line.lower()
        # direct patterns
        for pat, _ in patterns:
            m = re.search(pat, low, re.IGNORECASE)
            if m:
                val = m.group(1).strip() if m.groups() else None
                if val:
                    secs = parse_value(val)
                    if secs is not None:
                        pretty = _secs_to_pretty(secs)
                        cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                        return pretty, cost
        # If a line contains words and then a time-like token, try to parse last token
        tokens = low.split()
        for tok in reversed(tokens):
            secs = parse_value(tok)
            if secs is not None:
                pretty = _secs_to_pretty(secs)
                cost = f"${round(secs/3600.0 * COST_PER_HOUR, 2)}"
                return pretty, cost

    return None, None

def parse_gcode_stats(gcode_path, superslicer_output=None):
    """
    Try to extract time first from SuperSlicer's stdout/stderr (superslicer_output),
    then from the generated G-code file comments if available.
    Returns (pretty_time, cost) or ("Error", "$Error").
    """
    # 1) Try superslicer output (stdout/stderr)
    if superslicer_output:
        pretty, cost = parse_time_from_text(superslicer_output)
        if pretty:
            return pretty, cost

    # 2) Try parsing G-code file comments
    try:
        with open(gcode_path, "r", encoding="utf-8", errors="ignore") as f:
            # read first 200-500 lines where metadata usually lives
            for i, line in enumerate(f):
                if i > 500:
                    break
                if line.lstrip().startswith(";") or "estimated" in line.lower() or "time" in line.lower():
                    pretty, cost = parse_time_from_text(line)
                    if pretty:
                        return pretty, cost
    except FileNotFoundError:
        return "Error", "$Error"
    except Exception:
        return "Error", "$Error"

    return "Error", "$Error"
# ...existing code...

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Check if file is in the request
        if "file" not in request.files:
            flash("No file part")
            return redirect(request.url)
        
        file = request.files["file"]
        if file.filename == "":
            flash("No selected file")
            return redirect(request.url)
        
        if file and allowed_file(file.filename):
            filename = file.filename
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(filepath)
            
            # Generate output path
            output_gcode = os.path.join("output", filename.rsplit(".", 1)[0] + ".gcode")
            
            # Build SuperSlicer command
            cmd = [
                SUPERSLICER_PATH,
                "--load", PROFILE_PATH,
                filepath,
                "--export-gcode",
                "-o", output_gcode
            ]
            
            try:
                # capture stdout/stderr
                result = subprocess.run(cmd, check=True, capture_output=True, text=True)
                combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
                
                # Prefer what SuperSlicer printed to console
                print_time, cost = parse_gcode_stats(output_gcode, superslicer_output=combined_output)
                
                # If parsing failed (Error), try to parse raw combined output directly
                if print_time == "Error" or print_time is None:
                    p2, c2 = parse_time_from_text(combined_output)
                    if p2:
                        print_time, cost = p2, c2
                    else:
                        print_time, cost = "Error", "$Error"
                
                return render_template("results.html", print_time=print_time, cost=cost, filename=filename)
            except subprocess.CalledProcessError as e:
                # On failure, still attempt to extract time from stderr/stdout
                combined = (e.stdout or "") + "\n" + (e.stderr or "")
                p, c = parse_time_from_text(combined)
                if p:
                    return render_template("results.html", print_time=p, cost=c, filename=filename)
                flash(f"Error processing file: {e}")
                return redirect(request.url)
        else:
            flash("Invalid file type")
            return redirect(request.url)
    
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=True)
# ...existing code...
