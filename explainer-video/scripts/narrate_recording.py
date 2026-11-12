#!/usr/bin/env python3
"""
narrate_recording.py — lay skill-style narration + title/outro slides over an
EXISTING (silent) recording, without re-encoding it.

Companion to make_video.py: the recording's timeline is fixed, so narration
beats carry explicit start times instead of driving the timeline.

  beats.json -> edge-tts narration (mp3, measured with ffprobe)
             -> google-chrome slide PNGs (default size = input video dims)
             -> title/outro VIDEO segments matching the recording exactly
                (libopenh264, yuv420p bt709, same fps/timescale, silent aac twin)
             -> concat -c copy (the recording itself is never re-encoded)
             -> narration mix (adelay/amix) -> mux (-map 0:v -map 1:a -c:v copy)
             -> validated mp4 + frame-burst sheet

beats.json schema:
{
  "name":   "my-video-narrated",
  "input":  "/path/to/recording.mp4",
  "voice":  "en-US-JennyNeural",       # optional
  "rate":   "+20%",                    # optional
  "pad":    0.4,                       # optional silence after narration, s
  "gain_db": 0,                        # optional gain on the narration mix
  "slides": [ {"type": "title", "narration": "...", ...title fields},
              {"type": "outro", "narration": "...", ...outro fields} ],
  "beats":  [ {"t": 3.0, "narration": "..."}, ... ]   # t = seconds into recording
}

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
VALID_SLIDE_TYPES = {"title", "outro"}


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


def probe_streams(path):
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,"
             "sample_rate,channels,time_base", "-of", "json", str(path)])
    streams = json.loads(p.stdout)["streams"]
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not v:
        die(f"{path}: no video stream")
    return v, a


def parse_rate_str(rate_str):
    num, _, den = rate_str.partition("/")
    return float(num) / float(den or 1)


def synth(narration, voice, rate, out):
    """edge-tts one piece; retry once on transient failure."""
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


# --- slide rendering ---------------------------------------------------------

BASE_CSS_VALUES = {  # from templates/slides.template.html at 1920x1080
    "stage_pad_v": 120, "stage_pad_h": 150,
    "pagenum_fs": 24, "pagenum_top": 44, "pagenum_right": 64,
    "redbar_h": 14,
    "footer_fs": 22, "footer_bottom": 44,
    "kicker_fs": 24, "kicker_mb": 28,
    "h1_fs": 96,
    "dash_w": 64, "dash_h": 8, "dash_mb": 44,
    "text_fs": 42, "text_maxw": 1500,
    "bullets_gap": 48, "bullets_gapcol": 80, "bullets_mt": 12,
    "bullets_single_gap": 40,
    "bullet_fs": 40, "bullet_gap": 24, "bullet_mark": 16, "bullet_mark_mt": 16,
    "callout_mt": 60, "callout_border": 8, "callout_pad_v": 26,
    "callout_pad_h": 38, "callout_fs": 34, "callout_maxw": 1450,
    "title_h1_fs": 150, "title_h1_mb": 20, "title_sub_fs": 44,
    "meta_fs": 22, "meta_left": 150, "meta_bottom": 44,
    "outro_line_fs": 120, "outro_sub_fs": 38, "outro_sub_mt": 40,
}


def size_override_css(width, height):
    s = height / 1080.0
    v = {k: round(x * s) for k, x in BASE_CSS_VALUES.items()}
    v["text_maxw"] = round(BASE_CSS_VALUES["text_maxw"] * s)
    v["callout_maxw"] = round(BASE_CSS_VALUES["callout_maxw"] * s)
    return f"""
  html,body{{width:{width}px;height:{height}px;overflow:hidden}}
  #stage{{width:{width}px;height:{height}px;padding:{v['stage_pad_v']}px {v['stage_pad_h']}px}}
  .pagenum{{font-size:{v['pagenum_fs']}px;top:{v['pagenum_top']}px;right:{v['pagenum_right']}px}}
  .redbar{{height:{v['redbar_h']}px}}
  .footer{{font-size:{v['footer_fs']}px;bottom:{v['footer_bottom']}px}}
  .kicker{{font-size:{v['kicker_fs']}px;margin-bottom:{v['kicker_mb']}px}}
  h1{{font-size:{v['h1_fs']}px}}
  .dash{{width:{v['dash_w']}px;height:{v['dash_h']}px;margin-bottom:{v['dash_mb']}px}}
  .text{{font-size:{v['text_fs']}px;max-width:{v['text_maxw']}px}}
  .bullets{{gap:{v['bullets_gap']}px {v['bullets_gapcol']}px;margin-top:{v['bullets_mt']}px}}
  .bullets.single{{gap:{v['bullets_single_gap']}px}}
  .bullet{{font-size:{v['bullet_fs']}px;gap:{v['bullet_gap']}px}}
  .bullet::before{{flex:0 0 {v['bullet_mark']}px;height:{v['bullet_mark']}px;margin-top:{v['bullet_mark_mt']}px}}
  .callout{{margin-top:{v['callout_mt']}px;border-left-width:{v['callout_border']}px;padding:{v['callout_pad_v']}px {v['callout_pad_h']}px;font-size:{v['callout_fs']}px;max-width:{v['callout_maxw']}px}}
  .titlecard h1{{font-size:{v['title_h1_fs']}px;margin-bottom:{v['title_h1_mb']}px}}
  .titlecard .subtitle{{font-size:{v['title_sub_fs']}px}}
  .meta{{font-size:{v['meta_fs']}px;left:{v['meta_left']}px;bottom:{v['meta_bottom']}px}}
  .outro .line{{font-size:{v['outro_line_fs']}px}}
  .outro .sub{{font-size:{v['outro_sub_fs']}px;margin-top:{v['outro_sub_mt']}px}}
"""


def render_slide(data, html_path, png_path, chrome, size):
    width, height = size
    html = TEMPLATE.read_text()
    if "/*__DATA__*/null" not in html:
        die(f"template {TEMPLATE} is missing the /*__DATA__*/null marker")
    payload = json.dumps(data).replace("</", "<\\/")
    if (width, height) != (1920, 1080):
        html = html.replace("</head>",
                            f"<style>{size_override_css(width, height)}</style></head>")
    html_path.write_text(html.replace("/*__DATA__*/null", payload))
    run([chrome,
         "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-dev-shm-usage",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--window-size={width},{height}", "--virtual-time-budget=3000",
         f"--screenshot={png_path}", html_path.as_uri()])
    if not png_path.exists() or png_path.stat().st_size == 0:
        raise RuntimeError(f"chrome did not produce {png_path.name}")


# --- segments ----------------------------------------------------------------

def build_slide_segment(png, dur, out, vinfo, ainfo, fade_in, fade_out, workdir):
    """Video-only slide segment + silent aac twin, matched to the recording."""
    w, h = vinfo["width"], vinfo["height"]
    fps = vinfo["r_frame_rate"]
    timescale = int(vinfo["time_base"].split("/")[1])
    arate = int(ainfo["sample_rate"]) if ainfo else 48000
    ach = int(ainfo["channels"]) if ainfo else 2
    vf = [f"scale={w}:{h}:flags=lanczos:out_color_matrix=bt709", "format=yuv420p"]
    if fade_in:
        vf.append("fade=t=in:st=0:d=0.4")
    if fade_out:
        vf.append(f"fade=t=out:st={max(0.0, dur - 0.5):.3f}:d=0.5")
    base = ["ffmpeg", "-y", "-v", "error",
            "-loop", "1", "-framerate", fps, "-i", str(png),
            "-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={arate}:cl=mono" if ach == 1 else f"anullsrc=r={arate}:cl=stereo",
            "-filter_complex", f"[0:v]{','.join(vf)}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", f"{dur:.3f}",
            "-c:v", "libopenh264",
            "-video_track_timescale", str(timescale),
            "-c:a", "aac", "-b:a", "96k", "-ar", str(arate), "-ac", str(ach),
            str(out)]
    attempts = [
        base[:-1] + ["-rc_mode", "quality",
                     "-colorspace", "bt709", "-color_primaries", "bt709",
                     "-color_trc", "bt709", base[-1]],
        base[:-1] + ["-rc_mode", "quality", base[-1]],
        base,
    ]
    errs = []
    for cmd in attempts:
        try:
            run(cmd)
            return
        except RuntimeError as e:
            errs.append(str(e))
    raise RuntimeError(f"slide segment encode failed\n" + "\n  then: ".join(errs))


# --- narration mix -----------------------------------------------------------

def build_mix(parts, total, gain_db, out):
    """parts: list of (mp3_path, start_seconds). Renders one stereo wav."""
    inputs, chains, refs = [], [], []
    for i, (mp3, start) in enumerate(parts):
        inputs += ["-i", str(mp3)]
        ms = int(round(start * 1000))
        chains.append(
            f"[{i}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={ms}|{ms}[a{i}]")
        refs.append(f"[a{i}]")
    gain = f"volume={gain_db}dB," if gain_db else ""
    fc = (";".join(chains) + ";" + "".join(refs) +
          f"amix=inputs={len(parts)}:normalize=0,{gain}"
          f"apad,atrim=end={total:.3f}[out]")
    run(["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", fc, "-map", "[out]",
         "-c:a", "pcm_s16le", str(out)])


def window_max_volume(path, start, dur):
    p = run(["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
             "-i", str(path), "-map", "0:a:0", "-af", "volumedetect",
             "-f", "null", "-"])
    mx = None
    for line in p.stderr.splitlines():
        if "max_volume:" in line:
            mx = float(line.split("max_volume:")[1].split("dB")[0].strip())
    return mx


# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", help="beats JSON")
    ap.add_argument("--input", help="override recording path")
    ap.add_argument("--out", help="final mp4 (default ~/Videos/<name>.mp4)")
    ap.add_argument("--workdir", help="build dir (default /tmp/opencode/<name>-video)")
    ap.add_argument("--voice", help="edge-tts voice (default from script)")
    ap.add_argument("--rate", help="speech rate like '+20%%' (default from script)")
    ap.add_argument("--size", help="slide render size WxH (default = input dims)")
    ap.add_argument("--gain-db", type=float, help="narration mix gain (default from script)")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not on PATH")
    if not shutil.which("edge-tts"):
        die("edge-tts not on PATH — install with: /usr/bin/python3 -m pip install --user edge-tts")
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        die("no chrome/chromium found for slide rendering")
    if not TEMPLATE.exists():
        die(f"slide template missing: {TEMPLATE}")

    script_path = Path(args.script).expanduser().resolve()
    if not script_path.exists():
        die(f"script not found: {script_path}")
    try:
        script = json.loads(script_path.read_text())
    except json.JSONDecodeError as e:
        die(f"{script_path} is not valid JSON: {e}")

    name = script.get("name") or script_path.stem
    input_path = Path(args.input or script.get("input") or "").expanduser()
    if not input_path.exists():
        die(f"input recording not found: {input_path}")
    slides = script.get("slides") or []
    beats = script.get("beats") or []
    if not slides or not beats:
        die("script needs non-empty 'slides' (title first, outro last) and 'beats'")
    if slides[0].get("type") != "title" or slides[-1].get("type") != "outro":
        die("slides[0] must be type 'title' and slides[-1] type 'outro'")
    for i, s in enumerate(slides):
        if s.get("type") not in VALID_SLIDE_TYPES:
            die(f"slides[{i}]: type must be one of {sorted(VALID_SLIDE_TYPES)}")
        if not str(s.get("narration", "")).strip():
            die(f"slides[{i}]: narration is empty")
    for i, b in enumerate(beats):
        if not str(b.get("narration", "")).strip():
            die(f"beats[{i}]: narration is empty")
        if not (0 <= float(b.get("t", -1)) ):
            die(f"beats[{i}]: t must be >= 0")

    voice = args.voice or script.get("voice", "en-US-JennyNeural")
    rate = args.rate or script.get("rate", "+20%")
    pad = float(script.get("pad", 0.4))
    gain_db = args.gain_db if args.gain_db is not None else float(script.get("gain_db", 0))

    vinfo, ainfo = probe_streams(input_path)
    input_dur = probe_duration(input_path)
    input_end = float(script.get("input_end") or 0) or input_dur
    if not (0 < input_end <= input_dur + 0.01):
        die(f"input_end {input_end} outside recording (0, {input_dur}]")
    width, height = vinfo["width"], vinfo["height"]
    size = (width, height)
    if args.size:
        sw, sh = args.size.lower().split("x")
        size = (int(sw), int(sh))

    workdir = Path(args.workdir or f"/tmp/opencode/{name}-video").resolve()
    for sub in ("narration", "slides", "segments"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)
    out = Path(args.out or f"~/Videos/{name}.mp4").expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    # --- 1. narration + slot-fit check --------------------------------------
    pieces = []  # (mp3, label)
    for i, s in enumerate(slides):
        mp3 = workdir / "narration" / f"slide-{i:02d}.mp3"
        synth(str(s["narration"]), voice, rate, mp3)
        pieces.append((mp3, f"slide{i}"))
    beat_durs = []
    for i, b in enumerate(beats):
        mp3 = workdir / "narration" / f"beat-{i:02d}.mp3"
        synth(str(b["narration"]), voice, rate, mp3)
        d = probe_duration(mp3)
        beat_durs.append(d)
        pieces.append((mp3, f"beat{i}"))

    title_narr = probe_duration(pieces[0][0])
    outro_narr = probe_duration(pieces[1][0])
    title_dur = title_narr + pad + float(slides[0].get("hold", 0.8))
    outro_dur = outro_narr + pad + float(slides[-1].get("hold", 1.2))

    print("\n=== beat fit (recording timeline) ===")
    problems = []
    for i, b in enumerate(beats):
        t = float(b["t"])
        end = t + beat_durs[i]
        limit = float(beats[i + 1]["t"]) if i + 1 < len(beats) else input_end
        flag = ""
        if end + 0.6 > limit - 0.3:
            flag = "  << OVER"
            problems.append(i)
        print(f"  beat {i:02d}  t={t:7.1f}  narr {beat_durs[i]:5.1f}s  end {end:7.1f}"
              f"  (next at {limit:7.1f}){flag}")
    if problems:
        die("beats overrun their slots — trim words in beats: "
            + ", ".join(str(i) for i in problems), 2)
    last_end = float(beats[-1]["t"]) + beat_durs[-1]
    if last_end > input_end - 0.5:
        die(f"last beat ends {last_end:.1f}s but recording is trimmed to {input_end:.1f}s", 2)

    # --- 2. slides ------------------------------------------------------------
    for i, s in enumerate(slides):
        data = {k: v for k, v in s.items() if k != "narration"}
        render_slide(data,
                     workdir / "slides" / f"slide-{i:02d}.html",
                     workdir / "slides" / f"slide-{i:02d}.png",
                     chrome, size)
    print(f"[slides] rendered {len(slides)} frames at {size[0]}x{size[1]}")

    # --- 3. segments + concat -------------------------------------------------
    seg_paths = []
    for i, s in enumerate(slides):
        dur = title_dur if s["type"] == "title" else outro_dur
        seg = workdir / "segments" / f"slide-{i:02d}.mp4"
        build_slide_segment(workdir / "slides" / f"slide-{i:02d}.png", dur, seg,
                            vinfo, ainfo,
                            fade_in=True, fade_out=(s["type"] == "outro"),
                            workdir=workdir)
        seg_paths.append(seg)

    list_file = workdir / "segments" / "list.txt"
    list_file.write_text(
        f"file '{seg_paths[0].resolve()}'\n"
        f"file '{input_path.resolve()}'\n"
        f"outpoint {input_end:.3f}\n"
        f"file '{seg_paths[1].resolve()}'\n")
    concat = workdir / "concat.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(concat)])

    # --- 4. narration mix + mux ----------------------------------------------
    total = title_dur + input_end + outro_dur
    parts = [(pieces[0][0], 0.0)]
    for i, b in enumerate(beats):
        parts.append((pieces[2 + i][0], title_dur + float(b["t"])))
    parts.append((pieces[1][0], title_dur + input_end + 0.3))
    mix = workdir / "narration-mix.wav"
    build_mix(parts, total, gain_db, mix)

    run(["ffmpeg", "-y", "-v", "error", "-i", str(concat), "-i", str(mix),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)])

    # --- 5. validate -----------------------------------------------------------
    final_dur = probe_duration(out)
    if abs(final_dur - total) > 1.5:
        die(f"final duration {final_dur:.1f}s != expected {total:.1f}s", 3)
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height", "-of", "json", str(out)])
    streams = json.loads(p.stdout)["streams"]
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if not v or v.get("codec_name") != "h264" or v.get("width") != width:
        die(f"unexpected video stream: {v}", 3)
    if not a or a.get("codec_name") != "aac":
        die(f"unexpected audio stream: {a}", 3)

    print("\n=== narration presence (per beat) ===")
    bad = []
    for i, b in enumerate(beats):
        start = title_dur + float(b["t"])
        mx = window_max_volume(out, start + 0.2, max(0.5, beat_durs[i] - 0.4))
        ok = mx is not None and mx > -45
        if not ok:
            bad.append(i)
        print(f"  beat {i:02d} @ {start:7.1f}s  max {mx if mx is not None else '?'} dB  {'ok' if ok else '<< QUIET'}")
    if bad:
        die(f"narration missing or too quiet at beats: {bad}", 3)

    samples = 24
    cols, rows = 4, 6
    fps = samples / final_dur
    sheet = workdir / "sheet.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(out),
         "-vf", f"fps={fps:.4f},scale=480:-1,tile={cols}x{rows}",
         "-frames:v", "1", str(sheet)])

    print("\nFINAL  " + str(out))
    trimmed = f" (trimmed from {input_dur:.1f})" if input_end < input_dur - 0.05 else ""
    print(f"       {final_dur:.1f}s ({title_dur:.1f} title + {input_end:.1f} recording{trimmed}"
          f" + {outro_dur:.1f} outro), {size[0]}x{size[1]} h264+aac, "
          f"{out.stat().st_size / 1e6:.1f} MB")
    print(f"SHEET  {sheet}  <- read this to verify title card + beats visually")
    print(json.dumps({"out": str(out), "duration": round(final_dur, 2),
                      "title_dur": round(title_dur, 2),
                      "input_end": round(input_end, 2),
                      "outro_dur": round(outro_dur, 2),
                      "beats": len(beats), "voice": voice, "rate": rate,
                      "gain_db": gain_db, "sheet": str(sheet)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
