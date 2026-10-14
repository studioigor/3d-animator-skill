#!/usr/bin/env python3
"""Contact sheets and side-by-side review videos for animation QA.

  contact_sheet.py sheet VIDEO --times 1.0,1.2,1.4 [--cols 6] [--out sheet.jpg] [--label "text"] [--two-column]
  contact_sheet.py sheet VIDEO --range 3.0:5.0:0.1 ...      # start:end[:step] in seconds, end inclusive
  contact_sheet.py compare BEFORE AFTER --out cmp.mp4 [--labels "before,after"] [--half-speed] [--audio 2]
  contact_sheet.py grid V1 V2 V3 V4 --out grid.mp4 [--labels "a,b,c,d"] [--half-speed]
  (--labels takes "a|b" instead when a label contains a comma)

sheet    extracts exact frames (frame index = round((t - start) * fps), decoded with -fps_mode passthrough),
         makes tiles at --scale of the source size (half by default) with the time and frame number stamped
         on each tile by Pillow, and lays them out --cols wide. --range without a step takes every frame.
         --two-column splits a tall sheet into two side-by-side halves (read the left half top to bottom,
         then the right half).
compare  puts two videos side by side with burned-in labels. Different sizes are scaled to a common height
         (the smaller one, capped at --height). --half-speed also writes <out>_half.mp4.
grid     2x2 layout (V1 top-left, V2 top-right, V3 bottom-left, V4 bottom-right), e.g. before/after rows
         by gameplay-view/side-view columns. Cells are scaled to a common height and padded to a common width.

Labels are drawn with Pillow into one transparent PNG that is piped to ffmpeg and composited with the
overlay filter, so ffmpeg builds without drawtext work. No temporary files are written.
Needs ffmpeg and ffprobe on PATH and Pillow (pip install pillow).
"""
import argparse, json, math, os, shutil, subprocess, sys
from pathlib import Path

TILE_SCALE = 0.5             # sheet tile size as a fraction of the source frame
SHEET_GAP = 4                # pixels between sheet tiles
HALF_GAP = 24                # pixels between the two halves of a --two-column sheet
SHEET_BG = (24, 24, 24)      # sheet background colour
JPEG_QUALITY = 90            # quality for .jpg sheet output
COMPARE_MAX_H = 1080         # compare: common height cap in pixels (never upscales past the smaller input)
GRID_CELL_MAX_H = 720        # grid: cell height cap in pixels
CRF = 18                     # libx264 quality (lower = better, bigger)
PRESET = "medium"            # libx264 speed/size preset
LABEL_ALPHA = 170            # opacity (0-255) of the box behind burned-in labels
FONT_CANDIDATES = [          # first existing TrueType font wins; Pillow's built-in font is the fallback
    "/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
]
PNG_END = b"IEND\xaeB`\x82"  # last 8 bytes of every PNG file, used to split ffmpeg's image2pipe stream

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("contact_sheet.py needs Pillow: pip install pillow")


# ---------- helpers ----------

def need_tool(name):
    path = shutil.which(name)
    if not path:
        sys.exit(f"contact_sheet.py needs {name} on PATH "
                 "(macOS: brew install ffmpeg, Debian/Ubuntu: apt install ffmpeg, Windows: winget install ffmpeg)")
    return path


def font(size):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)     # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def text_size(draw, txt, fnt):
    x0, y0, x1, y1 = draw.textbbox((0, 0), txt, font=fnt)
    return x1 - x0, y1 - y0, y0


def label_box(draw, xy, txt, fnt, alpha=LABEL_ALPHA, pad=None):
    """Draw white text on a translucent black box with its top-left corner at xy."""
    tw, th, yoff = text_size(draw, txt, fnt)
    pad = pad if pad is not None else max(3, th // 3)
    x, y = xy
    draw.rectangle([x, y, x + tw + 2 * pad, y + th + 2 * pad], fill=(0, 0, 0, alpha))
    draw.text((x + pad, y + pad - yoff), txt, font=fnt, fill=(255, 255, 255, 255))
    return tw + 2 * pad, th + 2 * pad


def probe(path):
    """Size, frame rate, frame count and start time of the first video stream."""
    ffprobe = need_tool("ffprobe")
    if not Path(path).exists():
        sys.exit(f"no such file: {path}")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,start_time:"
           "stream_side_data=rotation:stream_tags=rotate:format=duration", "-of", "json", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffprobe failed on {path}: {r.stderr.strip()}")
    doc = json.loads(r.stdout)
    if not doc.get("streams"):
        sys.exit(f"no video stream in {path}")
    s = doc["streams"][0]

    def rate(txt):
        try:
            n, d = txt.split("/")
            return float(n) / float(d) if float(d) else 0.0
        except (ValueError, AttributeError):
            return 0.0

    fps = rate(s.get("avg_frame_rate")) or rate(s.get("r_frame_rate"))
    if fps <= 0:
        sys.exit(f"cannot read the frame rate of {path}")
    rot = 0
    for sd in s.get("side_data_list", []) or []:
        rot = int(float(sd.get("rotation", 0) or 0))
    rot = rot or int(float((s.get("tags") or {}).get("rotate", 0) or 0))
    w, h = int(s["width"]), int(s["height"])
    if abs(rot) % 180 == 90:                         # ffmpeg auto-rotates, so report the displayed size
        w, h = h, w
    dur = float(s.get("duration") or doc.get("format", {}).get("duration") or 0)
    n = int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else int(round(dur * fps))
    vfr = abs(rate(s.get("avg_frame_rate")) - rate(s.get("r_frame_rate"))) > 0.01 * fps
    return {"w": w, "h": h, "fps": fps, "frames": n, "duration": dur, "vfr": vfr,
            "start": float(s.get("start_time") or 0.0)}


def even(x):
    return max(2, int(round(x / 2.0)) * 2)


def pick_encoder(ffmpeg):
    out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx264 " in out:
        return ["-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET]
    if " h264_videotoolbox " in out:
        return ["-c:v", "h264_videotoolbox", "-b:v", "12M"]
    return ["-c:v", "mpeg4", "-q:v", "2"]


def run_ffmpeg(cmd, stdin_bytes=None):
    r = subprocess.run(cmd, input=stdin_bytes, capture_output=True)
    if r.returncode != 0:
        sys.exit("ffmpeg failed:\n" + r.stderr.decode("utf-8", "replace")[-3000:])


def png_bytes(img):
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------- sheet ----------

def parse_times(a, info):
    if a.times:
        try:
            return [float(t) for t in a.times.split(",") if t.strip()]
        except ValueError:
            sys.exit(f"--times must be comma-separated seconds, got '{a.times}'")
    parts = a.range.split(":")
    if len(parts) not in (2, 3):
        sys.exit(f"--range must be start:end[:step], got '{a.range}'")
    start, end = float(parts[0]), float(parts[1])
    step = float(parts[2]) if len(parts) == 3 else 1.0 / info["fps"]
    if step <= 0 or end < start:
        sys.exit("--range needs end >= start and a positive step")
    n = int(math.floor((end - start) / step + 1e-6)) + 1
    return [round(start + i * step, 6) for i in range(n)]


def extract_frames(video, indices, scale):
    """Decode the requested frame indices; returns {index: PIL image} at the requested scale."""
    ffmpeg = need_tool("ffmpeg")
    uniq = sorted(set(indices))
    sel = "+".join(f"eq(n,{i})" for i in uniq)
    vf = f"select='{sel}',scale='trunc(iw*{scale}/2)*2':'trunc(ih*{scale}/2)*2':flags=area"
    cmd = [ffmpeg, "-v", "error", "-i", str(video), "-vf", vf, "-fps_mode", "passthrough",
           "-frames:v", str(len(uniq)), "-f", "image2pipe", "-c:v", "png", "-"]
    import io
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frames, buf = [], b""
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        while True:
            k = buf.find(PNG_END)
            if k < 0:
                break
            frames.append(Image.open(io.BytesIO(buf[:k + len(PNG_END)])).convert("RGB"))
            buf = buf[k + len(PNG_END):]
    err = proc.stderr.read().decode("utf-8", "replace")
    proc.wait()
    if proc.returncode != 0:
        sys.exit(f"ffmpeg failed while extracting frames:\n{err[-2000:]}")
    if len(frames) != len(uniq):
        sys.exit(f"asked ffmpeg for {len(uniq)} frames, got {len(frames)} (variable frame rate or a "
                 f"frame index past the end?)")
    return dict(zip(uniq, frames))


def cmd_sheet(a):
    info = probe(a.video)
    if info["vfr"]:
        print("warning: variable frame rate video; frame indices from timestamps are approximate", file=sys.stderr)
    times = parse_times(a, info)
    if not times:
        sys.exit("no times to extract")
    last = max(0, info["frames"] - 1)
    idx = []
    for t in times:
        i = int(round((t - info["start"]) * info["fps"]))
        if i < 0 or i > last:
            print(f"warning: t={t:.3f}s is outside the video (0..{last / info['fps']:.3f}s), clamped",
                  file=sys.stderr)
        idx.append(min(max(i, 0), last))
    frames = extract_frames(a.video, idx, a.scale)
    tiles = []
    for t, i in zip(times, idx):
        im = frames[i].copy().convert("RGBA")
        over = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        fnt = font(max(12, im.height // 16))
        stamp = f"{t:.2f}s  f{i}"
        tw, th, _ = text_size(d, stamp, fnt)
        pad = max(3, th // 3)
        label_box(d, (4, im.height - th - 2 * pad - 4), stamp, fnt)
        tiles.append(Image.alpha_composite(im, over).convert("RGB"))

    tw, th = tiles[0].size
    cols = max(1, min(a.cols, len(tiles)))
    rows = math.ceil(len(tiles) / cols)
    blocks = [tiles]
    if a.two_column and rows > 1:
        split = math.ceil(rows / 2) * cols                 # left half keeps the first ceil(rows/2) rows
        blocks = [tiles[:split], tiles[split:]]
    block_rows = [math.ceil(len(b) / cols) for b in blocks]
    block_w = cols * tw + (cols - 1) * SHEET_GAP
    grid_w = len(blocks) * block_w + (len(blocks) - 1) * HALF_GAP
    grid_h = max(block_rows) * th + (max(block_rows) - 1) * SHEET_GAP
    header_h = 0
    if a.label:
        hf, sf = font(max(18, th // 7)), font(max(12, th // 14))
        info_line = (f"{Path(a.video).name}  {info['w']}x{info['h']}  {info['fps']:.3f} fps  "
                     f"{len(times)} frames  {times[0]:.2f}-{times[-1]:.2f}s")
        probe_d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        h1, h2 = text_size(probe_d, "Ag" + a.label, hf)[1], text_size(probe_d, "Ag" + info_line, sf)[1]
        header_h = h1 + h2 + 4 * SHEET_GAP
    W, H = grid_w + 2 * SHEET_GAP, grid_h + header_h + 2 * SHEET_GAP
    sheet = Image.new("RGB", (W, H), SHEET_BG)
    d = ImageDraw.Draw(sheet)
    if a.label:
        d.text((SHEET_GAP * 2, SHEET_GAP), a.label, font=hf, fill=(255, 255, 255))
        d.text((SHEET_GAP * 2, 2 * SHEET_GAP + h1), info_line, font=sf, fill=(170, 170, 170))
    for b, block in enumerate(blocks):
        x0 = SHEET_GAP + b * (block_w + HALF_GAP)
        for k, tile in enumerate(block):
            r, c = divmod(k, cols)
            sheet.paste(tile, (x0 + c * (tw + SHEET_GAP), SHEET_GAP + header_h + r * (th + SHEET_GAP)))
        if b > 0:
            xs = x0 - HALF_GAP // 2
            d.line([(xs, SHEET_GAP + header_h), (xs, H - SHEET_GAP)], fill=(90, 90, 90), width=2)
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        sheet.save(out, quality=JPEG_QUALITY)
    else:
        sheet.save(out)
    print(f"{out}  {W}x{H}  {len(tiles)} tiles  frames {idx[0]}..{idx[-1]}")


# ---------- compare / grid ----------

def parse_labels(txt, videos):
    sep = "|" if txt and "|" in txt else ","                 # "|" lets labels contain commas
    labels = [s.strip() for s in txt.split(sep)] if txt else [Path(v).stem for v in videos]
    if len(labels) != len(videos):
        sys.exit(f"--labels needs {len(videos)} comma-separated names, got {len(labels)}")
    return labels


def label_overlay(size, boxes):
    """One transparent RGBA image of the full composite with a label at each (x, y, text)."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fnt = font(max(14, size[1] // (22 if len(boxes) <= 2 else 40)))
    for x, y, txt in boxes:
        label_box(d, (x + 10, y + 10), txt, fnt)
    return img


def compose(videos, infos, labels, out, cells, xy, canvas, layout, a):
    """cells: per input (scaled w, h, padded w, h); xy: top-left corner of each cell in the composite;
    layout: ffmpeg filter that stacks [v0]..[vN] into [st]."""
    ffmpeg = need_tool("ffmpeg")
    fps = infos[0]["fps"]
    cmd = [ffmpeg, "-y", "-v", "error"]
    for v in videos:
        cmd += ["-i", str(v)]
    cmd += ["-f", "png_pipe", "-i", "pipe:0"]
    chains = []
    for i, (sw, sh, pw, ph) in enumerate(cells):
        pad = f",pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color=black" if (pw, ph) != (sw, sh) else ""
        chains.append(f"[{i}:v]fps={fps:.6f},scale={sw}:{sh}:flags=lanczos,setsar=1{pad}[v{i}]")
    n = len(videos)
    chains.append(layout)
    chains.append(f"[st][{n}:v]overlay=0:0:format=auto,format=yuv420p[out]")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[out]"]
    if a.audio:
        if not 1 <= a.audio <= n:
            sys.exit(f"--audio must be between 1 and {n}")
        cmd += ["-map", f"{a.audio - 1}:a?", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd += pick_encoder(ffmpeg) + ["-r", f"{fps:.6f}", "-movflags", "+faststart", str(out)]
    run_ffmpeg(cmd, png_bytes(label_overlay(canvas, [(x, y, t) for (x, y), t in zip(xy, labels)])))
    print(f"{out}  {canvas[0]}x{canvas[1]}  {fps:.3f} fps")
    if a.half_speed:
        half = out.with_name(out.stem + "_half" + out.suffix)
        cmd = [ffmpeg, "-y", "-v", "error", "-i", str(out), "-filter:v", "setpts=2.0*PTS"]
        cmd += ["-filter:a", "atempo=0.5", "-c:a", "aac", "-b:a", "192k"] if a.audio else ["-an"]
        cmd += pick_encoder(ffmpeg) + ["-r", f"{fps:.6f}", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                                       str(half)]
        run_ffmpeg(cmd)
        print(f"{half}  half speed")


def cmd_compare(a):
    vids = [a.before, a.after]
    labels = parse_labels(a.labels, vids)
    infos = [probe(v) for v in vids]
    h = even(min(min(i["h"] for i in infos), a.height or COMPARE_MAX_H))
    cells = []
    for i in infos:
        w = even(i["w"] * h / i["h"])
        cells.append((w, h, w, h))
    xy = [(0, 0), (cells[0][0], 0)]
    canvas = (cells[0][0] + cells[1][0], h)
    shortest = 1 if a.shortest else 0
    compose(vids, infos, labels, a.out, cells, xy, canvas, f"[v0][v1]hstack=inputs=2:shortest={shortest}[st]", a)


def cmd_grid(a):
    vids = a.videos
    if len(vids) != 4:
        sys.exit(f"grid needs exactly 4 videos, got {len(vids)}")
    labels = parse_labels(a.labels, vids)
    infos = [probe(v) for v in vids]
    h = even(min(min(i["h"] for i in infos), a.height or GRID_CELL_MAX_H))
    widths = [even(i["w"] * h / i["h"]) for i in infos]
    cw = max(widths)
    cells = [(w, h, cw, h) for w in widths]
    xy = [(0, 0), (cw, 0), (0, h), (cw, h)]
    shortest = 1 if a.shortest else 0
    layout = f"[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|{cw}_0|0_{h}|{cw}_{h}:shortest={shortest}[st]"
    compose(vids, infos, labels, a.out, cells, xy, (2 * cw, 2 * h), layout, a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="contact sheet of exact frames")
    s.add_argument("video")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--times", help="comma-separated seconds, e.g. 1.0,1.2,1.4")
    g.add_argument("--range", help="start:end[:step] seconds, end inclusive; no step = every frame")
    s.add_argument("--cols", type=int, default=6)
    s.add_argument("--out", default="sheet.jpg")
    s.add_argument("--label", help="header text")
    s.add_argument("--two-column", action="store_true", help="split a tall sheet into two side-by-side halves")
    s.add_argument("--scale", type=float, default=TILE_SCALE, help="tile size as a fraction of the frame")
    s.set_defaults(fn=cmd_sheet)
    for name, fn, nargs, hlp in (("compare", cmd_compare, None, "side-by-side video with labels"),
                                 ("grid", cmd_grid, 4, "2x2 video grid with labels")):
        s = sub.add_parser(name, help=hlp)
        if nargs:
            s.add_argument("videos", nargs=nargs)
        else:
            s.add_argument("before")
            s.add_argument("after")
        s.add_argument("--out", required=True)
        s.add_argument("--labels", help="comma- or |-separated labels (default: file names)")
        s.add_argument("--half-speed", action="store_true", help="also write <out>_half.mp4")
        s.add_argument("--height", type=int, help="common height cap in pixels")
        s.add_argument("--audio", type=int, help="keep the audio of input N (1-based); default silent")
        s.add_argument("--shortest", action="store_true", help="stop at the shortest input (default: hold "
                                                                "the last frame of shorter inputs)")
        s.set_defaults(fn=fn)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
