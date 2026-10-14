# Unity: import, playback, rig runtime and offline renders

Read this when you bring rigged characters, clips or facial curves into Unity, build the runtime that poses them
(Animator, PlayableGraph, Timeline), or render stills and reels from edit mode. How the motion should look lives in
locomotion.md, procedural.md and facial-animation.md; this file covers only the Unity mechanics.
Values marked **(estimate)** are starting points, not measurements.

## 1. Exporting for Unity and importing the model

**Blender FBX export: settings that round-trip cleanly.**

| Setting | Character mesh | Armature + clips |
|---|---|---|
| Axes / scale | forward −Z, up Y, apply unit scale `FBX_SCALE_ALL`, no `bake_space_transform` | same |
| Bones | primary axis Y, secondary X, armature node type NULL, no leaf bones | same; keep non-deform bones (`use_armature_deform_only=False`) so helpers and sockets survive |
| Pose | rest pose | set POSE before baking, REST for the mesh export |
| Mesh | modifiers **not** applied (applying drops the shape keys), face smoothing, no tangent space | no mesh, or the mesh in rest |
| Animation | none | `bake_anim_use_all_actions`, NLA off (one action = one take), force start/end keys, step 1.0, simplify 0 |

Before exporting actions: remove NLA tracks, give every action a fake user, and on Blender 4.4+ assign the
`action_slot`. Unity takes the model's default pose from the frame current at export, so export a clip-free
rest take whenever "rest" matters (locomotion.md).

**Model importer.**
- Blendshapes on; normals Import; blendshape normals Calculate; tangents MikkTSpace; mesh compression Off.
- `optimizeGameObjects = false`: procedural layers, sockets and the face player need the bone transforms.
- Material import none. Build materials in code from a manifest the exporter writes (per part: textures,
  smoothness multiplier, normal strength, alpha clip). Starting values that held up on scanned heads: head normal
  strength 0.6 and smoothness ×0.62; teeth smoothness 0.55, tongue 0.50.
- Lashes: alpha clip 0.5, cull off, no shadow casting, "mip maps preserve coverage" on the texture, alpha-test queue.
- Eyes: switch environment reflections off on the eye material, base tint (1, 0.97, 0.95), smoothness 0.85.
  Glossy eyes in a dark set mirror it and read chrome-grey; the catchlights should come from lights.
- Textures: normal-map type for normal maps; linear (sRGB off) for metallic, smoothness and mask maps; max size 4096,
  high-quality compression. Skip rewriting unchanged textures (compare pixel md5 at export) so Unity does not
  re-import and re-compress 4K maps on every run.
- Apply import rules in an `AssetPostprocessor` only on first import: gate on `assetImporter.importSettingsMissing`.
  Afterwards the build code owns the settings, and a re-import must not overwrite them.
- Check every new procedural mesh (collars, sweeps, cut caps) in Unity itself. URP and HDRP cull back faces;
  Blender's Eevee draws them, so an inside-out mesh can look fine in previews and show as a hole in the engine.
  Make fabric and collar materials two-sided where a gap could show the background.

## 2. Generic or Humanoid

| | Generic (clips baked per rig in the DCC) | Humanoid (Unity retarget) |
|---|---|---|
| Accuracy | exact: bone directions match the source to 0.00° | muscle space can lose knee and foot direction |
| Cost | one clip pack per character; re-bake on every rig change | one pack for every humanoid |
| Features | no Humanoid foot IK, no muscle masks, no cross-rig retarget | foot IK, retargeting, muscle-based masks |

- **Measure before you choose.** Run the leg probe (qa.md §4) on the raw source, the Humanoid import and the Generic
  import for two or three clips. On one auto-fitted rig Humanoid put the idle knees at −68/+78° (raw: −14/+44°)
  and yawed a foot 70° mid-walk (raw: 2°). The clip library's own pack on its own avatar showed the same idle error,
  so the cause was the muscle representation, not the rig. Nearly straight knees (flex 12–15°) have an ill-defined
  bend direction, which makes it worse. Avatar variants (legs aimed in T-pose with twist 0 / 0.5 / 1, no aiming,
  arms-only aiming) all came out worse.
- Rule of thumb: if Humanoid moves knee direction or foot yaw more than ~10° from the source, bake per rig and
  import Generic **(estimate)**. The baking method is in clips-retarget.md.
- **Humanoid avatar for an A-pose model.**
  - Pose the instance to the skin bind pose first (from the mesh `bindposes`). An FBX's default node pose can be
    an animation frame.
  - Then build an explicit T-pose skeleton: upper arm, forearm and hand along ±X, deciding each side from where the
    hand actually is; legs straight down.
  - Settings: arm and leg twist 0.5, stretch 0.05, feet spacing 0, no translation DoF.
- **Playing Generic clips.**
  - Set `animator.avatar = null` on the instance when the clips are not Humanoid.
  - `applyRootMotion = false` when a path or a script moves the character.
  - `cullingMode = AlwaysAnimate`, foot IK off.

## 3. Clip import

- **A clip-only FBX (armature, no mesh) needs `preserveHierarchy = true`.** Without it Unity drops the top
  "Armature" node, the curve paths (`Armature/root/pelvis/...`) stop matching the model, and nothing binds.
- Exact baked clips: resample curves off, animation compression Off.
- Set loop flags from an explicit list of clip names, not from guesses; `loopPose = false` for baked cycles whose
  first and last frames already match.
- Humanoid packs: bake root rotation into the pose and keep the original orientation; lock root height and XZ
  unless the clip is a root-motion take (a naming convention such as a trailing `_RM` helps).
- **Body clips must not own the face bones.** Clips baked against an older rest keyed the eye bones about 7 mm and
  several degrees off the re-seated eyes, and the eyes "looked down". Put an avatar/transform mask on every body
  clip at import (`maskType = CopyFromOther` with a mask asset that switches off eyes and jaw). Check the import
  log for 0 eye curves. The face player is then the only thing that drives the eyes.
- Clip names lie about content and speed; analyse every clip before building on it (clips-retarget.md, qa.md §4).

## 4. Runtime flags and editor traps

- `SkinnedMeshRenderer.updateWhenOffscreen = true`: characters moved along long paths leave their bind-pose bounds
  and get culled mid-shot. Turn skinned motion vectors on. Eyelashes cast no shadows.
- **Fake null.** `GetComponent<T>() ?? gameObject.AddComponent<T>()` fails in the editor: Unity's missing-component
  objects compare equal to null only through the overloaded `==`, and `??` checks the reference. Write
  `var c = GetComponent<T>(); if (c == null) c = gameObject.AddComponent<T>();`.
- Mount rigid accessories (helmets, wrist displays, glasses, backpacks) on bones instead of skinning them. Place each
  in the bind pose, then re-parent with `SetParent(bone, worldPositionStays: true)`. Store seat transforms in
  character-root space at the bind pose; derive "forward" from the skeleton (cross of hip lateral and up, or eyes
  minus head), never assume an axis.

## 5. Mecanim practice

- **Root motion that must hit a planned mark.**
  - Don't warp a locomotion cycle every frame toward a constant-speed schedule. A short-half-life pull drags the
    feet over the floor, and since the clip's root speed varies within the stride, the pull never settles.
  - Probe instead. In `Start`, set the facing and cycle phase, then step the Animator ahead with
    `Animator.Update(1/60f)` in a loop for the run's duration, while `OnAnimatorMove` only sums `deltaPosition`.
    The start point is the mark minus that sum. Pure root motion then lands within a few centimetres.
  - Probe pitfalls: rewinding with `Play` + `Update(0)` after the probe reports a bogus root delta, so restore the
    position yourself. A segment that starts in `Update` runs before that frame's Animator step, so probe one step
    less than the beat time.
  - Keep a weak safety net that follows the probed path, not a straight line: half-life about 0.5 s **(estimate)**.
    Log the error at the mark.
- **Phase-align cycles.** Start a looping run at the phase that puts the transition on the right pose: a sprint
  should arrive at a roll on the frame where the roll's first pose matches.
- **Cross-fade only between matching poses, and measure the match first.**
  - Compare bone positions relative to the pelvis, in the pelvis frame (heading removed), plus pelvis height and
    limb velocities, over a window of both clips. Pick the pair with the lowest cost.
  - An RMS of about 0.1 m blends invisibly. At 0.3 m or more, with a heading difference of tens of degrees, the
    blend can't be hidden **(estimate)**.
  - A blend between mismatched poses pushes limbs through the floor; the floor clamp then lifts the whole body and
    the character hovers in a half-crouch. That is the classic "why is he floating" bug.
  - Prefer one clip played through to a stable pose (both feet planted), then a 0.3–0.4 s fade into the stance,
    over splicing the second half of another clip.
- **Animator timing traps** (each one produced a visible pop):
  - `CrossFadeInFixedTime` counts in Animator time, which runs at `animator.speed`. At speed 0.35 a "0.35 s" fade
    takes a full second. Size fades as window × speed.
  - `Play()`, or `Play(state, layer, t)` at speed 0 for script-driven clip time, during a running transition
    cancels the transition and snaps to the target pose. One snap moved a hand 0.6 m in one frame.
  - Sampling poses from script (`Play` + `Update` in `Start`) also runs the IK pass. Compute everything the IK reads
    first, or zero the IK weights while sampling.
  - Negative `animator.speed` does not reverse a clip: the pose stops updating and root motion is zero. Drive
    normalised time from script instead.
- **Edit-mode sampling** (clip analysis, staging solves, stills): use `AnimationMode.SampleAnimationClip`. A
  PlayableGraph moves bones in edit mode only with `DirectorUpdateMode.Manual` and an explicit `graph.Evaluate()`
  each time; an auto-updating graph never ticks there.
- **Yaw is yours, translation is the clip's.** Ignore the clip's yaw and set the facing from script with damping.
  Never turn a character while root-motion travel plays: root motion follows the body's facing, so the travel
  bends into a curve. Turn after the travel hands over, and rotate any remaining world-space delta back through
  the rotation the Animator used.
- **Layered actions** (casting while kneeling): an upper-body layer with an Avatar Mask that **excludes the head and
  neck**, or the layer's head pose fights the body and the head twists backwards. Enable the IK pass on the base
  layer. Look-at weights in `OnAnimatorIK` that worked: body 0.15, head 0.8, eyes 1, clamp 0.55.
- **Contact IK** with `SetIKPosition` / `SetIKRotation`: fist pinned to the floor at an impact, palm on the floor
  when kneeling, feet planted.
- **Floor clamp:** after animation, push the body up if any bone, with a per-bone radius, goes under the floor.
  Library rolls often put the head a few centimetres under it. Log the lift; a large lift means a bad blend, not a
  bad floor.
- **Animators run on character time.** Set `animator.speed` from the game clock every frame, so hit-stops and slow
  motion freeze or slow the pose too.
- Spring or damp procedural overrides on game time (they must freeze in hit-stops); see procedural.md.

## 6. Timeline-driven rigs

Cinematics and scripted gameplay beats get the most control from a rig component that the Timeline feeds with
actions, rather than from Animator state machines.

- **Make the pose a pure function of the director's time.** Carry no state between frames. Read speed and gait
  phase from tables built from the path track at load (a 30 Hz table is enough). Then scrubbing, offline stills and
  play mode all agree: play-mode and edit-mode contact measurements matched to the millimetre at matching times.
- **Graph.** A PlayableGraph with `DirectorUpdateMode.Manual` and an `AnimationMixerPlayable` over the base clips
  (idle, talk, walk, jog, sprint) plus action clips. Normalise the weights, call `Evaluate(0)`, then write the
  procedural bones (IK, look-at, helper bones, additive layers). Pose in `LateUpdate`. In edit mode, also pose in
  `Update`, so components that read bones in `LateUpdate` (sockets, cameras) see the current pose.
- **Execution order** (`[DefaultExecutionOrder]`):

| Order | Component | Job |
|---|---|---|
| 40 | action pushers (Timeline clips, gameplay scripts) | push named actions with weights for this time |
| 50 | character and creature rigs | evaluate graph, procedural layers, re-apply helpers |
| 60 | prop sockets | blend sockets by action weight, attach and release props |
| 200 | face player | blendshapes, eyes, additive head after everything else |

- **A gameplay chain** (first-person weapon, AI creatures) needs the same discipline. One chain that worked:

| Order | Phase | Component |
|---|---|---|
| −40 | LateUpdate | camera kick: multiplies onto the head rotation before the viewmodel and the camera system read it |
| 0 | LateUpdate | viewmodel: rewrites the whole weapon pose from scratch each frame |
| 40 | Update | gameplay action pushers |
| 50 | LateUpdate | character rigs |
| 60 | LateUpdate | creature bodies, after the AI's Update |
| 200 | LateUpdate | weapon kick on top of the viewmodel |
| 220 | LateUpdate | reload hand and magazine contacts |

- **Keep every offset non-accumulating.** The controller rewrites the base value every frame (the head rotation in
  Update), and the offset (camera kick, recoil, sway) multiplies on top in LateUpdate. An offset added onto last
  frame's result drifts.
- **Action ownership.** Each pusher clears only its own actions before pushing (for example a
  `ClearActions(source)` method on your rig component). An action
  pushed without a source stays forever; a stale look-at did exactly that. Clear everything when a gameplay script
  takes over from the Timeline.
- **One action vocabulary for stand-ins and real characters.** A mannequin rig and the real-character rig answer
  the same action names, so blocking survives the swap. If a character's prefab or clips are missing, fall back
  to the mannequin.
- **Off-Timeline fallback** (gameplay, free roam): speed from the transform's velocity, smoothed exponentially at
  9/s rising and 4/s falling. Treat a step over 2.5 m as a teleport (speed 0) and cap at 8 m/s. A gameplay script
  takes over only once the director is done (`time ≥ duration − 0.05`).
- **Pose the touched body first.** When one rig reads another's bones (a hand on a shoulder, a claw on a suit),
  call the other rig's posing method for time t first, with a "posed at time" stamp and a recursion guard; otherwise its bones are
  a frame old. When one rig moves another's root (a held victim), re-pose the moved rig afterwards, so the result
  does not depend on update order.
- Re-apply half-rotation helper bones after every procedural override and after IK (rigging.md).
- Fire footsteps from gait-phase crossings, not from timers (locomotion.md).
- On Humanoid characters, a Timeline Override track with an Avatar Mask layers upper-body actions over locomotion.
  On Generic rigs use the rig's own actions.

## 7. Animation Rigging and polishing tools

- Unity Animation Rigging: Two Bone IK (fist or palm on the floor), Multi-Aim (head and chest track a target),
  Damped Transform (lag for tails and cloth).
- Constraints run in the animation jobs, before `LateUpdate` scripts. Spine or neck corrections written in
  `LateUpdate` carry the solved arms with them: a balance arm can end up over the head. When you correct the spine
  in script, solve the arms analytically after it, in the same `LateUpdate` (procedural.md).
- Timeline Override track with an Avatar Mask for Humanoid layering; Blender's NLA (Replace/Add layers, strength,
  holds, strip scale) for retiming before export.
- Once a motion is approved, bake the final clips into one Timeline. A state machine adds blends, drifts with
  speeds and can't be scrubbed frame by frame.

## 8. Face player mechanics

What the curves should contain is in facial-animation.md. In Unity:
- Run in `LateUpdate` after the Animator and the body rigs (order 200 above).
- **Clock:** `AudioSource.time` while the voice plays, so the face stays in sync through hitches; the director's
  time in Timeline. Expose `Evaluate(t)` that also works in edit mode, for scrubbing and offline renders.
- Unity blendshape weights run 0–100: `SetBlendShapeWeight(index, value * 100f)`. Cache the indices per mesh by name
  (`sharedMesh.GetBlendShapeIndex`) once, and drive every mesh that has a key of that name: head, lashes, teeth,
  tongue. Jaw keys then move the lower teeth and tongue in sync.
- Convert names once: `_L`/`_R` → `Left`/`Right` (ARKit spelling), at export or at load.
- On a strict ARKit rig, drive `min(mouthClose, jawOpen)` when the data uses mouthClose as "lips sealed"
  (facial-animation.md).
- **Additive head motion:** split it 35% neck / 65% head. Undo your own previous offset when nothing re-animated
  the bones this frame; otherwise the offset accumulates on bones no clip drives. Apply gaze relative to the
  head-in-root rest captured at the bind pose, not to the current animated head. Set the neck share to 0 when a
  helmet locks the neck.
- **Idle blink** when no clip plays: every 2.2–4.6 s, 0.16 s to close, 0.22 s to open, seeded per character so
  renders repeat.
- **Clip format:** load face-curves/1 JSON (`facial-animation.md`) directly, or convert it once to a compact
  TextAsset, for example `names[]`, `fps`, `leadIn`, `frames` and one curve-major `values[]` array
  (`values[c * frames + i]`), which loads fast and diffs well.

## 9. Offline renders from edit mode

Edit-mode loops that call `PlayableDirector.Evaluate()` and `Camera.Render()` per frame give exact, repeatable
stills and sequences, but several systems don't update there on their own.

- **Skinning and blendshapes lag** when you render many times per editor tick: a jawOpen of 90 rendered closed.
  Set `SkinnedMeshRenderer.forceMatrixRecalculationPerRender = true` on every skinned renderer.
- **`LateUpdate` never runs** in an edit-mode loop after `director.Evaluate()`, so poses and sockets go stale.
  Call `Update` and `LateUpdate` of every enabled `[ExecuteAlways]` behaviour yourself, by reflection, in execution
  order, after each Evaluate.
- **Replicate play-only controllers.** A depth-of-field controller that ran only in play mode left stills on a
  static focus of 8 m, and ~1 m close-ups came out soft. Copy its logic into the renderer: snap focus on cuts,
  smooth within a shot (SmoothDamp, 0.25 s). Search for `Application.isPlaying` guards in anything that affects
  the image.
- An "instantiated copy" of a post-processing profile shares its sub-assets with the original. Grade the profile in
  place and restore it afterwards.
- `AnimationMode.SampleAnimationClip` ignores a clip's Mirror import flag, while the Animator applies it at
  runtime. Mirror edit-mode samples of a mirrored clip by hand: swap left and right, negate x.
- **Cross-check edit mode against play mode** with the same measuring code at matching times. Values should agree
  to the millimetre; only in-between frames differ.
- **The preview renderer is not the target renderer.** Back-face culling, reflection probes, normal-map strength and
  eye materials all differ between Blender's Eevee and URP/HDRP. Confirm new meshes and texture fixes in the engine.
- Supersample finals 2× (render 3840×2160 for a 1080p deliverable). Write review previews as JPEG; PNG sequences
  fill disks fast.

## 10. Editor automation (scripts, agents, command-line evals)

- If you drive the editor through a code-eval bridge (a CLI or an MCP server), a call that hits its timeout (often a
  few seconds) may keep running in the editor. Write progress to a
  status file from the code and poll the file; never assume a timed-out call did nothing.
- A backgrounded editor may throttle or stop play-mode ticks (check Interaction Mode and Run In Background). If it
  does, enter play mode paused and call
  `EditorApplication.Step()` in a loop (about 0.02 s of game time per step), or render from edit mode (§9).
  Stepped play mode has its own traps:
  - Injected input events move nothing. Drive input through the components' own APIs (set the move vector, call
    the fire method).
  - FPS reads a constant 50 and GPU counters show "—". Don't take performance numbers from a stepped editor.
  - The frame after a screen capture can see a stale canvas size; skip it before measuring UI.
- Compile edits offline before handing them to a live editor: run Unity's bundled Roslyn compiler against the
  project's generated response files (`.rsp`). A compile error in a shared editor blocks every agent until fixed.
- **Several agents sharing one editor** (a convention that worked):
  - Take a lock before any editor step: `mkdir` of a lock directory (atomic) plus an owner file. Check the owner
    before working; a wait loop must never fall through into the work.
  - Hold it 10 minutes at most; treat a lock older than 25 minutes as stale.
  - Before releasing: exit play mode, wait for compilation to finish, leave no modal dialogs.
  - Open the scene you need yourself; never assume the previous owner left it open.
  - Keep helper scripts in per-owner folders, and give each agent ownership of distinct files.
- Build scenes and prefabs from code (one builder entry point per scene), so any agent can rebuild them and a
  rebuild is the undo.
- Don't drive the editor while the user is working in it.
