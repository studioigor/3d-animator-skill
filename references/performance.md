# Performance: weight, timing, impact, scale and acting

Read this when a motion has to *read*: attacks and their telegraphs, hits and deaths, giants and heavy bodies,
dodges, creature acting, and staging two bodies so the action is legible. Mechanics (IK, springs, contacts)
live in `procedural.md`; gaits in `locomotion.md`. Numbers are measured or sourced unless marked **(estimate)**.

## 1. Weight and impact

- **Three phases:** anticipation (wind-up), contact, recovery. Enemies get a long, readable wind-up; the
  player character a short one.
- **Readability floor.** Fighting-game patch data: dodgeable attacks at 433 ms; cutting reaction windows to
  400 ms gained nothing; an "unblockable" signal was lengthened from 500 to 600 ms. A human-scale blow needs
  at least ~0.5 s from a clear signal to contact.
- **Hold the impact.** On contact the target jumps into its hit pose and both hold the first frame; keep the
  camera shake small and let sound and the pose carry the blow, with big arcs and a strong follow-through.
- **Hit-stop grows with damage;** attacker and target freeze equally.
- **Contact where the limb is still at full speed,** just before the clip itself brakes. A contact placed in the
  clip's deceleration with IK pulling the hand over the whole strike reads as "hands placed on the ground". Let
  the clip and body bring the limb, let IK correct only the last third, add a short rebound.
- **Plan a knock-back from the pose the body is actually in.** A creature that rears before the hit is already
  a metre off its idle mark; a path planned from the mark lands short of its wall. Ease the knock in; full
  speed from frame one reads as a teleport.
- **Uneven retiming beats new clips.** Library clips have even, "cotton" timing. Hold extreme poses, go faster
  into contact, strengthen key poses. Clip libraries do not fix timing, weight and scale; retiming and
  re-posing the clips you have gives the biggest jump.
- **Slow motion goes on the impact or the dive,** never in front of a blow and never mid-tumble (a character
  upside down for half a second reads as broken).

## 2. Scale: making a giant feel big

- **Timing defines size.** A 20% timing change turns a 180 cm character into a 120 cm one. Falls follow the
  odd-number rule 1, 3, 5, 7.
- **Time grows as √size.** A creature about 5× human size needs the same motion about √5 ≈ 2.2× longer.
- **Rules from large-creature games:** a big mass cannot start moving abruptly but can stop abruptly; gravity is
  the same at every size; don't overuse overlap (tense muscles are stiff); plain slowing loses detail, so add
  high-frequency tremor back on impacts; prefer additive layers over fast transitions; the wave through the body
  after a stop travels slowly.
- **Honest physics looks underwater.** Cut the slow parts and go straight to the blow rather than slowing
  everything.
- **Style beats retarget quality.** A giant must not get a parody gait (dinosaur, zombie, animal walks); use
  heavy humanoid styles and slow them for scale.

**A giant's attack (estimate):**
1. Wind-up 0.9–1.3 s, accelerating.
2. Apex hold 0.3–0.5 s (the "delayed attack"). Some viewers read any hold as "up too fast, pause, blow"; then make
   wind-up and fall one motion: ease the rise in, keep an even pace, slow over the top to about half the rising
   pace, start the fall at that pace and accelerate. Watch for holds you did not author: a clip's body sagging
   under IK-owned arms reads as a hold.
3. Fall 0.35–0.5 s, fastest at contact; no slow-in before the blow (even 0.6× over the last 0.15 s braked the
   fists visibly). Give a fall from head height 10–14 frames with widening spacing, peaking around 30–50 m/s for
   a 9 m body; crammed into 4–5 frames at 90 m/s it reads as a pop. The body leads and the arms trail, then whip.
4. A hard stop: hit-stop of 100–150 ms for the giant only; effects keep playing.
5. Follow-through wave over 0.8–1.2 s (shoulders, head, tail, cloth); the fist stays down.

**Implementation:** drive the attack clip by normalised time through a time-warp curve (smoothstep to the apex
pose by the apex beat, a slow creep with a strain tremble, u² down to contact on the impact beat, slow play-out).
Better, time-warp along the effector's path: sample the hand's path against clip time, drive arc length with a
quintic Hermite (start velocity = the hand's speed at hand-over, end velocity and acceleration zero), invert back
to clip time. Easing clip time alone still delivers a whipping hand at speed and stops it dead.

## 3. Readable attacks (gameplay)

- **Cinematic lunge pattern** (timings from one cutscene, a starting point): threat display with a 0.9 s ease-in,
  a hiss 0.6 s later; coil 0.5 s (out-ease, small step back); launch on the sound cue with an in-ease travel to
  contact (0.87 s); hands close 0.12 s before contact; drive-back 0.38 s out-ease. Lock launch and contact to the
  shot's sound cues.
- **Gameplay lunge:** 0.42 s telegraph (crouch, slight rear-up, hiss, keeps facing the player), then 12.5 m/s.
  Aim at the player plus 0.2 s of their velocity, overshoot 0.9 m, cap at range + 1.5 m; snap the landing to the
  navmesh and stop at walls; duration = max(0.18 s, distance/12.5) with a 0.5 m arc; fire only from 2.2–5.5 m
  with line of sight. Player's window ≈ telegraph + 0.3–0.5 s of flight **(estimate)**.
- **Close-range combo:** an arm-raise telegraph 0–0.2 s, strike 0.18–0.3 s, the other arm 0.42–0.72 s, done at
  0.9 s; hit checks at the strike frames with range and facing tests; the chest twists into each strike.
- **Hit and retreat; never stick to the player.** After a landed hit, bounce off (3.4 m/s, decelerating
  12 m/s²), recover 0.32 s, retreat diagonally ~1.1 s, then stalk; wait 1.2–1.4 s before the next attack.
- **Pack readability:** only two attack tokens at a time; the others stalk at flank angles ≥70° apart, outside
  the player's frontal cone, on a ring at the edge of the light. The first of a big group screeches (a ~1 s
  rear-up with camera trauma), then the pack rushes.
- **Interruptible wind-ups:** a heavy hit staggers for 0.2 s and cancels the wind-up; a shot can trigger a quick
  side dodge (e.g. 40% chance, 2.4 m in 0.32 s).
- **Freeze and feint beats** while stalking: random stops of 0.2–0.45 s with a twitch and a sound; short darts
  in (2.6 m in 0.55 s) every 2–4 s.
- **Charging the lens** (one worked example): about 14 m/s, stopping about 1 m short of the camera so a backhand
  can reach; wind-up 0–0.14 s, sweep 0.14–0.28 s, contact on the shot's impact sound.
- **A bot playthrough proves the encounter works, not that it reads.** Bots have perfect information and instant
  turns; judge telegraphs with a human or on video.

## 4. Hits, deaths and dodges

- **Kill reaction:** thrown back 0.65 m (0.45 for a headshot), cut short 0.35 m before a wall found by raycast,
  slide `1 − e^(−7t)`; a hop `−0.14·sin` over 0.28 s; roll 78° toward the hit side over 0.45 s, drop 12 cm;
  decaying spasms `e^(−1.4t)`. Pivot the roll about the ground point (rotate the ground offset by the same roll)
  or the body sinks through the floor.
- **Corpses:** sinking slowly after several seconds keeps the floor readable (e.g. 8 cm/s after 9 s, gone at
  14 s). Deaths on ceilings or walls flip upright and snap to the floor below.
- **Hit reactions as layered springs** (`procedural.md` §7): directional body spring, visual knockback, arm
  flinch, head pop on headshots.
- **A good dodge roll (estimate):** diagonal past the blow; starts 0.15–0.25 s before contact; 2–4 frame crouch,
  compact tuck, shoulder → back → hip, centre of mass low; speed decays naturally; exit with a hand sliding on the
  floor into one knee, head up toward the threat. Roll so the turn-back afterwards is about 90°, not 140–170°
  (that reads as spinning). Hand over to the stance only when the character is up in a crouch on both feet and
  rising (hips about 55–65% of standing), then fade over about 0.3 s.

## 5. Creature acting

- **Build the display from what the mesh has.** With no jaw, threat is posture: tall, head thrown back about
  26°, arms wide and low (±34°), claws spread, tail over the back, a tail-tip rattle when agitated.
- **Stalk silhouette:** hips about 30 cm down, spine bent forward 18/16/10°, neck down, head up 8°, arms forward
  with elbows bent, tail root lifted about 44°.
- **Sensing is snap-and-hold, not smooth scanning:** 0.8 s buckets with the snap in the first 25%, yaw and tilt
  ±30°, sniff bursts of tiny fast shakes. Gameplay: holds of 0.25–0.9 s (shorter when agitated), snaps in
  0.07 s.
- **Unfurl from a coil bottom-up:** legs release first, the head last (`locomotion.md` gives the lag table).
- **Telegraph poses:** a rear-up (hips back, head back, claws high and wide, a fast small shake, body lifted
  ~16 cm); a crouched wind-up with a fast shiver; a flinch from light (head away and down, arms up as a shield).
- **Emergences:** rise from a curl over ~1.1 s while turning to the player; a ceiling drop as a quadratic fall
  over ~0.45 s, uncurling on the way, landing with camera trauma and sound; a ceiling run upside down with a
  half-twist to upright during a 0.5 s fall.
- **Non-human rhythm:** breathing from two non-harmonic sines (a hitch), slow constant spine and tail undulation
  instead of a human rhythm, deterministic snap twitches (`procedural.md` §1).

## 6. Staging bodies so the action reads

- **No idle character.** Every character in frame acts from the first frame: a run, an approach, a wind-up.
  A standing figure in the first second is noticed immediately.
- **Keep the victim in front of the strike line** for the camera, so the silhouettes don't merge. A crouched
  helper merged with the fallen character into one two-headed figure until they were about 1 m apart with new
  facings.
- **Distance kills action.** A drag 18–21 m away in haze and depth of field barely read; bring it closer, light
  it, or reframe.
- **Stage groups one behind another.** A trio walking abreast jostled; stagger starts and lanes about 2 m apart.
  Desynchronise group runs (`locomotion.md`).
- **Re-measure after swapping placeholder bodies.** A real head 0.12 m lower than the mannequin's moved every
  close-up's camera and aim height.
- **Swapping a clip must not move approved staging.** If cameras and other characters are solved from a spot the
  clip defines (where the fist lands), keep that spot and fit the new clip to it.
- **Turn transitions into the beat, not before it.** Never cross-fade an idle into the middle of an attack; open
  the shot inside the attack's own anticipation or pick the entry frame by pose matching.
- **No pops mid-shot:** characters, props and lights should not pop in or out during a shot; make activations at
  cuts. Reviewers notice every pop.

## 7. Talking and listening

- A talking body idle blended only while standing and speaking (weights in `procedural.md` §2); the face carries
  the line (`facial-animation.md`).
- Listeners act too: look-at windows tied to the other character's lines, small nods, eyes reset between lines.
- Desynchronise idles and talk cycles per character (time offsets such as seed × 0.71 and × 0.53) so two
  characters never breathe in unison.
