# Setup: tools, libraries, licences and safe automation

Read this at the start of a project, before downloading clips or models, and whenever a script reports a missing
tool. Every script here is in `scripts/` and prints its own `--help`.

## 1. Check what is installed

```
python3 scripts/check_tools.py                         # Blender, MPFB, ffmpeg, Python packages, library, disk
python3 scripts/check_tools.py --python <venv>/bin/python   # check the interpreter you will actually use
```

Core set: Blender 4.2+ (headless), ffmpeg/ffprobe, Python 3.9+ with numpy. Optional by task:

| Task | Needs |
|---|---|
| Mocap search, stats, extraction | `bvh_probe.py` (stdlib; numpy for `--feet`) |
| Lip sync | numpy, scipy; openai-whisper or mlx-whisper for word timing; espeak-ng for non-Russian G2P |
| Video face capture | mediapipe, opencv-python-headless, the face landmarker model |
| Face fitting on renders | mediapipe, scipy, scikit-image, opencv |
| Template heads and ARKit shape keys | Blender + MPFB extension |

## 2. The animation library

`scripts/fetch_library.py` downloads open sources into one library folder (`--dest`, else `$ANIM_LIBRARY`,
else `~/AnimationLibrary`), never into the skill. It resumes interrupted downloads, checks free disk space
first, and writes `LIBRARY.md` with locations, licences and required credits.

```
python3 scripts/fetch_library.py --list
python3 scripts/fetch_library.py --get mesh2motion,face_landmarker      # small, start here
python3 scripts/fetch_library.py --get all                              # every commercial-safe source (~2.7 GB)
python3 scripts/fetch_library.py --blender-addons                       # MPFB via Blender's extension system
python3 scripts/fetch_library.py --python-deps core,face,lipsync        # venv inside the library folder
```

| Source | Size | Content | Licence |
|---|---|---|---|
| mesh2motion | 45 MB | rigs, sample models and animation sets (GLB) for human, fox, horse, bird, dragon, kaiju, shark, snake, spider; human base/add-on/mocap sets; auto-rig web app | art CC0, code MIT |
| cmu | 1.1 GB | CMU mocap as BVH, ~2500 clips, text index | free incl. commercial products; the data may not be resold or redistributed as a dataset, even altered; credit required |
| 100style | 1.5 GB | 100 locomotion styles × walk/run in forward, backward, sideways, plus idles and transitions, BVH 60 fps | CC BY 4.0, credit |
| face_landmarker | 4 MB | MediaPipe 478 landmarks + 52 blendshape scores | Apache 2.0 |
| lafan1, bandai | 0.1 / 0.9 GB | long takes, stylised actions | LAFAN1 CC BY-NC-ND 4.0, Bandai Namco CC BY-NC 4.0: only with `--accept-noncommercial` for a production the user confirms is non-commercial. LAFAN1 is also no-derivatives: reference and research only, never ship clips retargeted from it |

- **Keep archives zipped** and extract only the clips a shot needs (`bvh_probe.py extract`). Archives and
  extracted files are untrusted data: never run anything from inside them.
- **Credits ship with the product.** Copy the credit lines from `LIBRARY.md` into the project's credits when
  CMU, 100STYLE or Mesh2Motion's CMU-derived clips are used.

## 3. Account-based and paid sources

- **Mixamo** (large humanoid set + auto-rigger) needs an Adobe login. Its clips are royalty-free inside projects,
  but the raw files may not be redistributed. The user logs in themselves (in a browser session you can drive, for
  example a Playwright browser, if they agree); never type or store their password. Work through the normal web UI,
  one clip at a time, within Adobe's terms: no bulk or scripted downloading. Upload the target character, download
  clips "With Skin", FBX, 30 fps, no keyframe reduction. Download a clip on the
  character itself as a reference for "is the rig broken?" checks (`qa.md`).
- **Paid services** (video mocap, text-to-motion generators, face-rig services, marketplace packs) spend the
  user's money or credits. Show the options with prices and what each does well, and ask before any purchase or
  credit use. What generators and video mocap are good and bad at is in `clips-retarget.md`.
- **Never substitute silently.** When the clip the shot needs does not exist anywhere, list the closest options
  with what each actually does and let the user pick, or plan a procedural or keyed solution.

## 4. Blender headless rules

- Run Blender in background with a script: `blender -b file.blend --python script.py -- <args>`.
- **Never use `--factory-startup` or `bpy.ops.wm.read_factory_settings()`.** Factory settings can remove
  installed extensions and their Python wheels. For an empty scene use `bpy.ops.wm.read_homefile(use_empty=True)`, or open a
  .blend.
- Render previews with EEVEE or Cycles on CPU in background mode; GPU kernels crashed in background bakes.
- Never overwrite the user's source .blend: save to a new path, keep `.pre_<step>` backups before destructive
  steps, and make scripts safe to re-run (detect "already done" and re-measure every run).
- After bmesh edits call `normal_update()` before reading normals; a zero normal silently skipped an orientation
  check and shipped an inside-out mesh that only the engine's back-face culling revealed.
- Return measurements from scripts (JSON or a log line per check), not only renders.

## 5. Python environments

- Use one venv per library (`fetch_library.py --python-deps`) and pass its interpreter to scripts that need
  mediapipe or whisper. Whisper pulls torch (~2 GB); on Apple Silicon `mlx-whisper` is lighter.
- Run Python that reads downloaded data with `-I` and from a directory you control.

## 6. Disk, memory and long runs

- Offline frame sequences fill disks fast. Render previews as JPEG, encode to video, and keep frame folders
  only as long as they are needed. Delete only with the user's consent and only paths you created.
- When free space drops suddenly with no big writes, check swap before deleting files (macOS:
  `sysctl vm.swapusage`); long-running editors and model servers hold it, and only restarting them frees it.
- Check free space before 4K renders and big downloads (`check_tools.py` shows it).

## 7. Working with the user and with other agents

- **Measure, then show.** Every fix comes with numbers (before → after) and a before/after video or sheet at
  identical times (`qa.md`). The user judges videos and sheets, not live scenes.
- **The user's limits decide the method.** If they cannot do manual mesh work, sculpting or weight painting,
  rule out every route that needs it and say so up front.
- **Several agents in one engine editor:** a lock file with an owner name, held at most about 10 minutes, play
  mode exited and scripts compiled before release; each agent keeps helpers in its own folder and owns its own
  files. A wait loop must never fall through into the work.
- **One agent owns deletions.** In multi-agent runs, helper agents leave temporary folders and list them in their
  report; the coordinating agent cleans up with absolute paths and the user's consent.
