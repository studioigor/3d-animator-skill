#!/usr/bin/env python3
"""Video face capture: a webcam, phone or rendered video of a face -> face-curves/1 JSON.

MediaPipe Face Landmarker (Tasks API, VIDEO mode) reads 52 ARKit-style blendshapes and the
head pose from every frame. This script calibrates them against a neutral reference, fills
dropouts, filters jitter (One Euro, run forward and backward so it adds no lag) and resamples
everything onto a uniform grid in the shared face-curves/1 format (same as lipsync.py).

  python3 face_capture.py take.mp4 -o take.json
  python3 face_capture.py take.mp4 -o take.json --preview take_qa.mp4
  python3 face_capture.py selfie.mov -o selfie.json --mirror --neutral-frames 0:20
  python3 face_capture.py take.mp4 -o take.json --gain jawOpen=1.2,mouthSmileLeft=0.9 --fps 30
  python3 face_capture.py reel.mp4 -o reel.json --crop 420,0,1080,1080 --start 2 --end 9.5
  python3 face_capture.py cg_render.mp4 -o cg.json --auto-gain 'eyeBlink*'   # weak blinks (CG eyes)

Model: --model, else $ANIM_LIBRARY/models/face_landmarker.task, else
~/AnimationLibrary/models/face_landmarker.task (get it with: python3 fetch_library.py --get face_landmarker).
Deps: mediapipe (Python 3.9-3.12), numpy, opencv-python-headless (optional: without it frames
come from an ffmpeg pipe, and --preview needs it). ffprobe gives per-frame container timestamps,
so variable-frame-rate phone video stays in sync (fallbacks: OpenCV timestamps, index / fps).

Calibration: the neutral reference (--neutral-frames A:B source frames, A s:B s seconds, auto =
median of the first 0.5 s with a face, or none) is subtracted per channel and the rest is
stretched so 1.0 stays reachable; --gain / --auto-gain multiply afterwards; clamped to 0..1.
Head pitch/yaw/roll come from the facial transformation matrix relative to the neutral pose.
eyeYaw/eyePitch are ESTIMATES derived from the eyeLook* blendshapes (EYE_*_DEG_PER_UNIT below).
Time: curve sample i is at t = i / fps - lead_in; t = 0 is video time --start + --audio-offset.

Known limits of MediaPipe blendshapes: mouthClose, cheekPuff, cheekSquint*, noseSneer* and
jawForward under-read or stay near 0; tongueOut is not produced at all (always 0 here); blinks
read weak on CG or small eyes (use --auto-gain 'eyeBlink*'); eyeLookUp barely moves; eyeLook*
also reacts to head turns and closing lids (eye angles fade out during blinks). Head angles
come out roughly 10-25% smaller than the true rotation (rendered test: 20 deg yaw read as 16).
Left/Right always mean the character's own left/right (ARKit; MediaPipe already reports it that
way for an unmirrored camera). Use --mirror for selfie video that was flipped horizontally.
"""
import argparse, fnmatch, json, math, os, shutil, subprocess, sys
from pathlib import Path

# ---------------------------------------------------------------- tunables
DEFAULT_FPS = 60                 # output curve rate
NEUTRAL_AUTO_SECONDS = 0.5       # auto neutral = median of the first N seconds that have a face
NEUTRAL_RESCALE = True           # after subtracting the neutral, divide by (1 - neutral) so 1.0 stays reachable
EYE_YAW_DEG_PER_UNIT = 28.0      # estimate: eye yaw in degrees when eyeLookOut/In = 1 (ARKit rigs: ~25-30)
EYE_PITCH_DEG_PER_UNIT = 22.0    # estimate: eye pitch in degrees when eyeLookUp/Down = 1 (~20-25)
FILL_MAX_GAP = 0.25              # dropouts up to this many seconds are bridged by linear interpolation
DECAY_TIME = 0.30                # longer dropouts ease to neutral (and back) over this many seconds
FILTER_DEFAULT = (1.7, 3.0, 1.0) # One Euro (min_cutoff Hz, beta, d_cutoff Hz) for most blendshapes
FILTER_BLINK = (10.0, 10.0, 1.0) # eyeBlink*: light filtering so blinks stay crisp
FILTER_EYELOOK = (3.0, 4.0, 1.0) # eyeLook*: saccades are fast
FILTER_HEAD = (1.5, 0.1, 1.0)    # head angles (degrees; beta is per deg/s)
BLINK_HIGH, BLINK_LOW = 0.5, 0.3 # hysteresis thresholds of the blink counter (mean of both eyes)
EYE_FADE_BLINK = (0.3, 0.8)      # eye angles fade out as the blink rises from a to b (closing lids read as look-down)
DEAD_CHANNEL_MAX = 0.05          # QA flags channels whose maximum stays below this
AUTO_GAIN_TARGET = 0.95          # --auto-gain: the channel's robust peak (99.5th percentile) is scaled to this
AUTO_GAIN_MAX = 3.0              # --auto-gain never multiplies by more than this
AUTO_GAIN_MIN_PEAK = 0.15        # --auto-gain leaves a channel alone if its robust peak is below this (no events)
MIN_CONFIDENCE = 0.5             # MediaPipe detection / presence / tracking confidence
PREVIEW_HEIGHT = 720             # preview video height in pixels
PREVIEW_FPS = 30                 # preview video rate (constant; VFR input is resampled to it)
PREVIEW_BARS = ["jawOpen", "mouthClose", "mouthFunnel", "mouthPucker", "mouthSmileLeft", "mouthSmileRight",
                "mouthFrownLeft", "mouthStretchLeft", "eyeBlinkLeft", "eyeBlinkRight", "eyeWideLeft",
                "browInnerUp", "browDownLeft", "browOuterUpLeft", "cheekPuff"]

ARKIT_52 = [
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight", "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight", "mouthFrownLeft", "mouthFrownRight", "mouthFunnel",
    "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthPressLeft", "mouthPressRight", "mouthPucker",
    "mouthRight", "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "noseSneerLeft", "noseSneerRight", "tongueOut",
]
HEAD = ["headPitch", "headYaw", "headRoll"]
EYES = ["eyeYaw", "eyePitch"]
CONVENTIONS = {
    "time": "t = i / fps - lead_in",
    "angles": "degrees; +pitch = chin up / look up, +yaw = turn/look to the character's left, "
              "+roll = top of head toward the character's left shoulder",
    "sides": "Left/Right = the character's own left/right (ARKit)",
    "blendshapes": "0..1, neutral reference subtracted, gain applied, clamped",
    "head": "relative to the neutral reference pose (MediaPipe facial transformation matrix)",
    "eyes": "eyeYaw/eyePitch are estimates from eyeLook* blendshapes (yaw %.0f deg, pitch %.0f deg per unit, "
            "faded out during blinks)" % (EYE_YAW_DEG_PER_UNIT, EYE_PITCH_DEG_PER_UNIT),
}


def die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


try:
    import numpy as np
except ImportError:
    die("numpy is missing: pip install numpy")
try:
    import cv2
except ImportError:
    cv2 = None


# ---------------------------------------------------------------- video input
def ffprobe_json(path, args):
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    r = subprocess.run([exe, "-v", "error", "-of", "json", *args, str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def probe_video(path):
    """Size (after rotation), average fps, rotation, audio presence, per-frame pts if ffprobe exists."""
    info = {"width": None, "height": None, "fps": None, "rotation": 0, "has_audio": False, "pts": None,
            "audio_start_delta": 0.0}
    js = ffprobe_json(path, ["-show_streams"])
    if js:
        vs = [s for s in js.get("streams", []) if s.get("codec_type") == "video"]
        aus = [s for s in js.get("streams", []) if s.get("codec_type") == "audio"]
        info["has_audio"] = bool(aus)
        if vs:
            v = vs[0]
            w, h = int(v.get("width", 0)), int(v.get("height", 0))
            rot = 0
            for sd in v.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = int(float(sd["rotation"]))
            rot = int(float(v.get("tags", {}).get("rotate", rot)))
            if abs(rot) % 180 == 90:
                w, h = h, w
            info.update(width=w, height=h, rotation=rot)
            for key in ("avg_frame_rate", "r_frame_rate"):
                num, _, den = str(v.get(key, "0/0")).partition("/")
                try:
                    if float(den or 1) and float(num) > 0:
                        info["fps"] = float(num) / float(den or 1)
                        break
                except ValueError:
                    pass
            if aus:
                try:
                    info["audio_start_delta"] = float(aus[0].get("start_time", 0)) - float(v.get("start_time", 0))
                except (TypeError, ValueError):
                    pass
        pk = ffprobe_json(path, ["-select_streams", "v:0", "-show_entries", "packet=pts_time"])
        pts = []
        if pk:
            for p in pk.get("packets", []):
                try:
                    pts.append(float(p["pts_time"]))
                except (KeyError, ValueError):
                    pts = None
                    break
        if pts:
            info["pts"] = sorted(pts)
    if (info["width"] is None or not info["fps"]) and cv2 is not None:
        cap = cv2.VideoCapture(str(path))
        info["width"] = info["width"] or int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        info["height"] = info["height"] or int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        info["fps"] = info["fps"] or (cap.get(cv2.CAP_PROP_FPS) or 30.0)
        cap.release()
    info["fps"] = info["fps"] or 30.0
    return info


def iter_frames(path, info):
    """Yield (index, cv2_or_index_time, rgb uint8 HxWx3) in presentation order."""
    if cv2 is not None:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            die("OpenCV cannot open %s" % path)
        i = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                yield i, t, cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                i += 1
        finally:
            cap.release()
        return
    ff = shutil.which("ffmpeg")
    if not ff:
        die("neither OpenCV nor ffmpeg is available: pip install opencv-python-headless (or install ffmpeg)")
    w, h = info["width"], info["height"]
    if not w or not h:
        die("could not read the video size (ffprobe missing?)")
    proc = subprocess.Popen([ff, "-v", "error", "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    n = w * h * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(n)
            if len(buf) < n:
                break
            yield i, i / info["fps"], np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def apply_crop(rgb, crop):
    if not crop:
        return rgb
    x, y, w, h = crop
    return np.ascontiguousarray(rgb[y:y + h, x:x + w])


# ---------------------------------------------------------------- math helpers
def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def matrix_to_angles(R):
    """R = Ry(a) Rx(b) Rz(c) in MediaPipe's canonical face frame (x = image right = character's left
    for an unmirrored camera, y = up, z = out of the face). Returns (pitch, yaw, roll) in degrees
    with the face-curves/1 signs: +pitch chin up, +yaw to the character's left, +roll top to the left."""
    b = math.asin(max(-1.0, min(1.0, -R[1, 2])))
    a = math.atan2(R[0, 2], R[2, 2])
    c = math.atan2(R[1, 0], R[1, 1])
    return -math.degrees(b), math.degrees(a), -math.degrees(c)


def angles_to_matrix(pitch, yaw, roll):
    return rot_y(math.radians(yaw)) @ rot_x(math.radians(-pitch)) @ rot_z(math.radians(-roll))


def orthonormal(M):
    u, _, vt = np.linalg.svd(np.asarray(M, float)[:3, :3])
    R = u @ vt
    if np.linalg.det(R) < 0:
        u[:, -1] *= -1
        R = u @ vt
    return R


def one_euro(x, t, params):
    """One Euro filter over rows of x (N, C) with timestamps t (N,). params: (C, 3) arrays."""
    mc, beta, dc = params[:, 0], params[:, 1], params[:, 2]
    out = np.empty_like(x)
    out[0] = x[0]
    dx_prev = np.zeros(x.shape[1])
    for i in range(1, len(x)):
        dt = max(t[i] - t[i - 1], 1e-4)
        dx = (x[i] - out[i - 1]) / dt
        ad = 1.0 / (1.0 + 1.0 / (2 * math.pi * dc * dt))
        dx_hat = dx_prev + ad * (dx - dx_prev)
        cutoff = mc + beta * np.abs(dx_hat)
        a = 1.0 / (1.0 + 1.0 / (2 * math.pi * cutoff * dt))
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
        dx_prev = dx_hat
    return out


def zero_lag_one_euro(x, t, params):
    fwd = one_euro(x, t, params)
    bwd = one_euro(x[::-1], -t[::-1], params)[::-1]
    return 0.5 * (fwd + bwd)


def mirror_name(n):
    if n.endswith("Left"):
        return n[:-4] + "Right"
    if n.endswith("Right"):
        return n[:-5] + "Left"
    return n


def parse_pairs(text, what):
    out = {}
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        k, sep, v = part.partition("=")
        if not sep:
            die("bad %s entry %r (expected name=value)" % (what, part))
        try:
            out[k.strip()] = float(v)
        except ValueError:
            die("bad %s value in %r" % (what, part))
    return out


def count_blinks(sig, fps, hi=BLINK_HIGH, lo=BLINK_LOW):
    times, state = [], False
    for i, v in enumerate(sig):
        if not state and v >= hi:
            state = True
            times.append(round(i / fps, 3))
        elif state and v <= lo:
            state = False
    return times


# ---------------------------------------------------------------- capture
def find_model(arg):
    cands = [arg] if arg else []
    if os.environ.get("ANIM_LIBRARY"):
        cands.append(os.path.join(os.environ["ANIM_LIBRARY"], "models", "face_landmarker.task"))
    cands.append(os.path.expanduser("~/AnimationLibrary/models/face_landmarker.task"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    fetch = Path(__file__).resolve().parent / "fetch_library.py"
    die("face_landmarker.task not found (looked in: %s).\nGet it with:\n  python3 %s --get face_landmarker\n"
        "or pass --model /path/to/face_landmarker.task" % (", ".join(c for c in cands if c), fetch))


def detect(path, info, args):
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mpt
        from mediapipe.tasks.python import vision
    except ImportError as e:
        die("cannot import mediapipe (%s): pip install mediapipe opencv-python-headless numpy\n"
            "(MediaPipe wheels exist for Python 3.9-3.12; or run: python3 fetch_library.py --python-deps face)" % e)
    model = find_model(args.model)
    opts = vision.FaceLandmarkerOptions(
        base_options=mpt.BaseOptions(model_asset_path=model), running_mode=vision.RunningMode.VIDEO,
        num_faces=1, output_face_blendshapes=True, output_facial_transformation_matrixes=True,
        min_face_detection_confidence=args.min_conf, min_face_presence_confidence=args.min_conf,
        min_tracking_confidence=args.min_conf)
    lm_det = vision.FaceLandmarker.create_from_options(opts)
    pts = info["pts"]
    t0 = pts[0] if pts else 0.0
    rows, last_ms, n_dec = [], -1, 0
    col = {n: i for i, n in enumerate(ARKIT_52)}
    for i, t_cv, rgb in iter_frames(path, info):
        n_dec += 1
        t = (pts[i] - t0) if (pts and i < len(pts)) else t_cv
        if args.start is not None and t < args.start - 1e-6:
            continue
        if args.end is not None and t > args.end + 1e-6:
            continue
        img = apply_crop(rgb, args.crop)
        ms = max(int(round(t * 1000)), last_ms + 1)
        last_ms = ms
        res = lm_det.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(img)), ms)
        row = {"i": i, "t": t, "t_cv": t_cv, "ok": False}
        if res.face_landmarks:
            bs = np.zeros(len(ARKIT_52), np.float32)
            if res.face_blendshapes:
                for c in res.face_blendshapes[0]:
                    if c.category_name in col:
                        bs[col[c.category_name]] = c.score
            R = orthonormal(res.facial_transformation_matrixes[0]) if res.facial_transformation_matrixes else np.eye(3)
            row.update(ok=True, bs=bs, R=R,
                       lm=np.array([[p.x, p.y] for p in res.face_landmarks[0]], np.float32))
        rows.append(row)
        if args.verbose and len(rows) % 200 == 0:
            print("  %d frames, t=%.2fs" % (len(rows), t))
    lm_det.close()
    if not rows:
        die("no frames decoded from %s" % path)
    # timestamps: ffprobe pts when the counts agree, else OpenCV, else index / fps
    if pts and len(pts) == n_dec:
        source = "ffprobe"
    else:
        tc = np.array([r["t_cv"] for r in rows])
        if cv2 is not None and len(tc) > 1 and np.all(np.diff(tc) > 0):
            source = "opencv"
            for r in rows:
                r["t"] = r["t_cv"]
        else:
            source = "index"
            for r in rows:
                r["t"] = r["i"] / info["fps"]
    return rows, source


def fill_gaps(t, valid, data):
    """Bridge short dropouts linearly, ease long ones to neutral (0). Returns the dropout report."""
    report, n = [], len(t)
    i = 0
    while i < n:
        if valid[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not valid[j + 1]:
            j += 1
        p = i - 1 if i > 0 else None
        q = j + 1 if j + 1 < n else None
        span = (t[q] if q is not None else t[j]) - (t[p] if p is not None else t[i])
        if p is not None and q is not None and span <= FILL_MAX_GAP + 1e-6:
            w = ((t[i:j + 1] - t[p]) / max(t[q] - t[p], 1e-6))[:, None]
            data[i:j + 1] = data[p] * (1 - w) + data[q] * w
            how = "interpolated"
        else:
            tt = t[i:j + 1]
            w_out = np.zeros_like(tt) if p is None else 0.5 * (1 + np.cos(np.pi * np.clip((tt - t[p]) / DECAY_TIME, 0, 1)))
            w_in = np.zeros_like(tt) if q is None else 0.5 * (1 + np.cos(np.pi * np.clip((t[q] - tt) / DECAY_TIME, 0, 1)))
            s = np.maximum(w_out + w_in, 1.0)
            prev = data[p] if p is not None else np.zeros(data.shape[1])
            nxt = data[q] if q is not None else np.zeros(data.shape[1])
            data[i:j + 1] = (prev[None] * w_out[:, None] + nxt[None] * w_in[:, None]) / s[:, None]
            how = "neutral"
        report.append({"t0": round(float(t[i]), 3), "t1": round(float(t[j]), 3), "frames": int(j - i + 1), "fill": how})
        i = j + 1
    return report


def neutral_indices(rows, spec):
    ok = [k for k, r in enumerate(rows) if r["ok"]]
    if spec == "none" or not ok:
        return []
    if spec == "auto":
        t_first = rows[ok[0]]["t"]
        return [k for k in ok if rows[k]["t"] <= t_first + NEUTRAL_AUTO_SECONDS]
    a, _, b = spec.partition(":")
    if a.endswith("s") or b.endswith("s"):
        lo, hi = float(a.rstrip("s") or 0), float(b.rstrip("s") or 1e9)
        sel = [k for k in ok if lo <= rows[k]["t"] < hi]
    else:
        lo, hi = int(a or 0), int(b or 10 ** 9)
        sel = [k for k in ok if lo <= rows[k]["i"] < hi]
    if not sel:
        die("--neutral-frames %s selects no frame with a detected face" % spec)
    return sel


def process(rows, info, args):
    n = len(rows)
    t = np.array([r["t"] for r in rows], float)
    valid = np.array([r["ok"] for r in rows])
    C = len(ARKIT_52)
    bs = np.zeros((n, C))
    ang = np.zeros((n, 3))  # pitch, yaw, roll (absolute, camera relative)
    for k, r in enumerate(rows):
        if r["ok"]:
            bs[k] = r["bs"]
            ang[k] = matrix_to_angles(r["R"])
    if args.mirror:
        bs = bs[:, [ARKIT_52.index(mirror_name(nm)) for nm in ARKIT_52]]
        ang[:, 1:] *= -1
    # neutral reference
    sel = neutral_indices(rows, args.neutral_frames)
    neutral = np.median(bs[sel], axis=0) if sel else np.zeros(C)
    neutral[ARKIT_52.index("tongueOut")] = 0.0
    if sel:
        p0, y0, r0 = np.median(ang[sel], axis=0)
        R0t = angles_to_matrix(p0, y0, r0).T
        for k in np.nonzero(valid)[0]:
            ang[k] = matrix_to_angles(R0t @ angles_to_matrix(*ang[k]))
    cal = bs - neutral
    if NEUTRAL_RESCALE:
        cal = cal / np.maximum(1.0 - neutral, 0.2)
    cal = np.clip(cal, 0.0, 1.0)
    data = np.concatenate([cal, ang], axis=1)
    data[~valid] = 0.0
    dropouts = fill_gaps(t, valid, data)
    # filtering
    params = np.array([FILTER_DEFAULT] * C + [FILTER_HEAD] * 3, float)
    for c, nm in enumerate(ARKIT_52):
        if nm.startswith("eyeBlink"):
            params[c] = FILTER_BLINK
        elif nm.startswith("eyeLook"):
            params[c] = FILTER_EYELOOK
    if args.min_cutoff is not None:
        params[:C, 0] *= args.min_cutoff / FILTER_DEFAULT[0]
    if args.beta is not None:
        params[:C, 1] *= args.beta / FILTER_DEFAULT[1]
    if not args.no_filter and n > 2:
        data = one_euro(data, t, params) if args.causal else zero_lag_one_euro(data, t, params)
    data[:, :C] = np.clip(data[:, :C], 0.0, 1.0)
    # uniform output grid: curve time tc <-> video time start + audio_offset + tc
    fps = float(args.fps)
    origin = (args.start or 0.0) + args.audio_offset
    dt_med = float(np.median(np.diff(t))) if n > 1 else 1.0 / info["fps"]
    duration = max(t[-1] + dt_med - origin, 0.0)
    nfr = int(math.floor((args.lead_in + duration + args.tail) * fps + 1e-6)) + 1
    tv = np.arange(nfr) / fps - args.lead_in + origin
    grid = np.stack([np.interp(tv, t, data[:, c]) for c in range(data.shape[1])], axis=1)
    curves = {nm: grid[:, c] for c, nm in enumerate(ARKIT_52)}
    curves["headPitch"], curves["headYaw"], curves["headRoll"] = grid[:, C], grid[:, C + 1], grid[:, C + 2]
    gains = parse_pairs(args.gain, "--gain")

    def gain_for(nm):
        g = 1.0
        for pat, v in gains.items():
            if fnmatch.fnmatchcase(nm, pat):
                g = v
        return g

    unknown = [p for p in gains if not any(fnmatch.fnmatchcase(nm, p) for nm in ARKIT_52 + HEAD + EYES)]
    if unknown:
        print("WARNING: --gain names match no curve: %s" % ", ".join(unknown))
    auto = {}
    for pat in filter(None, (p.strip() for p in (args.auto_gain or "").split(","))):
        for nm in ARKIT_52:
            if fnmatch.fnmatchcase(nm, pat) and nm not in auto:
                peak = float(np.percentile(curves[nm], 99.5))
                auto[nm] = round(min(AUTO_GAIN_MAX, AUTO_GAIN_TARGET / peak), 3) if peak >= AUTO_GAIN_MIN_PEAK else 1.0
    for nm in ARKIT_52:
        curves[nm] = np.clip(curves[nm] * gain_for(nm) * auto.get(nm, 1.0), 0.0, 1.0)
    args.auto_gains = auto
    for nm in HEAD:
        curves[nm] = curves[nm] * gain_for(nm)
    c_ = curves
    lo, hi = EYE_FADE_BLINK
    fade = np.clip((hi - np.maximum(c_["eyeBlinkLeft"], c_["eyeBlinkRight"])) / (hi - lo), 0.0, 1.0)
    curves["eyeYaw"] = 0.5 * ((c_["eyeLookOutLeft"] - c_["eyeLookInLeft"]) + (c_["eyeLookInRight"] - c_["eyeLookOutRight"])) \
        * args.eye_deg[0] * gain_for("eyeYaw") * fade
    curves["eyePitch"] = 0.5 * ((c_["eyeLookUpLeft"] - c_["eyeLookDownLeft"]) + (c_["eyeLookUpRight"] - c_["eyeLookDownRight"])) \
        * args.eye_deg[1] * gain_for("eyePitch") * fade
    return t, valid, curves, neutral, sel, dropouts, duration, origin, nfr, params


def qa_block(rows, t, valid, curves, dropouts, fps, source, info):
    n = len(rows)
    blink = 0.5 * (curves["eyeBlinkLeft"] + curves["eyeBlinkRight"])
    times = count_blinks(blink, fps)
    jaw = curves["jawOpen"]
    dts = np.diff(t) if n > 1 else np.array([1.0 / info["fps"]])
    chmax = {nm: round(float(np.max(curves[nm])), 3) for nm in ARKIT_52}
    rng = lambda a: [round(float(np.min(a)), 2), round(float(np.max(a)), 2)]
    return {
        "frames_in": n, "detected": int(valid.sum()), "detection_rate": round(float(valid.mean()), 4),
        "dropout_frames": int((~valid).sum()), "dropouts": dropouts,
        "timestamps": source, "src_fps_mean": round(float(1.0 / np.mean(dts)), 3),
        "vfr": bool(np.std(dts) > 0.05 * np.mean(dts)),
        "blink_count": len(times), "blink_times": times,
        "blink_peak": round(float(np.max(blink)), 3),
        "jawOpen_range": [round(float(jaw.min()), 3), round(float(jaw.max()), 3)],
        "jawOpen_p95": round(float(np.percentile(jaw, 95)), 3),
        "head_range_deg": {nm: rng(curves[nm]) for nm in HEAD},
        "eye_range_deg": {nm: rng(curves[nm]) for nm in EYES},
        "channel_max": chmax,
        "dead_channels": [nm for nm, v in chmax.items() if v < DEAD_CHANNEL_MAX],
    }


def write_json(path, obj):
    """Readable top level, one curve per line."""
    lines = ["{"]
    keys = list(obj)
    for k_i, k in enumerate(keys):
        comma = "," if k_i < len(keys) - 1 else ""
        if k == "curves":
            lines.append('  "curves": {')
            cn = list(obj[k])
            for c_i, c in enumerate(cn):
                lines.append("    %s: %s%s" % (json.dumps(c), json.dumps(obj[k][c], separators=(",", ":")),
                                             "," if c_i < len(cn) - 1 else ""))
            lines.append("  }" + comma)
        else:
            lines.append("  %s: %s%s" % (json.dumps(k), json.dumps(obj[k], ensure_ascii=False), comma))
    lines.append("}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- preview
def write_preview(path, src, info, args, rows, curves, origin, lead_in, fps):
    if cv2 is None:
        print("WARNING: --preview needs OpenCV: pip install opencv-python-headless")
        return
    by_i = {r["i"]: r for r in rows}
    gen = iter_frames(src, info)
    sample = next(gen)[2]
    gen.close()
    fh, fw = apply_crop(sample, args.crop).shape[:2]
    scale = PREVIEW_HEIGHT / float(fh)
    W, H = int(round(fw * scale)) // 2 * 2, PREVIEW_HEIGHT // 2 * 2
    panel = 300
    OW = W + panel
    ff = shutil.which("ffmpeg")
    proc = writer = None
    t_lo = rows[0]["t"]
    if ff:
        cmd = [ff, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % (OW, H),
               "-r", str(PREVIEW_FPS), "-i", "-"]
        if info["has_audio"]:
            cmd += ["-ss", "%.3f" % max(t_lo, 0.0), "-i", str(src), "-map", "0:v", "-map", "1:a:0", "-c:a", "aac",
                    "-shortest"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    else:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), PREVIEW_FPS, (OW, H))
    nfr = len(curves["jawOpen"])

    def val(nm, tvid):
        x = (tvid - origin + lead_in) * fps
        i0 = int(min(max(math.floor(x), 0), nfr - 1))
        i1 = min(i0 + 1, nfr - 1)
        w = min(max(x - i0, 0.0), 1.0)
        return float(curves[nm][i0] * (1 - w) + curves[nm][i1] * w)

    def render(rgb, r):
        img = cv2.resize(cv2.cvtColor(apply_crop(rgb, args.crop), cv2.COLOR_RGB2BGR), (W, H))
        if r["ok"]:
            for x, y in r["lm"]:
                cv2.circle(img, (int(x * W), int(y * H)), 1, (80, 255, 120), -1, cv2.LINE_AA)
        else:
            cv2.putText(img, "NO FACE (filled)", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
        pan = np.full((H, panel, 3), 24, np.uint8)
        tv = r["t"]
        cv2.putText(pan, "t=%.2fs  src#%d" % (tv, r["i"]), (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (230, 230, 230), 1,
                    cv2.LINE_AA)
        y = 44
        bh = max(12, min(26, (H - 160) // len(PREVIEW_BARS)))
        for nm in PREVIEW_BARS:
            v = val(nm, tv)
            cv2.rectangle(pan, (130, y), (130 + int(150 * v), y + bh - 4), (60, 180, 255), -1)
            cv2.rectangle(pan, (130, y), (280, y + bh - 4), (90, 90, 90), 1)
            cv2.putText(pan, nm[:17], (6, y + bh - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (220, 220, 220), 1, cv2.LINE_AA)
            y += bh
        y += 14
        for nm in HEAD + EYES:
            cv2.putText(pan, "%-9s %+6.1f deg" % (nm, val(nm, tv)), (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (200, 255, 200), 1, cv2.LINE_AA)
            y += 20
        return np.concatenate([img, pan], axis=1)

    k_tick = 0
    last = None
    for i, _, rgb in iter_frames(src, info):
        r = by_i.get(i)
        if r is None:
            continue
        # emit preview ticks whose time is reached by this frame (constant-rate output for VFR input)
        while t_lo + k_tick / PREVIEW_FPS < r["t"] - 1e-6 and last is not None:
            (proc.stdin.write(last.tobytes()) if proc else writer.write(last))
            k_tick += 1
        last = render(rgb, r)
        if t_lo + k_tick / PREVIEW_FPS <= r["t"] + 1e-6:
            (proc.stdin.write(last.tobytes()) if proc else writer.write(last))
            k_tick += 1
    if proc:
        proc.stdin.close()
        proc.wait()
    else:
        writer.release()
    print("preview: %s" % path)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="input video (mp4/mov/webm...)")
    ap.add_argument("-o", "--out", help="output JSON (default: <video>.face.json)")
    ap.add_argument("--id", help="curve id (default: video file stem)")
    ap.add_argument("--model", help="face_landmarker.task path")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS, help="output curve rate (default %(default)s)")
    ap.add_argument("--lead-in", type=float, default=0.0, help="seconds of curve before t=0 (default 0)")
    ap.add_argument("--tail", type=float, default=0.0, help="seconds of curve after the video end (default 0)")
    ap.add_argument("--audio-offset", type=float, default=0.0,
                    help="video time (s, after --start) that becomes curve t=0, e.g. when the voice file starts later")
    ap.add_argument("--start", type=float, help="trim: first video second to use")
    ap.add_argument("--end", type=float, help="trim: last video second to use")
    ap.add_argument("--crop", help="x,y,w,h pixel region to analyse (small faces in wide shots)")
    ap.add_argument("--neutral-frames", default="auto",
                    help="auto | none | A:B (source frame indices, end exclusive) | 1.0s:2.5s (default auto)")
    ap.add_argument("--gain", help="per-curve gains after calibration, e.g. jawOpen=1.2,mouthSmile*=0.9")
    ap.add_argument("--auto-gain", help="scale these channels (globs) so their 99.5th percentile reaches %g, capped at x%g;"
                    " e.g. 'eyeBlink*' when blinks read weak" % (AUTO_GAIN_TARGET, AUTO_GAIN_MAX))
    ap.add_argument("--mirror", action="store_true", help="input is a mirrored selfie: swap Left/Right, negate yaw/roll")
    ap.add_argument("--eye-deg", default="%g,%g" % (EYE_YAW_DEG_PER_UNIT, EYE_PITCH_DEG_PER_UNIT),
                    help="eye yaw,pitch degrees per eyeLook unit (estimate; default %(default)s)")
    ap.add_argument("--min-cutoff", type=float, help="One Euro min_cutoff Hz for blendshapes (default %g; blink/eyeLook"
                    " scale with it)" % FILTER_DEFAULT[0])
    ap.add_argument("--beta", type=float, help="One Euro beta for blendshapes (default %g)" % FILTER_DEFAULT[1])
    ap.add_argument("--causal", action="store_true", help="forward-only filter (adds lag; default is zero-lag)")
    ap.add_argument("--no-filter", action="store_true", help="skip filtering")
    ap.add_argument("--min-conf", type=float, default=MIN_CONFIDENCE, help="MediaPipe confidence (default %(default)s)")
    ap.add_argument("--preview", help="write a QA overlay video (landmarks + bars), e.g. take_qa.mp4")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    src = Path(args.video)
    if not src.is_file():
        die("video not found: %s" % src)
    try:
        args.eye_deg = [float(x) for x in args.eye_deg.split(",")]
        assert len(args.eye_deg) == 2
    except (ValueError, AssertionError):
        die("--eye-deg expects yaw,pitch e.g. 28,22")
    if args.crop:
        try:
            args.crop = [int(v) for v in args.crop.split(",")]
            assert len(args.crop) == 4 and args.crop[2] > 0 and args.crop[3] > 0
        except (ValueError, AssertionError):
            die("--crop expects x,y,w,h in pixels")
    out = Path(args.out) if args.out else src.with_suffix(".face.json")
    info = probe_video(src)
    print("video: %s  %sx%s  ~%.3f fps  audio=%s" % (src.name, info["width"], info["height"], info["fps"],
                                                     info["has_audio"]))
    rows, ts_source = detect(src, info, args)
    if not any(r["ok"] for r in rows):
        die("no face detected in %d frames; try --crop around the face, a lower --min-conf, or a brighter, "
            "more frontal shot" % len(rows))
    t, valid, curves, neutral, sel, dropouts, duration, origin, nfr, params = process(rows, info, args)
    qa = qa_block(rows, t, valid, curves, dropouts, args.fps, ts_source, info)
    rnd = lambda a, d: [round(float(v), d) for v in a]
    obj = {
        "format": "face-curves/1", "id": args.id or src.stem, "fps": args.fps if args.fps % 1 else int(args.fps),
        "lead_in": args.lead_in, "tail": args.tail, "frames": nfr, "duration": round(duration, 4),
        "source": "video", "conventions": CONVENTIONS,
        "video": {"path": str(src.resolve()), "width": info["width"], "height": info["height"],
                  "crop": args.crop, "start": args.start, "end": args.end, "audio_offset": args.audio_offset,
                  "curve_t0_video_time": round(origin, 4), "mirror": args.mirror,
                  "audio_start_delta": round(info["audio_start_delta"], 4)},
        "calibration": {"neutral_frames": args.neutral_frames, "neutral_count": len(sel),
                        "neutral_rescale": NEUTRAL_RESCALE,
                        "neutral": {nm: round(float(v), 4) for nm, v in zip(ARKIT_52, neutral) if v > 0.005},
                        "gain": parse_pairs(args.gain, "--gain"), "auto_gain": args.auto_gains,
                        "eye_deg_per_unit": args.eye_deg},
        "filter": {"type": "none" if args.no_filter else ("one-euro" if args.causal else "one-euro zero-lag"),
                   "default": list(params[0]), "blink": list(params[ARKIT_52.index("eyeBlinkLeft")]),
                   "eyeLook": list(params[ARKIT_52.index("eyeLookInLeft")]), "head": list(params[-1])},
        "qa": qa,
        "curves": {nm: rnd(curves[nm], 4) for nm in ARKIT_52},
    }
    for nm in EYES + HEAD:
        obj["curves"][nm] = rnd(curves[nm], 3)
    write_json(out, obj)
    print("wrote %s: %d frames @ %s fps, %.2f s" % (out, nfr, obj["fps"], duration))
    print("QA: detection %.1f%% (%d/%d), dropouts %d frames in %d gaps, blinks %d (peak %.2f), "
          "jawOpen %.2f..%.2f, head yaw %s pitch %s roll %s"
          % (100 * qa["detection_rate"], qa["detected"], qa["frames_in"], qa["dropout_frames"], len(dropouts),
             qa["blink_count"], qa["blink_peak"], qa["jawOpen_range"][0], qa["jawOpen_range"][1],
             qa["head_range_deg"]["headYaw"], qa["head_range_deg"]["headPitch"], qa["head_range_deg"]["headRoll"]))
    if qa["dead_channels"]:
        print("    near-zero channels: %s" % ", ".join(qa["dead_channels"]))
    if args.auto_gains:
        print("    auto gains: %s" % ", ".join("%s x%.2f" % kv for kv in args.auto_gains.items()))
    if qa["blink_peak"] < BLINK_HIGH and not args.auto_gain:
        print("    hint: blinks read weak (peak %.2f < %.2f, none counted); if the face does blink, try "
              "--auto-gain 'eyeBlink*' or --gain 'eyeBlink*=2'" % (qa["blink_peak"], BLINK_HIGH))
    if args.preview:
        write_preview(args.preview, src, info, args, rows, curves, origin, args.lead_in, args.fps)


if __name__ == "__main__":
    main()
