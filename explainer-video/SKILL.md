---
name: explainer-video
description: Produce short narrated explainer videos (1920x1080 mp4) from a simple scene script — Red Hat-styled slide frames plus Microsoft neural TTS narration (edge-tts), assembled deterministically with ffmpeg (libopenh264). Use this skill whenever the user asks for an explainer video, a short video about a topic, a narrated slide video, a 30-second or 60-second video, a TTS-narrated clip, or to "make a video explaining X" — even if they only paste a topic name, notes, or a doc path. Also use it when asked to rebuild, re-voice, or re-time a previous explainer video.
---

# explainer-video — short narrated explainer videos

Deterministic stills pipeline: **scene script → edge-tts narration →
headless-Chrome slide PNGs → ffmpeg (libopenh264) → validated mp4**.
No screen recording and no animation timing: measured narration durations
drive the timeline, so every build is repeatable and drift-free.

The one script does everything:

```bash
python3 <this-skill>/scripts/make_video.py script.json
```

## Requirements (verify before building)

- `edge-tts` on PATH — install once with
  `/usr/bin/python3 -m pip install --user edge-tts` (already on this box)
- `google-chrome` (or chromium) — renders the slide frames
- ffmpeg **with libopenh264** — Red Hat builds have NO libx264
- ffprobe

Check with: `command -v edge-tts google-chrome ffmpeg ffprobe`

## Workflow

### 1. Author the scene script (JSON)

5–8 scenes, classic explainer arc: title → what is it → part 1 → part 2 →
why it matters → outro. Write narration for the ear — and for the clock:

- **Flowing sentences, not staccato fragments.** The neural voice inserts
  ~0.7s of silence at every sentence boundary; a fragment-heavy script
  spends more time pausing than speaking. Use commas and colons inside
  one flowing sentence per beat.
- **Word budget:** the voice speaks ~2.65 words/sec at rate +20%
  (~3.0 at +30%). A 30-second video is ~60 spoken words TOTAL across all
  scenes. Count before building.
- Spell out anything the reader shouldn't hear literally: "ASD dash STE
  one hundred", "example dot org". "ASD-STE100" read raw gets spelled
  letter-by-letter, slowly.

See `examples/asd-ste100.json` for a complete working script.

### 2. Build

```bash
python3 <this-skill>/scripts/make_video.py script.json \
  --out ~/Videos/my-topic.mp4          # optional overrides: --voice --rate --target
```

The script narrates each scene, auto-fits the rate toward `--target`
(+5% steps up to `--rate-cap`, default +30%), renders slides, assembles,
and validates streams + duration. It prints per-scene durations and a
path to `sheet.jpg`.

### 3. Validate visually — do not skip this

Open the printed `sheet.jpg` (frame-burst montage of the final video)
with your image-read tool. Confirm every scene: text not clipped,
kicker/title/bullets correct, callout present, no overlaps. A build is
not done until the sheet looks right.

### 4. Iterate

- **Over target after the rate cap** → shorten the narration text of the
  longest scenes (the report lists them), rebuild. Tighten words before
  raising rate past +25% — faster speech stops sounding natural. Remember
  each sentence fragment costs ~0.7s of pause: merging fragments into a
  flowing sentence buys back real time.
- **Visual nits** → edit scene content in the script, or the shared look
  in `templates/slides.template.html` (colors, type sizes, layout).
- **Different voice** → `--voice en-US-AvaNeural` (crisper) or
  `en-US-JennyNeural` (friendly default) or `en-US-GuyNeural`.
  Browse all: `edge-tts --list-voices`.

## Scene script format

Top level:

| key      | default              | meaning                                  |
|----------|----------------------|------------------------------------------|
| `name`   | script filename      | output name + build dir name             |
| `voice`  | `en-US-JennyNeural`  | edge-tts voice                           |
| `rate`   | `+20%`               | speech rate                              |
| `target` | none                 | total seconds to auto-fit toward         |
| `pad`    | `0.4`                | silence after narration, s per scene     |
| `scenes` | required             | list of scene objects                    |

Scene objects — all except `narration` are optional:

| field      | types          | meaning                                     |
|------------|----------------|---------------------------------------------|
| `type`     | all            | `title` \| `body` \| `outro` (default body) |
| `narration`| all, required  | spoken text for this scene                  |
| `hold`     | all            | extra seconds after narration (title/outro: 0.8) |
| `kicker`   | body           | small red uppercase label above the title   |
| `title`    | body/title/outro | big heading ("Simple words." on outro)    |
| `accent`   | outro          | second line, rendered in red ("Clear docs.")|
| `subtitle` | title/outro    | line under the heading                      |
| `meta`     | title          | bottom-left label, e.g. "EXPLAINER \| 30 SECONDS" |
| `text`     | body           | prose paragraph (use instead of bullets)    |
| `bullets`  | body           | list; 2-column at 4+, single column at 3-   |
| `callout`  | body           | red-bordered highlight box                  |
| `footer`   | any            | bottom-center label, e.g. "LEARN MORE: ..." |

Scene 1 fades in, last scene fades out (video + audio) automatically.
Body scenes get 02, 03, ... page numbers top-right.

## Gotchas (learned the hard way)

- **No libx264 on Fedora ffmpeg.** Use `-c:v libopenh264 -rc_mode
  quality` (the script handles this, and falls back if `rc_mode` is
  unsupported). Audio: `-c:a aac`.
- **Stills pipeline only.** Never screen-record an animated HTML page —
  screencast timelines drift seconds over a minute. Static PNG per scene,
  duration from measured narration.
- **edge-tts is a network service.** Single failures are usually
  transient (429/network) — the script retries once; on repeated
  failure, wait a moment and rerun rather than switching voices.
- **edge-tts 403 `WSServerHandshakeError` on every call** (with
  `Sec-MS-GEC` in the URL) means the installed version's pinned token
  handling is stale, NOT clock skew and NOT a network problem — first
  check `date -u` vs a server `Date:` header (rule out skew in one
  step), then upgrade: `/usr/bin/python3 -m pip install --user --upgrade
  edge-tts`. Seen 2026-11-12: 7.2.8 → permanent 403s, 7.3.1 → clean.
- **ffmpeg `tile` filter quirk:** a single literal output filename keeps
  only the FIRST tile. Use one `-frames:v 1` montage (what the script
  does) or a `%d` pattern.
- **Chrome headless** needs `--window-size=1920,1080` plus
  `--virtual-time-budget` so the inline render script has run before the
  screenshot (script handles both).
- **Tilted/curly quotes in narration** are fine; URLs should be spoken
  forms ("dot org"), and long dashes read better as sentence breaks.

## Deliverables

- Final mp4 — default `~/Videos/<name>.mp4`
- Build dir `/tmp/opencode/<name>-video/` with `narration/`, `slides/`,
  `segments/`, and `sheet.jpg` (the validation montage)
- Console JSON summary: paths, duration, scene count, voice, rate
