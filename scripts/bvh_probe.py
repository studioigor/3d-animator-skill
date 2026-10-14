#!/usr/bin/env python3
"""Find, measure and extract mocap clips from the animation library.

  bvh_probe.py search "walk" [--lib DIR]           # CMU index, 100STYLE styles, Mesh2Motion GLB clip names, loose BVH
  bvh_probe.py stats CLIP [--feet] [--target-speed 1.4]
  bvh_probe.py extract CLIP --out DIR [--trim]     # copy one clip out of the archives
  bvh_probe.py sheet CLIP [CLIP...] --out sheet.png [--times 8] [--trim]   # stick-figure contact sheet

CLIP is a .bvh path, a CMU id (08_01), or a 100STYLE id (Angry_FW). --feet needs numpy and adds
foot contacts, cadence and stride from forward kinematics. --target-speed says what playback rate
the clip needs for a given travel speed in m/s and warns when the gait would read wrong.
Metric values assume a hip-to-ankle leg of 0.85 m (estimate); scale-free values are in leg lengths.
sheet draws one row per clip with N evenly spaced frames, each as a front view (the character faces you)
and a side view (the character faces right), from the file's forward kinematics: left limbs blue, right
limbs red, spine grey, a tick for the head direction, a ground line, and root travel in body heights.
sheet needs numpy and Pillow.
"""
import argparse, csv, io, json, math, os, re, struct, sys, zipfile
from pathlib import Path

LEG_M = 0.85            # assumed adult hip-to-ankle length in metres (estimate)
RATE_OK = (0.85, 1.2)   # playback-rate band where a walk or run still reads natural (estimate)
SHEET_CELL_H = 200      # sheet: height of one stick-figure view in pixels
SHEET_CELL_W = 165      # sheet: width of one stick-figure view in pixels
SHEET_FILL = 0.8        # sheet: body height as a fraction of the view height
SHEET_COLORS = {"left": (70, 130, 255), "right": (235, 60, 60), "mid": (160, 160, 160)}  # limb colours
LEFT_RE = re.compile(r"(?i:left)|^L(?=[A-Z_ .])|[_ .:][Ll]$|^l_")      # joint-name patterns for the
RIGHT_RE = re.compile(r"(?i:right)|^R(?=[A-Z_ .])|[_ .:][Rr]$|^r_")    # left and right sides


def lib_root(arg):
    return Path(arg or os.environ.get("ANIM_LIBRARY") or Path.home() / "AnimationLibrary").expanduser()


def find_sources(root):
    src = {"cmu": None, "style": None, "glb": [], "bvh": []}
    for p in root.rglob("*"):
        if p.name == "cmubvh-main.zip":
            src["cmu"] = p
        elif p.name == "100STYLE.zip":
            src["style"] = p
        elif p.suffix == ".glb" and "animations" in p.parent.name:
            src["glb"].append(p)
        elif p.suffix.lower() == ".bvh":
            src["bvh"].append(p)
    return src


# ---------- archives ----------

def cmu_index(z):
    name = next(n for n in z.namelist() if n.endswith("cmu-mocap-index-text.txt"))
    rows, subject = [], ""
    for line in z.read(name).decode("utf-8", "replace").splitlines():
        m = re.match(r"Subject #(\d+) \((.*)\)", line.strip())
        if m:
            subject = m.group(2)
        m = re.match(r"(\d\d+_\d\d+)\s+(.*)", line.strip())
        if m:
            rows.append((m.group(1), m.group(2), subject))
    return rows


def cmu_read(z, clip):
    inner = next((n for n in z.namelist() if n.endswith(f"/{clip}.zip")), None)
    if not inner:
        return None
    with zipfile.ZipFile(io.BytesIO(z.read(inner))) as iz:
        return iz.read(next(n for n in iz.namelist() if n.endswith(".bvh"))).decode("latin-1")


def style_tables(z):
    styles = list(csv.DictReader(io.StringIO(z.read("100STYLE/Dataset_List.csv").decode("utf-8", "replace"))))
    cuts = {r["STYLE_NAME"]: r for r in csv.DictReader(io.StringIO(z.read("100STYLE/Frame_Cuts.csv").decode()))}
    return styles, cuts


STYLE_DIRS = {"FW": "forward walk", "FR": "forward run", "BW": "backward walk", "BR": "backward run",
              "SW": "sideways walk", "SR": "sideways run", "ID": "idle",
              "TR1": "transitions 1", "TR2": "transitions 2", "TR3": "transitions 3"}


def glb_animation_names(path):
    with open(path, "rb") as f:
        magic, _, _ = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF":
            return []
        ln, kind = struct.unpack("<I4s", f.read(8))
        doc = json.loads(f.read(ln))
    return [a.get("name", f"anim{i}") for i, a in enumerate(doc.get("animations", []))]


def load_clip(clip, root):
    p = Path(clip).expanduser()
    if p.suffix.lower() == ".bvh" and p.exists():
        return p.stem, p.read_text(encoding="latin-1"), None
    src = find_sources(root)
    if re.fullmatch(r"\d\d+_\d\d+", clip) and src["cmu"]:
        with zipfile.ZipFile(src["cmu"]) as z:
            text = cmu_read(z, clip)
        if text:
            return clip, text, None
    m = re.fullmatch(r"(.+)_(FW|FR|BW|BR|SW|SR|ID|TR1|TR2|TR3)", clip)
    if m and src["style"]:
        with zipfile.ZipFile(src["style"]) as z:
            name = f"100STYLE/{m.group(1)}/{clip}.bvh"
            if name in z.namelist():
                _, cuts = style_tables(z)
                row = cuts.get(m.group(1), {})
                cut = (row.get(f"{m.group(2)}_START"), row.get(f"{m.group(2)}_STOP"))
                cut = tuple(int(c) for c in cut) if all(c and c != "N/A" for c in cut) else None
                return clip, z.read(name).decode("latin-1"), cut
    sys.exit(f"clip '{clip}' not found (library: {root})")


# ---------- BVH ----------

class Bvh:
    def __init__(self, text):
        self.names, self.parent, self.offset, self.channels, self.ends = [], [], [], [], {}
        tok = iter(text.split())
        stack, in_end = [], False
        for t in tok:
            if t in ("ROOT", "JOINT"):
                self.names.append(next(tok))
                self.parent.append(stack[-1] if stack else -1)
                self.offset.append(None)
                self.channels.append([])
                stack.append(len(self.names) - 1)
            elif t == "End":
                next(tok)
                in_end = True
            elif t == "OFFSET":
                v = [float(next(tok)) for _ in range(3)]
                if in_end:
                    self.ends[stack[-1]] = v
                else:
                    self.offset[stack[-1]] = v
            elif t == "CHANNELS":
                self.channels[stack[-1]] = [next(tok) for _ in range(int(next(tok)))]
            elif t == "}":
                if in_end:
                    in_end = False
                else:
                    stack.pop()
            elif t == "MOTION":
                break
        next(tok)                       # Frames:
        self.nframes = int(next(tok))
        next(tok); next(tok)            # Frame Time:
        self.dt = float(next(tok))
        width = sum(len(c) for c in self.channels)
        vals = [float(v) for v in tok]
        self.frames = [vals[i * width:(i + 1) * width] for i in range(len(vals) // width)]
        self.nframes = len(self.frames)
        self.col = []
        k = 0
        for ch in self.channels:
            self.col.append(list(range(k, k + len(ch))))
            k += len(ch)

    def find(self, *patterns):
        for pat in patterns:
            for i, n in enumerate(self.names):
                if re.search(pat, n, re.I):
                    return i
        return None

    def leg_length(self, foot):
        """Thigh + shin: the foot's offset (shin) plus its parent's offset (thigh)."""
        knee = self.parent[foot]
        return math.dist(self.offset[foot], (0, 0, 0)) + math.dist(self.offset[knee], (0, 0, 0))

    def root_positions(self, up):
        ch = self.channels[0]
        idx = {c[0].upper(): self.col[0][i] for i, c in enumerate(ch) if c.lower().endswith("position")}
        if len(idx) < 3:
            return None
        return [(f[idx["X"]], f[idx["Y"]], f[idx["Z"]]) for f in self.frames]


def stats(name, text, cut, up, feet, target):
    b = Bvh(text)
    frames = b.frames[cut[0]:cut[1]] if cut else b.frames
    b.frames = frames
    b.nframes = len(frames)
    fps = 1.0 / b.dt
    dur = b.nframes * b.dt
    ui = "XYZ".index(up)
    hz = [i for i in range(3) if i != ui]
    lfoot = b.find(r"^l(eft)?[_ ]?foot$", r"left.*foot", r"left.*ankle", r"^l.*ankle")
    leg = b.leg_length(lfoot) if lfoot is not None else 0.0
    out = {"clip": name, "fps": round(fps, 2), "frames": b.nframes, "seconds": round(dur, 2),
           "joints": len(b.names),
           "finger_joints": sum(bool(re.search(r"finger|thumb|index|pinky|ring|middle", n, re.I)) for n in b.names)}
    if cut:
        out["trimmed_to"] = list(cut)
    pos = b.root_positions(up)
    if pos and leg > 0:
        m = LEG_M / leg
        hip = sorted(p[ui] for p in pos)[len(pos) // 2]
        path = sum(math.dist([pos[i][k] for k in hz], [pos[i - 1][k] for k in hz]) for i in range(1, len(pos)))
        net = math.dist([pos[-1][k] for k in hz], [pos[0][k] for k in hz])
        w = max(1, min(len(pos) - 1, round(1.0 / b.dt)))      # 1 s windows: robust to curved or looping paths
        win = sorted(math.dist([pos[i + w][k] for k in hz], [pos[i][k] for k in hz]) / (w * b.dt)
                     for i in range(0, len(pos) - w, max(1, w // 4)))
        speed = win[len(win) // 2] if win else net / dur
        out.update({"units_per_m_est": round(1 / m, 2), "hip_height_m": round(hip * m, 2),
                    "speed_m_s": round(speed * m, 2), "net_speed_m_s": round(net / dur * m, 2),
                    "path_speed_m_s": round(path / dur * m, 2), "speed_legs_per_s": round(speed / leg, 2)})
        if target and speed > 0:
            rate = target / (speed * m)
            out["rate_for_target"] = round(rate, 2)
            out["rate_verdict"] = ("ok" if RATE_OK[0] <= rate <= RATE_OK[1] else
                                   "too fast: steps will shuffle or skate, pick a faster clip or a run" if rate > RATE_OK[1]
                                   else "too slow: steps will look floaty, pick a slower clip")
    if feet:
        out.update(foot_stats(b, up, leg))
    return out


def fk_global(b, np):
    """Global 4x4 transform of every joint for every frame (numpy arrays of shape frames x 4 x 4)."""
    F = b.nframes
    data = np.array(b.frames)
    glob = [None] * len(b.names)

    def rot(axis, deg):
        a = np.radians(deg); c, s = np.cos(a), np.sin(a)
        R = np.zeros((len(a), 3, 3)); R[:, 0, 0] = R[:, 1, 1] = R[:, 2, 2] = 1
        i, j = {"X": (1, 2), "Y": (2, 0), "Z": (0, 1)}[axis]
        R[:, i, i] = c; R[:, j, j] = c; R[:, i, j] = -s; R[:, j, i] = s
        return R

    for j in range(len(b.names)):
        T = np.tile(np.eye(4), (F, 1, 1))
        T[:, :3, 3] = b.offset[j]
        R = np.tile(np.eye(3), (F, 1, 1))
        for k, ch in enumerate(b.channels[j]):
            col = data[:, b.col[j][k]]
            if ch.lower().endswith("position"):
                T[:, "XYZ".index(ch[0].upper()), 3] += col if j == 0 else 0
            else:
                R = R @ rot(ch[0].upper(), col)
        T[:, :3, :3] = R
        glob[j] = T if b.parent[j] == -1 else glob[b.parent[j]] @ T
    return glob


def foot_stats(b, up, leg):
    try:
        import numpy as np
    except ImportError:
        return {"feet": "needs numpy"}
    F = b.nframes
    glob = fk_global(b, np)
    ui = "XYZ".index(up)
    hz = [i for i in range(3) if i != ui]
    res, lf_h = {}, None
    for side, pats in (("left", (r"^l(eft)?[_ ]?foot$", r"left.*foot", r"left.*ankle")),
                       ("right", (r"^r(ight)?[_ ]?foot$", r"right.*foot", r"right.*ankle"))):
        j = b.find(*pats)
        if j is None:
            continue
        p = glob[j][:, :3, 3]
        h = p[:, ui]
        v = np.linalg.norm(np.diff(p[:, hz], axis=0), axis=1) / b.dt
        v = np.append(v, v[-1])
        floor = np.percentile(h, 5)
        contact = (h < floor + 0.06 * leg) & (v < 0.35 * leg)
        if side == "left":
            lf_h = h
        slide = float(np.median(v[contact]) / leg * LEG_M) if contact.any() else 0.0
        res[f"{side}_contact_pct"] = round(float(contact.mean() * 100), 1)
        res[f"{side}_slide_m_s"] = round(slide, 3)
    if lf_h is not None and F * b.dt > 1.5:
        x = lf_h - lf_h.mean()
        ac = np.correlate(x, x, "full")[F - 1:]
        lo, hi = int(0.4 / b.dt), min(F - 1, int(2.5 / b.dt))     # stride period window
        if hi > lo + 2:
            seg = ac[lo:hi]
            peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]
                     and seg[i] > 0.3 * ac[0]]
            if peaks:
                period = (lo + peaks[0]) * b.dt
                res["stride_period_s"] = round(period, 3)
                res["cadence_steps_per_min"] = round(120 / period, 1)
    return res


# ---------- stick-figure sheet ----------

def joint_side(name):
    return "left" if LEFT_RE.search(name) else "right" if RIGHT_RE.search(name) else "mid"


def sheet_font(size):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def clip_pose(name, text, cut, up, n, np):
    """Joint and end-site positions at n evenly spaced frames plus everything the sheet row needs."""
    b = Bvh(text)
    if cut:
        b.frames = b.frames[cut[0]:cut[1]]
        b.nframes = len(b.frames)
    if b.nframes < 1:
        sys.exit(f"{name}: no frames")
    ui = "XYZ".index(up)
    upv = np.eye(3)[ui]
    glob = fk_global(b, np)
    J = len(b.names)
    P = np.stack([g[:, :3, 3] for g in glob], 1)                          # frames x joints x 3
    ends = sorted(b.ends)
    E = np.stack([(glob[j] @ np.append(b.ends[j], 1.0))[:, :3] for j in ends], 1) if ends else np.zeros((b.nframes, 0, 3))
    # rest pose (all rotations zero): body height and the forward direction
    rest = [None] * J
    for j in range(J):
        o = np.array(b.offset[j], float)
        rest[j] = o if b.parent[j] < 0 else rest[b.parent[j]] + o
    rest_all = np.array(rest + [rest[j] + np.array(b.ends[j]) for j in ends])
    ext = rest_all.max(0) - rest_all.min(0)
    height = ext[ui] if ext[ui] > 0.5 * ext.max() else ext.max()          # lying rest pose: largest extent
    side = [joint_side(nm) for nm in b.names]
    left = [rest[j] for j in range(J) if side[j] == "left"]
    right = [rest[j] for j in range(J) if side[j] == "right"]
    lat = (np.mean(left, 0) - np.mean(right, 0)) if left and right else np.eye(3)[0]
    fwd_rest = np.cross(lat, upv)
    toes = [rest[j] + np.array(b.ends[j]) - rest[j] for j in ends if re.search(r"toe|foot", b.names[j], re.I)]
    if toes and np.dot(np.mean(toes, 0), fwd_rest) < 0:                   # trust the toes for the sign
        fwd_rest = -fwd_rest
    fwd_rest -= upv * np.dot(fwd_rest, upv)
    fwd_rest /= np.linalg.norm(fwd_rest) or 1.0
    idx = np.unique(np.linspace(0, b.nframes - 1, n).round().astype(int))
    f0 = idx[0]
    fwd = glob[0][f0, :3, :3] @ fwd_rest                                   # facing at the first shown frame
    fwd -= upv * np.dot(fwd, upv)
    fwd /= np.linalg.norm(fwd) or 1.0
    head = b.find(r"(^|:)head$", r"head")
    hz = [i for i in range(3) if i != ui]
    root = P[:, 0]
    net = float(np.linalg.norm(root[-1, hz] - root[0, hz]))
    path = float(np.linalg.norm(np.diff(root[:, hz], axis=0), axis=1).sum())
    lows = np.concatenate([P[..., ui], E[..., ui]], 1).min(1)
    return {"b": b, "P": P, "E": E, "ends": ends, "idx": idx, "fwd": fwd, "up": upv, "ui": ui, "height": height,
            "floor": float(np.percentile(lows, 2)), "side": side, "head": head, "glob": glob, "fwd_rest": fwd_rest,
            "net": net / height, "path": path / height, "fps": 1.0 / b.dt, "frames": b.nframes}


def draw_view(c, f, view, np):
    """One stick figure (frame f) as a Pillow image. view: 'front' (faces you) or 'side' (faces right)."""
    from PIL import Image, ImageDraw
    W, H = SHEET_CELL_W, SHEET_CELL_H
    img = Image.new("RGB", (W, H), (28, 28, 30))
    d = ImageDraw.Draw(img)
    upv, fwd = c["up"], c["fwd"]
    leftv = np.cross(upv, fwd)
    xaxis, depth = (leftv, fwd) if view == "front" else (fwd, -leftv)
    s = SHEET_FILL * H / c["height"]
    ground = H - 14
    cx = float(np.dot(c["P"][f, 0], xaxis))

    def pt(p):
        return (W / 2 + (float(np.dot(p, xaxis)) - cx) * s, ground - (float(p[c["ui"]]) - c["floor"]) * s)

    d.line([(0, ground), (W, ground)], fill=(90, 90, 80), width=1)
    b, P, E = c["b"], c["P"][f], c["E"][f]
    segs = [(P[b.parent[j]], P[j], c["side"][j]) for j in range(len(b.names)) if b.parent[j] >= 0]
    segs += [(P[j], E[k], c["side"][j]) for k, j in enumerate(c["ends"]) if j != c["head"]]
    segs.sort(key=lambda sg: float(np.dot(sg[0] + sg[1], depth)))           # far segments first
    width = max(2, H // 70)
    for a, e, sd in segs:
        d.line([pt(a), pt(e)], fill=SHEET_COLORS[sd], width=width)
    if c["head"] is not None:
        h = c["head"]
        top = E[c["ends"].index(h)] if h in c["ends"] else P[h] + upv * 0.12 * c["height"]
        hc = (P[h] + top) / 2
        r = 0.06 * c["height"] * s
        x, y = pt(hc)
        d.ellipse([x - r, y - r, x + r, y + r], outline=SHEET_COLORS["mid"], width=width)
        look = c["glob"][h][f, :3, :3] @ c["fwd_rest"]                    # head direction relative to rest
        tip = pt(hc + look * 0.14 * c["height"])
        d.line([(x, y), tip], fill=(255, 220, 60), width=width)
        d.ellipse([tip[0] - 2, tip[1] - 2, tip[0] + 2, tip[1] + 2], fill=(255, 220, 60))
    return img


def cmd_sheet(a):
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError as e:
        sys.exit(f"sheet needs numpy and Pillow: pip install numpy pillow ({e})")
    root = lib_root(a.lib)
    if a.times < 1:
        sys.exit("--times must be 1 or more")
    clips = []
    for clip in a.clips:
        name, text, cut = load_clip(clip, root)
        if a.trim and not cut:
            print(f"note: {name} has no clean-section cut; using every frame", file=sys.stderr)
        clips.append((name, clip_pose(name, text, cut if a.trim else None, a.up, a.times, np), cut if a.trim else None))
    pad, head_h, lab_h = 8, 26, 18
    pair_w = 2 * SHEET_CELL_W + 4
    ncols = max(len(c["idx"]) for _, c, _ in clips)
    W = pad + ncols * (pair_w + pad)
    row_h = head_h + SHEET_CELL_H + lab_h + pad
    title_h = 30
    sheet = Image.new("RGB", (max(W, 900), title_h + len(clips) * row_h + pad), (16, 16, 18))
    d = ImageDraw.Draw(sheet)
    big, small = sheet_font(15), sheet_font(12)
    d.text((pad, 7), "front: faces you | side: faces right | blue left, red right, grey spine | yellow tick: "
                     "head direction | travel in body heights (BH)", font=small, fill=(200, 200, 200))
    for r, (name, c, cut) in enumerate(clips):
        y0 = title_h + r * row_h
        trim = f"  trimmed {cut[0]}-{cut[1]}" if cut else ""
        d.text((pad, y0 + 4), f"{name}   {c['fps']:.0f} fps  {c['frames']} frames ({c['frames'] / c['fps']:.2f} s){trim}"
                              f"   root travel {c['net']:.2f} BH net, {c['path']:.2f} BH path", font=big,
               fill=(240, 240, 240))
        f0 = c["idx"][0]
        hz = [i for i in range(3) if i != c["ui"]]
        for k, f in enumerate(c["idx"]):
            x = pad + k * (pair_w + pad)
            sheet.paste(draw_view(c, f, "front", np), (x, y0 + head_h))
            sheet.paste(draw_view(c, f, "side", np), (x + SHEET_CELL_W + 4, y0 + head_h))
            adv = float(np.dot(c["P"][f, 0] - c["P"][f0, 0], c["fwd"])) / c["height"]
            d.text((x + 2, y0 + head_h + SHEET_CELL_H + 2), f"{f / c['fps']:.2f}s  f{f}  fwd {adv:+.2f} BH",
                   font=small, fill=(190, 190, 190))
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"{out}  {sheet.width}x{sheet.height}  {len(clips)} clip(s) x {ncols} frames")


# ---------- commands ----------

def cmd_search(a):
    root = lib_root(a.lib)
    src = find_sources(root)
    q = re.compile(a.query, re.I)
    hits = []
    if src["cmu"]:
        with zipfile.ZipFile(src["cmu"]) as z:
            hits += [f"cmu       {cid:8s} {d}  [{s}]" for cid, d, s in cmu_index(z) if q.search(d) or q.search(s)]
    if src["style"]:
        with zipfile.ZipFile(src["style"]) as z:
            styles, _ = style_tables(z)
        found = [r for r in styles if q.search(f"{r['Style Name']} {r['Description']}")]
        hits += [f"100style  {r['Style Name']}_<FW|FR|BW|BR|SW|SR|ID|TR1-3>  {r['Description']}" for r in found]
        if not found and q.search("walk run idle sideways backward forward strafe"):
            hits.append("100style  all 100 styles have walk and run in FW/BW/SW (forward, backward, sideways) "
                        "plus ID idle: search a style word (old, proud, sneak, wound) to narrow")
    for g in src["glb"]:
        hits += [f"glb       {g.stem}: {n}" for n in glb_animation_names(g) if q.search(n)]
    hits += [f"bvh       {p}" for p in src["bvh"] if q.search(p.stem)]
    if not hits:
        print(f"no matches in {root}")
    for kind in ("cmu", "100style", "glb", "bvh"):
        group = [h for h in hits if h.startswith(kind)]
        if not group:
            continue
        print("\n".join(group[:a.limit]))
        if len(group) > a.limit:
            print(f"... {len(group) - a.limit} more {kind} matches (raise --limit or narrow the query)")


def cmd_stats(a):
    root = lib_root(a.lib)
    name, text, cut = load_clip(a.clip, root)
    print(json.dumps(stats(name, text, cut if a.trim else None, a.up, a.feet, a.target_speed), indent=1))


def cmd_extract(a):
    root = lib_root(a.lib)
    name, text, cut = load_clip(a.clip, root)
    if a.trim and cut:
        head, motion = text.split("MOTION", 1)
        lines = motion.strip().splitlines()
        body = [l for l in lines[2:] if l.strip()][cut[0]:cut[1]]
        text = f"{head}MOTION\nFrames: {len(body)}\n{lines[1]}\n" + "\n".join(body) + "\n"
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{name}.bvh"
    dest.write_text(text, encoding="latin-1")
    print(dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lib")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=25, help="per source")
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("stats"); s.add_argument("clip"); s.add_argument("--feet", action="store_true")
    s.add_argument("--target-speed", type=float); s.add_argument("--up", default="Y", choices="XYZ")
    s.add_argument("--trim", action="store_true", help="100STYLE: measure only the clean section from Frame_Cuts.csv")
    s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("extract"); s.add_argument("clip"); s.add_argument("--out", required=True)
    s.add_argument("--trim", action="store_true", help="100STYLE: keep only the clean section from Frame_Cuts.csv")
    s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("sheet"); s.add_argument("clips", nargs="+", metavar="CLIP")
    s.add_argument("--out", required=True, help="output image (.png or .jpg)")
    s.add_argument("--times", type=int, default=8, help="evenly spaced frames per clip")
    s.add_argument("--trim", action="store_true", help="100STYLE: draw only the clean section from Frame_Cuts.csv")
    s.add_argument("--up", default="Y", choices="XYZ")
    s.set_defaults(fn=cmd_sheet)
    for p in sub.choices.values():
        p.add_argument("--lib", default=argparse.SUPPRESS)
    a = ap.parse_args()
    a.lib = getattr(a, "lib", None)
    a.fn(a)


if __name__ == "__main__":
    main()
