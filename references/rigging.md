# Rigging generated and scanned characters

Read this before rigging or re-skinning a character: an AI-generated or photogrammetry mesh, a decimated scan, a bulky
suit or armour, a creature. It covers mesh preparation, skeleton fitting, skinning, hands, the limits of hard costumes,
rig checks and the export to an engine. Values without a marker were measured in production; **(estimate)** marks
starting points.

## 1. Prepare the mesh

### 1.1 Repair what the importer did
- **Weld seam splits first.** The glTF importer splits vertices along UV seams; skinning, smoothing and any
  graph walk then treat the two sides of a seam as separate vertices. Run remove-doubles at 1e-6 right after import.
  UVs live on loops, so nothing is lost. Log the vertex count before and after, per mesh.
- **Generated meshes are often triangle soup:** hundreds of separate shells, fused lips, painted eyes, sculpted
  half-curled fingers. Connectivity splits fail on them and a voxel remesh of a hand loses the fingers. Find parts
  by **position** (slices, bands along an axis), not by connectivity.

### 1.2 Measure orientation and proportions before fitting anything
- Write a height profile: 100 horizontal slices with width, depth and the centre-column depth, to JSON.
- Render orthographic front, side and back views. Confirm which side the face is on; never assume an axis.
- Take one scale factor from the low-poly height and apply it to every LOD. Bake the transform into the mesh data and
  ground the mesh at z = 0.
- Derive "forward" from the skeleton once it exists: `cross(hip lateral, up)`, or eyes minus head.

### 1.3 Segment by texture, not geometry alone
Separating a head from a suit at a tilted collar ring is unreliable on geometry. Label each vertex from its textures:
- metallic: the glTF metal-rough map's B channel > 0.5;
- or low-saturation grey: saturation < 0.16 and value > 0.38.

Fit the cut as a plane: fix its tilt (0.38 rad worked for one collar ring; the texture thresholds above were tuned on
one grey-metal suit, so tune both per model) and fit only the height, from the back half,
where a jaw cannot be mistaken for the ring. Require at least 12 of 36 angular sectors to find the ring, or abort.
Drop leftover islands smaller than 2% of the largest. Check with orthographic front, side, back and three-quarter
renders of the area, plus head-only and body-only renders.

### 1.4 Cut with a plane, then clean up
- **Bisect** the faces that straddle the plane. Deleting whole faces along a cut leaves spikes.
- Delete loose islands under 300 faces near the cut.
- Thin decimated flaps above the cut stick up once whatever covered them is gone: press vertices above the line down
  to 15% of their height over it.
- Press body vertices just outside a new ring 2.5 mm under it so old scan shards don't poke through.
- **Count triangles per dominant bone before and after every cut or re-skin.** A height test in the bind pose ("drop
  everything below the hips" for a legless creature over a smoke tail) also takes the hands of an A-pose rig. What
  shows is an open wrist ring, visible only when the wrist turns to camera, so it looks like a pose bug. Exempt
  vertices skinned ≥ 50% to the arm chain from any height test.

### 1.5 Legless or floating creatures
Rig them with legs so an auto-rigger builds a standard skeleton. Then cut the lower-body triangles in the DCC or
engine (below the hips along the hips-to-head axis, plus anything weighted mostly to the legs) and keep the bones.
Hide the cut with VFX: smoke, fire, a vortex.

## 2. Fit the clip library's own skeleton

### 2.1 Why
Import the skeleton of the clip set you will animate with (Mesh2Motion, Mixamo or another library) and move its joints
into the mesh, keeping its bone names and local-axis conventions. Retargeting then becomes a near-exact copy: the
bake check measured **0.00° bone-direction error on every clip** (see `clips-retarget.md` §7). Auto-rigging with a
different skeleton makes every clip a lossy retarget.

### 2.2 Joint placement from slices (H = character height)
Proven on bulky humanoid suits; adjust the ratios if the character is stylised.

| Joint | Placement |
|---|---|
| Ankle | centroid of the 7.5–11% H slab |
| Knee | 27–30% H slab, placed at 28.5% H |
| Hip | 44–47% H slab, moved inward ×0.92, placed at 51.5% H |
| Pelvis | 54.5% H |
| Spine joints | 58.5%, 65.5%, 73.5% H; 11.5 cm behind the 2nd-percentile chest front (ignores a backpack) |
| Arm axis | per slice, the outermost vertex cluster (split at gaps > 1.5 cm); fit an axis through the centroids by SVD |
| Fingertip | the lowest vertex near the arm axis |
| Shoulder | on the arm axis at 81.2% H, 3.5 cm inward so it sits inside a shoulder pad |
| Hand length | 10.5% H |
| Elbow | at 55.5% of the shoulder-to-wrist line, bent 1.2 cm backward to give IK a bend direction |

### 2.3 Carry the roll
For each moved bone, rotate its old frame by the minimal rotation from the old direction to the new one:
`align_roll(rot_min @ old_z)`; tails go to the child heads. Each bone's frame stays "minimal rotation × source
frame". The leftover is a small fixed twist per bone (2–4° on fingers), which the bake stores as a constant offset.

### 2.4 Bones with no connected child keep the source direction
Every retargeted wrist bent inward on one rig: the hand bone had no child at its tail, so the A-pose fit left it in
the T-pose direction, about 72° off the modelled glove. Aim the hand from the wrist to the middle knuckle, carry its
roll the same way and move any helper bone with it. **List every bone that has no child at its tail** and aim each
one explicitly (hands, head, feet, toes, tail tips).

## 3. Skin bulky meshes

### 3.1 Automatic weights, with a fallback
Bone-heat weights do work on decimated scans (0 unweighted vertices on three suits). Keep a fallback anyway: if more
than 2% of vertices come out unweighted, weight a voxel-remeshed copy (voxel 1.2 cm × H/1.8) and copy the weights
back with a Data Transfer modifier (nearest-face interpolation).

### 3.2 Widen the shoulder blend
Raised arms folded shoulder pads into fins and dragged the armpits: bone heat switches from torso to arm within a
few cm. Laplacian-smooth the weights along mesh edges inside a sphere around the shoulder:
- groups: upper spine (2 bones), clavicle, upper arm and its helper;
- radius 0.16 m, 12 iterations, factor 0.5;
- mask 1 inside 2/3 of the radius, smoothstep to 0 at the edge;
- keep each vertex's total weight across those groups.

### 3.3 Half-rotation helper bones
A cheap stand-in for dual-quaternion skinning or corrective shapes, which fixed creased knees and elbows and
collapsed shoulders under linear skinning:
- add `<child>_mid` at the shoulder, elbow, wrist, hip, knee and ankle: at the child's head, with the child's rest
  frame, parented to the child's parent;
- move the mixed part of the weights, `m = 2·min(w_parent, w_child)`, onto it;
- drive it to `slerp(identity, child local rotation, 0.5)`;
- **key it in every baked clip**, and at runtime re-apply it after every procedural override and after IK.

### 3.4 Rigid parts belong to one bone
A collar ring tore when the head turned because bone heat gave it neck and head weights. Put rigid costume parts
100% on the bone they ride: here all neck/head weight on vertices above the ring plane minus 12 cm went to the upper
chest bone. Same for armour plates, helmets fused to the suit, belts.

### 3.5 Head weighting
With exact clips, faces looked stretched and lips slid over the teeth on head tilts: a height gradient had left the
chin 7–34% and the lips 48–76% on the neck bone, while the teeth were 100% head.
- Face, jaw and skull 100% on `head`. The neck blend lives only below the chin.
- Head weight = smoothstep up the neck column, from 1 cm above the collar plane to 4 mm below the chin, OR-ed with a
  term for radius outside 1.08 × the neck radius on the upper neck (jaw and under-chin).
- Below the collar, blend to the upper spine.
- Check: the minimum head weight at the mouth is 1.00.

### 3.6 Prune weight bleed on creatures
Bone heat let a tail pull the legs and claws pull the thighs. Drop influences from bones farther than
1.8 × the nearest bone's distance + 6 cm; keep the top 4 influences; renormalise.

### 3.7 Mount accessories, don't skin them
Helmet on the upper-chest bone, wrist display on the forearm, glasses on the head. Place each in the bind pose, then
re-parent keeping its world pose. Store seat transforms in character-root space at the bind pose (+Z = face
direction) and place them onto the character's actual forward, not an assumed axis.

## 4. Hands

### 4.1 Solid scanned gloves: freeze the fingers
Any finger motion "melts" scanned gloves: they are solid half-curled shapes with no real fingers inside. Default to
freezing the fingers; the glove keeps its scanned pose and moves with the hand. Copying the source finger rotations
raw deforms the glove, and offsets relative to the source's relaxed idle hand did not read better in tests.

### 4.2 Grips without finger animation
Define the palm point as `lerp(hand, middle_01, 0.55)` and put the prop socket 1.2 cm below it; the glove's scanned
curl does the gripping. A +2.5 cm lift left a grip floating above the glove. Calibration of sockets per action is in
`procedural.md`.

### 4.3 Fists on a rig with no finger bones
Generated characters often have one bone per hand with claws or curled fingers sculpted in. A blow with that hand
lands as a slap; a blow needs a fist, and on close shots a visible closing of the hand.
- **Procedural fists silently do nothing** on such a rig. Check the bone map before writing finger code; log the
  mapped bones once.
- **Make fist shape keys in the DCC, not new bones.** The avatar, weights and clips stay as they are.
- **Palm normal from the bind-pose mesh:** the vertices skinned mostly to the hand, their spread across the hand's
  length, the minor axis (the hand's thin direction), signed toward where the fingertips curl. Compute it offline
  and serialise it; skinned meshes are often unreadable at runtime.
- **Find the fingers by position.** In an A-pose the fingers hang side by side across the hand's width under the
  knuckle line: band them along that axis; the thumb is the cluster on the palm side, at the front. Plot the hand's
  vertices from two sides before choosing bands.
- **Curl each finger on its own centreline**, built from slices along it. Joints at about 0, 40% and 70% of its
  length; rotate vertices toward the palm, far joint first, with ~1 cm smooth blends across each joint. Start at
  65/75/45° **(estimate)**; fold the thumb across once the fingers are halfway in.
- **A curl seen moving needs in-between shapes.** One shape blended linearly moves each fingertip along the chord of
  its ~180° arc, through the palm. Bake 25/50/75% stages as separate shapes and step between neighbours in code; FBX
  export does not carry in-between frames.
- **Later mesh edits must carry the shapes.** Code that adds vertices (a cut, a mouth slit) extends every existing
  shape's deltas onto the new vertices, interpolated the way the vertices were made. A guard like "mesh already has
  blendshapes, so it was processed" silently skips the step.
- **A fist is shorter than open fingers:** re-measure the hand's lowest point at a floor contact on the baked mesh.

## 5. Limits of bulky costumes

### 5.1 Arms above the shoulder break hard suits
Shoulder pads, chest plates and scanned suits cannot take overhead arms. Enforce the limit everywhere:
- **In the bake:** cap the shoulder swing at 75° from the A-pose. Beyond that, lower the whole arm rigidly so the
  forearm and hand keep their pose. The clavicle takes `0.4 × (swing − 40°)`, at most 20°.
- **At runtime:** every arm IK clamps the wrist at least 2 cm below the shoulder.
- **In pose tables:** reduce the mannequin values (in one case −150 → −84, −90 → −84, −66 → −34).
- The bake check will then report large bone errors on capped clips (41–42° in that case). That is intended; note it.

### 5.2 Weights alone do not fix overhead poses
Helpers, shoulder smoothing and the clavicle share together still turned the shoulders and armpits inside out on an
arms-overhead push. The fix that worked was to drop the clip and make the action a procedural pose at shoulder
height. Avoid overhead
clips on hard costumes; otherwise the fix is new topology or corrective shapes, which is manual mesh work. Ask the
user whether that is possible before promising it.

### 5.3 Offsets authored on thin mannequins
Placeholder mannequins are thinner than real costumes. A shove pose's forward hip offset had to drop from 0.28 to
0.12 m after the real bodies went in (one case), and a real head sat 0.12 m lower than the placeholder, so close-up camera and
aim heights changed by the same amount. After any body swap, re-check every pose with a hip offset, every contact and
every camera aimed at a head.

## 6. Check the rig before any clip work

- **Bone overlay:** X-ray bone sticks over the mesh, orthographic front and side.
- **Stress poses**, rendered front and side:

| Pose | Values |
|---|---|
| Arms | upper arm −60°, forearm 70° |
| Squat | thigh −70°, calf 110°, foot −40° |
| Twist | spine 25° + 20°, neck 20° |
| Fist | each finger joint −70° |
| Head | yaw ±35°, pitch ±20° (collars, gaiters, helmets) |

- **Ablation for seam bugs:** render the same frame with the head, the body and any hair shell hidden in turn, and
  sweep the camera yaw through about 12°, 50°, 110°, 150° and 180°.
- **Check in the target renderer, not only the DCC preview.** A procedurally swept collar came out inside out: its
  orientation check read a bmesh face normal before `normal_update()`, when it is still zero. The engine's back-face
  culling hid the faces; the DCC preview draws back faces, so it looked fine there. Call `normal_update()` before
  reading normals, and make thin costume parts two-sided where a gap could show background.
- **"Is the rig broken?"** Put the same clip on the character through the library's own path and next to your
  retarget. If they match, the rig and retarget are fine; the problem is the clip or the skinning.

## 7. Export from Blender

### 7.1 Headless Blender
- Start from an empty home file: `bpy.ops.wm.read_homefile(use_empty=True)`, or open the .blend.
- **Never** use `--factory-startup` or `read_factory_settings()`: factory settings can remove installed extensions
  and their Python wheels.
- Bake on the CPU in background mode when GPU kernels are unstable.

### 7.2 Pose state
- Save the source .blend in REST. Set POSE before baking actions; REST for the mesh export.
- Remove NLA tracks, give actions a fake user, and on Blender 4.4+ assign the action slot (`action_slot`) when
  assigning actions by script.

### 7.3 FBX settings that round-trip cleanly

| Setting | Armature / clips | Character mesh |
|---|---|---|
| Axes | forward −Z, up Y | same |
| Scale | apply unit scale, `FBX_SCALE_ALL`; no `bake_space_transform` | same |
| Bones | primary axis Y, secondary X; armature node type NULL; no leaf bones | same |
| Deform only | off (`use_armature_deform_only=False`): keeps helpers and sockets | off |
| Animation | `bake_anim_use_all_actions`, NLA off (one take per action); force start/end keys; step 1.0; simplify 0 | none |
| Mesh | — | rest pose; no modifiers applied (keeps shape keys); face smoothing; no tangent-space export |

Engine-side import settings are in `engine-unity.md`.

## 8. Pipeline hygiene for rig steps

- **Back up before every destructive step** (`.pre_hands`, `.pre_helpers`, `.pre_shoulder`, …) so any step can be
  undone and A/B'd.
- **Re-run guards:** each step detects that it already ran ("helpers already present") and either skips or restarts
  from the backup. Never stack the same fix twice (two seam fixes built two coincident neck patches).
- **Dry runs:** a `--test` / `--blend` mode that writes outside the engine's asset folder, so the engine never imports
  a half-built character.
- **md5 cache:** skip rewriting unchanged 4K textures, so the engine does not re-import and re-compress them.
- **Log numbers on every run:** vertex counts before and after welds and cuts, unweighted vertex count, triangles per
  dominant bone, bones without a child. The log is the review.
- **Archive rejected versions completely** (renders, sources, engine export, a README with the user's verdict), so a
  later approach can be compared against them and rolled back to.
