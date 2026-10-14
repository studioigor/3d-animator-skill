# Locomotion: walks, runs, creature gaits, quadrupeds, mounts

Read this when a character or creature moves across the ground: blocking walks and runs on paths, matching clip speed
to travel speed, gait blending, foot contacts, group movement, procedural creature legs, keyed quadruped cycles,
uneven ground, riders and giants. Values without a marker were measured in production; **(estimate)** marks starting
points.

## 1. The rule: change stride and phase, not playback rate

Feet that "shuffle", blur or skate almost always mean a gait clip is played much faster or slower than its natural
speed. Measured root cause: the walk clip was a ~0.7 m/s stroll while blocking moved the characters at 1.0–2.7 m/s,
so the rig raised the playback rate up to **2.4×**; sine-eased paths also peaked at 1.6× their average speed.
- Keep the playback rate within about **0.85–1.2×** of the clip's natural speed **(estimate)**. Outside that band pick
  another clip (a faster walk, a jog) or change the stride (§3).
- Check before blocking: `python3 scripts/bvh_probe.py stats <clip> --feet --target-speed <m/s>` prints the needed
  rate and a verdict.
- A speed you cannot cover with the clip set is a blocking problem, not an animation problem (§8).

## 2. Measure each gait clip per character

Measure ground speed from the planted foot, on each character, after the bake: pelvis scale makes it differ per
character.

| Gait (one clip set, three characters) | Ground speed |
|---|---|
| Walk | 0.64–0.75 m/s |
| Jog | 4.19–4.5 m/s |
| Sprint | 6.66–7.06 m/s |

Library references (`bvh_probe.py`): CMU walk 1.45 m/s at 112 steps/min, slow walk 0.99 m/s at 83, run 3.5 m/s;
100STYLE neutral walk 0.74 m/s at 87, neutral run 1.25 m/s at 150. A jog clip with a low cadence (~1.7 steps/s)
looks floaty below about 2.8 m/s.

## 3. Split stride from cadence

When the path speed differs from the clip speed, lengthen or shorten the stride first and let cadence take the rest.
- **Stride:** scale the leg swing around the clip's cycle-mean rotation (averaged over 24 samples per cycle) with an
  unclamped slerp. Arms take 70% of the leg change.
- **Stride factor per gait**, v = path speed, v_clip = clip speed:

| Gait | Stride factor | Clamp |
|---|---|---|
| Walk | (v / v_clip)^0.45 | 1–1.45 |
| Jog | (v / v_clip)^0.55 | 0.6–1.12 |
| Sprint | (v / v_clip)^0.45 | 0.8–1.1 |

- **Cadence** = speed / (metres per cycle after the stride change).
- A longer stride lowers the hips at heel strike: pin the feet (§6).

## 4. One gait phase, driven by distance

- Phase-align the gait clips: in a working set the left heel strike sat at ≈ 0.04 and the right at ≈ 0.54 of the
  cycle in walk, jog and sprint. One phase then drives all three, and blends never cross the legs. **Check the
  alignment before blending**; shift a clip's start if needed.
- Integrate the phase over signed distance travelled, not over time. Build a table at 30 Hz from the path data.
- Compute path distance analytically where you can: `distance(t) = lengths of earlier path clips + ease(u) × this
  clip's length` (96 samples per clip). Scrubbing backward then walks backward.
- **Make the pose a pure function of the timeline's time.** No state carried between frames; speed and phase are read
  from the path. Then scrubbing, offline stills and play mode agree (measured to the millimetre at matching times).

## 5. Smooth the speed, blend gaits in bands

- Box-filter the path speed twice: ±0.1 s for moving vs stopped, ±0.3 s for choosing the gait and the stride.
- Walk → jog over 1.9–2.4 m/s; jog → sprint over 5.4–6.2 m/s.
- Move weight (idle ↔ gait) = smoothstep over 0.06–0.35 m/s.
- **Without a timeline** (gameplay, physics-driven): speed from the transform's velocity, exponentially smoothed at
  9/s rising and 4/s falling; a step over 2.5 m in one frame is a teleport (speed 0); cap at the fastest gait
  (8 m/s for humans). A gameplay script takes over only after the timeline ends (time ≥ duration − 0.05 s).

## 6. Pin the feet

After stride scaling and blends, move the pelvis so the lowest sole contact touches the ground. Contact radii: toe
2.5 cm, ankle 8.5 cm. This holds on flat ground; on ramps it is approximate. For slopes and steps use per-foot IK
with floor offsets (§15).

## 7. Shape path speed as a trapezoid

Eased paths race the legs mid-path: a sine ease peaks at ≈ 1.57× the average speed, and an out-quad ease started
walkers at 2.1 m/s.
- Accelerate and decelerate over at most 45% of the move each.
- Cruise speed `v = L / (T − a/2 − d/2)` for path length L, duration T, acceleration time a, deceleration time d.
- Split into three path segments: in-quad acceleration, linear cruise, out-quad deceleration.

## 8. Block at speeds the clip set can show

- Choose path lengths and durations from the gait table, for example walks at 1.3–1.5 m/s, a slow entrance at
  about 1.2 m/s easing to a stop over 4 s, a jog at about 2.9 m/s.
- Find the gaps early. One clip set covered nothing between 1.8 and 2.8 m/s (no brisk walk, the jog floaty below
  2.8). Search the library for a brisk walk or slow jog (`bvh_probe.py search "walk|jog"` then `stats`), offer the
  candidates to the user, or re-block around the gap.
- Walking down ramps: libraries rarely have downhill takes (reversed uphill reads as walking backwards). Build it from
  a walk + foot IK + a back lean + a shorter stride, or slow the walk and pin.

## 9. Direction: backwards, sideways, turns

- If the explicit facing points against the travel direction (dot < −0.2), run the gait in reverse.
- For sideways and backward travel prefer real clips (100STYLE has SW/BW for every style) over reversed forward walks.
- Face along the path with damping. Turning in place reads as spinning; plan paths so turns happen while moving.
- Library capture-volume walks curve: straighten the root yaw before use (`clips-retarget.md` §3.3).

## 10. Groups

- **Desynchronise.** Per-character stride factor 0.96 / 1.0 / 1.06; step-phase offset seed × 0.37; idle and talk
  clip time offsets seed × 0.71 and × 0.53.
- A second runner starts 0.35 s later and a little slower, with an eased start.
- **Stage one behind another.** Three characters walking side by side jostled; stagger the starts and put the second
  walker about 2 m behind in another lane.

## 11. Footsteps, starts and stops

- **Footsteps from the phase:** fire a step when the phase crosses the heel-strike values (0.04, 0.54) and the move
  weight is above 0.4. Audio stays locked to contacts at any speed.
- **Starts and stops** without dedicated clips: the move-weight ramp (§5) plus the trapezoid path (§7) covered them.
  Dedicated start/stop clips are better where the camera is close; look for them in the library.
- **Settle steps on a stop:** while the cycle eases into idle, the body creeps on and a planted foot can end out of
  reach. A foot stretched past about 15 cm steps in once the walk is mostly gone; any foot still more than about 8 cm
  out steps in at the stand, farthest first.

## 12. Root motion with planned marks

When a root-motion clip must arrive at a mark on a beat (a sprint into a dive):
- **Do not warp a cycle every frame** toward a constant-speed schedule. A short-half-life pull drags the feet, and the
  clip's root speed varies within the stride, so it never settles.
- **Probe instead:** set the facing and cycle phase, step the animation ahead for the run's duration while summing
  only the root deltas. The start point is the mark minus that sum. Pure root motion then lands within a few cm.
- Keep a weak safety net that follows the probed path (half-life about 0.5 s **(estimate)**) and log the error at the
  mark.
- Start a looping run at the phase that puts the transition on the right pose; crossfade only between matching poses.
- **Never turn a character while root-motion travel plays:** root motion follows facing, so a turn bends the travel
  into a curve. Turn after the travel clip hands over.
- Engine specifics (probe pitfalls, fade timing, edit-mode sampling): `engine-unity.md`.

## 13. Procedural creature gaits

For creatures without clips (or clips that cannot cover chase speeds), drive the legs procedurally from the same
distance phase (§4). Numbers below are from a 2.75 m biped creature; scale lengths with leg length.

### 13.1 Foot curve: placed, not stepped
- Duty factor (stance share) 0.66; stride 2.3 m; foot lift 0.3 m.
- **Stance:** the ankle target slides back linearly in body space (it stands still in the world).
- **Swing:** forward progress `1 − (1 − s)^2.2` (reaches forward early), height `sin(s^0.8 · π)` (rises fast, settles
  slowly). Toes hang at −70° × lift during the swing.

### 13.2 Two-bone leg IK
- Law of cosines; clamp the reach to `[|la − lb| + 2 cm, 0.999 (la + lb)]`, so the knee never locks or folds through.
- Knee pole forward ± 0.25 × the body's right; feet level in the body frame plus the pose's own foot rotation.
- Known gap: flat floor only, and feet slide on curved paths, likely because the targets are placed in body space
  while the body turns. Fix by latching each foot's world target at touchdown and holding it through stance
  **(estimate)**; floor heights per footfall as in §15.

### 13.3 Weight from counter-motion (per stride)
- Pelvis sways ±7 cm over the stance foot and dips 6 cm at each plant.
- Hips yaw 7°, roll 4°; spine counter-twists −6°, chest −4°.
- Head counter-rotates +8° so it stays on its target.
- Arms swing ±10°.
- A tail counter-waves 5°, lagging 0.5 rad per segment.

### 13.4 Fast multi-limb skitter (gameplay)
- Diagonal pairs move together: left arm with right leg, offset +0.15 rad; swing 34°/30°, lift 38°/40° (arms/legs).
- Hips yaw/roll 6°/5°; chest counter 7°; 9 Hz Perlin jitter up to 8° of chest yaw.
- Stride grows with speed: `stride(v) = stride × (1 + 0.13 × max(0, v − 3))`, so 8 m/s reads as a fast skitter, not
  a buzz.
- Speed from smoothed transform deltas, rejecting per-frame jumps > 2 m (warps).
- Known gap: no foot IK, so feet slide at chase speed.

### 13.5 Who owns what
- **Animation owns rotation; the navigation agent only moves.** Turn off agent rotation; slerp the body to the target
  heading exponentially (rate 16/s chase, 9/s stalk, 14/s wind-up). Agent settings that kept it responsive:
  acceleration 60, angular speed 1200, auto-braking off, stopping distance 0.3, warp onto the navmesh when re-enabled.
- When a script moves the body (crawling out of a hole), drive the gait with an explicit speed override (3 m/s crawl,
  0 when dormant or dead, −1 = use the measured speed).

### 13.6 Poses on creature rigs
- **Unfurl from a coil bottom-up** with a per-bone lagged weight `wb = smoothstep(w / (1 − lag))`. Lags: legs and
  hips 0, spine 0.15 × index, neck and head 0.5 + 0.08 × index, arms 0.45, tail 0.2.
- **Retarget procedural layers onto a denser chain** by splitting each angle 50/50 between the driven bone and its
  follower (a 5-bone tail driver on a 9-bone tail).
- **Ground a stance on the posed mesh:** bake it, take the lowest vertex (skip tail-dominant vertices), offset =
  −lowest + 1 cm.
- Deaths and death rolls: `performance.md` §4.

## 14. Quadrupeds keyed from a video reference

Get the reference first (`clips-retarget.md` §6). Then:
- **Key the cycle from a script, on the real rig, in place.** One cycle; planted feet slide back in body space at the
  walking speed, so in the world they stand still. The engine plays it by phase and moves the root (§15).
- **Size the stride by the rig's reach, not by the video.** Probe each leg: how far its toe reaches forward with the
  pad flat, and back with the heel peeled, before the elbow or stifle locks. Subtract a margin for the body's bob.
  Longest stride = reach / duty factor; cadence = speed / stride. Rough leg-length estimates mislead both ways: the
  heel peel alone added about 40 cm of rear reach on a camel.
- **Pin the stance with a per-frame solve** against a copy of the armature's kinematics (rest matrices plus local
  rotations, exactly as the DCC evaluates them). Damped least squares per frame finds the swing, abduction, knee and
  toe angles that put the toe tip where it belongs under a body that bobs, pitches, rolls and sways. A planar
  two-bone IK ignores roll and sway: at 2.5° of roll a 1.4 m leg moves its hoof about 6 cm sideways.
- **Encode the anatomy as constraints:** the carpus locked straight in stance; the hind cannon kept parallel to the
  thigh (the reciprocal apparatus).
- **Build the swing from both ends of the stance:** a Hermite blend of the joint angles using the stance velocities
  at lift-off and touchdown, with fold curves on top shaped as plateaus (the carpus folds fast and holds; the toe
  flips and is flat again before landing). Single-peak bumps let the leg pass straight under the body and the hoof
  scuffs. Add a clearance guard: below a minimum tip height for that point of the swing, fold the carpus or hock more.
- **Drive the fore swing by the fetlock's path, not fold angles.** A keyed carpus fold runs ahead of the forearm and
  the hoof flicks back after lift-off (35 cm in body space, the cannon at 28°/frame): viewers see a jerk. Give the
  fetlock a body-space path (slow rise out of lift-off, an arc about 20 cm high, a little past touchdown then back,
  so the hoof lands moving back), solve forearm and cannon by two-bone IK from the elbow with the carpus bending
  forward, let the hoof hang at its lift-off angle at first and level it before landing.
- **Measure each limb's kick:** hoof travel back after lift-off and peak cannon swing in degrees per frame; compare
  with a limb the user approved.
- **Verify on the evaluated pose:** world toe tips per frame over several cycles (slide during stance), and the
  deformed mesh's lowest point per frame (sinking or hovering).
- **Leave alone what rides on a soft part:** don't jiggle a hump under a rigid saddle, or the seat sinks.

## 15. Playing an approved cycle on uneven ground

- **Play the cycle by gait phase, unchanged, and correct only the contacts.** Sample the clip at
  `frac(phase) × length`, the phase advancing at speed / stride: stance feet stand still at any speed. Below a walking
  speed, ease into the rest pose by a speed weight.
- **Export the rest pose as its own take.** An FBX stores the pose at the current frame as the model's default (in one
  case the walk's first frame, a hoof 21 cm up); "rest" taken from the imported model would make the stand ease into a
  stride.
- **Export a contact table by phase** (fetlock/ankle and toe tip per leg, in model space as the engine sees it).
  Check one sample against the engine's own evaluation before building on it.
- **Plant the point the cycle pins, not the joint above it.** A hoof cycle pins the toe tip; at heel-off the fetlock
  rolls up and forward over it (11 cm). Holding the fetlock at its touchdown point missed by up to 14 cm at every late
  stance, on flat ground.
- **Floor offset per footfall:** the floor height under the sole's heel and toe, the higher of the two (a foot over a
  step's edge rests on the higher tread). Hold it through stance; blend it over the swing with extra lift when
  stepping up. Solve the hip or shoulder to the ankle with two-bone IK (lower joints keep their keyed angles) and keep
  the foot's angle to the level, not to the pitched body.
- **Log per frame what the eye would catch:** a sole inside the floor, a planted sole above it, the IK miss. Check
  where the feet are in frame before polishing a contact nobody can see.

## 16. Mounts and riders

- **Size the pair by their ratio before anything else.** A real-size dromedary under a rider modelled 2.3 m tall read
  as a pony. Split the change between mount and rider so the set and cameras built around the old pair still fit
  (in one case mount ×1.2 and rider ×0.8: the pair grew 8%, while scaling the mount alone by 1.5 outgrew the
  framing).
- **One size number for the mount.** Contact table, stride, floor offsets and settle distances scale with it; speed
  does not, so a bigger animal steps less often (free weight). Counter-scale the rider's frame on the saddle so seat
  and stirrups stay in the rider's metres. Place stirrups from the barrel's width measured on the deformed mesh.
- **Measure the saddle against the legs.** If the pad's half-width plus thigh thickness exceeds the thigh's sideways
  reach, the thigh goes through the pad at any knee height below level. Then the knees go out and up over the pad
  (IK hint out and up in the saddle frame) and the stirrups up and out, or the pad gets narrower (an asset change:
  ask). Check contacts over the whole take: hips rock and the saddle rolls.
- **The pose can win over the contact.** Spread knees clear the felt but change the silhouette; some reviewers prefer
  hanging legs sunk a little into the pad. Show both, keep both behind one switch.
- **A rider moved only by the saddle is wooden.** Add on top: the pelvis rocking with each footfall (2–3°); chest and
  shoulders following late and overshooting (phase lag ~0.1 stride); the head steadying itself (about half its pitch
  and roll taken back); the rein hand giving with the mount's nod (a few cm); reactions to events (head and chest
  turn, capped ~55° aside and ~28° up). Put it behind one weight so weight 0 is the old rider exactly.

## 17. Giants and scale

- **Timing defines size.** Fall time grows as √size: a creature about 5× human size needs the same motion about
  √5 ≈ 2.2× longer. A 20% timing change reads as a 180 cm character becoming 120 cm.
- **Use heavy humanoid styles, slowed for scale,** not creature-parody gaits (dinosaur, zombie, animal walks) for a
  giant humanoid.
- **Keep speed, grow stride:** a bigger body steps less often at the same travel speed. Don't scale playback to fake
  size.
- A big mass cannot start abruptly but can stop abruptly; the wave through the body after a stop travels slowly.
  Attacks and impacts on giants: `performance.md`.

## 18. Flight

- **Bat flight is not bird flight.** Wings spread and swept forward on the downstroke, elbow and fingers folded on the
  upstroke; erratic jinks (several incommensurate wobbles); bank from lateral acceleration.
- Rotate bones about the body's axes: `local = restLocal × (inverse(restModel) × Q_model × restModel)`, with each
  bone's rest rotation in body space stored at build time.
- Dark flyers against dark rock read only by motion and numbers (a dense pour, 40+ in under a second) and against the
  sky; a translucent membrane catches back light.
