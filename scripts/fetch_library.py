#!/usr/bin/env python3
"""Fetch open animation libraries, models and tools for character animation work.

Stdlib only. Downloads resume after an interruption, check free disk space first and
write LIBRARY.md (what is where, licences, required credits) into the library folder.

  python3 fetch_library.py --list
  python3 fetch_library.py --get mesh2motion,face_landmarker
  python3 fetch_library.py --get all              # every commercial-safe source
  python3 fetch_library.py --get lafan1 --accept-noncommercial
  python3 fetch_library.py --blender-addons       # MPFB (MakeHuman for Blender) via Blender's extension system
  python3 fetch_library.py --python-deps core,face,lipsync   # venv inside the library folder

Library folder: --dest, else $ANIM_LIBRARY, else ~/AnimationLibrary.
"""
import argparse, os, shutil, subprocess, sys, time, urllib.request, zipfile
from pathlib import Path

SOURCES = {
    "mesh2motion": {
        "what": "Mesh2Motion: rigs, sample models and animation sets as GLB for human, fox, horse, bird, dragon, "
                "kaiju, shark, snake and spider (static/rigs, static/models, static/animations; the human has "
                "base, add-on and mocap sets), plus the browser auto-rig app source",
        "url": "https://github.com/Mesh2Motion/mesh2motion-app/archive/refs/heads/main.zip",
        "file": "mesh2motion/mesh2motion-app-main.zip", "approx_mb": 45, "extract": True,
        "licence": "Art CC0, code MIT; the human mocap set includes clips derived from CMU (see cmu)",
        "commercial": True,
        "credit": "Mesh2Motion (mesh2motion.org); CMU-derived clips: data obtained from mocap.cs.cmu.edu",
    },
    "cmu": {
        "what": "CMU Graphics Lab motion capture database as BVH (cgspeed conversion, community GitHub mirror; "
                "official site mocap.cs.cmu.edu): ~2500 clips, index in cmu-mocap-index-text.txt",
        "url": "https://github.com/Shriinivas/cmubvh/archive/refs/heads/main.zip",
        "file": "cmu/cmubvh-main.zip", "approx_mb": 1140, "extract": False,
        "licence": "Free for any use including commercial products; the data may not be resold or "
                   "redistributed as a dataset, even in altered form",
        "commercial": True,
        "credit": "The data used in this project was obtained from mocap.cs.cmu.edu. "
                  "The database was created with funding from NSF EIA-0196217.",
    },
    "100style": {
        "what": "100STYLE: 100 locomotion styles x walk/run in 8 directions, idles and turns, BVH 60 fps, "
                "28 joints, no fingers",
        "url": "https://zenodo.org/records/8127870/files/100STYLE.zip?download=1",
        "file": "100style/100STYLE.zip", "approx_mb": 1470, "extract": False,
        "licence": "CC BY 4.0", "commercial": True,
        "credit": "100STYLE dataset: I. Mason, S. Starke, T. Komura, Real-Time Style Modelling of Human "
                  "Locomotion via Feature-Wise Transformations and Local Motion Phases (2022), "
                  "doi:10.1145/3522618, data zenodo.org/records/8127870, CC BY 4.0 (add 'modified' when clips were changed)",
    },
    "face_landmarker": {
        "what": "MediaPipe Face Landmarker model (478 face landmarks + 52 ARKit-style blendshape scores) "
                "for landmarking renders of a head and for video face capture",
        "url": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "file": "models/face_landmarker.task", "approx_mb": 4, "extract": False,
        "licence": "Apache 2.0", "commercial": True, "credit": None,
    },
    "lafan1": {
        "what": "Ubisoft La Forge LAFAN1: 77 long takes of locomotion, fighting, dancing, obstacles, BVH 30 fps",
        "url": "https://github.com/ubisoft/ubisoft-laforge-animation-dataset/raw/master/lafan1/lafan1.zip",
        "file": "lafan1/lafan1.zip", "approx_mb": 140, "extract": False,
        "licence": "CC BY-NC-ND 4.0: non-commercial, no derivatives (reference only: never ship retargeted "
                   "clips, even in a non-commercial project)", "commercial": False,
        "credit": "Ubisoft La Forge Animation Dataset (LAFAN1)",
    },
    "bandai": {
        "what": "Bandai Namco Research motion dataset: stylised everyday actions (walk, run, dance, fight) "
                "in many styles, BVH",
        "url": "https://github.com/BandaiNamcoResearchInc/Bandai-Namco-Research-Motiondataset/archive/refs/heads/master.zip",
        "file": "bandai/bandai-motiondataset-master.zip", "approx_mb": 900, "extract": False,
        "licence": "CC BY-NC 4.0: non-commercial", "commercial": False,
        "credit": "Bandai Namco Research Motion Dataset",
    },
}

MANUAL = [
    ("Mixamo", "Large humanoid clip library with an auto-rigger, royalty-free in projects; the raw files may not "
               "be redistributed. Needs an Adobe login: the user logs in themselves, then clips are downloaded one "
               "at a time through the normal web UI (no bulk or scripted downloading), 'With Skin' on the target "
               "character, 30 fps, FBX, no keyframe reduction. Never type the user's password."),
    ("ActorCore / Rokoko / video mocap", "Account-based or paid. Ask the user before spending credits."),
    ("Text-to-motion generators", "Paid credits. Good for stances, idles and everyday actions, weak for specific combat moves."),
]

PY_GROUPS = {
    "core": ["numpy", "scipy", "pillow"],
    "face": ["mediapipe", "opencv-python-headless", "scikit-image"],
    "lipsync": ["openai-whisper"],   # pulls torch (~2 GB); on Apple Silicon `mlx-whisper` is a lighter alternative
}


def lib_root(arg):
    return Path(arg or os.environ.get("ANIM_LIBRARY") or Path.home() / "AnimationLibrary").expanduser()


def free_gb(path):
    p = Path(path)
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def download(url, dest, approx_mb):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    need = approx_mb / 1000 * 1.2 + 1.0
    if free_gb(dest.parent) < need:
        sys.exit(f"not enough disk space for {dest.name}: need ~{need:.1f} GB free, have {free_gb(dest.parent):.1f} GB")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "fetch_library/1.0"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:      # server ignored the range: start over
            have = 0
        total = r.headers.get("Content-Length")
        total = int(total) + have if total else None
        mode = "ab" if have else "wb"
        t0, done = time.time(), have
        with open(part, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.time() - t0 > 2:
                    t0 = time.time()
                    pct = f" {done / total * 100:5.1f}%" if total else ""
                    print(f"  {dest.name}: {done / 1e6:8.1f} MB{pct}", flush=True)
    if total and part.stat().st_size != total:
        sys.exit(f"{dest.name}: incomplete ({part.stat().st_size} of {total} bytes); run again to resume")
    part.rename(dest)
    return dest


def extract(zpath):
    out = zpath.parent
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.startswith("__MACOSX/")]
        z.extractall(out, members=names)
    return out


def write_index(root):
    lines = ["# Animation library", "",
             "Downloaded by fetch_library.py. Large archives stay zipped: extract only the clips a shot needs,",
             "into the project (`unzip <zip> '<path inside>'`). Keep the credits below wherever the data ships.", "",
             "| Source | Where | What | Licence | Credit |", "|---|---|---|---|---|"]
    for key, s in SOURCES.items():
        p = root / s["file"]
        if p.exists() or (s["extract"] and any(p.parent.glob("*/"))):
            lines.append(f"| {key} | `{s['file']}` | {s['what']} | {s['licence']} | {s['credit'] or '-'} |")
    lines += ["", "Not downloadable by script:", ""] + [f"- **{n}**: {t}" for n, t in MANUAL]
    (root / "LIBRARY.md").write_text("\n".join(lines) + "\n")


def find_blender():
    cands = [os.environ.get("BLENDER"), shutil.which("blender"),
             "/Applications/Blender.app/Contents/MacOS/Blender",
             r"C:\Program Files\Blender Foundation\Blender\blender.exe"]
    return next((c for c in cands if c and Path(c).exists()), None)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--get", help="comma-separated keys, or 'all' (commercial-safe only)")
    ap.add_argument("--accept-noncommercial", action="store_true",
                    help="allow non-commercial sources (CC BY-NC / BY-NC-ND) for a production the user confirms "
                         "is non-commercial; no-derivatives data (LAFAN1) stays reference only")
    ap.add_argument("--no-extract", action="store_true")
    ap.add_argument("--blender-addons", action="store_true", help="install MPFB through Blender's extension system")
    ap.add_argument("--python-deps", help="comma-separated groups: " + ",".join(PY_GROUPS))
    a = ap.parse_args()
    root = lib_root(a.dest)

    if a.list or not (a.get or a.blender_addons or a.python_deps):
        print(f"library folder: {root}  (free {free_gb(root):.1f} GB)\n")
        for k, s in SOURCES.items():
            state = "have" if (root / s["file"]).exists() else "    "
            tag = "" if s["commercial"] else "  [non-commercial: needs --accept-noncommercial]"
            print(f"{state} {k:16s} ~{s['approx_mb']:>5} MB  {s['licence']}{tag}\n      {s['what']}")
        print("\nmanual sources:")
        for n, t in MANUAL:
            print(f"  {n}: {t}")
        return

    root.mkdir(parents=True, exist_ok=True)
    if a.get:
        keys = [k for k, s in SOURCES.items() if s["commercial"]] if a.get == "all" else a.get.split(",")
        for k in keys:
            s = SOURCES.get(k)
            if not s:
                sys.exit(f"unknown source '{k}'; see --list")
            if not s["commercial"] and not a.accept_noncommercial:
                print(f"skip {k}: {s['licence']}. Pass --accept-noncommercial only for a non-commercial production; "
                      "no-derivatives data is reference only.")
                continue
            dest = root / s["file"]
            if dest.exists():
                print(f"have {k}: {dest}")
            else:
                print(f"get  {k} (~{s['approx_mb']} MB) <- {s['url']}")
                download(s["url"], dest, s["approx_mb"])
            if s["extract"] and not a.no_extract and dest.suffix == ".zip":
                print(f"     extracting {dest.name}")
                extract(dest)
        write_index(root)
        print(f"index: {root / 'LIBRARY.md'}")

    if a.blender_addons:
        b = find_blender()
        if not b:
            sys.exit("Blender not found: set $BLENDER to its executable")
        print(f"installing MPFB with {b}")
        subprocess.run([b, "--command", "extension", "install", "--sync", "--enable", "mpfb"], check=False)

    if a.python_deps:
        bad = [g for g in a.python_deps.split(",") if g not in PY_GROUPS]
        if bad:
            sys.exit(f"unknown python group(s): {','.join(bad)}; choose from {','.join(PY_GROUPS)}")
        venv = root / "venv"
        if not venv.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        pkgs = [p for g in a.python_deps.split(",") for p in PY_GROUPS[g]]
        subprocess.run([str(pip), "install", "--upgrade", *pkgs], check=True)
        print(f"python: {venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')}")


if __name__ == "__main__":
    main()
