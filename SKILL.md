---
name: 3d-animator
description: Character animation for 3D games and cinematics, body and face. Covers rigging scanned, AI-generated or template characters in Blender, finding and downloading open mocap (CMU, 100STYLE, Mesh2Motion, Mixamo through the user's login), retargeting and baking clips, locomotion without foot sliding, procedural layers (IK, look-at, hand contacts between characters, props and weapons, hit reactions, creatures, first-person viewmodels), facial rigs with ARKit blendshapes on a scanned head, lip sync from voice audio and a script, video face capture, and engine integration (Unity Animator, Timeline, offline renders). Ships scripts that fetch the libraries, measure clips, generate lip-sync curves and apply face curves. Use it whenever the user wants to rig, animate, retarget, fix or review a character or creature: walking or running, attacks, grabs, idles, dialogue, blinking, a mouth that doesn't match the voice, feet that slide, arms that twist, a face that looks wrong. Use it even when they never say "animation".
---

# 3D Animator: character body and facial animation, measured

This skill takes a character from a mesh to approved motion: rig, clips, locomotion, procedural layers, face,
lip sync, engine integration and review. The working method is the same at every step:
1. Measure the problem with a script or probe.
2. Fix the cause the numbers point to.
3. Show a before/after at identical times with the numbers next to it.

Most of the time goes into that loop, not into first passes.

## Where the knowledge is

| File | Read when |
|---|---|
| [`references/setup.md`](references/setup.md) | Start of a project: tools, the animation library and licences, Mixamo and paid sources, Blender headless rules, disk, multi-agent etiquette |
| [`references/rigging.md`](references/rigging.md) | Rigging a scanned, generated or template body: mesh prep, skeleton fit, skinning bulky costumes, hands, deformation limits, export |
| [`references/clips-retarget.md`](references/clips-retarget.md) | Choosing and measuring clips, text-to-motion and video references, retargeting, baking, Humanoid or Generic |
| [`references/locomotion.md`](references/locomotion.md) | Walks, runs, gait blends, foot pinning, path speeds, groups, quadrupeds, creature gaits, uneven ground, riders, giants |
| [`references/performance.md`](references/performance.md) | Making motion read: weight, impact, attack timing and telegraphs, hits and deaths, giants, creature acting, staging |
| [`references/procedural.md`](references/procedural.md) | Code-driven posing: layer architecture, poses, look-at, IK, hand contacts, props and weapons, springs, creatures without clips, first-person viewmodels |
| [`references/facial-rig.md`](references/facial-rig.md) | Building a face that animates: approach choice, landmarks, scan-exact topology, expression transfer, mouth, eyes, lids, textures, seams, gates |
| [`references/facial-animation.md`](references/facial-animation.md) | Driving the face: the face-curves format, lip sync, emotion layer, blinks and gaze, video capture, applying curves, runtime player, lip-sync QA |
| [`references/engine-unity.md`](references/engine-unity.md) | Unity import and avatars, Mecanim traps, Timeline-driven rigs, execution order, offline edit-mode renders |
| [`references/qa.md`](references/qa.md) | Before showing anything: building an animation lab, numeric probes, a contact checker, diagnosing complaints, review takes, critics |

Scripts (`scripts/`, each has `--help`):

| Script | Does |
|---|---|
| `check_tools.py` | Reports Blender, MPFB, ffmpeg, Python packages, library contents and disk; prints the fix for each gap |
| `fetch_library.py` | Downloads open mocap, rigs and models into a library folder with licence gating, resumable; installs MPFB and Python deps |
| `bvh_probe.py` | Searches the library (CMU index, 100STYLE styles, Mesh2Motion clip names); measures a clip's speed, cadence, contacts and the playback rate a target speed needs; extracts clips; `sheet` draws stick-figure contact sheets |
| `lipsync.py` | Voice audio + script line → 60 fps ARKit blendshape, eye and head curves with emotion, blinks and QA |
| `face_capture.py` | Video of a performer → the same curve format via MediaPipe, calibrated and filtered |
| `apply_face_curves.py` | Keys face curves onto shape keys and head/eye bones in Blender, headless |
| `identity_check.py` | Compares a rig's neutral render with the original model: SSIM in the face oval, landmark error in mm, against a baseline |
| `mesh_gate.py` | Counts folded, tilted and sliver triangles per shape key and on combined extremes, in Blender, headless |
| `subdiv_keys.py` | Subdivides a shape-keyed mesh with Catmull-Clark and carries every key over exactly |
| `contact_sheet.py` | Frame-exact contact sheets from review videos, plus side-by-side and 2×2 comparison videos with labels |

## Workflow

0. **Intake** (ask only what you cannot find out):
   - the characters and where they come from (scan, image-to-3D, template, marketplace);
   - the engine and the deliverable (cinematic shots, gameplay, both);
   - must the face stay identical to the source?
   - what can the user do by hand (many users can't sculpt or paint weights: then plan a fully scripted path);
   - the budget for paid tools or credits, and the dialogue language.
1. **Setup.** Run `check_tools.py` and fetch the small sources first (`fetch_library.py --get
   mesh2motion,face_landmarker`). Agree on the library folder before multi-GB downloads.
2. **Rig.** Fit the clip library's own skeleton into the mesh, skin it, and add helper bones where the costume
   needs them. Gate with stress poses and bone overlays (`rigging.md`). Write the costume's deformation limits
   down: they become design rules for every later step.
3. **Motion sources.** Search and measure before choosing (`bvh_probe.py`, stick-figure sheets). Bake clips per
   rig by copying world rotations, and decide Humanoid vs Generic by measurement (`clips-retarget.md`).
4. **Locomotion and layers.** Match gait clips to the speeds the shots need. Drive phase by distance, and keep
   procedural layers deterministic (`locomotion.md`, `procedural.md`).
5. **Face.** Choose the face approach by the identity requirement. Build and gate it automatically, then drive it
   with lip sync or capture (`facial-rig.md`, `facial-animation.md`, gates: `identity_check.py`, `mesh_gate.py`).
6. **Engine.** Import, play and render with the settings that preserve the data (`engine-unity.md`).
7. **Review.** Build an animation lab, run numeric probes and make before/after media (`qa.md`,
   `contact_sheet.py`). The user approves each stage on videos and sheets.
8. **Iterate.** Make one measured point fix at a time. Archive rejected versions with the user's verdict so you
   can A/B and roll back.

## Principles

- **Measure first.** Per-frame traces, probes and checkers find the cause; eyes find only the symptom. Quote
  numbers with every fix ("knee direction −68° → −14°", "contact gap 1.2 m → 0.7 cm").
- **Exact beats clever.** A rig built from the library's skeleton, with clips copied by world rotation, retargets
  with 0.00° error. A general-purpose retarget layer adds its own errors, and you then have to chase them.
- **Never fix speed with playback rate.** Stride and cadence are separate. A walk clip played at 2.4× shuffles.
  Choose a clip whose natural speed is near the target, and drive phase by distance travelled.
- **The pose is a function of time.** Deterministic layers scrub, render offline and match play mode to the
  millimetre. Hidden state accumulated from frame deltas does none of that.
- **Put corrections into the data.** Helper rotations, limits and caps are baked into clips, so every player gets
  them.
- **The mesh decides what is possible.** Bulky scanned suits cannot raise their arms overhead, and solid gloves
  cannot move their fingers. Encode such limits as clamps instead of fighting them pose by pose.
- **Identity is measured.** For faces, gate identity (SSIM, landmark error in mm: `identity_check.py`) and
  cleanliness (folds, slivers: `mesh_gate.py`; lip closures: `lipsync.py` QA) automatically against a baseline
  before the user sees anything.
- **Check in the target renderer.** Preview renderers draw back faces, ignore probes and differ in normal maps. A
  defect can exist only in the engine.
- **Fix motion where it is cheap to see.** Judge timing, poses and contacts in a neutral lab with grid floors and
  several views. Judge the look in the real scene.
- **The user owns taste and money.** Offer options with trade-offs as before/after media and let them choose.
  Ask before spending credits. Never substitute a clip, a voice or an approach silently.

## Hard rules

- Blender headless: never `--factory-startup` or `read_factory_settings()`; use
  `read_homefile(use_empty=True)` or open a .blend. Never overwrite the user's source files.
- No pops mid-shot: characters, props and lights appear, disappear or switch only at cuts.
- Review videos with dialogue burn in subtitles that show who speaks and what.
- Never delete the user's files without their consent. In multi-agent runs one coordinating agent owns deletions,
  using absolute paths.
- Respect licences. Non-commercial, no-derivatives data stays out of commercial work, and required credits ship
  with the product.
- Never type or store the user's passwords. They log in themselves; you use the session.

## Evolving this skill

When a project teaches something new, add it in general form. That means no project or character names, and one
home per topic (correct older text in place instead of appending a contradiction). Keep measured numbers, and mark
your own starting points "(estimate)".
