import json
import subprocess
import time
from pathlib import Path

# ---------------- Paths ----------------
INPUT_DIR = Path("/media/daniel/Aether OS")
OUTPUT_DIR = Path("/home/daniel/Videos/files")

# ---------------- File types ----------------
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".mpg", ".mpeg", ".m4v"}

# ---------------- Mode selection ----------------
#   MODE = "cpu_master"   -> Highest quality, very slow (x265 10-bit)
#   MODE = "intel_qsv"    -> Hardware encode (much faster), still very good
MODE = "intel_qsv"

# ---------------- Quality knobs ----------------
# CPU master (x265)
CPU_CRF = 16                 # 14-18 (lower = higher quality + slower + bigger)
CPU_PRESET = "slow"          # slow|slower|veryslow
CPU_10BIT = True             # True recommended for masters

# Intel QSV (hardware)
# global_quality: lower is better. Typical range 18-28.
QSV_GLOBAL_QUALITY = 20
QSV_TARGET_FPS = None        # keep source fps if None

# Skip if already 4K+
SKIP_IF_4K_OR_HIGHER = True

# Progress output
PRINT_EVERY_SECONDS = 2.0
ETA_AVG_WINDOW_SECONDS = 60.0


# ---------------- Utilities ----------------
def fmt_hms(seconds: float) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "??:??:??"
    s = int(seconds + 0.5)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def run_capture(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True).stdout


def ffprobe_info(path: Path):
    """
    Returns: duration_seconds(float), width(int), height(int), fps(float)
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json",
        str(path)
    ]
    data = json.loads(run_capture(cmd))

    duration = float(data["format"]["duration"])
    s = data["streams"][0]
    w, h = int(s["width"]), int(s["height"])

    rr = s.get("r_frame_rate", "0/1")
    num, den = rr.split("/")
    fps = float(num) / float(den) if float(den) != 0 else 0.0

    return duration, w, h, fps


def ffmpeg_exists():
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True, text=True)
        return True
    except Exception:
        return False


def ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True
        ).stdout
        return name in out
    except Exception:
        return False


def intel_qsv_available() -> bool:
    return ffmpeg_has_encoder("hevc_qsv") or ffmpeg_has_encoder("h264_qsv")


# ---------------- Filters ----------------
def scaler_for_source(src_w: int, src_h: int) -> str:
    """
    Use widely supported flags only.
    - bicubic for small sources: faster, still good
    - lanczos for larger sources: sharper, best quality
    """
    if src_h <= 720:
        # Fast, safe
        return "scale=3840:2160:flags=bicubic+accurate_rnd+full_chroma_int"
    # High quality
    return "scale=3840:2160:flags=lanczos+accurate_rnd+full_chroma_int"


# ---------------- Command builders ----------------
def build_cmd_cpu_master(input_path: Path, output_path: Path, src_w: int, src_h: int):
    vf = scaler_for_source(src_w, src_h)

    x265_params = [
        "aq-mode=3",
        "aq-strength=1.0",
        "psy-rd=2.0",
        "psy-rdoq=1.2",
        "rd=4",
        "rdoq-level=2",
        "deblock=-1,-1",
        "sao=1",
        "ref=4",
        "bframes=6",
        "rc-lookahead=32",
        "me=star",
        "subme=4",
        "strong-intra-smoothing=1"
    ]

    pix_fmt = "yuv420p10le" if CPU_10BIT else "yuv420p"

    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx265",
        "-preset", CPU_PRESET,
        "-crf", str(CPU_CRF),
        "-pix_fmt", pix_fmt,
        "-x265-params", ":".join(x265_params),
        "-c:a", "copy",
        str(output_path)
    ]


def build_cmd_intel_qsv(input_path: Path, output_path: Path, src_w: int, src_h: int):
    """
    Conservative QSV pipeline:
    - scale on CPU (safe)
    - force nv12 before QSV encoder
    - use hevc_qsv if available else h264_qsv
    """
    vf = scaler_for_source(src_w, src_h)

    encoder = "h264_qsv"

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-i", str(input_path),

        # Scale first, then force nv12 (QSV friendly)
        "-vf", f"{vf},format=nv12",

        "-c:v", encoder,
        "-global_quality", str(QSV_GLOBAL_QUALITY),
        "-look_ahead", "1",
        "-b_strategy", "1",
        "-c:a", "copy",
        str(output_path)
    ]

    return cmd


# ---------------- Encoding with ETA ----------------
def encode_with_eta(output_path: Path, cmd, duration: float):
    print(f"Duration: {fmt_hms(duration)} | Output: {output_path.name}")

    start_wall = time.time()
    last_print = 0.0

    speed_samples = []  # (wall_time, speed)

    out_time_us = 0
    speed_inst = 0.0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue

            key, value = line.split("=", 1)

            if key == "out_time_us":
                try:
                    out_time_us = int(value)
                except ValueError:
                    pass

            elif key == "out_time_ms":
                try:
                    out_time_us = int(value) * 1000
                except ValueError:
                    pass

            elif key == "speed":
                v = value.strip().lower().replace("x", "")
                try:
                    speed_inst = float(v)
                    now = time.time()
                    speed_samples.append((now, speed_inst))

                    cutoff = now - ETA_AVG_WINDOW_SECONDS
                    while speed_samples and speed_samples[0][0] < cutoff:
                        speed_samples.pop(0)
                except ValueError:
                    pass

            elif key == "progress":
                now = time.time()
                if now - last_print >= PRINT_EVERY_SECONDS or value == "end":
                    last_print = now

                    done_sec = min(out_time_us / 1_000_000.0, duration)
                    remaining_sec = max(0.0, duration - done_sec)

                    avg_speed = None
                    if speed_samples:
                        avg_speed = sum(s for _, s in speed_samples) / len(speed_samples)

                    use_speed = avg_speed if avg_speed and avg_speed > 0 else speed_inst
                    eta_sec = remaining_sec / use_speed if use_speed and use_speed > 0 else float("inf")

                    pct = min((done_sec / duration) * 100.0, 100.0) if duration > 0 else 0.0
                    elapsed_wall = now - start_wall

                    print(
                        f"Progress: {pct:6.2f}% | "
                        f"Video: {fmt_hms(done_sec)}/{fmt_hms(duration)} | "
                        f"Speed: {use_speed:0.3f}x | "
                        f"ETA: {fmt_hms(eta_sec)} | "
                        f"Wall: {fmt_hms(elapsed_wall)}",
                        flush=True
                    )

                if value == "end":
                    break

        rc = proc.wait()
        if rc != 0:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"ffmpeg failed with exit code {rc}\n{err}")

        print("Done.")
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------------- Main ----------------
def main():
    if not ffmpeg_exists():
        raise RuntimeError("ffmpeg not found. Install it: sudo apt install ffmpeg")

    if not INPUT_DIR.exists():
        raise FileNotFoundError(f"Input folder does not exist: {INPUT_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    videos = [
        p for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not videos:
        print("No video files found in input folder.")
        return

    qsv_ok = intel_qsv_available()
    if MODE == "intel_qsv" and not qsv_ok:
        print("Warning: Intel QSV not detected in ffmpeg encoders. Using cpu_master.")
    mode = MODE if (MODE != "intel_qsv" or qsv_ok) else "cpu_master"

    for video in videos:
        duration, w, h, _fps = ffprobe_info(video)

        if SKIP_IF_4K_OR_HIGHER and (w >= 3840 or h >= 2160):
            print(f"Skipping (already >=4K): {video.name} ({w}x{h})")
            continue

        out_qsv = OUTPUT_DIR / f"{video.stem}_4k_qsv.mp4"
        out_cpu = OUTPUT_DIR / f"{video.stem}_4k_master.mp4"

        if mode == "intel_qsv":
            out = out_qsv
        else:
            out = out_cpu

        if out.exists():
            print(f"Skipping (already exists): {out.name}")
            continue

        print(f"\nEncoding: {video.name} ({w}x{h}) -> 3840x2160 | Mode: {mode}")

        if mode == "intel_qsv":
            cmd = build_cmd_intel_qsv(video, out, w, h)
            try:
                encode_with_eta(out, cmd, duration)
            except RuntimeError as e:
                # If QSV fails, fall back to CPU master for this file
                print("\nQSV failed for this file. Falling back to CPU master.")
                print(str(e).splitlines()[-1])  # last line usually contains the key error

                out = out_cpu
                if out.exists():
                    print(f"Skipping CPU fallback (already exists): {out.name}")
                    continue

                cmd = build_cmd_cpu_master(video, out, w, h)
                encode_with_eta(out, cmd, duration)
        else:
            cmd = build_cmd_cpu_master(video, out, w, h)
            encode_with_eta(out, cmd, duration)

    print("\nBatch upscale complete.")


if __name__ == "__main__":
    main()
