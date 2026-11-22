# ComfyUI-hypbr

ComfyUI custom nodes and ready-to-run workflows for [**hypbr**](https://github.com/wrong-nebula/hypbr) (using the [`cfryant/hypbr`](https://github.com/cfryant/hypbr) fork with 17-map seamless cutout opacity + sRGB emissive glow, `--transparency auto|on|off`, and `--emissive auto|on|off` gating).

Generate **17 seamlessly tileable PBR maps** from a single photo or 75+ procedural materials, inspect them live in 3D via [**ComfyUI-Texture-Simple**](https://github.com/gokayfem/ComfyUI-Texture-Simple) (**Texture Viewer Pro**), render deterministic Three.js turntable / light-orbit MP4 preview videos, and export directly to glTF/GLB, Blender, Unreal, Unity, Godot, MaterialX, USD, and Vulkan.

## Features

- **5 ComfyUI nodes** (zero extra heavy packages inside ComfyUI's own Python env — runs `hypbr` via a configurable `HYPBR_PYTHON` subprocess or auto-detected venv):
  1. **hypbr Photo to PBR**: Convert any surface photo (`IMAGE`) into 17 seamless PBR maps (`albedo`, `normal`, `bent_normal`, `height`, `height16`, `ao`, `roughness`, `metallic`, `orm`, `curvature`, `cavity`, `edge_wear`, `anisotropy`, `specular`, `thickness`, `opacity`, `emissive`, `preview`, `material_dir`).
  2. **hypbr Procedural Material**: Synthesize any of `hypbr`'s 75+ seamless procedural materials (including `chainlink_fence_galvanized` with real over/under diamond wire weave, rust streaks, and cutout `opacity.png`, plus `hardops_plate`).
  3. **hypbr Blend**: Physically blend two material folders via `height`, `cavity`, or `linear` mode with recomputed HBAO, curvature, ORM, and gated opacity.
  4. **hypbr Render Preview Video**: Deterministic headless Three.js studio preview (`turntable`, `lightorbit`, `closeup_displacement`, `tiling_zoom`, `channel_split`) with a **Mesh** dropdown (`auto`, `sphere`, `cube`, `cylinder`, `plane`) that applies to every shot. `auto` (default) keeps the current studio layout — a framed fence panel for cutouts, or a sphere + swatch otherwise. `plane` is a finely subdivided displaced plane viewed at ~40° with a grazing light so relief (and cutout shadows on the studio wall/floor) reads clearly. Outputs an MP4 plus an `IMAGE` frame batch for `CreateVideo` / `SaveVideo` or `VHS_VideoCombine`.
  5. **hypbr Export**: Package any material folder for `glb`, `gltf`, `blender`, `unreal`, `unity`, `unity-hdrp`, `godot`, `threejs`, `materialx`, `usd`, `vulkan`, or `all`.

- **Smart `transparency: auto | on | off` gating**:
  - `auto` *(default)*: Only emits `opacity.png` and glTF `alphaMode: "MASK"` / `doubleSided: true` when the material is a transparent preset (e.g. `chainlink_fence_galvanized`) or has `> 0.5%` transparent pixels (`< 0.5` alpha). All opaque materials stay strictly opaque (`RGB` base color, no `alphaMode: MASK`).
  - `on`: Always emits `opacity.png` and cutout export flags.
  - `off`: Forces opaque output everywhere (even for `chainlink_fence_galvanized`).

- **Smart `emissive: auto | on | off` gating & glowing presets**:
  - `auto` *(default)*: Only emits `emissive.png` and engine emissive slots (`emissiveTexture` + `emissiveFactor`, plus `KHR_materials_emissive_strength` when `emissive_strength > 1.0`) when the material is an emissive preset (or blend containing one) with `> 0.1%` glowing pixels. Non-emissive materials never emit `emissive.png` or an emissive slot.
  - `on`: Always emits `emissive.png` (black if there is no glow) and export slots.
  - `off`: Disables emissive output everywhere.
  - Includes 6 opaque glowing materials (`cooling_lava_cracks`, `scifi_panel_emissive`, `circuit_board_glow`, `glowing_charcoal_embers`, `carved_runic_stone`, `neon_brick_wall`) with zero transparency, plus automatic studio-light dimming during `lightorbit` video previews so glow reads clearly.
  - **Note on Texture Viewer Pro**: `ComfyUI-Texture-Simple` (`Texture Viewer Pro`) does not expose an `emissive_map` input port (it supports `color`, `displacement`, `normal`, `ao`, `metalness`, `roughness`, and `alpha`). Inspect the `emissive` output image or use **hypbr Render Preview Video** / **hypbr Export** (`.glb`) to see emissive glow in 3D.

- **Bundled 3D preview meshes (`assets/`)**:
  - `assets/cylinder.glb` (UV-mapped cylinder)
  - `assets/rounded_cube.glb` (UV-mapped beveled cube)
  Load either file in **Texture Viewer Pro** using its **Load GLB** button. Texture Viewer Pro's own **Mesh** dropdown already includes Sphere, Cube, Torus, Plane, and Showcase (workflows leave the default on Sphere).

- **CLI mesh flag** (same options as the Comfy node, works with every shot):
  ```bash
  python video_preview/render_demo.py ./my_material --mesh plane --shot lightorbit --seconds 4 --out plane.mp4
  ```

---

## Installation

### 1. Clone `ComfyUI-hypbr` and the recommended 3D viewer (`ComfyUI-Texture-Simple`)
Inside your `ComfyUI/custom_nodes/` folder:

```bash
git clone https://github.com/cfryant/ComfyUI-hypbr.git
git clone https://github.com/gokayfem/ComfyUI-Texture-Simple.git
```

> **Credit:** Interactive in-canvas 3D preview is provided by [**ComfyUI-Texture-Simple**](https://github.com/gokayfem/ComfyUI-Texture-Simple) (`Texture Viewer Pro`, MIT, numpy + Pillow only).

### 2. Install `hypbr` (isolated venv recommended — keeps ComfyUI's venv untouched)

**Option A — Separate venv (recommended):**
```bash
python -m venv /path/to/hypbr-venv
# Activate it, then install the cfryant/hypbr fork + playwright (for video preview):
pip install "git+https://github.com/cfryant/hypbr.git" playwright
```
For **hypbr Photo to PBR**, also install PyTorch (CUDA) and Apple Depth Pro 2 (`pip install timm safetensors huggingface-hub git+https://github.com/apple-aiml-research/ml-depth-pro-2.git`), plus `ffmpeg` on `PATH` (or `pip install imageio-ffmpeg`) for video rendering.

Then tell `ComfyUI-hypbr` which Python to use — either set the environment variable:
```bash
export HYPBR_PYTHON=/path/to/hypbr-venv/bin/python   # Windows: C:\...\venv\Scripts\python.exe
```
or create a local gitignored `ComfyUI-hypbr/hypbr_config.json`:
```json
{
  "python_path": "/path/to/hypbr-venv/Scripts/python.exe",
  "ffmpeg_path": "/path/to/ffmpeg"
}
```
*(If `hypbr` is installed inside `ComfyUI-hypbr/.venv`, on `PATH`, or in ComfyUI's own Python environment, `ComfyUI-hypbr` auto-detects it automatically with no config needed.)*

**Option B — Directly inside ComfyUI's Python environment:**
```bash
pip install "git+https://github.com/cfryant/hypbr.git" playwright
```

### 3. Open the included workflows
Copy (or open) the two annotated workflows from `workflows/`:
- `workflows/hypbr Photo to PBR + Preview Video.json`
- `workflows/hypbr Procedural Material + Preview Video.json`

Restart ComfyUI, open **Workflows**, and run!

## Workflow Previews

| Photo to PBR + Texture Viewer Pro + Video | Procedural Chain-Link Cutout + Light-Orbit |
| --- | --- |
| ![Photo to PBR](workflows/hypbr%20Photo%20to%20PBR%20%2B%20Preview%20Video.png) | ![Procedural Cutout](workflows/hypbr%20Procedural%20Material%20%2B%20Preview%20Video.png) |

---

## Licensing & Depth Pro 2 Weights Notice

- **`ComfyUI-hypbr`** and **`hypbr` procedural synthesis** are **MIT-licensed**, building on [`wrong-nebula/hypbr`](https://github.com/wrong-nebula/hypbr).
- **Apple Depth Pro 2 weights** (`depthpro2-s`) used by **hypbr Photo to PBR** are **not bundled** in this repository — they are downloaded automatically on first use under Apple's **non-commercial research license (`apple-amlr`)**. Procedural synthesis (`hypbr Procedural Material`) uses no Depth Pro 2 weights and is 100% MIT.
