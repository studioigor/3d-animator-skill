#!/usr/bin/env python3
"""Identity check: does the rig's neutral still look like the original model?

  python3 identity_check.py --original orig.png --candidate rig.png [--baseline previous.png]
          [--model face_landmarker.task] [--eye-distance-mm 63] [--json out.json] [--overlay overlay.png]

Render the original model and the rigged head's neutral from the same camera (same size, same framing),
then this compares them:
  * SSIM of the grey images inside the face oval (the oval comes from MediaPipe landmarks on the original);
  * MediaPipe landmark error in millimetres, scaled by the inter-ocular (iris centre) distance, which is
    --eye-distance-mm (default 63 mm, an adult average; pass the model's real value when you know it);
    reported as mean, p95 and per region: eyes, lips, brows, nose, face oval, iris;
  * the MediaPipe eyeBlink blendshape score of each image, as a sanity check that the neutral's eyes read
    as open as the original's. It is coarse on renders (fully closed rendered lids can still score ~0.1),
    so treat a large difference as a prompt to look, not as a measurement.
--baseline is the previous approach rendered the same way; it is measured against the original too, so
the candidate's numbers can be read as better or worse. Detection thresholds are low (renders score low).

Model: --model, else $ANIM_LIBRARY/models/face_landmarker.task, else ~/AnimationLibrary/models/face_landmarker.task
(get it with: python3 fetch_library.py --get face_landmarker).
Needs numpy, Pillow and mediapipe (Python 3.9-3.12), plus one of scikit-image, scipy or opencv for SSIM.
Exit code 0, or 2 when no face is found in the original.
"""
import argparse, json, os, sys
from pathlib import Path

EYE_DISTANCE_MM = 63.0   # assumed inter-pupillary distance in mm when --eye-distance-mm is not given (estimate)
MIN_CONFIDENCE = 0.2     # MediaPipe detection and presence confidence (renders score low)
SSIM_SIGMA = 1.5         # Gaussian window sigma for SSIM, in pixels
BLINK_WARN = 0.15        # warn when a blink score differs from the original's by more than this
PANEL_H = 480            # overlay panel height in pixels
HEAT_LOW = 0.5           # overlay SSIM colour scale: this value and below = red, 1.0 = green
FONT_CANDIDATES = [      # first existing TrueType font wins; Pillow's built-in font is the fallback
    "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
]

# MediaPipe face mesh landmark indices per region
OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149,
        150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]
EYES = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246, 263, 249, 390, 373, 374, 380,
        381, 382, 362, 398, 384, 385, 386, 387, 388, 466]
LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 185, 40, 39, 37, 0, 267, 269, 270, 409, 78, 95, 88, 178,
        87, 14, 317, 402, 318, 324, 308, 191, 80, 81, 82, 13, 312, 311, 310, 415]
BROWS = [46, 53, 52, 65, 55, 70, 63, 105, 66, 107, 276, 283, 282, 295, 285, 300, 293, 334, 296, 336]
NOSE = [168, 6, 197, 195, 5, 4, 1, 19, 94, 2, 98, 97, 326, 327, 294, 278, 344, 440, 275, 45, 220, 115, 48, 64]
IRIS = list(range(468, 478))
IRIS_CENTRES = (468, 473)

try:
    import numpy as np
except ImportError:
    sys.exit("identity_check.py needs numpy: pip install numpy")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("identity_check.py needs Pillow: pip install pillow")


def load_mediapipe():
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mpt
        from mediapipe.tasks.python import vision
    except ImportError:
        sys.exit("identity_check.py needs mediapipe: pip install mediapipe  (wheels exist for Python 3.9-3.12)")
    return mp, mpt, vision


def find_model(arg):
    cands = [arg] if arg else []
    if os.environ.get("ANIM_LIBRARY"):
        cands.append(os.path.join(os.environ["ANIM_LIBRARY"], "models", "face_landmarker.task"))
    cands.append(str(Path.home() / "AnimationLibrary" / "models" / "face_landmarker.task"))
    for c in cands:
        if c and Path(c).expanduser().exists():
            return str(Path(c).expanduser())
    sys.exit("face_landmarker.task not found (tried: " + ", ".join(cands) + "); get it with: "
             "python3 fetch_library.py --get face_landmarker, or pass --model")


def ssim_map(a, b, sigma):
    """Per-pixel SSIM of two float images in [0, 1]."""
    try:
        from skimage.metrics import structural_similarity
        _, S = structural_similarity(a, b, data_range=1.0, gaussian_weights=True, sigma=sigma,
                                     use_sample_covariance=False, full=True)
        return S
    except ImportError:
        pass
    try:
        from scipy.ndimage import gaussian_filter as blur

        def g(x):
            return blur(x, sigma)
    except ImportError:
        try:
            import cv2

            def g(x):
                return cv2.GaussianBlur(x, (0, 0), sigma)
        except ImportError:
            sys.exit("SSIM needs one of: pip install scikit-image | scipy | opencv-python-headless")
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    ma, mb = g(a), g(b)
    va, vb, cab = g(a * a) - ma * ma, g(b * b) - mb * mb, g(a * b) - ma * mb
    return ((2 * ma * mb + C1) * (2 * cab + C2)) / ((ma * ma + mb * mb + C1) * (va + vb + C2))


def load_rgb(path, size=None):
    if not Path(path).exists():
        sys.exit(f"no such image: {path}")
    im = Image.open(path)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    if size and im.size != size:
        print(f"warning: {Path(path).name} is {im.size[0]}x{im.size[1]}, resized to {size[0]}x{size[1]} "
              "(render both from the same camera at the same size for a fair comparison)", file=sys.stderr)
        im = im.resize(size, Image.LANCZOS)
    return im


class Detector:
    def __init__(self, model):
        self.mp, mpt, vision = load_mediapipe()
        opts = vision.FaceLandmarkerOptions(
            base_options=mpt.BaseOptions(model_asset_path=model), output_face_blendshapes=True, num_faces=1,
            min_face_detection_confidence=MIN_CONFIDENCE, min_face_presence_confidence=MIN_CONFIDENCE)
        self.det = vision.FaceLandmarker.create_from_options(opts)

    def __call__(self, im):
        W, H = im.size
        r = self.det.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.asarray(im).copy()))
        if not r.face_landmarks:
            return None, {}
        L = np.array([[p.x * W, p.y * H] for p in r.face_landmarks[0]])
        bs = {c.category_name: float(c.score) for c in r.face_blendshapes[0]} if r.face_blendshapes else {}
        return L, bs


def measure(orig, L0, bs0, mask, im, det, eye_mm):
    """Metrics of one image against the original."""
    g0 = np.asarray(orig.convert("L"), np.float64) / 255.0
    g1 = np.asarray(im.convert("L"), np.float64) / 255.0
    S = ssim_map(g0, g1, SSIM_SIGMA)
    out = {"ssim_face": round(float(S[mask].mean()), 4)}
    L, bs = det(im)
    if L is None:
        out["landmarks"] = "no face detected"
        return out, S, None
    px_mm = np.linalg.norm(L0[IRIS_CENTRES[0]] - L0[IRIS_CENTRES[1]]) / eye_mm
    e = np.linalg.norm(L[:468] - L0[:468], axis=1) / px_mm
    out.update({"lm_mean_mm": round(float(e.mean()), 2), "lm_p95_mm": round(float(np.percentile(e, 95)), 2),
                "eyes_mm": round(float(e[EYES].mean()), 2), "lips_mm": round(float(e[LIPS].mean()), 2),
                "brows_mm": round(float(e[BROWS].mean()), 2), "nose_mm": round(float(e[NOSE].mean()), 2),
                "oval_mm": round(float(e[OVAL].mean()), 2)})
    if len(L) >= 478 and len(L0) >= 478:
        out["iris_mm"] = round(float(np.linalg.norm(L[IRIS] - L0[IRIS], axis=1).mean() / px_mm), 2)
    bl = [bs.get("eyeBlinkLeft"), bs.get("eyeBlinkRight")]
    bl0 = [bs0.get("eyeBlinkLeft"), bs0.get("eyeBlinkRight")]
    out["blink"] = [round(x, 3) if x is not None else None for x in bl]
    if None not in bl and None not in bl0:
        out["blink_diff"] = [round(bl[0] - bl0[0], 3), round(bl[1] - bl0[1], 3)]
    return out, S, L


def font(size):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def overlay(path, orig, L0, cands, maps, metrics):
    """Panels: original with oval, each image with landmark displacement, each SSIM map inside the oval."""
    s = PANEL_H / orig.height
    W = int(orig.width * s)
    panels = []

    def base(im):
        return im.resize((W, PANEL_H), Image.LANCZOS).convert("RGB")

    p = base(orig)
    d = ImageDraw.Draw(p)
    d.line([tuple(q * s) for q in L0[OVAL]] + [tuple(L0[OVAL[0]] * s)], fill=(0, 255, 120), width=2)
    panels.append(("original + face oval", p))
    for (label, im, L), S, m in zip(cands, maps, metrics):
        p = base(im)
        d = ImageDraw.Draw(p)
        if L is not None:
            for a, b in zip(L0[:478] * s, L[:478] * s):
                d.line([tuple(a), tuple(b)], fill=(255, 220, 0), width=1)
            for q in L0[:478] * s:
                d.point(tuple(q), fill=(0, 255, 120))
            for q in L[:478] * s:
                d.point(tuple(q), fill=(255, 60, 60))
        panels.append((f"{label}: landmarks {m.get('lm_mean_mm', '-')} mm", p))
        v = np.clip((S - HEAT_LOW) / (1 - HEAT_LOW), 0, 1)     # red = low SSIM, green = high
        rgb = np.stack([(1 - v) * 255, v * 255, np.full_like(v, 40)], -1).astype(np.uint8)
        heat = Image.fromarray(rgb).resize((W, PANEL_H))
        mk = Image.new("L", orig.size, 0)
        ImageDraw.Draw(mk).polygon([tuple(q) for q in L0[OVAL]], fill=255)
        dim = Image.blend(base(im), Image.new("RGB", (W, PANEL_H)), 0.7)
        panels.append((f"{label}: SSIM in oval {m['ssim_face']}", Image.composite(heat, dim, mk.resize((W, PANEL_H)))))
    fnt = font(max(12, PANEL_H // 30))
    cols = 1 + 2 * len(cands)
    sheet = Image.new("RGB", (cols * W, PANEL_H + PANEL_H // 14), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    for i, (title, p) in enumerate(panels):
        sheet.paste(p, (i * W, PANEL_H // 14))
        d.text((i * W + 6, 4), title, font=fnt, fill=(235, 235, 235))
    sheet.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--original", required=True, help="render of the original model")
    ap.add_argument("--candidate", required=True, help="render of the rig's neutral, same camera")
    ap.add_argument("--baseline", help="render of the previous approach's neutral, same camera")
    ap.add_argument("--model", help="face_landmarker.task path")
    ap.add_argument("--eye-distance-mm", type=float, default=EYE_DISTANCE_MM,
                    help=f"inter-pupillary distance of the model in mm (default {EYE_DISTANCE_MM}, estimate)")
    ap.add_argument("--json", help="write the metrics here")
    ap.add_argument("--overlay", help="write a comparison image here")
    a = ap.parse_args()
    load_mediapipe()                                   # name a missing package before looking for the model
    det = Detector(find_model(a.model))
    orig = load_rgb(a.original)
    L0, bs0 = det(orig)
    if L0 is None:
        print(f"no face found in the original render {a.original}", file=sys.stderr)
        sys.exit(2)
    mk = Image.new("L", orig.size, 0)
    ImageDraw.Draw(mk).polygon([tuple(p) for p in L0[OVAL]], fill=1)
    mask = np.asarray(mk) > 0
    iod = float(np.linalg.norm(L0[IRIS_CENTRES[0]] - L0[IRIS_CENTRES[1]]))
    res = {"original": a.original, "eye_distance_mm": a.eye_distance_mm,
           "eye_distance_is_estimate": a.eye_distance_mm == EYE_DISTANCE_MM,
           "inter_ocular_px": round(iod, 1), "mm_per_px": round(a.eye_distance_mm / iod, 4),
           "original_blink": [round(bs0.get("eyeBlinkLeft", float("nan")), 3),
                              round(bs0.get("eyeBlinkRight", float("nan")), 3)]}
    cands, maps, metrics = [], [], []
    for role, path in (("candidate", a.candidate), ("baseline", a.baseline)):
        if not path:
            continue
        im = load_rgb(path, orig.size)
        m, S, L = measure(orig, L0, bs0, mask, im, det, a.eye_distance_mm)
        m["file"] = path
        res[role] = m
        cands.append((role, im, L))
        maps.append(S)
        metrics.append(m)
        for i, side in enumerate(("left", "right")):
            if "blink_diff" in m and abs(m["blink_diff"][i]) > BLINK_WARN:
                print(f"warning: {role} {side} eye blink score differs from the original by "
                      f"{m['blink_diff'][i]:+.2f}: the neutral's eyes read more {'closed' if m['blink_diff'][i] > 0 else 'open'}",
                      file=sys.stderr)

    keys = ["ssim_face", "lm_mean_mm", "lm_p95_mm", "eyes_mm", "lips_mm", "brows_mm", "nose_mm", "oval_mm", "iris_mm"]
    roles = [r for r in ("candidate", "baseline") if r in res]
    print(f"identity check vs {Path(a.original).name}  (1 px = {res['mm_per_px']} mm at "
          f"{a.eye_distance_mm:g} mm eye distance{', estimate' if res['eye_distance_is_estimate'] else ''})")
    print(f"{'metric':12s}" + "".join(f"{r:>12s}" for r in roles) + ("   candidate vs baseline" if len(roles) == 2 else ""))
    def cell(v):
        return "/".join(str(x) for x in v) if isinstance(v, list) else str(v)

    for k in keys + ["blink"]:
        vals = [res[r].get(k) for r in roles]
        if all(v is None for v in vals):
            continue
        row = f"{k:12s}" + "".join(f"{cell(v):>12s}" for v in vals)
        if len(roles) == 2 and k != "blink" and None not in vals and vals[0] != vals[1]:
            better = vals[0] > vals[1] if k == "ssim_face" else vals[0] < vals[1]
            row += "   better" if better else "   worse"
        print(row)
    print(f"{'orig blink':12s}{cell(res['original_blink']):>12s}   (eyeBlink left/right scores)")
    if a.json:
        Path(a.json).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with open(Path(a.json).expanduser(), "w") as f:
            json.dump(res, f, indent=1)
        print("wrote", a.json)
    if a.overlay:
        Path(a.overlay).expanduser().parent.mkdir(parents=True, exist_ok=True)
        overlay(str(Path(a.overlay).expanduser()), orig, L0, cands, maps, metrics)
        print("wrote", a.overlay)


if __name__ == "__main__":
    main()
