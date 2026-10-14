# Procedural animation: layers, IK, look-at, contacts, props, creatures, first-person

Read this when code poses a character on top of (or instead of) clips: rigs driven by a timeline or gameplay,
IK, look-at, hand contacts between characters, held props and weapons, creatures with no clip library, and
first-person viewmodels. Numbers are measured unless marked **(estimate)**.

## 1. Architecture: make the pose a pure function of time

- **Pose = f(director time).** Carry no state between frames: compute every layer from the timeline time or
  the action's clip-local time, take the gait phase from path distance at t (`locomotion.md`), and take
  "randomness" from hashed time buckets. A rig that accumulates phase from `deltaTime` freezes in edit mode
  (dt = 0), drifts when scrubbing, and differs between offline stills and play mode. Measured: a
  deterministic rig matched play mode (50 fps) against edit mode (24 fps) to the millimetre at matching times.
- **Deterministic jitter.** Bucket `k = floor(t / period + salt)`; about 55% of buckets hold the rest pose,
  the others snap to a hashed offset within `snap` seconds and hold. Periods 0.57–1.31 s, snaps 0.08–0.18 s.
  Hash: `frac(sin(n·91.7)·43758.5453)`. Use incommensurate periods per bone so nothing visibly repeats.
- **Offsets in a character frame.** Author every pose as Euler angles in the character's frame (X right,
  Y up, Z forward) around the captured rest pose:
  `local = restLocal · (restRel⁻¹ · Euler(offset) · restRel)`, where `restRel` is the bone's rotation in the
  character frame captured at bind time. "+X leans forward" then means the same thing on every bone,
  whatever the import axes or bone rolls.
- **Fixed layer order** (one function, in this order):
  1. base clips through a manual mixer (normalise the weights, evaluate once);
  2. full-body poses lerped by weight in a fixed priority, independent of track order;
  3. additive layers (breathing, look-around, tremble, recoil, flinch);
  4. leg IK and grounding;
  5. look-at (spine, neck, head), then eyes;
  6. hand contacts and prop IK;
  7. helper bones re-applied last (half-rotation joints, see `rigging.md`).
- **Calm factor.** Mute idle layers during violence: `calm = 1 − lunge − coil − 0.6·seize` (or the
  equivalent for your actions) scales breathing and idle twitches.
- **Execution order and ownership.** Action pushers run before rigs, rigs before prop sockets, the face
  player last (`engine-unity.md` gives a working order). Each pusher clears only its own actions. An action
  pushed with no owner stays forever: a stale look-at did, until a gameplay script cleared everything at
  hand-over.
- **Off-timeline fallback** (speed from transform velocity, teleport guard, hand-over after the timeline):
  `locomotion.md` §5.
- **Don't serialise procedural tuning.** Install runtime rigs from code with defaults in code, so scenes
  saved earlier cannot freeze stale values.

## 2. Poses

- **Arm angles in a pose are relative to the bent spine.** A hanging arm follows the torso. In a pose with a
  75° forward torso, an arm at −84° relative ends up about 9° ahead of vertical. Compute the world angle you
  want minus the spine bend, and check the sign on a still: the first fix of one fall pose pointed the arms
  up instead of down.
- **Ground full-body poses on their lowest contact.** Candidates with radii: toe 2.5 cm, ankle 8.5, knee 7,
  pelvis 12, chest 16; the lowest goes to the floor, weighted by the pose weight. Flatten the feet at
  0.75 × pose weight, except poses with extended feet (kneeling, seated).
- **Never pin an airborne moment.** A pinned stumble hung the character over the floor. Airborne poses are not
  grounded; offset the hips instead (e.g. down 0.22 m, forward 0.22 m).
- **The idle's arms may not match your pose table.** An idle with hands on hips breaks poses that assume
  hanging arms. Use a hanging-arm reference (upper arm about 8° out from vertical, forearm 12° forward),
  a relaxed-arms weight of 0.9 and ±0.8° breathing sway.
- **Chain poses for a fall:** stumble (airborne, arms thrown forward) → hands and knees (pinned) → crouch →
  stand.
- **Seated hurt pose:**
  - Sink into it over 2.2 s (smoothstep). Hands on the knee and thigh through targets parented 78% and 50%
    along the thigh bone.
  - A 1.3 s pained shift every 5–9 s, glancing at the leg while its weight is over 0.35.
  - Back up to a wall when one is within 1.4 m: 16 rays at chest height, wall normal |y| < 0.6, stand-off
    0.38 m.
  - Ground it by its lowest contact including the seat. The first version floated with the pelvis at
    0.66 m; grounded it sits at 0.34 m.
- **Re-check every pose with a hip offset after swapping placeholder bodies for real ones.** Offsets authored
  on thin mannequins push bulky bodies into each other (one shove offset went from 0.28 m to 0.12 m).
- **Talking body.** Blend a talking idle only while standing and speaking: weight 0.85 × (1 − 0.5 × the
  upper-body pose weight), window −0.3/+0.25 s around the line, fade in 0.45 s, out 0.6 s.

## 3. Look-at and head control

- **Share the turn down the chain.** Spine 0.3 of the yaw and 0.15 of the pitch, head 0.7 and 0.8; clamp yaw
  ±95° and pitch ±55°. Eyes on top: yaw ±28°, pitch −28/+22°.
- **Tie look windows to lines and beats,** and reset the eyes to rest when no line plays, so a gaze never
  goes stale.
- **Damp a look-at that follows a moving target** (half-life about 0.25 s). Frame-by-frame following made a
  watcher's head shake with every bob of a runner.
- **Measure the face direction, not a head-top bone.** In some rigs the head's end bone leans back from the
  face; "level" by that bone still looked 35–45° up. Take the face direction in head space from the bind
  pose and cap the face's pitch.
- **Cap a look-up** at about 25° above horizontal, shared 40/60 between neck and head; the eyes carry the
  rest. Library clips facing a giant throw the head back 50–80°, and users see it at once.
- **Hard limits on whippy clips make the head jerk.** Hit and stagger clips whip chest and head at
  300–700°/s; a clamp switching on or off kinks the speed. After the limits, run each spine, neck and head
  rotation through a spring (natural frequency about 16 1/s for a large character) on character time so it
  freezes in hit-stops. Spring the spine before arm IK so hand contacts still land; blend the spring in over
  about 0.1 s after a hit so the hit frame keeps its snap.
- **Roll is limited separately from tilt.** A knock-back may lean the body back, but a sideways fold of the
  spine or a rolled head reads as broken. Cap roll against the body's right axis: spine about 9°, head about
  8° **(estimate)**. A head lying on its side after a reaction needs a swing-twist clamp relative to the
  chest (swing about 30°, twist about 60°) plus a righting reflex keeping the head's up within about 35° of
  world up.

## 4. IK essentials

- **Analytic two-bone IK after your spine edits.** Engine IK passes usually solve before late spine and neck
  corrections, which then carry the solved arm away (a balance arm ended up over the head). Solve in your
  own late pass, after spine edits, with an elbow hint; turn each bone by the smallest rotation so the clip's
  twist is kept.
- **Saturate reach softly** at 0.95–0.985 of the chain length. A target beyond reach locks the limb straight,
  and when it comes back the limb bends again ("the arm bends, then returns"). For authored arcs cap targets
  at about 0.9 of the arm length (elbow about 130°): a straight arm reads as a mannequin.
- **Blend IK by moving the goal,** not by blending bone rotations: move the goal (and elbow hint) from the
  current pose to the target, then solve fully. Partial rotation blends give a bend that is neither pose.
- **Start a solve from the clip's elbow plane** (capture the elbow at hand-over) and turn the hint to the
  designed one over the move; a designed hint on the first frame swings the elbow round the arm.
- **Place elbow hints from each shoulder, in the body frame,** not over the hands. Hints above two hands near
  the midline fold both elbows inward and tuck the forearms under the chest.
- **Palm facing.** Two-bone IK keeps the clip's twist, so lowered hands may hang palm-out. After every arm
  pass, measure the palm's facing and turn it about the forearm's own line toward a natural target, splitting
  the turn about half forearm, half wrist (one joint alone wrings the skin).
- **Never unwrap a twist correction frame to frame against body or world axes.** It winds up a full turn
  (−300° instead of +60°) while the palm still faces correctly, and the wrist reads as broken. Measure the
  forearm twist from the elbow's hinge (the palm's angle about the forearm from the direction across the
  bend); of the two corrections that face the palm, take the one that keeps the twist in about
  −75…+100° from mid-prone, and clamp at the ends.
- **Check each elbow's bend plane against its hinge.** The angle about the upper arm's axis between the plane
  the arm bends in and the bind pose's flexion axis: mocap stays within ~15°; above ~70° reads as broken.
  Plan contacts with no anatomical solution (a hand behind a hanging upper arm, arm bent) as a nearly straight
  arm swung from the shoulder.
- **Write the mesh's deformation limits into the IK.** On costumes that break above the shoulder: wrist at
  least 2 cm below the shoulder, forearm-grip targets at least 8 cm below it, and no reach pose above shoulder
  height (`rigging.md` has the bake-side cap).
- **Hit-stops freeze just short of the contact.** Anything keyed to "time ≥ contact" fires on the first frame
  after the freeze and snaps (a fist jumped 0.4 m in one frame). Capture the pose shown during the freeze and
  blend from it.
- **Swung limbs in polar terms around the shoulder:** slerp the direction from where the clip left the hand to
  the target and change the distance one way only, so the elbow opens or closes once.

## 5. Hand contacts between characters

Staging whole bodies without hand targets means no contact: measured misses were 50–120 cm and 0% of frames
passed. Build contacts like this:

1. **Block the bodies at reach distance first** (a shover runs in to 0.8 m; a helper stands 0.8 m off the
   fallen character's side). IK cannot fix bad blocking.
2. **Surface anchors, not "bone + guessed radius".** At build time, in the bind pose, cast a ray from outside
   along a character-space direction toward a point a fraction along the bone; intersect the skinned body
   (via bindposes) and rigid parts riding that bone, including transparent parts the hand must not enter
   (visors, glass), but not fake-volume meshes such as light cones. Create a child of the bone at the hit with forward = outward normal. Examples:
   shoulder top (∓0.85, 0.52, 0) at 20% along the upper arm; chest front-right (0.55, −0.1, 0.83); outside
   of the upper arm (0.93, 0, 0.35) at 40%. Guessed radii left a gap on one side of a torso and buried the
   hand 10 cm deep on the other.
3. **Palm-flat arm IK.** Palm = contact + 4 cm along the normal (palm plus curled fingers). Wrist = palm −
   tangent × 0.85 × palm length (8.5 cm) + 3 cm along the normal, with the tangent = shoulder→contact
   projected onto the surface. Aim the hand's finger axis (measured hand → middle finger in the bind pose)
   along `tangent − 0.12·normal`. Elbow pole: down 0.7, out 0.6, back 0.2.
4. **Claws hook over, never stab.** Knuckles 8.5 cm outside the anchor; arrive at a shallow angle
   (`0.8·tangent − 0.3·normal`); drape the claws along the surface toward world down; remove any curl into
   the body. Facing the victim, the attacker's left hand takes the victim's right shoulder.
5. **Pose the touched body first, for the same time.** If the other rig hasn't posed at this t, pose it first
   (re-entrancy guard). When one rig moves another's root (a held victim), re-pose the victim afterwards so
   the result doesn't depend on update order.
6. **The held body follows the grip.** Target root = holder's chest + frame · grip offset (front
   (0, −0.2, 0.62), from behind (0, −0.48, 0.5), drag under the hand); yaw from the holder's facing, own tilt
   kept; blend by the hold weight eased in over 0.04–0.08 s; a path lift of 0.30–0.34 m puts the victim on
   their toes.
7. **Contact goes both ways.** The victim's hands on the attacker's forearm: the point on the forearm axis
   nearest their shoulder, kept 25–80% along, slid down until at least 8 cm below their shoulder.
8. **Drag by a rigid part (a neck ring, a harness, a belt), never by a transparent visor,** and use a limp pose that is not floor-pinned: pinned poses fight a
   root the grip moves.
9. **Keep contact weights tight around the contact moment:** reach 0.1–0.2 s before, hold from the contact
   frame with a 0.06 s ease-in, hit windows about 0.16 s, release over 0.04–0.08 s.
10. **A hit on a first-person camera:** model the camera as a head sphere (about 0.19 m); aim the swipe
    0.3 m in front of the lens.
11. **Measure** with the contact checker (`qa.md`): gap ≤ 2.5 cm and depth ≤ 3 cm over the held plateau.
    Measured after these fixes: held contacts passed 67–92% (pulling someone up from the floor: median gap
    0.7 cm); a fast strike still sat 3.5–4 cm deep, traced to the wrist path crossing the costume's rigid neck
    ring.

## 6. Props, sockets and weapons

- **Calibrate sockets on the posed skeleton, per action:** pose the rig with one action at full weight, create
  the socket on the real bone keeping its world pose. A socket component blends between socket A and B by
  action weight (smoothstep); a release action detaches the prop for a drop.
- **Put the grip in the palm, not above it.** Palm point = lerp(hand, middle knuckle, 0.55); socket 1.2 cm
  below it. A +2.5 cm lift left a grip floating over the glove. Author each prop with its pivot at "palm on the
  grip axis".
- **A handheld screen read pose:** hand further out and lower, head tilted 6–8°, head and eyes on the screen
  socket; build the socket from the palm-to-eyes vector so the screen faces the eyes; tilt the device about
  35° back from vertical so it reads as held.
- **Back carry reads by silhouette.** Muzzle-up reads as a dark shape over the shoulders. Lay the long gun
  flat on the pack, thin side against it, diagonal, muzzle down toward the hip; measure the pack surface
  depth before placing it.
- **Two-hand aim socket:** barrel axis from the rear palm to the front palm in the aim pose; up = world up
  projected off that axis.
- **Hand-offs (drawing, picking up):** IK the hand onto the grip before the grab frame, re-parent keeping the
  world pose, then blend into a grip derived from the hand's bones over about 0.25 s. Turn a blade to the
  guard during the clip's follow-through, not after it ends.
- **Carrying a drawn weapon into locomotion:** a standing-guard arm IK does not survive a run underneath it
  (a 100°+ wrist shortfall was measured). Give locomotion its own carry pose keyed in the chest frame: fist low
  by the hip and a little forward, blade forward and slightly up, elbow back and down, keeping about 30% of
  the clip's arm swing; blend in over about 0.3 s. A blade raised beside the shoulder reads as ceremonial.
- **Drawing a long blade from the back with a clip made for short weapons:** keep only the clip's body;
  author the reach as a path in the chest frame (out beside the shoulder at ear height, over the hilt from
  outside, down onto it), pull up and out away from the head, then keys for the wheel and the guard
  (Catmull-Rom positions, slerped rotations). Split the fist's turn by swing-twist about the forearm (twist
  ≤ about 75° to the forearm, swing ≤ about 60° to the wrist); never turn the weapon inside the hand. Aim the
  reach at the fist, a hand's length past the wrist. Hang the sheathed grip behind the back's surface, above the
  shoulder line, a hand's width in from the shoulder; a grip at shoulder-joint depth forces any reach through
  the neck.
- **Grip from the hand's own bones:** knuckle line index → little knuckle (ring finger as fallback), palm side
  from the thumb base, handle across the base of the fingers against the palm's flesh. Wrap fingers like a
  strap round a post: lay each phalanx along the tangent from its joint to the handle surface (radius measured
  from the mesh), clamped to joint ranges. "Bend until it touches" stops at the first touch and leaves fingers
  straight across a thick handle.
- **Weapon blade versus body:** a keep-out solver turning the blade about the grip a few degrees at a time out
  of the deepest body capsule, warm-started from last frame; switch it off while the blade still lies along the
  back.

## 7. Additive layers and springs

- **Springs for reactions instead of clips.** Damping ratios of about 0.6–0.75 (slightly underdamped) read
  alive; a true critical damping reads mechanical **(estimate from what shipped)**.
- **Directional hit spring:** k = 420, c = 30. Impulse in deg/s: pitch −z·520, yaw x·420, roll −x·480 (x, z =
  hit direction in body space), × clamp(damage/25, 0.2…1.8); spread chest 0.6, spine 0.4, neck 0.5, hips
  0.15. Knockback of the visual 1.5 m/s × strength + 0.25 up (spring 150/16, clamped to 0.4 m), stacking over
  a burst. Arm flinch 22 × strength (spring 220/20). Head pop on headshots (−900, ±350, ±450) deg/s, spring
  320/22.
- **Burst flinch on a cinematic character:** recoil `e^(−3.5t)` plus a per-round stutter `|sin 30t|·e^(−6t)`;
  head −18° − 10° × stutter; arms spread ±20°.
- **Small layers:** look-around yaw ±48° at 1.15 rad/s; fear tremble head roll 0.8° at 31 rad/s, arms 1.5° at
  27 and 29 rad/s; recoil kicks decaying at 9 Hz. Measure whether a library "scared" take actually contains a
  tremor (5–12 Hz energy in hands and head) before relying on it; one did not.
- **Breathing with a hitch** for non-humans: `sin(1.7t) + 0.35·sin(3.9t + 1)` on spine (1.6°), chest (−2.2°),
  clavicles (±2°) and hips (1.2 cm).
- **Human-pace shake on a giant reads as a tremor.** Play the clip near half speed, low-pass body rotations
  and hips (time constant about 0.15 s), add a slow whole-body rock (±2°, period about 1.5 s).

## 8. Creatures without a clip library

- **Joints from cross-sections.** Slice the mesh every 4 cm (±0.6 cm band), cluster each slice (eps 2.5 cm,
  ≥7 vertices), print centroids and extents; limb and tail centres come from them. Bake offset and scale into
  the mesh (identity object transform) before rigging.
- **Bone budget for a tall biped with a tail: about 34.** Root, hips, 2 spine, chest, 2 neck (snake-like head
  jabs), head; per side clavicle, upper arm, forearm, hand, separate claw bone (drapes independently), thigh,
  shin, foot, toe (hangs in swing); 9 tail bones (smooth travelling wave). 23 bones work at gameplay distance
  but bend coarsely.
- **Consistent roll:** every bone's local X to the creature's right; feet, toes and tail rolled to world up.
- **Weights:** auto weights, then prune distant bones (`rigging.md` §3.6).
- **Facing from anatomy, not exporter axes:** turn the model so the toes' midpoint is ahead of the tail tip,
  hips over the root.
- **Tune the stance in the DCC with a ground measurement** (rotate pose bones about world axes through the
  inverse rest matrix; evaluate the deformed mesh; print the lowest vertex and lowest hand and foot), then port
  the same model-space numbers to the engine.
- **Scale one hero rig down for gameplay** by one dimension (head height) on a visual parent; leave agent and
  capsule numbers as they were, then re-check hip height against the hit volume. Retarget a coarse procedural
  driver onto a denser chain by splitting each angle 50/50 between the slot bone and its follower.
- **Motion language from what the mesh has** (see `performance.md` §5 for timing): threat display from
  posture when there is no jaw; stalk silhouette low with the tail lifted; sensing as snap-and-hold head turns;
  hiss as decaying neck jabs `j = max(0, sin 9t)·e^(−1.2t)`.
- **Tail wave:** with k = (i + 1)/n per segment, yaw = `sin(0.9t − 0.55i)·(3 + 6k) + sin(0.37t + 0.3i)·4k`,
  pitch = `sin(0.7t − 0.5i + 1.3)·(2 + 3k)`, agitation lash `sin(9t − 0.9i)·9k·a + sin(23t − 1.4i)·5k²·a`
  (degrees).
- **Legless or floating creatures:** `rigging.md` §1.5. **Flight** (bats are not birds): `locomotion.md` §18.

## 9. First-person viewmodel

- **Hands from the character's own mesh:** keep vertices with >0.5 weight on the arm chain, bake in the bind
  pose with curled finger joints (pistol grip 62/70/45°, thumb 28°; support hand 48/55/38°, thumb 18°), express
  in a palm frame, render as a static mesh without shadows.
- **No second viewmodel camera.** Draw the viewmodel at about 0.56 scale at the hip, lerping to 1.0 when aiming
  down sights; multiply sprint, reload and wall offsets and the recoil translation by the current scale. Scaling
  about the grip keeps the grip fixed on screen and keeps the muzzle out of walls (only scaling about the eye
  is optically identical to holding a full-size gun further out).
- **Hip pose targets:** muzzle about 8° right and 6° below the crosshair; the firing hand on the grip, in frame;
  the support hand on the handguard from the side so glove and sleeve show bottom-left. Cutting or folding a
  stock to clear the view read as a blank block; a sling swung across the view in reloads.
- **Reflex sight:** an open frame (not a solid scope block) and a lens shader that draws the reticle from the
  view direction in the lens's own space (`tan(angle) = xy/z`), so it has no parallax and moves with recoil
  like a real optic. Aiming: FOV 58 → 45; sway ×0.22, bob ×0.18, breathing ×0.3, spread ×0.35, kick ×0.6, move
  speed ×0.55; blend at 11/s.
- **Reload as hand keys in weapon space** with smoothstep between keys (normalised: hand on magazine 0.10,
  release 0.15, out 0.21 and dropped as a physics prop, hand off screen 0.36, new magazine under the well 0.47,
  seated 0.55, charging handle 0.68–0.80, rest 0.93). The new magazine appears only while the hand is off
  screen and follows the hand's offset into the well. Measured glove-to-part gap at contact keys: 0.1–0.5 cm.
- **Sprint and wall poses:** sprint only above about 0.72 normalised speed, not aiming or reloading, no shot in
  the last 0.45 s; wall weight from a 1.05 m head ray, `InverseLerp(1.05, 0.45, d) × (1 − 0.6 × aim)`, pose
  lowered and swung across the body. Pulling the gun straight back filled the view; pitching the muzzle down
  raised the stock into view.
- **Idle and movement layers:** look sway −Δpitch/−Δyaw × 1.6, roll −0.6 × Δyaw, clamp 7°; figure-eight bob
  x = cos × 1.1 cm, y = |sin| × 1.4 − 0.7 cm at 1.4 → 8.5 rad/s walking, 11 sprinting; landing dip spring
  140/14 when falling faster than 2.2 m/s.
- **Keep recoil out of the look sway:** compute sway from head rotation × inverse(recoil offset), or the gun
  lags its own recoil and amplifies it.
- **Recoil = a climbing camera spring + a two-spring weapon punch.** Camera spring 95/13, up 1.25° per round ×
  [1, 1.3, 1.55] across a burst ±10%, trauma shake on Perlin noise applied squared. Weapon: a stiff position
  spring 420/30 for the back kick, a softer rotation spring 190/17 for the rise, sub-stepped twice per frame,
  clamped to 14 cm and 22°. Shots fire along head × recoil offset so the climb really moves the aim.
- **Non-accumulating camera offset:** the controller rewrites the head rotation every Update; kicks multiply
  on top in a late pass, before the viewmodel and camera system read it.
- **Light the viewmodel on its own rendering layer.** A head lamp 0.3–0.4 m above the gun burned hands and gun
  white; a dim fill on the viewmodel layer that follows the lamp, plus the muzzle flash on both layers, fixed it.
- **Player hit reaction:** camera jolt along the force (pitch z·6 − 3, yaw x·7, roll −x·14, × strength),
  trauma 0.35 × strength, FOV −2.5° × strength. In linear colour space a 0.26 white UI flash reads as about 55%
  grey: cap full-screen flashes near 0.05 and take the max of simultaneous calls instead of stacking.
