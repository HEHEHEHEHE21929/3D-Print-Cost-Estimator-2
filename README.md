# 3D Print Cost Estimator

A Flask web app for estimating 3D print costs and times using SuperSlicer. Users upload 3D models, get instant estimates, and are directed to Shopify for ordering.

## Features

- Upload STL, 3MF, or OBJ files
- Set infill and wall thickness
- Automatic slicing and G-code generation (server-side, no download needed)
- Print time and cost estimation
- Direct link to Shopify for order placement

## Setup

### Prerequisites

- Python 3.8+
- SuperSlicer (CLI version, not included in repo)

### Installation

1. Clone this repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Download SuperSlicer CLI from [SuperSlicer releases](https://github.com/supermerill/SuperSlicer/releases) and place the executable in your project folder (or use a build script for deployment).
4. Set the `SUPERSLICER_PATH` environment variable to the SuperSlicer executable location, or edit the path in `app.py`.

### Running the App

```bash
python app.pywha
```

For production (Linux/Render):
- Use a build script to download and extract SuperSlicer CLI.
- Set `SUPERSLICER_PATH` to the correct path (e.g., `/opt/render/project/src/superslicer_console`).

### Environment Variables

| Variable           | Description                       | Default                        |
|--------------------|-----------------------------------|--------------------------------|
| SUPERSLICER_PATH   | Path to SuperSlicer executable    | See `app.py`                   |
| SECRET_KEY         | Flask secret key                  | dev-key-change-in-production   |
| PORT               | Server port                       | 5000                           |
| DEBUG              | Debug mode                        | True                           |

## Usage

1. Upload your 3D model and adjust settings.
2. View the estimated print time and cost.
3. Click the Shopify link to place your order.

## File Structure

- `app.py` — Main Flask app
- `templates/` — HTML templates
- `static/` — CSS styles
- `uploads/` — Uploaded files
- `output/` — Generated G-code files
- `profiles/` — SuperSlicer config profiles

## SuperSlicer Profile

- Place a valid SuperSlicer config at `profiles/my_config.ini`.
- Create/export this file from SuperSlicer.

## Deployment (Render Example)

1. Add a build script to download SuperSlicer CLI in your Render dashboard or `render.yaml`:
   ```sh
   wget https://github.com/supermerill/SuperSlicer/releases/download/2.5.59.13/SuperSlicer-2.5.59.13-linux.tar.xz
   tar -xf SuperSlicer-2.5.59.13-linux.tar.xz
   mv SuperSlicer-2.5.59.13-linux/superslicer_console /opt/render/project/src/superslicer_console
   chmod +x /opt/render/project/src/superslicer_console
   ```
2. Set `SUPERSLICER_PATH` to `/opt/render/project/src/superslicer_console`.

## Troubleshooting

- **SuperSlicer not found:** Check `SUPERSLICER_PATH`.
- **Slicing fails:** Verify your SuperSlicer profile and uploaded file.
- **Python errors:** Run `pip install -r requirements.txt`.

## License

Provided as-is for educational and commercial use.