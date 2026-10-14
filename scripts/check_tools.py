#!/usr/bin/env python3
"""Report which animation tools are present and how to get the missing ones.

  python3 check_tools.py                 # system python
  python3 check_tools.py --python ~/AnimationLibrary/venv/bin/python
  python3 check_tools.py --dest /path/to/library

Exit code 0 when the core set (Blender, ffmpeg, numpy) is present, 1 otherwise.
"""
import argparse, glob, json, os, shutil, subprocess, sys
from pathlib import Path

PY_MODULES = [  # (import name, pip name, used for, core?)
    ("numpy", "numpy", "all scripts", True),
    ("scipy", "scipy", "smoothing, Laplacian solves, alignment", False),
    ("PIL", "pillow", "contact sheets, texture work", False),
    ("cv2", "opencv-python-headless", "image warps, landmark overlays", False),
    ("mediapipe", "mediapipe", "face landmarks and video face capture", False),
    ("whisper", "openai-whisper", "word timestamps for lip sync", False),
    ("mlx_whisper", "mlx-whisper", "word timestamps on Apple Silicon (alternative to whisper)", False),
]


def find_blender():
    cands = [os.environ.get("BLENDER"), shutil.which("blender"),
             "/Applications/Blender.app/Contents/MacOS/Blender",
             *sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"), reverse=True)]
    return next((c for c in cands if c and Path(c).exists()), None)


def blender_ext_dirs():
    home = Path.home()
    pats = [home / "Library/Application Support/Blender/*/extensions/*",
            home / ".config/blender/*/extensions/*",
            Path(os.environ.get("APPDATA", home)) / "Blender Foundation/Blender/*/extensions/*"]
    return [Path(p) for pat in pats for p in glob.glob(str(pat))]


def py_has(python, mod):
    code = f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec({mod!r}) else 1)"
    return subprocess.run([python, "-I", "-c", code], capture_output=True).returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python", default=sys.executable, help="interpreter whose packages to check")
    ap.add_argument("--dest", help="library folder (default $ANIM_LIBRARY or ~/AnimationLibrary)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rows, core_ok = [], True

    def add(name, ok, detail, fix="", core=False):
        nonlocal core_ok
        rows.append({"tool": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix})
        if core and not ok:
            core_ok = False

    b = find_blender()
    ver = ""
    if b:
        out = subprocess.run([b, "--version"], capture_output=True, text=True).stdout
        ver = out.splitlines()[0] if out else "?"
    add("blender", b, f"{ver} at {b}" if b else "not found",
        "install Blender 4.2+ from blender.org, or set $BLENDER", core=True)
    mpfb = [d for d in blender_ext_dirs() if (d / "mpfb").exists()]
    add("blender: MPFB extension", mpfb, str(mpfb[0] / "mpfb") if mpfb else "not installed",
        "python3 fetch_library.py --blender-addons  (or Blender > Get Extensions > MPFB)")

    for tool, why in (("ffmpeg", "encode reels, extract audio"), ("ffprobe", "read media durations")):
        p = shutil.which(tool)
        add(tool, p, p or f"not found ({why})", "brew install ffmpeg / apt install ffmpeg / winget install ffmpeg",
            core=tool == "ffmpeg")

    for mod, pipname, why, core in PY_MODULES:
        ok = py_has(a.python, mod)
        add(f"python: {mod}", ok, why, f"{a.python} -m pip install {pipname}", core=core)
    whisper_any = any(r["ok"] for r in rows if r["tool"] in ("python: whisper", "python: mlx_whisper"))

    root = Path(a.dest or os.environ.get("ANIM_LIBRARY") or Path.home() / "AnimationLibrary").expanduser()
    have = sorted(p.parent.name + "/" + p.name for p in root.glob("*/*") if p.is_file() and p.name != "LIBRARY.md")
    add("library", have, f"{root}: {', '.join(have) if have else 'empty'}", "python3 fetch_library.py --list")
    probe = root if root.exists() else Path.home()
    add("disk", shutil.disk_usage(probe).free > 10e9, f"{shutil.disk_usage(probe).free / 1e9:.1f} GB free",
        "free space: offline frame sequences and mocap archives need 5-20 GB")

    if a.json:
        print(json.dumps({"core_ok": core_ok, "rows": rows}, indent=1))
    else:
        for r in rows:
            print(f"{'ok ' if r['ok'] else '-- '} {r['tool']:26s} {r['detail']}")
            if r["fix"]:
                print(f"{'':31s}fix: {r['fix']}")
        if not whisper_any:
            print("\nlip sync needs one of whisper / mlx_whisper for word timing (or supply a word-timed transcript).")
        print("\ncore set:", "ready" if core_ok else "incomplete")
    sys.exit(0 if core_ok else 1)


if __name__ == "__main__":
    main()
