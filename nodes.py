"""ComfyUI custom nodes wrapping hypbr (https://github.com/cfryant/hypbr).

Runs hypbr and the headless Three.js video renderer in a configurable Python
environment (via HYPBR_PYTHON, local hypbr_config.json, .venv, or PATH) so
ComfyUI's own Python environment needs no extra heavy dependencies.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image

try:
    import folder_paths  # type: ignore
except ImportError:  # Standalone testing outside ComfyUI
    folder_paths = None

NODE_PACK_DIR = Path(__file__).resolve().parent
RENDER_DEMO_PY = NODE_PACK_DIR / "video_preview" / "render_demo.py"
CONFIG_PATH = NODE_PACK_DIR / "hypbr_config.json"

PBR_MAP_SPECS: tuple[tuple[str, str, float], ...] = (
    ("albedo", "albedo_delit.png", 0.5),
    ("normal", "normal.png", 0.5),
    ("bent_normal", "bent_normal.png", 0.5),
    ("height", "height.png", 0.5),
    ("height16", "height16.png", 0.5),
    ("ao", "ao.png", 1.0),
    ("roughness", "roughness.png", 0.5),
    ("metallic", "metallic.png", 0.0),
    ("orm", "orm.png", 0.5),
    ("curvature", "curvature.png", 0.5),
    ("cavity", "cavity.png", 1.0),
    ("edge_wear", "edge_wear.png", 0.0),
    ("anisotropy", "anisotropy.png", 0.5),
    ("specular", "specular.png", 0.5),
    ("thickness", "thickness.png", 0.5),
    ("opacity", "opacity.png", 1.0),
    ("preview", "preview.png", 0.2),
)

MATERIAL_CATALOG: tuple[str, ...] = (
    "chainlink_fence_galvanized",
    "hardops_plate",
    "acoustic_kerf_felt",
    "acoustic_pegboard",
    "acoustic_pyramid_foam",
    "acoustic_slat_wood",
    "brushed_anodized_plate",
    "burlap_jute_sackcloth",
    "cable_knit_wool",
    "carbon_fiber_weave",
    "carrara_veined_marble",
    "cast_iron_skillet_pebble",
    "chainlink_wire_mesh",
    "circular_perf_sheet",
    "corkboard_pin_surface",
    "corrugated_cardboard",
    "corrugated_sheet_metal",
    "damascus_folded_steel",
    "diamond_tread_plate",
    "dimpled_golf_grip",
    "drip_tray_sieve_grate",
    "embossed_braille_strip",
    "expanded_metal_mesh",
    "fine_asphalt_tarmac",
    "fluted_reeded_glass",
    "frosted_prism_diffuser",
    "gilded_tin_ceiling_tile",
    "hammered_copper_dimple",
    "heatsink_extrusion_fins",
    "herringbone_tweed_wool",
    "hex_mosaic_tile",
    "hvac_slotted_vent",
    "keyboard_keycap_grid",
    "knurled_metal_grip",
    "knurled_straight_flutes",
    "mermaid_fishscale_tile",
    "micro_hex_grip",
    "modular_stud_grid",
    "patinated_verdigris_bronze",
    "pcb_solder_mask_traces",
    "penny_round_mosaic",
    "perforated_leather_auto",
    "quilted_leather_stitch",
    "reeded_coin_milling",
    "ribbed_corduroy_velvet",
    "ribbed_rubber_mat",
    "riveted_sheet_seam",
    "rubber_tread_stair",
    "sidewalk_expansion_joint",
    "solar_cell_grid",
    "speaker_mesh_hex",
    "stair_nosing_grooves",
    "stamped_louver_micro",
    "stamped_vent_slots_stagger",
    "street_storm_grate",
    "subway_metro_tile",
    "terrazzo_speckled_stone",
    "terry_velcro_loops",
    "travertine_porous_stone",
    "tufted_emerald_velvet",
    "venetian_plaster_trowel",
    "waffle_cotton_linen",
    "waffle_radiator_fins",
    "waffle_silicone_pad",
    "window_blinds_slats",
    "wood_parquet_herringbone",
    "woven_bamboo_tatami",
    "woven_denim_twill",
    "woven_nylon_strap",
    "woven_rattan_cane",
)

PHOTO_PRESETS: tuple[str, ...] = (
    "v13-default",
    "glazed-tile",
    "carved-stone",
    "ornamental-iron",
    "weathered-timber",
    "hardops-plate",
)

BLEND_MODES: tuple[str, ...] = ("height", "cavity", "linear")

VIDEO_SHOTS: tuple[str, ...] = (
    "turntable",
    "lightorbit",
    "closeup_displacement",
    "tiling_zoom",
    "channel_split",
)

VIDEO_RESOLUTIONS: tuple[str, ...] = (
    "1280x720",
    "1920x1080",
    "960x540",
    "768x512",
    "512x512",
)

TRANSPARENCY_MODES: tuple[str, ...] = ("auto", "on", "off")

EXPORT_TARGETS: tuple[str, ...] = (
    "glb",
    "gltf",
    "all",
    "blender",
    "unreal",
    "unity",
    "unity-hdrp",
    "godot",
    "threejs",
    "materialx",
    "usd",
    "vulkan",
)


def _load_local_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _get_output_root() -> Path:
    if folder_paths is not None:
        return Path(folder_paths.get_output_directory())
    return Path.cwd() / "output"


def _sanitize_name(raw: str, fallback: str = "material") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (raw or "").strip()).strip("._-")
    return cleaned or fallback


def _next_numbered_dir(parent: Path, stem: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_name(stem)
    for idx in range(1, 10000):
        cand = parent / f"{safe}_{idx:04d}"
        if not cand.exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    raise RuntimeError(f"Too many output folders under {parent} for {safe}")


def _next_numbered_file(parent: Path, stem: str, ext: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_name(stem)
    ext = ext if ext.startswith(".") else f".{ext}"
    for idx in range(1, 10000):
        cand = parent / f"{safe}_{idx:04d}{ext}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"Too many output files under {parent} for {safe}{ext}")


def _resolve_hypbr_python() -> list[str]:
    """Locate the Python interpreter (or hypbr CLI) used to run hypbr."""
    cfg = _load_local_config()
    candidates = [
        os.environ.get("HYPBR_PYTHON", "").strip(),
        str(cfg.get("python_path", "")).strip(),
        str(NODE_PACK_DIR / ".venv" / "Scripts" / "python.exe"),
        str(NODE_PACK_DIR / ".venv" / "bin" / "python"),
        str(NODE_PACK_DIR / "venv" / "Scripts" / "python.exe"),
        str(NODE_PACK_DIR / "venv" / "bin" / "python"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return [cand, "-m", "hypbr.cli"]

    hypbr_cli = shutil.which("hypbr")
    if hypbr_cli:
        cli_path = Path(hypbr_cli).resolve()
        for py_name in ("python.exe", "python3", "python"):
            sib = cli_path.parent / py_name
            if sib.exists():
                return [str(sib), "-m", "hypbr.cli"]
        return [str(cli_path)]

    if importlib.util.find_spec("hypbr") is not None:
        return [sys.executable, "-m", "hypbr.cli"]

    raise RuntimeError(
        "Could not locate hypbr. Install https://github.com/cfryant/hypbr into a "
        "dedicated venv and set HYPBR_PYTHON (or create ComfyUI-hypbr/hypbr_config.json)."
    )


def _resolve_python_exe() -> str:
    cmd = _resolve_hypbr_python()
    if len(cmd) >= 3 and cmd[1] == "-m":
        return cmd[0]
    if importlib.util.find_spec("playwright") is not None:
        return sys.executable
    return sys.executable


def _resolve_ffmpeg() -> str:
    cfg = _load_local_config()
    candidates = [
        os.environ.get("HYPBR_FFMPEG", "").strip(),
        str(cfg.get("ffmpeg_path", "")).strip(),
        shutil.which("ffmpeg") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise RuntimeError(
        "ffmpeg not found. Install ffmpeg on PATH or set HYPBR_FFMPEG / ffmpeg_path in hypbr_config.json."
    )


def _build_subprocess_env(scratch_root: Path) -> dict[str, str]:
    cfg = _load_local_config()
    env = os.environ.copy()

    tmp_dir = Path(str(cfg.get("temp_dir") or (scratch_root / "_tmp")))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env.setdefault("TEMP", str(tmp_dir))
    env.setdefault("TMP", str(tmp_dir))
    if cfg.get("temp_dir"):
        env["TEMP"] = str(tmp_dir)
        env["TMP"] = str(tmp_dir)

    for cfg_key, env_key in (
        ("cache_dir", "HYPBR_CACHE_DIR"),
        ("torch_home", "TORCH_HOME"),
        ("hf_home", "HF_HOME"),
        ("ffmpeg_path", "HYPBR_FFMPEG"),
        ("browser_path", "HYPBR_BROWSER"),
    ):
        val = str(cfg.get(cfg_key, "")).strip()
        if val:
            env[env_key] = val

    extra_bins: list[str] = []
    if env.get("HYPBR_FFMPEG"):
        extra_bins.append(str(Path(env["HYPBR_FFMPEG"]).resolve().parent))
    py_cmd = _resolve_hypbr_python()
    if py_cmd:
        extra_bins.append(str(Path(py_cmd[0]).resolve().parent))
    if extra_bins:
        env["PATH"] = os.pathsep.join(extra_bins + [env.get("PATH", "")])
    return env


def _run_subprocess(cmd: list[str], env: dict[str, str], cwd: Path | None = None) -> str:
    kwargs: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {' '.join(cmd)}\n{proc.stdout or ''}"
        )
    return proc.stdout or ""


def _load_image_tensor(path: Path, default_fill: float, fallback_size: tuple[int, int]) -> torch.Tensor:
    if path.exists():
        with Image.open(path) as im:
            if im.mode in ("I;16", "I;16B", "I;16L", "I"):
                arr = np.asarray(im, dtype=np.float32)
                max_val = 65535.0 if arr.max() > 255.0 or im.mode.startswith("I;16") else 255.0
                arr = np.clip(arr / max_val, 0.0, 1.0)
                if arr.ndim == 2:
                    arr = np.repeat(arr[..., None], 3, axis=-1)
                else:
                    arr = arr[..., :3]
            else:
                arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
        return torch.from_numpy(np.ascontiguousarray(arr[None, ...]))

    w, h = fallback_size
    arr = np.full((1, h, w, 3), float(default_fill), dtype=np.float32)
    return torch.from_numpy(arr)


def _load_all_maps(mat_dir: Path) -> tuple[torch.Tensor, ...]:
    fallback_size = (512, 512)
    albedo_p = mat_dir / "albedo_delit.png"
    if albedo_p.exists():
        with Image.open(albedo_p) as im:
            fallback_size = im.size
    tensors = [
        _load_image_tensor(mat_dir / fname, fill, fallback_size)
        for _port, fname, fill in PBR_MAP_SPECS
    ]
    return tuple(tensors)


def _decode_mp4_to_tensor(mp4_path: Path, width: int, height: int, env: dict[str, str]) -> torch.Tensor:
    ffmpeg_bin = _resolve_ffmpeg()
    cmd = [
        ffmpeg_bin,
        "-v", "error",
        "-i", str(mp4_path),
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-",
    ]
    kwargs: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg frame decode failed ({mp4_path}): {err}")

    frame_bytes = width * height * 3
    raw = proc.stdout
    n_frames = len(raw) // frame_bytes
    if n_frames <= 0:
        raise RuntimeError(f"Decoded 0 frames from {mp4_path} ({width}x{height})")
    usable = raw[: n_frames * frame_bytes]
    arr = np.frombuffer(usable, dtype=np.uint8).reshape(n_frames, height, width, 3)
    return torch.from_numpy(np.ascontiguousarray(arr.astype(np.float32) / 255.0))


MAP_RETURN_TYPES = tuple(["IMAGE"] * len(PBR_MAP_SPECS) + ["STRING"])
MAP_RETURN_NAMES = tuple([spec[0] for spec in PBR_MAP_SPECS] + ["material_dir"])


class HypbrProceduralMaterial:
    CATEGORY = "hypbr"
    DESCRIPTION = (
        "Synthesize a seamless 16-map procedural PBR material (including cutout opacity "
        "for chainlink_fence_galvanized) into ComfyUI's output folder."
    )
    RETURN_TYPES = MAP_RETURN_TYPES
    RETURN_NAMES = MAP_RETURN_NAMES
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "material": (
                    list(MATERIAL_CATALOG),
                    {
                        "default": "chainlink_fence_galvanized",
                        "tooltip": "Pick any built-in hypbr material preset. chainlink_fence_galvanized includes real transparent cutout holes.",
                    },
                ),
                "size": (
                    "INT",
                    {
                        "default": 512,
                        "min": 128,
                        "max": 4096,
                        "step": 64,
                        "tooltip": "Square texture resolution in pixels (512 is fast for previews; 1024 or 2048 for final assets).",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": 42,
                        "min": 0,
                        "max": 2147483647,
                        "tooltip": "Random seed controlling pattern variation, rust placement, and micro-detail.",
                    },
                ),
                "wear": (
                    "FLOAT",
                    {
                        "default": 0.45,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.05,
                        "tooltip": "Surface aging/weathering intensity from 0.0 (factory fresh) to 1.0 (heavily weathered).",
                    },
                ),
                "transparency": (
                    list(TRANSPARENCY_MODES),
                    {
                        "default": "auto",
                        "tooltip": "auto = write opacity.png only for cutout materials (>0.5% holes); on = always write opacity.png; off = force opaque.",
                    },
                ),
                "output_name": (
                    "STRING",
                    {
                        "default": "proc",
                        "tooltip": "Subfolder prefix created inside ComfyUI/output/hypbr/.",
                    },
                ),
            }
        }

    def run(
        self,
        material: str,
        size: int,
        seed: int,
        wear: float,
        transparency: str,
        output_name: str,
    ) -> tuple[Any, ...]:
        out_root = _get_output_root() / "hypbr"
        run_dir = _next_numbered_dir(out_root, f"{output_name}_{material}")
        env = _build_subprocess_env(out_root)
        cmd = _resolve_hypbr_python() + [
            "synth",
            material,
            "--size", str(int(size)),
            "--seed", str(int(seed)),
            "--wear", f"{float(wear):.3f}",
            "--transparency", transparency,
            "--out", str(run_dir),
        ]
        _run_subprocess(cmd, env=env)
        mat_dir = run_dir / material
        if not (mat_dir / "albedo_delit.png").exists():
            raise RuntimeError(f"hypbr synth did not produce maps in {mat_dir}")
        return (*_load_all_maps(mat_dir), str(mat_dir))


class HypbrPhotoToPBR:
    CATEGORY = "hypbr"
    DESCRIPTION = (
        "Convert a single surface photo into 16 seamless PBR maps using Apple Depth Pro 2 "
        "and hypbr's 47-node PBR graph. Note: Depth Pro 2 weights are Apple non-commercial."
    )
    RETURN_TYPES = MAP_RETURN_TYPES
    RETURN_NAMES = MAP_RETURN_NAMES
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": (
                    "IMAGE",
                    {"tooltip": "Input surface photograph (first image in batch is used)."},
                ),
                "preset": (
                    list(PHOTO_PRESETS),
                    {
                        "default": "carved-stone",
                        "tooltip": "47-node PBR graph tuning preset matched to your surface type.",
                    },
                ),
                "tileable": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Wrap edges circularly so the output maps tile seamlessly.",
                    },
                ),
                "size": (
                    "INT",
                    {
                        "default": 512,
                        "min": 128,
                        "max": 2048,
                        "step": 64,
                        "tooltip": "Target square resolution. Resizes input cleanly before PBR synthesis.",
                    },
                ),
                "output_name": (
                    "STRING",
                    {
                        "default": "photo_pbr",
                        "tooltip": "Subfolder prefix created inside ComfyUI/output/hypbr/.",
                    },
                ),
            },
            "optional": {
                "normal_convention": (
                    ["OpenGL (+Y)", "DirectX (-Y)"],
                    {
                        "default": "OpenGL (+Y)",
                        "tooltip": "OpenGL (+Y) for Blender/Three.js/Godot/Unity; DirectX (-Y) for Unreal.",
                    },
                ),
                "transparency": (
                    list(TRANSPARENCY_MODES),
                    {
                        "default": "auto",
                        "tooltip": "auto = opaque unless >0.5% alpha holes exist; on = always write opacity.png; off = force opaque.",
                    },
                ),
            },
        }

    def run(
        self,
        image: torch.Tensor,
        preset: str,
        tileable: bool,
        size: int,
        output_name: str,
        normal_convention: str = "OpenGL (+Y)",
        transparency: str = "auto",
    ) -> tuple[Any, ...]:
        out_root = _get_output_root() / "hypbr"
        run_dir = _next_numbered_dir(out_root, output_name)
        env = _build_subprocess_env(out_root)

        arr = np.clip(image[0].detach().cpu().numpy(), 0.0, 1.0)
        pil_im = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), mode="RGB")
        target_sz = int(size)
        if target_sz > 0 and pil_im.size != (target_sz, target_sz):
            pil_im = pil_im.resize((target_sz, target_sz), Image.LANCZOS)

        stem = _sanitize_name(output_name, "photo")
        in_photo = run_dir / f"{stem}.png"
        pil_im.save(in_photo)

        cmd = _resolve_hypbr_python() + [
            "photo",
            str(in_photo),
            "--out", str(run_dir),
            "--preset", preset,
            "--transparency", transparency,
        ]
        if tileable:
            cmd.append("--tileable")
        if normal_convention == "DirectX (-Y)":
            cmd.append("--dx")

        _run_subprocess(cmd, env=env)
        mat_dir = run_dir / stem
        if not (mat_dir / "albedo_delit.png").exists():
            # Fallback if hypbr wrote directly into run_dir
            if (run_dir / "albedo_delit.png").exists():
                mat_dir = run_dir
            else:
                raise RuntimeError(f"hypbr photo did not produce maps in {mat_dir}")
        return (*_load_all_maps(mat_dir), str(mat_dir))


class HypbrBlend:
    CATEGORY = "hypbr"
    DESCRIPTION = (
        "Physically blend two hypbr material folders using height-lerp, cavity-mask, or linear "
        "blending, recomputing AO, curvature, ORM, and opacity."
    )
    RETURN_TYPES = MAP_RETURN_TYPES
    RETURN_NAMES = MAP_RETURN_NAMES
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "material_dir_a": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Path to base material folder A (wire from material_dir or paste path).",
                    },
                ),
                "material_dir_b": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Path to overlay material folder B (wire from material_dir or paste path).",
                    },
                ),
                "mode": (
                    list(BLEND_MODES),
                    {
                        "default": "height",
                        "tooltip": "height = Unreal-style height lerp; cavity = settle B into A's crevices; linear = uniform mix.",
                    },
                ),
                "factor": (
                    "FLOAT",
                    {
                        "default": 0.45,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.05,
                        "tooltip": "Blend amount of Material B over Material A (0.0 = all A, 1.0 = all B).",
                    },
                ),
                "contrast": (
                    "FLOAT",
                    {
                        "default": 0.15,
                        "min": 0.01,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": "Sharpness of the height/cavity transition edge (lower = crisper transition).",
                    },
                ),
                "transparency": (
                    list(TRANSPARENCY_MODES),
                    {
                        "default": "auto",
                        "tooltip": "auto = carry opacity only when a source has active cutout holes; on = always write opacity.png; off = force opaque.",
                    },
                ),
                "output_name": (
                    "STRING",
                    {
                        "default": "blended",
                        "tooltip": "Subfolder prefix created inside ComfyUI/output/hypbr/.",
                    },
                ),
            }
        }

    def run(
        self,
        material_dir_a: str,
        material_dir_b: str,
        mode: str,
        factor: float,
        contrast: float,
        transparency: str,
        output_name: str,
    ) -> tuple[Any, ...]:
        dir_a = Path(material_dir_a.strip())
        dir_b = Path(material_dir_b.strip())
        if not (dir_a / "albedo_delit.png").exists():
            raise FileNotFoundError(f"Material A missing albedo_delit.png: {dir_a}")
        if not (dir_b / "albedo_delit.png").exists():
            raise FileNotFoundError(f"Material B missing albedo_delit.png: {dir_b}")

        out_root = _get_output_root() / "hypbr"
        mat_dir = _next_numbered_dir(out_root, output_name)
        env = _build_subprocess_env(out_root)
        cmd = _resolve_hypbr_python() + [
            "blend",
            str(dir_a),
            str(dir_b),
            "--out", str(mat_dir),
            "--mode", mode,
            "--factor", f"{float(factor):.3f}",
            "--contrast", f"{float(contrast):.3f}",
            "--transparency", transparency,
        ]
        _run_subprocess(cmd, env=env)
        return (*_load_all_maps(mat_dir), str(mat_dir))


class HypbrRenderPreviewVideo:
    CATEGORY = "hypbr"
    DESCRIPTION = (
        "Render a deterministic Three.js PBR studio preview video (MP4 + IMAGE frames batch). "
        "Supports cutout shadow casting for materials with opacity.png."
    )
    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("mp4_path", "frames")
    OUTPUT_NODE = True
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "material_dir": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Path to a synthesized hypbr material folder (connect from material_dir).",
                    },
                ),
                "shot": (
                    list(VIDEO_SHOTS),
                    {
                        "default": "lightorbit",
                        "tooltip": "Camera/lighting preset. lightorbit sweeps shadows across the floor/wall; turntable spins the camera.",
                    },
                ),
                "seconds": (
                    "FLOAT",
                    {
                        "default": 2.0,
                        "min": 1.0,
                        "max": 30.0,
                        "step": 0.5,
                        "tooltip": "Preview duration in seconds (2s is fast for quick checks; 4-6s for showcase clips).",
                    },
                ),
                "fps": (
                    "INT",
                    {
                        "default": 24,
                        "min": 8,
                        "max": 60,
                        "step": 1,
                        "tooltip": "Frames per second of the rendered preview.",
                    },
                ),
                "resolution": (
                    list(VIDEO_RESOLUTIONS),
                    {
                        "default": "960x540",
                        "tooltip": "Output frame resolution (WxH).",
                    },
                ),
                "show_label": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Show the material folder name pill in the bottom-left corner.",
                    },
                ),
                "transparency": (
                    list(TRANSPARENCY_MODES),
                    {
                        "default": "auto",
                        "tooltip": "auto = use cutout fence scene only when opacity.png has >0.5% transparent holes; on = force cutout; off = force opaque sphere+swatch.",
                    },
                ),
                "output_name": (
                    "STRING",
                    {
                        "default": "preview",
                        "tooltip": "MP4 filename prefix saved under ComfyUI/output/hypbr_previews/.",
                    },
                ),
            }
        }

    def run(
        self,
        material_dir: str,
        shot: str,
        seconds: float,
        fps: int,
        resolution: str,
        show_label: bool,
        transparency: str,
        output_name: str,
    ) -> dict[str, Any]:
        mat_dir = Path(material_dir.strip())
        if not (mat_dir / "albedo_delit.png").exists():
            raise FileNotFoundError(f"Expected albedo_delit.png in material_dir: {mat_dir}")
        if not RENDER_DEMO_PY.exists():
            raise FileNotFoundError(f"Missing Three.js renderer script: {RENDER_DEMO_PY}")

        parts = resolution.lower().split("x")
        width = int(parts[0]) - (int(parts[0]) % 2)
        height = int(parts[1]) - (int(parts[1]) % 2)

        prev_root = _get_output_root() / "hypbr_previews"
        mp4_path = _next_numbered_file(prev_root, f"{output_name}_{shot}", ".mp4")
        env = _build_subprocess_env(_get_output_root() / "hypbr")
        # Ensure ffmpeg is discoverable inside render_demo.py
        env.setdefault("HYPBR_FFMPEG", _resolve_ffmpeg())

        cmd = [
            _resolve_python_exe(),
            str(RENDER_DEMO_PY),
            str(mat_dir),
            "--shot", shot,
            "--seconds", f"{float(seconds):.2f}",
            "--fps", str(int(fps)),
            "--res", f"{width}x{height}",
            "--transparency", transparency,
            "--out", str(mp4_path),
        ]
        if not show_label:
            cmd.append("--no-label")

        _run_subprocess(cmd, env=env)
        frames = _decode_mp4_to_tensor(mp4_path, width, height, env=env)
        return {
            "ui": {"text": [str(mp4_path)]},
            "result": (str(mp4_path), frames),
        }


class HypbrExport:
    CATEGORY = "hypbr"
    DESCRIPTION = (
        "Export a hypbr material folder to game engines and 3D tools (glTF/GLB with alphaMask "
        "cutout, Blender, Unreal, Unity, Godot, Three.js, MaterialX, USD, Vulkan)."
    )
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("export_dir",)
    OUTPUT_NODE = True
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "material_dir": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Path to a synthesized hypbr material folder (connect from material_dir).",
                    },
                ),
                "target": (
                    list(EXPORT_TARGETS),
                    {
                        "default": "glb",
                        "tooltip": "Engine/format preset to export. 'all' writes every format into subfolders.",
                    },
                ),
                "material_name": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "Optional asset name inside exported manifests (blank = source folder name).",
                    },
                ),
                "transparency": (
                    list(TRANSPARENCY_MODES),
                    {
                        "default": "auto",
                        "tooltip": "auto = alphaMode MASK only if >0.5% cutout holes exist; on = force MASK; off = force OPAQUE RGB export.",
                    },
                ),
                "output_name": (
                    "STRING",
                    {
                        "default": "export",
                        "tooltip": "Subfolder prefix created inside ComfyUI/output/hypbr_exports/.",
                    },
                ),
            }
        }

    def run(
        self,
        material_dir: str,
        target: str,
        material_name: str,
        transparency: str,
        output_name: str,
    ) -> dict[str, Any]:
        mat_dir = Path(material_dir.strip())
        if not (mat_dir / "albedo_delit.png").exists():
            raise FileNotFoundError(f"Expected albedo_delit.png in material_dir: {mat_dir}")

        exp_root = _get_output_root() / "hypbr_exports"
        out_dir = _next_numbered_dir(exp_root, f"{output_name}_{target}")
        env = _build_subprocess_env(_get_output_root() / "hypbr")
        cmd = _resolve_hypbr_python() + [
            "export",
            str(mat_dir),
            "--target", target,
            "--transparency", transparency,
            "--out", str(out_dir),
        ]
        if material_name.strip():
            cmd += ["--name", material_name.strip()]
        _run_subprocess(cmd, env=env)
        return {
            "ui": {"text": [str(out_dir)]},
            "result": (str(out_dir),),
        }


NODE_CLASS_MAPPINGS = {
    "HypbrPhotoToPBR": HypbrPhotoToPBR,
    "HypbrProceduralMaterial": HypbrProceduralMaterial,
    "HypbrBlend": HypbrBlend,
    "HypbrRenderPreviewVideo": HypbrRenderPreviewVideo,
    "HypbrExport": HypbrExport,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "HypbrPhotoToPBR": "hypbr Photo to PBR",
    "HypbrProceduralMaterial": "hypbr Procedural Material",
    "HypbrBlend": "hypbr Blend",
    "HypbrRenderPreviewVideo": "hypbr Render Preview Video",
    "HypbrExport": "hypbr Export",
}
