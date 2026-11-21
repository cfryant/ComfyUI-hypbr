#!/usr/bin/env python3
"""Deterministic headless Three.js video & still renderer for hypbr PBR materials.

Renders a material directory (with albedo, normal, roughness, metallic, ao, height,
and optional opacity cutout map) into an MP4 video or PNG still using Three.js
inside headless Chromium and encodes with ffmpeg.

Examples:
  python render_demo.py ./demos/01_galvanized_chainlink_rust --shot lightorbit --seconds 6 --fps 30 --res 1920x1080 --out fence.mp4
  python render_demo.py ./demos/02_scifi_bulkhead_weathered --shot turntable --seconds 4 --fps 30 --res 1280x720 --out bulkhead.mp4
  python render_demo.py ./demos/04_damascus_blued_steel_wet --still damascus_still.png --res 800x600
  python render_demo.py ./demos/02_scifi_bulkhead_weathered --mesh plane --shot lightorbit --seconds 4 --out plane.mp4
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SHOTS = ("turntable", "lightorbit", "closeup_displacement", "tiling_zoom", "channel_split", "still")
MESHES = ("auto", "sphere", "cube", "cylinder", "plane")


def find_browser_executable() -> str | None:
    """Locate a Chromium-based browser on Linux or Windows so Playwright needs no extra download."""
    candidates = [
        os.environ.get("HYPBR_BROWSER", ""),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def find_ffmpeg() -> str:
    """Find ffmpeg via HYPBR_FFMPEG, PATH, or optional imageio_ffmpeg."""
    env_ff = os.environ.get("HYPBR_FFMPEG", "").strip()
    if env_ff and Path(env_ff).exists():
        return env_ff
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise FileNotFoundError("ffmpeg not found on PATH or HYPBR_FFMPEG. Install ffmpeg or set HYPBR_FFMPEG.")


def start_asset_server(mat_dir: Path) -> tuple[socketserver.TCPServer, int]:
    """Serve scene.html + local Three.js vendor files + /mat/* textures on an ephemeral local port."""
    mat_dir = mat_dir.resolve()

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            clean = path.split("?", 1)[0].split("#", 1)[0]
            if clean.startswith("/mat/"):
                rel = clean[len("/mat/"):].lstrip("/")
                return str(mat_dir / rel)
            rel = clean.lstrip("/") or "scene.html"
            return str(HERE / rel)

        def log_message(self, _fmt: str, *_args) -> None:
            return

    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def parse_res(res_str: str) -> tuple[int, int]:
    parts = res_str.lower().strip().split("x")
    if len(parts) != 2:
        raise ValueError(f"Expected WxH resolution (e.g. 1920x1080), got {res_str!r}")
    w, h = int(parts[0]), int(parts[1])
    # H.264 requires even dimensions
    return (w - (w % 2), h - (h % 2))


def render_video_or_still(
    mat_dir: Path,
    shot: str,
    seconds: float,
    fps: int,
    width: int,
    height: int,
    out_path: Path,
    still_path: Path | None = None,
    still_u: float = 0.15,
    show_label: bool = True,
    transparency: str = "auto",
    mesh: str = "auto",
) -> None:
    if not (mat_dir / "albedo_delit.png").exists():
        raise FileNotFoundError(f"Expected albedo_delit.png in {mat_dir}")

    httpd, port = start_asset_server(mat_dir)
    browser_exe = find_browser_executable()
    launch_args = [
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--disable-gpu-sandbox",
    ]
    # On Linux headless boxes without a GPU, SwiftShader ANGLE gives deterministic WebGL2 rendering
    if sys.platform != "win32":
        launch_args += ["--use-gl=angle", "--use-angle=swiftshader"]
    else:
        # Use ANGLE D3D11 on Windows GPU when available (or fallback automatically)
        launch_args += ["--use-gl=angle"]

    label_flag = "1" if show_label else "0"
    url = f"http://127.0.0.1:{port}/scene.html?name={quote(mat_dir.name)}&label={label_flag}&transparency={quote(transparency)}&mesh={quote(mesh)}"

    try:
        with sync_playwright() as pw:
            launch_kwargs: dict = {"headless": True, "args": launch_args}
            if browser_exe:
                launch_kwargs["executable_path"] = browser_exe
            browser = pw.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.goto(url, wait_until="networkidle")
            page.wait_for_function("() => window.__ready === true", timeout=30000)

            if still_path is not None:
                still_path.parent.mkdir(parents=True, exist_ok=True)
                total = 100
                idx = int(round(still_u * (total - 1)))
                page.evaluate(f"window.__renderFrame({idx}, {total}, {shot!r})")
                page.wait_for_timeout(40)
                still_path.write_bytes(page.screenshot(type="png"))
                browser.close()
                return

            total_frames = max(2, int(round(seconds * fps)))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            ffmpeg_bin = find_ffmpeg()
            cmd = [
                ffmpeg_bin,
                "-y",
                "-f", "image2pipe",
                "-vcodec", "png",
                "-r", str(fps),
                "-i", "-",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "17",
                "-preset", "medium",
                "-movflags", "+faststart",
                str(out_path),
            ]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdin is not None
            try:
                for frame_idx in range(total_frames):
                    page.evaluate(f"window.__renderFrame({frame_idx}, {total_frames}, {shot!r})")
                    png_bytes = page.screenshot(type="png")
                    proc.stdin.write(png_bytes)
            finally:
                proc.stdin.close()
                stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                rc = proc.wait()
            browser.close()
            if rc != 0:
                raise RuntimeError(f"ffmpeg failed (exit {rc}):\n{stderr}")
    finally:
        httpd.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic Three.js PBR video & still renderer for hypbr.")
    ap.add_argument("material_dir", type=Path, help="Path to synthesized hypbr material directory")
    ap.add_argument("--shot", choices=SHOTS, default="turntable", help="Camera/lighting animation preset")
    ap.add_argument("--seconds", type=float, default=6.0, help="Video duration in seconds (default: 6)")
    ap.add_argument("--fps", type=int, default=30, help="Frames per second (default: 30)")
    ap.add_argument("--res", type=str, default="1920x1080", help="Output resolution WxH (default: 1920x1080)")
    ap.add_argument("--out", type=Path, default=Path("preview.mp4"), help="Output MP4 path")
    ap.add_argument("--still", type=Path, default=None, help="Optional: render a single PNG still instead of MP4")
    ap.add_argument("--still-u", type=float, default=0.15, help="Normalized time [0..1] for --still (default: 0.15)")
    ap.add_argument("--no-label", action="store_true", help="Hide bottom-left material name label")
    ap.add_argument("--transparency", choices=("auto", "on", "off"), default="auto", help="Cutout opacity mode (default: auto)")
    ap.add_argument(
        "--mesh",
        choices=MESHES,
        default="auto",
        help="Hero mesh: auto keeps the current studio layout (framed fence panel for cutouts, sphere+swatch otherwise); "
             "sphere, cube, cylinder, or plane use that single shape with every shot (default: auto)",
    )
    args = ap.parse_args()

    w, h = parse_res(args.res)
    render_video_or_still(
        mat_dir=args.material_dir,
        shot=args.shot,
        seconds=args.seconds,
        fps=args.fps,
        width=w,
        height=h,
        out_path=args.out,
        still_path=args.still,
        still_u=args.still_u,
        show_label=not args.no_label,
        transparency=args.transparency,
        mesh=args.mesh,
    )
    target = args.still if args.still is not None else args.out
    print(f"Rendered {args.material_dir.name} ({args.shot}, mesh={args.mesh}, {w}x{h}) -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
