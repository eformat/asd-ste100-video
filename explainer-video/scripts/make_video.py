#!/usr/bin/env python3
"""
make_video.py — build a short narrated explainer video from a scene script.

Deterministic stills pipeline (no screen recording, no animation timing):
  script.json -> edge-tts narration (mp3, measured with ffprobe)
              -> google-chrome headless slide PNGs (1920x1080)
              -> ffmpeg per-scene segments (libopenh264 + aac)
              -> concat -> validated mp4 + frame-burst validation sheet

Usage:
  make_video.py script.json [--out FILE] [--workdir DIR] [--voice V]
                            [--rate +20%] [--target 30] [--pad 0.4]
                            [--rate-cap +30%]

Exit codes: 0 ok | 1 usage/env | 2 build | 3 validation
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "slides.template.html"
VALID_TYPES = {"title", "body", "outro"}


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n"
            f"stderr: {p.stderr[-2000:]}"
        )
    return p


def probe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(p.stdout.strip().splitlines()[0])


def parse_rate(rate):
    s = str(rate).strip().rstrip("%")
    try:
        return int(s)
    except ValueError:
        die(f"bad rate {rate!r} — use a signed percentage like '+20%'")


def fmt_rate(pct):
    return f"{'+' if pct >= 0 else ''}{pct}%"


def synth(narration, voice, rate, out):
    """edge-tts one scene; retry once on transient failure."""
    err = ""
    for attempt in (1, 2):
        p = subprocess.run(
            ["edge-tts", "--voice", voice, f"--rate={rate}",
             "--text", narration, "--write-media", str(out)],
            capture_output=True, text=True)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return
        err = p.stderr[-500:]
        if attempt == 1:
            time.sleep(2)  # transient network/429 — one quiet retry
    raise RuntimeError(f"edge-tts failed for {out.name}: {err}")


def render_slide(data, html_path, png_path, chrome):
    html = TEMPLATE.read_text()
    if "/*__DATA__*/null" not in html:
        die(f"template {TEMPLATE} is missing the /*__DATA__*/null marker")
    payload = json.dumps(data).replace("</", "<\\/")
    html_path.write_text(html.replace("/*__DATA__*/null", payload))
    run([chrome,
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--window-size=1920,1080", "--virtual-time-budget=3000",
         f"--screenshot={png_path}", html_path.as_uri()])
    if not png_path.exists() or png_path.stat().st_size == 0:
        raise RuntimeError(f"chrome did not produce {png_path.name}")


def build_segment(png, mp3, dur, out, fade_in, fade_out):
    """One scene: looped PNG + padded narration -> h264/aac segment."""
    vf = ["scale=1920:1080:flags=lanczos", "format=yuv420p"]
    af = ["aresample=44100", "aformat=sample_fmts=fltp:channel_layouts=stereo", "apad"]
    if fade_in:
        vf.append("fade=t=in:st=0:d=0.5")
        af.append("afade=t=in:st=0:d=0.3")
    if fade_out:
        vf.append(f"fade=t=out:st={max(0.0, dur - 0.5):.3f}:d=0.5")
        af.append(f"afade=t=out:st={max(0.0, dur - 0.6):.3f}:d=0.6")
    base = ["ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", "30", "-i", str(png),
            "-i", str(mp3),
            "-filter_complex", f"[0:v]{','.join(vf)}[v];[1:a]{','.join(af)}[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
            "-c:v", "libopenh264", "-c:a", "aac", "-b:a", "128k",
            "-ar", "44100", "-ac", "2", str(out)]
    try:
        run(base[:-1] + ["-rc_mode", "quality", base[-1]])
    except RuntimeError as e1:
        try:
            run(base)
        except RuntimeError as e2:
            raise RuntimeError(
                f"segment encode failed\n  with rc_mode: {e1}\n  without: {e2}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="scene script JSON")
    ap.add_argument("--out", help="final mp4 (default ~/Videos/<name>.mp4)")
    ap.add_argument("--workdir", help="build dir (default /tmp/opencode/<name>-video)")
    ap.add_argument("--voice", help="edge-tts voice (default from script, else en-US-JennyNeural)")
    ap.add_argument("--rate", help="speech rate like '+20%%' (default from script, else +20%%)")
    ap.add_argument("--target", type=float, help="target total seconds (default from script)")
    ap.add_argument("--pad", type=float, help="silence after narration per scene (default 0.4)")
    ap.add_argument("--rate-cap", default="+30%", help="auto-fit rate ceiling (default +30%%)")
    args = ap.parse_args()

    # --- environment -------------------------------------------------------
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not on PATH")
    edge_tts = shutil.which("edge-tts")
    if not edge_tts:
        die("edge-tts not on PATH — install with: /usr/bin/python3 -m pip install --user edge-tts")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        die("no chrome/chromium found for slide rendering")
    if not TEMPLATE.exists():
        die(f"slide template missing: {TEMPLATE}")

    # --- script ------------------------------------------------------------
    script_path = Path(args.script).expanduser().resolve()
    if not script_path.exists():
        die(f"script not found: {script_path}")
    try:
        script = json.loads(script_path.read_text())
    except json.JSONDecodeError as e:
        die(f"{script_path} is not valid JSON: {e}")
    name = script.get("name") or script_path.stem
    scenes = script.get("scenes") or []
    if not scenes:
        die("script has no scenes")
    for i, s in enumerate(scenes):
        if s.get("type", "body") not in VALID_TYPES:
            die(f"scene {i}: type must be one of {sorted(VALID_TYPES)}")
        if not str(s.get("narration", "")).strip():
            die(f"scene {i} ({s.get('title', '?')}): narration is empty")

    voice = args.voice or script.get("voice", "en-US-JennyNeural")
    cap_pct = parse_rate(args.rate_cap)
    pct = parse_rate(args.rate or script.get("rate", "+20%"))
    pct = min(pct, cap_pct)
    target = args.target or script.get("target")
    pad = args.pad if args.pad is not None else script.get("pad", 0.4)

    workdir = Path(args.workdir or f"/tmp/opencode/{name}-video").resolve()
    for sub in ("narration", "slides", "segments"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    out = Path(args.out or f"~/Videos/{name}.mp4").expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    # --- 1. narration (+ auto-fit loop) ------------------------------------
    rate = fmt_rate(pct)
    scene_durs = []
    for _attempt in range(6):
        durs = []
        for i, s in enumerate(scenes):
            mp3 = workdir / "narration" / f"scene-{i:02d}.mp3"
            synth(str(s["narration"]), voice, rate, mp3)
            durs.append(probe_duration(mp3))
        holds = [float(s.get("hold", 0)) for s in scenes]
        scene_durs = [max(2.0, d + pad + h) for d, h in zip(durs, holds)]
        total = sum(scene_durs)
        print(f"[fit] rate {rate}: narration total {sum(durs):.1f}s, "
              f"with pads {total:.1f}s (target {target if target else 'n/a'})")
        if target is None or total <= target or pct >= cap_pct:
            break
        pct = min(cap_pct, pct + 5)
        rate = fmt_rate(pct)
        print(f"[fit] over target — re-synthesizing at rate {rate}")
    else:
        print("WARNING: still over target at rate cap; shorten the longest scenes:")
        for i, (s, d) in enumerate(sorted(enumerate(scene_durs), key=lambda x: -x[1])[:3]):
            print(f"  scene {s[0]} ({scenes[s[0]].get('title', '?')}): {d:.1f}s")

    # --- 2. slides ----------------------------------------------------------
    n = len(scenes)
    for i, s in enumerate(scenes):
        data = {k: v for k, v in s.items() if k != "narration"}
        data["type"] = s.get("type", "body")
        # body scenes get 02.. style page numbers; title/outro stay clean
        if data["type"] == "body":
            data["page"] = f"{i + 1:02d}"
        render_slide(data,
                     workdir / "slides" / f"scene-{i:02d}.html",
                     workdir / "slides" / f"scene-{i:02d}.png",
                     chrome)
    print(f"[slides] rendered {n} frames at 1920x1080")

    # --- 3. segments + concat ----------------------------------------------
    seg_paths = []
    for i in range(n):
        seg = workdir / "segments" / f"seg-{i:02d}.mp4"
        build_segment(workdir / "slides" / f"scene-{i:02d}.png",
                      workdir / "narration" / f"scene-{i:02d}.mp3",
                      scene_durs[i], seg,
                      fade_in=(i == 0), fade_out=(i == n - 1))
        seg_paths.append(seg)
    list_file = workdir / "segments" / "list.txt"
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_paths))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", "-movflags", "+faststart",
         str(out)])
    print(f"[assemble] {out}")

    # --- 4. validate ---------------------------------------------------------
    final_dur = probe_duration(out)
    expected = sum(scene_durs)
    if abs(final_dur - expected) > 1.5:
        die(f"final duration {final_dur:.1f}s != expected {expected:.1f}s", 3)
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height", "-of", "json", str(out)])
    streams = json.loads(p.stdout)["streams"]
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not v or v.get("codec_name") != "h264" or v.get("width") != 1920:
        die(f"unexpected video stream: {v}", 3)
    if not a or a.get("codec_name") != "aac":
        die(f"unexpected audio stream: {a}", 3)

    samples = max(6, 2 * n)
    cols = 3 if samples <= 12 else 4
    rows = math.ceil(samples / cols)
    fps = samples / final_dur
    sheet = workdir / "sheet.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(out),
         "-vf", f"fps={fps:.4f},scale=480:-1,tile={cols}x{rows}",
         "-frames:v", "1", str(sheet)])

    # --- report --------------------------------------------------------------
    print("\n=== scene durations ===")
    for i, s in enumerate(scenes):
        print(f"  {i:02d}  {scene_durs[i]:5.1f}s  {s.get('type','body'):5s}  "
              f"{s.get('kicker', s.get('title', ''))}")
    print(f"\nFINAL  {out}")
    print(f"       {final_dur:.1f}s, 1920x1080 h264+aac, {out.stat().st_size/1e6:.1f} MB")
    print(f"SHEET  {sheet}  <- read this to verify every scene visually")
    print(json.dumps({"out": str(out), "duration": round(final_dur, 2),
                      "scenes": n, "voice": voice, "rate": rate,
                      "sheet": str(sheet)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
