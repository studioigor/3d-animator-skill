# Clips: finding, judging, retargeting and baking library motion

Read this when a character needs motion from a library (mocap BVH, GLB/FBX clip packs, Mixamo-style services,
text-to-motion generators, video references). It covers where clips come from, how to measure them before use, which
retarget path to take, and how to bake clips that play exactly in the engine. Download commands are in `setup.md`.
Values without a marker were measured in production; **(estimate)** marks starting points.

## 1. Sources and licences

| Source | Content | Rate | Licence | Notes |
|---|---|---|---|---|
| Mesh2Motion | rigs + GLB clip sets for human (base, add-on, mocap), fox, horse, bird, dragon, kaiju, shark, snake, spider | 24 fps | art CC0, code MIT | "add-on" clips start at frame 1, base/mocap at 0; loops are in place unless the name ends in `_RM` |
| CMU mocap (BVH conversion) | ~2500 clips, everyday actions, sports, walks, runs, interactions | 120 fps | free incl. commercial; the data may not be resold or redistributed as a dataset | frame 0 is an added T-pose: never use it; credit mocap.cs.cmu.edu (NSF EIA-0196217) |
| 100STYLE | 100 locomotion styles × walk/run forward, backward, sideways, idle, transitions | 60 fps | CC BY 4.0 (credit) | T-pose and walk-in at the start; clean spans in `Frame_Cuts.csv`; no fingers |
| LAFAN1, Bandai Namco | long takes, stylised actions | 30 fps / var. | LAFAN1 CC BY-NC-ND 4.0, Bandai Namco CC BY-NC 4.0 | non-commercial only, when the user confirms a non-commercial production. LAFAN1 is also no-derivatives: a retarget is a derivative, so use it for reference and research only |
| Mixamo | large humanoid set + auto-rigger | 30 fps | free with an Adobe account; royalty-free in projects, raw files may not be redistributed | the user logs in; one clip at a time through the normal web UI, within Adobe's terms; never type a password |
| Text-to-motion generators | stances, idles, everyday actions | ~20 fps | per service | paid credits: ask before spending (§5) |
| Video mocap (Move One, QuickMagic, Rokoko Vision…) | the user's own performance | — | per service | check current prices; good on floor work (rolls, kneeling) in reviews |

Licence first, quality second. Keep the credit lines wherever the data ships (the library index written by
`fetch_library.py` lists them).

## 2. Find and measure clips with `bvh_probe.py`

```
python3 scripts/bvh_probe.py search "limp|injur"              # CMU index, 100STYLE styles, GLB clip names, loose BVH
python3 scripts/bvh_probe.py stats 07_01 --feet --target-speed 1.4
python3 scripts/bvh_probe.py stats Neutral_FW --trim --feet    # 100STYLE: only the clean span
python3 scripts/bvh_probe.py extract Old_FW --trim --out ./clips
```

`stats` reports fps, frames, seconds, joint and finger-joint counts, an estimated unit scale, hip height, speed
(median over 1 s windows, robust to looping capture paths; `net_speed` is meaningless on a circle), speed in leg
lengths per second, and with `--feet` the contact share, stance slide and cadence (stride period by
autocorrelation of the foot height). `--target-speed` returns the playback rate the clip would need and a verdict.
Metric values assume a 0.85 m hip-to-ankle leg **(estimate)**; compare clips by the scale-free leg-lengths value.

Measured examples:

| Clip | Speed | Cadence | Notes |
|---|---|---|---|
| CMU 07_01 walk | 1.45 m/s | 112 steps/min | a normal walk |
| CMU 07_04 slow walk | 0.99 m/s | 83 steps/min | |
| CMU 09_01 run | 3.5 m/s | — | 1.2 s: too short for cadence |
| 100STYLE Neutral_FW | 0.74 m/s | 87 steps/min | small capture volume: slow, curving |
| 100STYLE Neutral_FR | 1.25 m/s | 150 steps/min | a jog-in-place pace |
| 100STYLE Old_FW | 0.22 m/s | 67 steps/min | contact 74% per foot |

Search with word boundaries (`\bold\b`): plain substrings match "hold", "folding".

## 3. Judge every candidate before building on it

### 3.1 Numbers, written to a file
For each candidate clip record: duration; root travel distance and direction; pelvis height range (a "roll" whose
pelvis stays at 0.19 m is lying down); facing change; first and last pose; contact frames; hands above the shoulders
(frames); speed and cadence for gaits. Choose with the table in front of you.

### 3.2 A stick-figure contact sheet
`bvh_probe.py sheet CLIP [CLIP...] --out sheet.png`: front and side views per clip, left limbs blue and right red, a tick for head direction, root travel in body heights.
One sheet per action set. Of 36 needed actions, a library covered 31 well, 1 partly, and 4 needed keyframing (a
running trip, adjusting a rifle sling, walking down a ramp, sniffing): know that before blocking the shot.

### 3.3 What the libraries get wrong
- **Labels lie.** Whole subjects are duplicates of each other under different numbers; "Giving Directions" was a
  crouched search. Look at the motion.
- **Names lie about speed and content.** A "Jog" was nearly a run with a flight phase; a "Walk" was a 0.64–0.75 m/s
  stroll; "Push" was a crate-pushing loop; "Help_*" was waving for help; a knockback started from a crouched fighting
  stance (blend into it over 2–3 frames).
- **Weapon clips carry the weapon.** "Melee Attack Downward" is an axe attack, with a gripping hand, a cocked wrist
  and a rolled-out upper arm. On an unarmed character it reads as "fingers instead of a fist". Read the description;
  prefer unarmed sets (brawler, zombie, mutant) for unarmed characters.
- **Calibration faults.** One subject's right leg was splayed about 25° (foot 0.25 H wide, 0.04 H high). Use only
  that take's upper body, masked onto idle legs.
- **Direction and space gaps.** Every "slope" take went uphill; reversed, it read as walking backwards. Runs from a
  small capture volume curve all the time: straight stretches were 1–2 s for runs and 3–5 s for walks. Straighten the
  root yaw, and build downhill from a walk + foot IK + a back lean + a shorter stride.
- **Micro-motion you assume is there.** A "scared" take had no tremor: hand and head energy at 5–12 Hz matched plain
  standing. Measure it; add a procedural tremor layer if missing (`procedural.md`).
- **Published frame cuts can contain turns, T-poses or arm raises.** Sample wrist height over the range and drop
  frames where the hands go above the shoulders.

### 3.4 When the clip you need doesn't exist
List the closest options with what each actually does (numbers + a sheet), and let the user pick. Do not substitute
silently. Other routes: a text-to-motion generation (§5), the user's own video mocap, keyframing on the rig
(`locomotion.md` §14 for quadrupeds), or a procedural pose (`procedural.md`).

### 3.5 When a user-picked clip "looks wrong"
Check your use of it first: a speed-up, an early cut into another clip, a procedural override on top. Play it to its
own end pose at near-native speed and hold that pose (with breathing) before reaching for another clip.

## 4. Swapping clips without breaking approved work

- **A new clip must not move approved staging.** If cameras and other characters were solved from a spot the clip
  defines (where a fist lands, where a body ends up), a different clip moves that spot. Keep the spot fixed and fit
  the new clip to it: offset or turn the actor so the new contact lands there.
- **"Is the rig broken?"** Play the same clip through the library's own retarget onto the character and through
  yours, side by side. If they match, the rig and retarget are fine; look at the clip or the skinning.

## 5. Text-to-motion generators

- **Good at** stances, idles and everyday human actions (a fencer's en garde, arms raised in front and overhead).
- **Bad at** specific combat moves (a two-fist overhead slam, a sword drawn over the shoulder) and fast locomotion:
  "heavy charging run", "heavy jog" and "fast sprint, both feet leave the ground" all came back as 0.5–1 m/s walks.
  Runs come from a library.
- "A, then B" often performs only A and holds it. Splitting into timed shots did not help.
- A familiar everyday analogue lands where combat wording fails: "chopping wood" gave a real overhead-to-ground swing.
- Generations are cheap: test 3–5 phrasings, then judge.
- **Judge the raw motion on the generator's own body**, apart from any retarget, so you know which step failed.
- Don't trust an add-on's built-in retarget onto auto-rigged characters: their bind pose is often an A-pose while the
  generator rests in a T-pose, and the transfer kept the A offset (hands behind the back or across the body). Use §7.2.
- Render previews as JPEG and encode; PNG sequences of multi-view takes filled a near-full disk.

## 6. Video references for animals and creatures

No open mocap covers most quadrupeds. An image-to-video model makes a usable reference:
- **Start the video from a render of your own model:** side view, flat ground, the camera tracking alongside. The
  proportions match the rig and the scale is known from the render camera.
- **Check the gait before keying:** build a kymograph (the foot row of every frame, stacked). A planted foot is a
  streak moving with the ground; a swing is a gap. Read cadence and phase offsets from it. Check the footfall order
  on full-resolution frames (a camel walks a lateral sequence: hind and fore of one side lift close together).
- **Generated speed rarely matches the shot** (a reference walked 0.7 m/s with a 1.5 m stride; the shot needed
  1.6–1.8 m/s). Take the shape (foot paths, knee fold, head-and-neck motion) and the phase relations (lateral lag,
  duty factor) from the video; rescale stride and cadence to the shot. Keying the cycle: `locomotion.md` §14.

## 7. Retarget

### 7.1 Same skeleton: copy world rotations (exact)
When the character's rig was fitted from the library's own skeleton (`rigging.md` §2), bake each clip onto it:
- per bone: `character frame = (minimal rotation × source frame) @ D`, with `D` the fixed per-bone twist left by the
  roll carry;
- child positions come from the rest offsets (forward kinematics);
- only pelvis/root translation comes from the source, scaled by the pelvis-height ratio (1.011 and 1.082 on two
  characters);
- import the result as a plain transform-curve clip (Generic in Unity).

Print a check for every clip: maximum bone-direction error vs the source (0.00° achieved), and the lowest toe height
vs rest (walk −0.2 to +1.5 cm, idle −0.9 to −0.7 cm; jog/sprint +25 to +32 cm is the flight phase, not an error).

### 7.2 Different conventions: joint positions plus the hinge plane
Rest-pose twist conventions differ between libraries (Mixamo vs Xsens/CMU BVH): copying rotation deltas turns
forearms and shins inside out ("the arms are wrung") even when every bone points the right way.
- **Reference pose:** a T-pose of the character (its rig posed to T, or frame 1 of a library take downloaded on it),
  not its A-pose bind.
- Turn each limb segment of the reference to point where the source's rest segment points: shoulders, arms,
  forearms, thighs, shins, neck. Even "T-poses" stand with legs apart; that offset bows the knees in every pose.
- **Do not align the feet.** Ankle-to-toe pitch depends on footwear; forcing the source's pitch lifts a boot's toe.
- Give each limb bone the source segment's direction, then twist the upper bone so the target's own elbow/knee hinge
  lies in the source's bend plane. Blend the twist in with the bend angle (about 8–22°): a straight limb has no plane.
- **Hips:** scale travel by hip height. Set the hips' height from the lower foot, then reach the other planted foot
  with two-bone IK; with different leg proportions it hung 7–14 cm up otherwise.
- **Verify by numbers before rendering:** dot of each source and target segment direction ≈ 1; hinge error ≈ 0° on
  every clip; toe heights on planted frames ≈ 0.

### 7.3 BVH specifics
- **A BVH has no floor.** Its rest pose hangs below the root; take the floor as the lowest ankle over the clip, or the
  character hovers.
- **Face forward:** takes walk in any direction across the volume. Rotate by the clip's mean heading so the character
  walks along its own forward.
- Units vary: CMU conversions are ≈17 units/m, 100STYLE ≈100 (`bvh_probe.py stats` estimates it).

## 8. Muscle-space (Humanoid) vs exact (Generic) clips

Engine muscle-space retargeting (Unity Humanoid and similar) normalises poses into muscle values. On custom rigs it
can lose knee and foot direction:
- idle knee direction −68/+78° under Humanoid vs −14/+44° raw;
- one character's foot yawed 70° mid-walk under Humanoid vs 2° raw;
- the library's own clip pack on its own Humanoid avatar showed the same idle error, so the cause was the muscle
  representation, not the target rig. Nearly straight knees (flex 12–15°) have an ill-defined bend direction.
- Avatar variants tried were all worse (legs aimed in T-pose with twist 0/0.5/1, no aiming, arms-only aiming): walk
  knee −73° to −161°.

**Decide with a leg probe**, not by habit: knee flex, knee direction, foot yaw and ankle width per frame on the raw
source, the muscle-space version and the exact version. If muscle space keeps the knees, use it for its benefits
(cross-rig sharing, built-in foot IK, muscle masks). If not, bake exact clips per character.

**The cost of exact clips:** one clip pack per character; re-bake whenever a rig changes; no built-in foot IK,
cross-rig retargeting or muscle-based masks (use transform masks and your own IK, `procedural.md`).

## 9. Bake clips that play exactly

- **Quaternion continuity:** negate a key whose dot with the previous key is negative, or interpolation goes the long
  way round. Apply it to helper bones too.
- **Put the corrections inside the clip:** helper half-rotations, the arm cap and the clavicle share
  (`rigging.md` §3.3, §5.1) are keyed, so any player (state machine, playable graph, timeline) gets the corrected
  motion.
- **Pose state:** POSE for baking, REST for mesh export; no NLA tracks; fake user on actions; action slot on newer
  Blender (`rigging.md` §7).
- **Keep root motion honest:** move horizontal translation and yaw from the hips to a root bone if the engine needs
  it, or drive the path in the engine. **Never scale root motion:** a dive compressed to 55% made the character flop
  prone and sink into the floor.
- **Body clips must not animate face bones.** Clips baked on an older rig keyed the eyes at an old rest about 7 mm and
  several degrees off the re-seated eyes, and the eyes "looked down". Mask the eye (and jaw) transforms off every body
  clip on import; the face player is the only thing that drives them. Check the import log shows 0 eye curves.

## 10. Carrying poses over from a mannequin rig

Blocking is often done on a placeholder mannequin with a pose table. To port it to the real rig:
- treat each pose offset as a rotation in the character's frame (X right, Y up, Z forward) around that bone's idle
  reference: `Delta = inv(refRoot) · Euler · refRoot`;
- limb entries override the clip; spine and head entries are additive;
- spread spine offsets 0.3/0.35/0.35 over three bones; head offsets 0.4 neck / 0.6 head.

Most values port 1:1. The exceptions: costume limits (`rigging.md` §5), body volume (hip offsets, contacts) and
airborne poses (never ground-pin them; `procedural.md`).

## 11. Polish what you have before shopping for more

- Library clips have a "cotton" feel: even timing, soft poses. Retime unevenly: holds on extreme poses, faster into
  contact, stronger key poses. Weight and timing rules are in `performance.md`.
- Tools: Blender NLA (replace/add layers, strength, holds, strip scale for retiming); Cascadeur (unbake, AutoPhysics,
  AutoPosing; has a Mixamo clean-up workflow; check the current tiers for export limits); engine constraint rigs (two-bone IK,
  multi-aim, damped transform).
- **Clip libraries do not fix timing, weight or scale.** The biggest gain comes from retiming and re-posing the clips
  you already have. Once motion is approved, bake the final clips into one scrubbable timeline.
