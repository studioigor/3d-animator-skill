# Animation QA: labs, probes, reviews and acceptance

Read this before showing the user any body or face motion, whenever the user says something "looks wrong", and
after any rig, retarget, skinning or clip change. It covers the review setup, the numeric probes, how to diagnose a
complaint, and the process rules that keep fixes from breaking approved work.
Values marked **(estimate)** are starting points, not measurements.

## 1. Rules

- **Numbers before eyes.** Measure, change one thing, measure again. Report the numbers with every fix
  ("hand pop 90 → 5 m/s", "knee 85° → 135°", "PP closures 50/50").
- **The user judges videos and sheets, not live scenes.** Many users won't operate an engine scene, even with a
  control panel built for them. Deliver before/after videos and sheets by default.
- **Fix motion where it is cheap to see:** in a lab, not in the dressed shot. Put fixes back into the shot only once
  the lab is clean.
- **Never move what the user approved** to make a new piece fit. Fit the new piece to the approved staging (§5).
- **Gate every build automatically** before the user sees it (identity, contacts, lip-sync QA). Measure the previous
  approach the same way, as a baseline.

## 2. The animation lab

A second scene that you build next to the shot (from the same builder code, if scenes are built from code), with
the same characters, rigs and clips.

**Character and face lab (look and deformation).**
- Neutral cyclorama. Fixed lights: key, fill, two rims, a top pool and a backdrop light. A filmic tonemapper (ACES or similar), the
  same one the shots use.
- Physical camera presets:

| Preset | Focal length | Distance | Aperture | Notes |
|---|---|---|---|---|
| close-up | 100 mm | 0.95 m | f/2.2 | eyes in focus every frame |
| medium close-up | 85 mm | 1.45 m | f/2.4 | |
| medium | 50 mm | 2.4 m | | |
| full | 35 mm | 4.4 m | | camera turned 35° off the face axis |

- The camera follows the eyes softly: 60% of the head's motion with a lerp of 0.12 (0.15 of the motion in full
  shots). Depth-of-field focus goes on the eyes every frame.
- Render 2× supersampled (3840×2160 for 1080p). Burn captions into the reel: clip name, line text, pipeline version.
- Drive takes from JSON shot lists (character, clip or line, preset, duration) → reels. Make one reel per character
  covering every baked clip; it is the acceptance gate for a rig or a clip pack.
- When a take has a voice track, delay the audio by the lab's pre-roll when encoding.
- **Face views:** head-locked front, three-quarter, profile, and the gameplay angle (often from below). Aim them from
  the head bone's bind pose; aimed from the model's forward on the first frame, they looked down from above the brow.
  Cut the hair away from a profile view with the camera's near clip.
- **Face take:** the shot's performance, then each control swept 0 → 1 → 0, then every expression preset held.
  Record the old and new rig on the same program and deliver them side by side.

**Staging lab (timing, poses, contacts, transitions).**
- Grid floor with 1 m lines (foot slide and travel read at once), flat daylight, no fog or effects.
- Four views at once: the gameplay or shot camera, side, front, and a wide shot of all characters.
- Burn in: output time, clock scales (hit-stop, slow motion), the last beat, and each character's clip and clip time.
- A take records in about a minute. Judge timing, poses and contacts here; judge the look in the real scene.
- **Choreography is data, not code:** a JSON list of segments (clip, start in output seconds, offset into the clip,
  speed, cross-fade length) that both the lab and the shot read. Give the controller a fixed number of slot states
  filled at runtime through an override controller, so any downloaded clip can go in any slot without regenerating
  anything.
- **Optional user panel** (play mode, an IMGUI panel): play, pause, frame steps and slow speeds; a timeline with beats
  where a click jumps to that moment (re-simulate from the start with cameras off, so the jump is exact); a clip
  picker with nudges for start, offset, speed and fade; Apply that restarts at the same moment, plus Undo; a free
  orbit camera; notes typed at a moment and appended to a file with the time. The user's tuning then becomes the
  choreography directly.

## 3. Review artefacts

- **Before/after video per element** (the run, the cast, the end stance; `scripts/contact_sheet.py compare` and
  `grid` make them). Layout 2×2: before and after side by side,
  the shot view on top and the side view over the grid below. Burn in labels naming the change, add a half-speed
  copy, and put the explaining numbers in the report.
- Burn labels in as image overlays with ffmpeg's `overlay` filter (render the text with Pillow). Many ffmpeg builds
  lack `drawtext`.
- Compare at the identical time: the same Timeline second, the same pose, the same camera, the same lights.
- **Contact sheets** for problem windows (rise, hit, draw): frames every 0.1–0.2 s, selected by index
  (`ffmpeg -fps_mode passthrough`), tiles at half size with the time stamped on each. Rearrange tall sheets into two
  columns. One sheet per character per window reads in seconds; scrubbing a video doesn't.
- **Stick-figure sheets** for choosing clips: front and side views, left limbs blue and right red, a tick for the head
  direction, root travel in body heights. Of 36 candidate actions screened this way, 31 were good, 1 partial, and 4
  had to be keyframed.
- Short clips of the problem windows at 24 fps, 720p, are enough for review.
- **Contact stills:** show every contact from two kinds of view. The shot camera hides misses; side cameras expose
  them.
  - Free cameras orbit the hand or claw at azimuths of ±90° relative to the toucher's facing, with post-processing
    off and a key light 0.3 m above the camera.
  - Orbit cameras can end up inside a big body: choose the distance per contact, or check what the near clip cuts.
  - Pale skin and light suits blow out under the key light; lower it rather than judging a white blob.
- **Gait check:** a free camera panning with the character at 12 fps, separate from the shot cameras. Stride, foot
  plants and slide read better there than in a cut.
- **Pose sheets with forced action weights:** pose the rig with one action at full weight per tile. For gameplay
  characters and creatures, make a state-by-view grid (for example walk, idle, crouch, lunge, curl, dead × side,
  three-quarter, rear). One grid caught a tail through the floor and a death roll sinking into it.
- **Face sheets:** three columns, original | previous approach | new approach. Poses neutral, jawOpen 0.5, smile,
  blink, funnel, pucker, from the front and at three-quarter. Eye close-ups: neutral, blink, half-blink, squint,
  wide. Add the combined extremes (scream mix, sob): they reveal folds and lid crossings that single keys hide.
- Previews as JPEG; delete frame sequences after encoding only with the user's consent (§10).

## 4. Numeric probes

Write each probe once as a tool, keep its output as a file, and choose with the numbers in front of you.

| Probe | What it measures | Pass / reading |
|---|---|---|
| **Clip analysis** | duration, root travel distance and direction, pelvis height range, facing change, first and last pose, contact frames | a "roll" whose pelvis stays at 0.19 m is lying down; use `scripts/bvh_probe.py stats --feet` for BVH (speed, cadence, contact %, stance slide) |
| **Gait speed** | the clip's natural ground speed against the speed it must play at | playback rate within ~0.85–1.2× **(estimate)**; beyond that use another clip or a stride change (locomotion.md); `bvh_probe.py stats --target-speed` |
| **Bake check** | max bone-direction error of the baked clip against the source; lowest toe height against rest | 0.00° for an exact bake; walk toes −0.2…+1.5 cm, idle within ~1 cm; runs show +25–32 cm in the flight phase |
| **Retarget check** | dot product of source and target segment directions; elbow and knee hinge error | dot ≈ 1, hinge error ≈ 0° on every clip |
| **Leg probe** | knee flex, knee direction (bend-plane azimuth), foot yaw, ankle width, for raw source vs each import path | decided Generic over Humanoid (engine-unity.md §2) |
| **Contact checker** | signed distance from the hand (or claw) vertices to the touched body, on skinned meshes baked every frame | gap ≤ 2.5 cm and depth ≤ 3 cm over the contact plateau, not over arrival and departure |
| **Effector trace** | position and speed (|Δp|/dt) of the limb in question every frame, with the Animator state, normalised time and transition flag | a pop is a frame faster than ~3× its neighbours; the state columns usually name the cause |
| **Angular trace** | world rotation speed of hips, spine, chest and head, and its change per frame | a chest jump of 200°/s in one frame is the pop even when the eye blames the head |
| **Floor trace** | every 0.1 s: clamp lift, lowest bone and its height, foot heights, clip and clip time | hovering is almost always the clamp answering a bad blend or retarget |
| **Pose probe** | hip height as % of standing, pelvis facing to the target, trunk lean, knee and elbow angles, hand heights, root travel | a "ready" stance at 87–91% reads as a half-crouch next to 95%; a stance turned 50–60° off the target reads as strange |
| **Elbow hinge** | angle between the arm's bend plane and the bind flexion axis carried by the upper arm | mocap stays within ~15°; above ~70° reads as broken |
| **Forearm twist** | palm angle about the forearm, measured from the elbow's hinge | natural range about −75…+100° from mid-prone |
| **Palm facing** | palm normal against the intended facing after every procedural arm pass | turn about the forearm's own line if wrong (procedural.md) |
| **IK reach** | target distance as a fraction of the limb length; miss distance | saturate softly at 0.95–0.985; a target beyond reach locks the limb straight |
| **Muscle limits** (Humanoid) | `HumanPoseHandler.GetHumanPose` muscles on the final pose | flag \|m\| > 1.1; skip fingers, jaw, eyes; shoulder muscles are advisory, arm and forearm twist spans are real |
| **Self-intersection** | a capsule per bone segment (torso chain, head, upper arm, forearm, hand, thigh, shin) | radius = 55th percentile distance of the vertices whose dominant weight ≥ 0.6, from `BakeMesh(mesh, useScale: true)` and `localToWorldMatrix`; test forearms and hands against other chains with 35% slack of the thinner radius; skip collapsed capsules |
| **Weapon** | blade against body capsules (skip the holding hand and forearm); handle axis vs knuckle line; open fingers off the handle | blade never inside a capsule; fingers wrap the handle |
| **Gaze** | cornea apex (the farthest 5% of eye vertices from the pivot) minus the pivot → pitch, yaw; centre-to-bone offset in mm | catches "looking down" and off-centre eye pivots |
| **Footstep sync** | step events against foot contacts | events fire on the contact frames |
| **Lip-sync QA** | closures, chatter, ranges, blinks per line (facial-animation.md) | every PP closure ≥ 0.9 for ≥ 40 ms; chatter 0; values in range, no NaN |

Log per frame what the eye would catch (a sole inside the floor, a planted sole above it, an IK miss), and check
where the feet are in frame before polishing a contact nobody can see.

**The contact checker in detail.** This is the probe that turned "the grabs look off" into fixable numbers.
1. Evaluate the timeline at the chosen fps and tick the rigs as the renderer does (engine-unity.md §9).
2. Bake the skinned meshes every frame: bake with scale, then transform by the renderer's matrix without scale.
3. **Toucher vertices:** the vertices whose weights on the hand chain sum to ≥ 0.5.
4. **Touched body:** its opaque renderers (render queue < 2900) plus any transparent parts the hand must not enter
   (visors, glass), minus light cones and other fake-volume meshes. Put the triangles in a spatial hash with 4 cm cells.
5. **Per frame:** the gap (the closest outside toucher vertex), the depth (the deepest inside one), and the name of
   the mesh the deepest vertex is in. The mesh name matters: once the body mesh passed, it showed that the
   remaining depth sat in a wrist accessory and headgear, which pointed at the wrist's path rather than the hand's.
6. **Inside or outside:** every surface point within the nearest distance + 1.5 cm votes with its normal; points
   farther than 12 cm count as outside. Using the nearest normal alone, one flipped scan normal produced fake
   penetrations of 70 cm.
7. **Pass:** gap ≤ 2.5 cm and depth ≤ 3 cm. Report the held plateau separately from the full window (one contact
   passed 60% of the full window but 92% of the hold).

Reference results: with whole bodies staged and no hand targets, 0% of frames passed on all 8 contacts (misses of
50–120 cm). With surface anchors and palm IK (procedural.md §5), held contacts passed up to 92%, and a fast strike 8%.

**Sample fast moves at render rate or faster.** During a 0.16 s yank, a claw dipped deeper for 2–3 frames that fell
between 24 fps samples. Around fast motion, measure at the output frame rate or above.

## 5. Diagnosing a motion complaint

When a reviewer says a motion "looks wrong", "jerks", "floats" or "isn't believable", measure before changing
anything.

| The user says | Measure first | Usual cause |
|---|---|---|
| jerks, pops, "the head jerks" | effector and angular traces from the first frame of the take | a cross-fade or `Play()` snap, a hard clamp switching on, a hit-stop releasing onto a different pose |
| floats, hovers, half-crouch | floor trace, pose probe | the floor clamp answering a mismatched blend; a stand-in retarget; a grounded pose in an airborne moment |
| shuffles, skates, slides | gait-speed probe, stance slide | playback rate far above the clip's natural speed; no foot pinning after a stride change |
| arm broken, wrist wrung | elbow hinge, forearm twist, palm facing | twist unwrapped frame to frame against body axes; an elbow hint on the wrong side; an unreachable target |
| hands through the body | self-intersection capsules, contact checker | no hand IK, guessed contact radii, the touched body posed a frame late |
| strange stance | pose probe against the target | a library stance turned 50–70° off; hips too low |
| face stretched, lips slide over teeth | head weights at the mouth | chin and lips partly on the neck bone (rigging.md) |
| eyes look down | gaze probe; eye curves in body clips | body clips keying eye bones at an old rest |
| hand missing, hollow, normals flipped | triangles per dominant bone before and after each cut or re-skin | geometry cut away by a height test in the bind pose |
| limb through a prop or debris | contacts on posed meshes | a missing collider before it is a pose problem |

- **"Is the rig broken?"** Play the same clip on the character through another path (a web library's retarget onto
  the uploaded character, or your DCC retarget) next to yours, on one clip board. If they match, the rig and
  retarget are fine and the problem is the clip or the skinning; don't rework the rig.
- **Read the clip's description, not only its name.** Weapon clips carry the weapon: an "attack downward" can be an
  axe attack with a gripping hand and a cocked wrist, which reads as an open hand where a fist is expected on
  an unarmed creature. Mocap labels are wrong too: duplicate subjects under different numbers, mislabelled takes.
- **A clip the user picked "looks wrong" in the cut:** check your use of it first (speed-up, an early cut into
  another clip, a procedural override on top). Play it to its own end pose at near-native speed and hold that pose,
  with breathing, before reaching for another clip.
- **Swapping a clip must not move approved staging.** If other characters and the camera are solved from a spot the
  clip defines (where a fist lands), a new clip silently moves that spot. Keep the approved spot fixed and fit the
  new clip to it: turn or offset the actor so the new contact lands there.
- **Geometry at the key beat:** distance and elevation from the acting limb to its target, gaze pitch, and the line
  of sight to the target. A throw at 70° up, a gaze tipped past ~30°, or a target hidden behind the opponent's arm
  show up here first.
- **Missing geometry vs pose.** Any mesh edit made by height in the bind pose can take the limbs hanging there: an
  A-pose has the hands at hip height, so "drop everything below the hips" drops the fingers. What shows is an open
  cuff, visible only when the wrist turns toward the camera, so it looks like a pose problem. Count triangles per
  dominant bone before and after every cut; exempt vertices weighted ≥ 50% to the arm chain from height tests.
  Two failed pose fixes on a "missing" part mean it's time to check the mesh.
- Never substitute a clip silently. When the clip you need doesn't exist, list the closest options with what each
  actually does, and let the user pick.
- Fix the cause the numbers point to, then show a before/after with the numbers.

## 6. The frame-by-frame review take

When the user lists several broken motions at once (hands through the body, wrenched arms, a weapon through the
torso), stop judging the shot and review each character on its own, measured, frame by frame.

- Six viewports in one 1920×1080 frame (640×540 each). Top row, the first character: front, side, shot camera.
  Bottom row, the second: from the opponent's side, from the right, from behind.
- Re-aim every camera at the character's chest each frame, so the body fills the panel whatever the root does.
- Hide anything that hides limbs: smoke, fire sheets, fog, VFX.
- Burn in output time, the clip and clip time per character, and the last beat.
- Once a weapon is in hand, swap the "from behind" panel for a close-up of the weapon hand. A grip can't be judged
  from 4 m.
- Cut contact sheets per character per problem window at 0.1–0.2 s steps (`contact_sheet.py sheet --range`).
- Run a QA component last in the frame (after all IK and bone edits) that logs problem spans to the console and a
  CSV: muscle limits, self-intersections, weapon in body, IK misses, floor violations (§4).

## 7. Critics

Once the measured spans are clean and the sheets read right, hand the sheets, the take and the CSVs to independent
reviewers (separate agents with fresh context), each with one lens:
- **a game animator:** weight, poses, arcs, timing, overlap;
- **a combat or weapon specialist:** grip, edge alignment, stance, the line of a strike;
- **a naive viewer:** lists only what looks broken, in plain words.

Ask for per-item verdicts, not an overall grade. They catch a fix that moved the fault (a blade leaving the torso but
passing through the head). Critic passes on rendered stills of the finished shot also catch stale poses and sockets
from the offline renderer (engine-unity.md §9).

## 8. Face acceptance (summary)

- **Identity:** render the neutral rig and the original from the same cameras; measure SSIM inside the face oval and
  landmark error in mm. Measure the previous approach the same way. Details and thresholds: facial-rig.md.
- **Deformation:** fold and sliver counts per key and on combined extremes, gated relative to a known-good template
  (facial-rig.md).
- **Motion:** lip-sync QA per line; blinks, gaze and head ranges (facial-animation.md).
- **Renderer:** confirm texture and mesh fixes in the engine, not only in the DCC preview.
- A cheap sanity check: read MediaPipe blendshape scores off the rendered neutral and the original. A blink score
  far from the original's flags eyes rendered more open or closed than the source.

## 9. Regression after rig, skinning or clip changes

- Re-bake every clip and read the bake log (bone-direction error, toe heights) before anything else.
- Re-run the contact checker on every authored contact, and the lip-sync QA on every line if the face changed.
- Re-render the lab reels for the changed characters and compare against the last approved reels at the same times.
- After swapping placeholder mannequins for real bodies, re-measure heights and spacing: one real head sat 0.12 m
  lower, which moved a close-up's camera and aim heights by the same amount. Re-check every pose that has a hip
  offset (body volume changes interpenetration) and every pair whose silhouettes could merge.
- Gameplay: run a scripted bot playthrough (hits, deaths, kills, beats, the end screen) and require a log with no new
  errors or warnings. Know its limit: the bot has perfect information and turns instantly, so it never tests
  whether an attack's telegraph reads for a human. Say so in the report.
- Cross-check edit-mode and play-mode measurements: call the same measuring code by reflection on live play-mode
  frames and compare at matching times. They should agree to the millimetre (engine-unity.md §9).

## 10. Process rules

- **Back up before every destructive step** (`.pre_<step>` copies) and make scripts idempotent: re-run guards
  ("helpers already present"), restart from backups detected by md5, re-measure on every run. Give build scripts a
  dry-run mode that writes outside the engine's asset folder.
- **Archive every rejected version completely:** recordings, shot lists, 3D sources, the engine export, and a README
  with the user's verdict in their words. You will need the A/B and the rollback.
- **Show trade-offs instead of deciding them.** When a fix changes something visible (knees spread over a saddle
  pad vs legs sunk a little into it), render both, put them behind one switch so either is one build away, and let
  the user choose.
- **Ask the user about what only they can judge** (where a mouth line goes, whether a steep throw is acceptable), on
  a picture they can mark up.
- Don't drive the editor while the user works in it.
- **One agent owns deletions.** In multi-agent runs, helper agents leave temporary files in their own scratch
  folders and list them in their report; the coordinating agent removes them with absolute paths and the user's
  consent.
- Disk: frame sequences and mocap archives are large. Encode, then propose the deletion; don't delete source frames
  on your own.
- Several agents on one engine instance: follow the lock and ownership rules in engine-unity.md §10.
