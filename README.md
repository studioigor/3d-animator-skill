<div align="center">

<img src="assets/banner.png" alt="3d-animator — character animation skill for Claude Code" width="800">

**Character animation for Claude Code, body and face** — from the author of the YouTube channel [@studioigor](https://www.youtube.com/@studioigor).

Rig a scanned or generated character, pull open mocap, keep the feet planted, make the face speak a voice line — and prove every fix with numbers.

**English** · [Русский](README.ru.md)

![Claude Code skill](https://img.shields.io/badge/Claude_Code-skill-D97757)
[![YouTube @studioigor](https://img.shields.io/badge/YouTube-@studioigor-FF0000?logo=youtube&logoColor=white)](https://www.youtube.com/@studioigor)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB)
![Blender 4.2+](https://img.shields.io/badge/Blender-4.2%2B-E87D0D?logo=blender&logoColor=white)

</div>

## Install in one prompt

Send this prompt to your agent (Claude Code recommended, but works with others):

```text
Install the skill 3d-animator https://github.com/studioigor/3d-animator-skill
```

Then send to your agent: `/3d-animator` and describe the character and what it has to do. You don't even have to
say "animation": *"the feet slide"* or *"her mouth doesn't match the voice"* is enough.

---

**3d-animator** takes a character from a mesh to approved motion: rig, clips, locomotion, procedural layers, face,
lip sync, engine integration and review. Every step runs the same loop:

1. **Measure** the problem with a script or a probe.
2. **Fix** the cause the numbers point to.
3. **Show** a before/after at identical times, with the numbers next to it.

**You judge the result. The numbers find the cause.**

## Measure first

<img src="assets/speed.gif" alt="One target speed, three ways: a pushed walk skates, a sped-up walk shuffles, a run phased by distance stays planted" width="800">

One target speed, three ways to reach it, real CMU mocap:

- **A.** Push a walk clip along faster than it walks: the feet skate **1.3 m per step**.
- **B.** Play the walk 2.8× faster: the feet stay planted, but it becomes a **300 steps/min** shuffle.
- **C.** Take a clip whose own speed matches and drive its phase by distance: **3 cm** of skate.

`bvh_probe.py` measures speed, cadence, contacts and the playback rate a target speed needs, for any clip,
before you pick it.

## Lip sync from a voice line

<img src="assets/lipsync.gif" alt="lipsync.py curves driving a CC0 MakeHuman head: words, waveform, visemes and ARKit curve values" width="800">

`lipsync.py` turns a voice take and its script line into 60 fps ARKit blendshape curves plus eyes, head and an
emotion layer, then checks itself: every p/b/m closes the lips, no jaw chatter, ranges in bounds.
`apply_face_curves.py` keys the curves onto a rig in Blender: ARKit, CC/Reallusion, VRM or your own name map.
Here a CC0 MakeHuman head says the skill's motto. Russian has built-in pronunciation rules; English and other
languages use espeak-ng, with rough English rules as a fallback.

## A library you can search and measure

<img src="assets/styles.gif" alt="Four 100STYLE walks found with bvh_probe.py search, with measured speed and cadence" width="800">

`fetch_library.py` downloads open motion data with licence gating: CMU (~2500 clips), 100STYLE (100 locomotion
styles), Mesh2Motion rigs and clips, the MediaPipe face model. `bvh_probe.py search` finds clips by words,
`stats` measures them and `sheet` draws stick-figure contact sheets.

## How it feels

```text
You       This scanned soldier has to walk to the door, grab the handle and say one line.
Animator  Two questions: which engine, and must the face stay identical to the scan?
Animator  Rig: the clip library's own skeleton fitted into the mesh, so clips copy over exactly.
          Stress poses: the shoulder pads tear above 75°, so arm swing is capped there. Sheet attached.
Animator  Walk at 1.4 m/s: CMU 35_01 walks at 1.27 m/s, so it plays at 1.1×. Contacts are clean.
You       The feet slide when he stops.
Animator  Measured the stop: the root keeps moving after both feet are planted. Phase now follows
          distance. Before/after video with the numbers attached.
Animator  Face: lip sync from your voice take, all p/b/m closures pass. Review reel with subtitles attached.
```

## What's inside

| Knowledge area | Covers |
|---|---|
| Setup | tools, the animation library and licences, Mixamo and paid sources, headless Blender rules |
| Rigging | scanned, generated or template bodies: mesh prep, skeleton fit, skinning bulky costumes, hands, export |
| Clips and retargeting | choosing and measuring clips, text-to-motion and video references, exact retargets, baking |
| Locomotion | gaits without foot sliding, path speeds, groups, quadrupeds and creatures, uneven ground, riders |
| Performance | weight, impact, attack timing and telegraphs, hits and deaths, giants, staging |
| Procedural | IK, look-at, hand contacts between characters, props and weapons, springs, first-person viewmodels |
| Facial rig | a face that animates on a scanned head: landmarks, scan-exact topology, ARKit expression transfer |
| Facial animation | the face-curves format, lip sync, emotion, blinks and gaze, video face capture |
| Unity | import and avatars, Mecanim traps, Timeline-driven rigs, offline edit-mode renders |
| QA | the animation lab, numeric probes, contact checks, diagnosing complaints, review videos |

| Script | Does |
|---|---|
| `check_tools.py` | reports Blender, MPFB, ffmpeg, Python packages, library and disk; prints the fix for each gap |
| `fetch_library.py` | downloads open mocap, rigs and models with licence gating, resumable |
| `bvh_probe.py` | searches the library, measures clips (speed, cadence, contacts), extracts them, draws contact sheets |
| `lipsync.py` | voice + script line → 60 fps ARKit curves with emotion, blinks and QA |
| `face_capture.py` | performer video → the same curve format via MediaPipe |
| `apply_face_curves.py` | keys face curves onto shape keys and head/eye bones in Blender, headless |
| `identity_check.py` | does the rig's neutral still look like the original? SSIM and landmark error in mm |
| `mesh_gate.py` | folded, tilted and sliver triangles per shape key and on combined extremes |
| `subdiv_keys.py` | Catmull-Clark subdivision that carries every shape key over exactly |
| `contact_sheet.py` | frame-exact contact sheets and side-by-side review videos |

## Principles

- 📏 **Measure first.** Per-frame traces and probes find the cause; eyes find only the symptom.
- 🦶 **Never fix speed with playback rate.** Choose a clip near the target speed and drive its phase by distance.
- 🎯 **Exact beats clever.** A rig built from the library's own skeleton retargets with 0.00° error.
- ⏱️ **The pose is a function of time,** so it scrubs, renders offline and matches play mode.
- 🧥 **The mesh decides what is possible.** Bulky suits and solid gloves become clamps, not fights.
- 🙂 **Identity is measured.** Faces are gated on likeness and clean deformation before you see them.
- 💸 **You own taste and money.** Options come as before/after media; credits are never spent without asking,
  and nothing is substituted silently.

## Open data, licences respected

| Source | Licence | How the skill treats it |
|---|---|---|
| Mesh2Motion | art CC0, code MIT | the small first download; its CMU-derived clips keep the CMU credit |
| CMU mocap | free incl. commercial, not to be resold as a dataset | credit line kept in `LIBRARY.md` |
| 100STYLE | CC BY 4.0 | credit line kept in `LIBRARY.md` |
| LAFAN1, Bandai Namco | non-commercial | only with `--accept-noncommercial`; LAFAN1 is reference only |
| Mixamo | Adobe terms | you log in yourself, clips one at a time, your password is never typed |

The library lives outside the skill (`~/AnimationLibrary` or `$ANIM_LIBRARY`).

## Try saying

- *"Rig this AI-generated character and make it walk to the door"*
- *"The feet slide when she runs"*
- *"Here's the voice take, make him say it"*
- *"His arms twist when he raises them"*
- *"Find me a sneaky walk in free mocap"*
- *"Capture my face from this video and put it on the character"*

## Requirements

Claude Code, Python 3.9+, Blender 4.2+ (headless is fine), ffmpeg. Optional by task: MPFB for template heads,
whisper or mlx-whisper for word timing, espeak-ng for more languages, mediapipe for video face capture.
`check_tools.py` reports what is missing and how to install it. Engine notes are written for Unity; the Blender
and data side works for any engine.

## Credits for the demo media

- Motion: [CMU Graphics Lab Motion Capture Database](http://mocap.cs.cmu.edu) (created with funding from NSF
  EIA-0196217) and [100STYLE](https://zenodo.org/records/8127870) by I. Mason, S. Starke and T. Komura,
  CC BY 4.0.
- Head: the MakeHuman base mesh and expression units through [MPFB](https://static.makehumancommunity.org/mpfb.html), CC0.
- The voice line for the lip-sync demo was synthesized with text-to-speech; no audio ships with the skill.
- Fonts in the graphics: Press Start 2P and Silkscreen, SIL Open Font License.

## License

MIT — see [LICENSE](LICENSE).

## Links

- 📺 YouTube channel: [@studioigor](https://www.youtube.com/@studioigor)
- 💬 Discord server: [discord.gg/Jb7dBHSDG](https://discord.gg/Jb7dBHSDG)
- 📲 Telegram channel: [t.me/studioigor](https://t.me/studioigor)
- ❤️ Support the channel on Boosty: [boosty.to/studioigor](https://boosty.to/studioigor)
- ⚡️ Donate: [boosty.to/studioigor/donate](https://boosty.to/studioigor/donate)
