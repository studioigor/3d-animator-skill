# Facial rig: making a head that can act

Read this before you build, fit or repair a face that has to animate: a scanned or AI-generated head, a template head
(MPFB/MakeHuman, MetaHuman, ICT-FaceKit), or a generated creature with no mouth at all. It covers the mesh and texture
side: topology, eyes, mouth, expression shapes, gates and export. Driving the shapes (lip sync, emotion, capture,
runtime players) is in `facial-animation.md`.

Numbers come from production heads and are measured unless marked **(estimate)**.

## 1. Choose the approach before touching the mesh

There are two families of tools, and they differ in what they keep.

| Family | Examples | What you get | Identity |
|---|---|---|---|
| Template replacement | MPFB/MakeHuman, MetaHuman, Headshot, FaceBuilder, FLAME or ICT used as the rendered mesh | A clean rigged head fitted to look like the target | Drifts by design: the template's eyes, lids and statistical prior win |
| Rig on your own mesh | Polywink, Faceit, a freelancer, in-house deformation transfer | The scan stays the neutral; only the expression deltas come from elsewhere | Kept |

- **Ask whether the face must stay identical to the original model.** If yes, only the second family qualifies. Fitting
  a template harder does not fix identity. Measured: 290 morph targets gave mean 2.5 mm / p95 5.6 mm surface error,
  many targets hit their 1.0 bound, and on renders the eye-landmark error stayed 4.9–6.0 mm with face SSIM 0.82–0.85.
  In likeness reviews these heads did not read as the original models. Identity lives in the eye and lid geometry, not in
  landmark positions.
- **Filter tools by licence, platform and automation before judging quality.**
  - Licences: FLAME, DECA, EMOCA and TopoRig are academic-only. Some neural fitters ship weights trained on
    non-commercial data. ICT-FaceKit is MIT.
  - Platforms: some wrap tools have no macOS build; Audio2Face needs CUDA.
  - Automation: Faceit's landmark step is manual.
- **Ask what the user can do by hand.** Many users can press buttons or pay for a service but cannot sculpt or paint
  corrective weights. That rules out every path that ends in manual mesh work, so plan a fully scripted pipeline.
- **Never animate raw scan triangle soup directly.** AI scans have fused lips, painted eyes and no inner mouth. Slits cut
  into the soup with template deltas bound to it deform badly, and the neck cut stays exposed; it reads as broken in review.
- **Never use a free non-rigid ICP wrap without a shape prior.** It follows noise and cavities and tears at the jaw and
  lips. A Laplacian-regularised residual (§6) cannot spike.

**The approach that kept identity:** take a clean animation topology (the template head subdivided once, about 17k
vertices), put every visible exterior vertex exactly on the scan surface, bake the scan texture 1:1 onto it, and
transfer only the expression deltas. The template brings the hard parts for free: mouth bag, inner lip curls, lid curls
(conjunctiva), eyeballs, teeth, tongue, lashes and 52 ARKit keys.

| Front-view metric | Template fit | Scan-exact head |
|---|---|---|
| SSIM inside the face oval | 0.82–0.85 | 0.89–0.92 |
| Mean landmark error | 2.0–2.7 mm | 0.30–0.59 mm |
| Eye landmark error | 4.9–6.0 mm | 0.34–0.78 mm |
| Lip landmark error | — | 0.17–0.41 mm |
| Snap distance to scan, p95 | — | 0.05–0.09 mm |

## 2. Preparation checklist (any tool needs it)

Every rig-on-your-own-mesh option, paid or scripted, needs the same prep. Finish it first.
- Eyes open, mouth closed in the neutral.
- Lips split along the real contact line, with inner-lip curls and a mouth bag.
- Teeth, gums and tongue as separate meshes.
- Eye openings cut on the painted lid margins, with eyeballs behind them.
- Neck cut hidden (sunk into a collar or blended into a body, §13).
- glTF imports split vertices at UV seams: weld at 1e-6 right after import (UVs live on loops, nothing is lost).
- Confirm orientation and scale with numbers: a height profile and orthographic front/side renders; which side is
  the face.

## 3. Landmarks on renders

- **Render, detect, lift.** Render the textured scan as a flat-lit orthographic front view of the face box. Run MediaPipe
  Face Landmarker (478 points; `scripts/fetch_library.py --get face_landmarker`). Lower detection and presence
  confidence to 0.2 for renders. Raycast each 2D landmark onto the scan to get 3D points.
- **Not every landmark is trustworthy.**
  - Drop landmarks whose template point falls through an eye or mouth opening onto a cavity wall (16 of 463 dropped).
  - Drop outliers more than 12 mm off after a first fit.
  - Weight the face-oval landmarks 0: they sit on the render silhouette.
  - Weights for the rest: lips 3, eyes 2, brows and nose 1.
  - Anchor eye-socket walls with no data term, or they chase the painted eyeball through the lid opening.
- **A template's landmark binding can be offset from its own anatomy.** One template's MediaPipe lip binding sat
  5–9 mm above its own mouth slit, so fitting landmarks parked the slit on the scan's lower lip. Measure the offset
  on the template once; pin the slit to a detected contact line (§4) instead of the inner-lip landmarks.

## 4. Lip contact line and lid margins (detect them, don't guess)

**Lip contact line:**
1. Render the scan front-on, orthographic, flat-textured: 0.10 m across 1800 px.
2. Smooth the luminance (9 px box horizontally, 5 px vertically). In each column, find the darkest crease relative to
   the rows 1.5 mm above and below.
3. Pass 1: 80 central columns in a band from 14 mm above to 3 mm below the current seam; fit a quadratic.
4. Pass 2: walk outward following the curve. The corner is where crease contrast drops below 30% of the centre's.
5. Fit a quartic, take 41 points, raycast them onto the scan. Write a debug overlay (old seam vs detected).

**Painted lid margins:**
1. Flat-textured orthographic renders around each eye.
2. Classify pixels: sclera = saturation < 0.45 × skin saturation and value > 0.55 × skin value; iris = a dark disc
   within 1.1 × the MediaPipe iris radius. Prior: the MediaPipe eye polygon scaled (1.25, 1.6).
3. Clean the mask: open 2 iterations, close 6, keep components > 5% of the largest, fill holes.
4. Read top and bottom boundaries per column; fit a robust (reweighted) quartic.
5. Clamp within 1 mm of the MediaPipe contour; the upper curve never goes above the lash line. Extend a corner to the
   MediaPipe corner when that lies more than 4 px further out. Resample to 48 points and raycast.

**Margins on the template:** its "interior" flags are unreliable on lids. The margin is the fold seen from the front:
the cheapest corner-to-corner edge path preferring silhouette edges. Compute it once on a reference head, store base
mesh indices, and map them to other heads of the same topology.

**Smooth noisy painted contours before using them as targets:** 8 passes of 0.5/0.25 with corners fixed, the correction
spread over 2 rings (0.6, then 0.5) in the basis and every key. Measured moves: max ~2 mm, mean 0.26–0.34 mm.

## 5. Put the topology exactly on the scan

- **Never put a vertex on the painted eyeball, and never across the lip seam.** Lid rims go onto the painted lid margins,
  mouth rims onto the contact line (resample upper and lower rim chains onto the seam polyline; map left and right
  halves separately, split at the midline, so symmetric keys stay symmetric; measured rim moves mean 1.3–2.2 mm).
- **Hidden parts stay where the template had them:** scalp under hair, neck inside the collar, template ears under the
  scan's ear shell.
- **Restrict projection by side.** Upper-lip vertices may project only onto scan triangles above the seam, lower-lip
  ones only below; the same for upper and lower lids around the aperture midline. Otherwise lid vertices jump to the
  opposite lid and sheet over the eye. Exclude painted-eyeball, hair-shell and ear triangles from the targets.
- **Free stray vertices instead of pinning them.** Vertices that land inside a painted aperture, pinned to the margin,
  collapse whole rows onto the rim. For the first passes take them and their neighbours off the scan and let the smooth
  solve place them (measured: 34–208 offending vertices → 0–1). Pin only as a last resort.
- **Force every lid vertex in front of the globe (within 2 cm) onto the scan.** An anchored one stays behind as a skin
  sheet over the new, larger opening.
- **Solve a smooth displacement field, then snap.** Bi-Laplacian field D with point-to-plane on scan vertices (weight 5),
  a weak tangential term (1e-3), rims (50), anchors (2), sunk neck (50). 6 iterations × up to 4 passes, then snap
  exactly to closest points. Coverage (scan → new surface): p50 0.05–0.06 mm, p95 0.26–1.9 mm.
- **Assert hidden ordering assumptions.** Blender's one-level subdivision orders vertices as original points, then one
  per edge, then one per face. If a solver relies on it, assert it (face points vs face centroids < 4 mm).

## 6. If you use a template as the rendered mesh: morph fit recipe

- Bounded least squares on modifier weights in [0, 1], ridge 2e-4, landmark weight 3, after an Umeyama similarity fit.
- Correspondences: distance threshold 30 → 15 → 8 mm, normal agreement > 0.5, only above the collar plane, ears at
  weight 0.5, scalp excluded.
- Laplacian residual on top: keep the template's cotangent Laplacian, stiffness 30 → 5, point-to-plane +
  0.15 × point-to-point, anchors 0.25 for skin with no data. Result: surface mean 1.1 mm, p95 2.2 mm.
- Expect identity drift anyway (§1). Use it when likeness is not a hard requirement.

## 7. Expression transfer (52 ARKit keys onto the new neutral)

- **Deformation transfer (Sumner–Popović) on the shared topology:** per-triangle source gradients with the 4th-vertex
  normal trick, area-weighted least squares, vertices no key moves anchored at weight 10, one factorisation for all 52
  keys. Measured difference from the template deltas: median 0.9–1.2 mm, max 5.9–10.8 mm (largest on blinks).
- **Don't rotate deltas by a per-vertex frame where the warp is a translation.** The lid warp is mostly a forward shift
  of the eye region; rotating deltas swings the blink around. Use plain offsets or true deformation transfer.
- **Smooth the key deltas, never the neutral:** 10 passes of 0.5-Laplacian on each key's delta over exterior edges,
  rims and inner surfaces fixed. Because the lips are cut, nothing mixes across the slit.
- **Untangle each key locally, in two phases.** Phase 1 (≤ 40 iterations): smooth the delta where this key creates new
  folds (dot with the smoothed normal < 0.1), growing the patch every 10 iterations. Phase 2 (≤ 60): smooth the posed
  surface on the remaining patch, which irons out neutral creases (alar crease, lid crease) that a key compresses.
  Rims keep their deltas so closures are unchanged. Left after this: 1–11 folds per key, all next to a rim.
- **Untangle the displacement field globally if the neutral folds.** Re-solve D as min Σ wᵢ|Dᵢ − D0ᵢ|² + α|LD|² with
  rims, inner surfaces and the sunk neck fixed; try α in {0.3, 1, 3, 10, 30, 100} and keep the smallest with 0 folded
  exterior triangles; add the same correction to every key.
- Optional upgrade: A/B the expression source per key, the template's 52 against ICT-FaceKit's 53 (rename _L/_R, sum
  the cheekPuff pair, keep the template's tongueOut).

## 8. Fold and sliver gate

Closest-point projection bunches and folds triangles that only show under expression; a 1:1 bake hides them at rest
(first measured ~300 folds and ~900 slivers per head; in close-ups they read as torn polygons).

- **Fold:** triangle normal against its pose's 3×-smoothed vertex normal, built from exterior skin only (inner surfaces
  face the other way and spoil the average at margins). Folded: dot < 0. Tilted: dot < 0.5.
- **Sliver:** q = 4√3·A / Σℓ², sliver when q < 0.1.
- Exclude interior faces and the 2 rows around every rim.
- **Make the gate relative to a known-good baseline:** pass when folds ≤ max(2 × template, 2) for the neutral, each of
  the 52 keys, and a scream combination. Zero folds was the first plan; it is not reachable on scans.
- **Test combined extremes, not single keys.** Single keys at 1.0 hide chips and lid crossings that mixes reveal. Scream
  mix: jawOpen .94, eyeSquint .88, mouthStretch .81, cheekSquint .78, browDown .73, noseSneer .73,
  mouthLowerDown/UpperUp .58, browInnerUp .57, eyeBlink .47. Also: sob, half-blink, squint, wide eyes.
- Local fold relaxation (tangential Laplacian step 0.5 only around folded triangles, re-projected within 3 mm, patch
  grown every 25 passes up to 150, rims pinned) cut folds 159–181 → 116–149 but did not reduce slivers.
- Expect a trade-off between identity and cleanliness (adding fold relaxation and repaints dropped SSIM 0.92–0.93 →
  0.89–0.92). Gate both: folds ≤ 2× template and SSIM ≥ 0.89.

## 9. Mouth

- **A slit on the real lip line is the single most visible mouth fix** (§4, §5).
- **Teeth follow the lips.** Fits move lips, not the template teeth; incisors then sit 4–6 mm above the lip line and
  never show. Move teeth and tongue (basis and every key) so the incisor tips sit 1.5 mm below the contact line. Then
  check: any incisor in front of the closed-lip skin within 3 cm of the mouth is pushed back by its depth + 0.8 mm
  (measured poke-through after: 0.00 mm).
- **Never AO-bake an enclosed cavity:** it bakes black. Bake a depth gradient into the teeth and tongue base colour
  instead: full brightness from ~4 mm behind the front incisors, smoothstep down over 31 mm, floor 0.32 for teeth and
  0.22 for tongue.
- **Paint the mouth bag by graph depth** from the outer skin through the inner surfaces, carrying the rim colour inward.
  Blend lip-deep (0.30, 0.09, 0.08) to cavity (0.08, 0.025, 0.025) over 12 mm, up to 16 mm more near the corners so a
  wide jaw shows no dark tear. Inner roughness 0.35.
- **Fade the scan's dark crease at the mouth corners:** it reads as a black tear once the corners stretch. Harmonic-fill
  within 1.4 mm of the rim and 7 mm of the corners (weight ramp 4 → 7 mm, soft ring 2.6 / 8.5 mm).
- **One subdivision level rounds faceted lip corners** (§15).
- Drive teeth and tongue by the same key names as the face so jaw keys move them in sync (`facial-animation.md`).

## 10. Eyes

- **Painted eyes vs real globes.** Painted eyes are pixel-identical at rest, but gaze needs a shader iris offset, a blink
  needs lid geometry, there is no glint or parallax, and lid motion stretches painted lashes. Use real eyeballs behind
  openings cut exactly on the painted margins.
- **Re-seat the globe.** Fit a fixed-radius sphere to the painted-eye bulge inside the aperture shrunk to 70%. A small
  cap barely constrains the centre sideways: regularise x/z and solve mostly for depth, starting from the template globe
  shifted by the margin-centroid move. Put the globe front ~2.6 mm behind the painted surface so the lid covers the top
  of the iris. If lids don't clear it, shrink first (down to 82%; template globes run large), then move it back until
  clearance ≥ 0.25 mm. Measured: radius 14.6–15.9 mm, moves 4–7 mm.
- **Aim the iris at the painted iris:** rotate so the mesh iris axis (iris-disc centroid − globe centre) points at
  MediaPipe iris landmark 468/473; cap at 40° (corrections applied were 8.5–14.7°). Eye bone head at the globe centre,
  tail along the iris axis, roll 0.
- **Sample colours per eye from the painting.** Iris: rings at 0.45–0.85 of the iris radius, luminance between the 30th
  and 85th percentiles (no pupil, no highlights); this keeps heterochromia. Sclera: samples with saturation < 0.25 and
  max > 0.3. Scale the iris disc to the painted size, clamped 0.92–1.03. Blend the template's bright halo outside the
  limbal ring to sclera, or it reads as a white target ring.
- **Chrome-grey eyes** come from environment reflections of a dark set on a glossy eye plus a dark iris acting as a
  mirror. Turn environment reflections off on the eye material, tint (1, 0.97, 0.95), smoothness ~0.85; catchlights
  come from the lights. In Blender, specular IOR level 0.25.
- **Tuck the lid curls (conjunctiva) behind the new margins.** Scan apertures differ from the template's (widths
  24.6–28.9 vs 23.3–26.0 mm; margins moved 7–11 mm, up to 14). Remap each curl vertex in front of the globe centre
  radially (front view) from the template aperture to the scan aperture, at the same distance off the moved globe
  (≥ 0.15 mm).
- **Inner eye surfaces swing with their margin in every key:** each inner vertex follows its 4 nearest margin vertices
  (rotation about the eye centre by their angle plus residual translation). In a closed blink fold them 0.6 mm behind
  the closure line. Otherwise blinks show dark shards.
- **"Curtain" blink for scans.** Scans have no skin folded under the lid crease, so the template blink stretches a
  2–3 mm band of lid texture 3–4× into streaks. Swing the upper lid rigidly over the globe about the eye's corner axis:
  full angle within 1 mm of the margin, smoothstep to none at ~7–13 mm, 30 Laplacian passes inside the band, skin above
  held still so brow texture never drags over the closed eye. Measured swing: 22–34°.
- **Lids meet, never cross.** Resample the lower rim along the upper rim and solve a biharmonic correction (rims fixed,
  still skin at zero) so the edges meet slightly in front of the lower rim. Rim gap: 1.3–2.7 mm → 0.07–0.40 mm.
- **Closed lids clear the cornea.** Push blink-key lid skin radially to at least the eye surface + 0.7 mm (max eye-vertex
  radius within 7° of each direction), smoothed by max-preserving diffusion; re-run after the per-key untangle. In every
  key keep lid vertices ≥ 0.2 mm off the globe.
- **Smooth the margin in closing keys** (eyeBlink, eyeSquint, cheekSquint): 10 passes along each margin chain, ends
  fixed, carried 0.6 / 0.3 into the next two rows. Otherwise the lash line is ragged.
- **Gaze diagnostic:** gaze = cornea apex (the farthest 5% of eye vertices from the pivot) − bone pivot; report pitch,
  yaw and centre-to-bone offset in mm. It catches "looking down" numerically.
- **Body clips must not own the eye bones.** Clips baked on an older rig key the eyes at the old rest (measured ~7 mm and
  several degrees off re-seated eyes; the eyes "looked down"). Mask the eye transforms out of every body clip; the face
  player alone drives the eyes, from a rest captured at the bind pose.

## 11. Lashes and brows

- Template lashes are often too long: move each lash vertex to 0.62 of its distance from the nearest lid vertex (when
  > 1.5 mm), same shift in the basis and every key.
- Lashes ride the new margins: each lash vertex follows its 4 nearest margin vertices, like the inner surfaces (§10).
- Material: dark brown (0.085, 0.062, 0.048), not black clumps; alpha clip 0.5, cull off, no shadow casting, mip maps
  that preserve coverage, alpha-test queue.
- Keep scan brows in the texture; protect them by holding skin above the blink band still.

## 12. Textures

- **Repack UVs before baking.** A template body layout left the head on ~15% of the square. Repack the head alone,
  shrink cavity-only islands to 30%, island margin 0.004, bake at 4096².
- **Short bake rays.** Target is the scan itself: cage 2 mm, max ray 5 mm. Template within 1–2 mm: 6 / 15 mm. Long rays
  project the other lip across the slit. Bake margin 12 × RES/1024 (48 px at 4K). Cycles on CPU in background mode.
- **Reliability mask + harmonic in-paint (template bakes).** A texel is reliable only if a ray from 8 mm inside along
  the normal hits within 7 mm with normal agreement > 0.6, it doesn't face away from the camera near mouth or eyes,
  it isn't on a rim or first ring, and it isn't a lid vertex that moves in a blink (those sat over the painted eyeball
  and baked sclera). Fill the rest harmonically from neighbours.
- **Poisson clone, not smooth fill, for large bad patches** (a bulge > 7 mm off the fit looked flat): clone skin from a
  reliable area of the same UV island (`cv2.seamlessClone`) with one offset for base colour, normal and roughness.
- **Repaint the lid liner band:** painted liner and crease shadow become dirty streaks on a closed lid. Harmonic-fill
  4.5 mm on the upper lid and 1.3 mm on the lower (soft rings 3.0 / 1.2 mm); dark crease boundary values become the
  edge's median skin, floored against cheek skin 10–20 mm below the eye; add back a thin lash line ~0.4 mm onto the lid.
- **Clean lids that cover the eye when closed:** faces that grow > 1.35× in the blink fade to the lid's low-passed colour
  (smoothstep 1.3 → 2.2); faces covering the open aperture when closed get clean lid colour; skin that swings down
  > 2–4 mm and is painted as socket shadow is lifted toward lid colour by travel × darkness (deep-set eyes).
- **Re-project the lower face from the front.** Normal-direction bakes around the chin can hit the scan's flat-coloured
  underside (a brown block instead of stubble). Front-facing texels (normal y < −0.25, blend to −0.5) take the colour
  seen straight from the front.
- **Fill the scan's own neck-cut line:** in the band above the collar plane, texels darker than 0.72 × reference
  luminance grow 2 rings and fill harmonically; unbaked hidden skin (luminance < 0.08) grows 1 ring only.
- **Normal-map tilt chips at openings.** At nostrils, lips, mouth corners and lid margins the topology curls inward while
  the scan faces outward, so the 1:1 tangent-space bake stores normals tilted 40–150°. Expressions swing those faces
  outward and the shading normal points sideways: orange/grey chips and dark streaks in expressions only. Proof: unlink
  the normal map and the chips vanish. Fix inside landmark zones by blending tilt toward flat with a ramp 18° → 40°:
  6–10 mm around the eyes, 4–8 mm around the inner lips, 9–15 mm at mouth corners, 4–8 mm around nostrils. Anywhere on
  the face, tilts > 50° (bake garbage) blend with a ramp 50° → 75°. Smooth weights in UV, dilate into the bake margin,
  give roughness the local median at the same weight. Measured: zone texels tilted > 40° went 1.6–2.2% → 0%.
- **Scan specks:** two passes of Telea in-painting on zone texels standing out from a 15 px median (Lab: dark > 26,
  bright > 22, chroma > 14). Median-smooth the dark nostril area (21 px) so its edge is soft.
- **An inner-surface paint can roll outward** (an inner lid curl painted at roughness 0.35 showed orange gloss in
  squint). Move its colour ~55% toward lid skin and set skin roughness.
- **Traps:** take per-vertex colour as the mean texture at the centroids of the vertex's faces (a UV on an island border
  hits unbaked margin texels); when rasterising repaint triangles, blend once at the end (shared pixels blended twice
  showed an edge net).
- **Make every texture pass idempotent:** back up source PNGs before the first run, restart from backups detected by
  md5, and skip rewriting unchanged 4K textures by pixel md5 so the engine doesn't re-import them.

## 13. Neck, collar and seams

When the head is a separate mesh, the cut between head and body has to disappear. The numbers below come from
costumes with a rigid neck ring (the "collar"); with a bare neck, blend the head's neck into the body mesh instead.

- **The head cut must never be visible.** Sink the head's neck into the collar (trim 3.5 cm below the ring plane, hold
  the sunk neck rigid in the solve) and add a dark inner neck tube (undersuit, turtleneck) whose floor sits below the
  collar rim.
- Prefer a modelled ring with a padded liner and a gaiter over a procedural rim (a procedural rim tends to read as a
  neck pillow). Skin the gaiter from the ring bone, blending to the head's neck weights at the hem (smoothed, no sawtooth).
  Pose-test at yaw ±35° and pitch ±20°.
- **Check procedural meshes in the target renderer.** A swept collar came out inside out because a bmesh face normal was
  read before `normal_update()` (still zero). The engine's back-face culling hid it; the DCC preview draws back faces and
  looked fine. Call `normal_update()`, flip if the summed dot with the ring normal is negative, and make thin fabric
  materials two-sided.
- Don't stack two seam fixes (a liner and a neck seal give two coincident turtlenecks).
- Clean the body at the cut: bisect straddling faces at the plane (no spikes), delete loose islands < 300 faces near the
  neck, flatten flaps above the ring to 15% height, press costume vertices just outside the ring 2.5 mm under it, and make
  rigid neck-ring parts 100% on the upper chest bone.
- A static hair/ear shell must not contain pieces below the chin or wider than the neck (they ride the head bone and
  tear on turns). Drop shell islands < 150 faces and anything over the animated face.
- **Localise seam bugs by ablation:** render the same frame with head, body and hair shell hidden in turn; sweep camera
  yaw through 12°, 50°, 110°, 150°, 180°.

## 14. Head weights

- **Face, jaw and skull 100% on the head bone.** A height-gradient weight left the chin 7–34% and the lips 48–76% on the
  neck while the teeth were 100% head: faces looked stretched and lips slid over teeth on head tilts.
- Head weight = smoothstep up the neck column (1 cm above the collar plane → 4 mm below the chin), or-ed with a term for
  radius outside 1.08 × neck radius on the upper neck (jaw, under-chin); below the collar blend to the upper spine.
  Check: minimum head weight at the mouth = 1.00.
- Push non-face template skin ≥ 1.5 mm under the scan shell (smoothed) so it never pokes through hair or ears.

## 15. Subdivision with shape keys

Catmull-Clark is linear in the control points: subdiv(basis + delta) = subdiv(basis) + subdiv(delta). Evaluate the
subsurf once per key and store the result as that key. Settings: uv_smooth NONE (keeps coarse bakes aligned), boundary
PRESERVE_CORNERS, vertex groups carried (`scripts/subdiv_keys.py` does this and verifies it). One level (4.3k →
~17k vertices) rounds faceted lip corners and the silhouette.
Solve the scan fit on the subdivided topology so more vertices land on the scan; then don't subdivide again at export.

## 16. A generated head with no mouth, jaw or blendshapes

For a roar or grimace on a generated creature at gameplay distance:
- **The mouth line is the user's call.** Render an orthographic textured front projection and ask them to draw the line
  in red (never on the UV atlas). A dark crease under a moustache often sits millimetres too high. A depth raster finds a
  closed-lip sculpt's seam (a 2–8 mm step back) as a first proposal; deliver it drawn on the projection for correction.
- **Cut exactly along the line:** split every front triangle it crosses; two new vertices per crossed edge (upper and
  lower lip), shared by neighbours; duplicate on-line vertices for the lower side; where the line ends inside a triangle,
  join both halves to the far vertex. Interpolate every attribute and blend bone weights. Cut both sides of a UV seam at
  the same point. Following edge chains instead stair-steps the corners.
- **Roll the lips inward:** a strip of a few rings (~1 cm at human scale) from each slit edge back into the head, curling
  up into the upper lip and down into the lower; flatten at the corners.
- **Jaw bone outside the humanoid avatar,** turned in late update: bindpose = inverse(T(hinge in head space)) × head
  bindpose. Move weight below the lip line and in front of the hinge to the jaw, fading down a beard so it stretches.
  Hinge in front of the ears, a little above the lip line; wide open ~20–22° **(estimate)**. Never map a jaw into the
  avatar: clips without jaw curves hold it half open.
- **One skinned cavity cup behind the lips:** roof on the head, floor on the jaw, walls blended. A static cup or two rigid
  halves fail (the lower lip pulls back ≥ 1 cm; halves open a see-through wedge). Centre its rings on the mouth line and
  split by longitude; rim 1–2 mm inside the raycast skin surface.
- **Glow depth (lava/fire mouths):** cup ~2× the opening's half-height deep, hot core only in the deepest 10–20%, walls
  falling to near black with a smooth fall-off, roof ~⅓ as bright as the floor **(estimate)**. Shallow cups read as a
  glowing disc. Tune at gameplay size, not in close-up. No light inside the mouth; keep scene lights off the cavity
  (black base, its own rendering layer).
- **Dark teeth** silhouetted against the glow make the hole read as a mouth; hang the upper row right under the lip edge.
  Leave teeth out unless properly modelled (cone fangs read as artefacts).
- **Keep the lip lift small** (a few mm), tapered to zero at the slit ends. Add a corner-pull shape driven from the jaw
  past about a quarter open **(estimate)** so a big opening reads as a shout, not an O-shaped gasp.
- **Fist and finger shapes** on rigs without finger bones follow the same rule: make shape keys in the DCC, keep bones and
  clips; bake in-between shapes (25/50/75%) for any curl seen moving, because FBX drops in-between frames.
- Downstream mesh edits that add vertices must extend every existing shape's deltas onto them. A guard like "the mesh
  already has blendshapes" silently skips the step.

## 17. Export (blendshape meshes)

- FBX: modifiers not applied (applying drops shape keys), face smoothing, no tangent space, no leaf bones, armature node
  type NULL, primary bone axis Y / secondary X, rest pose, no animation.
- Write a manifest at export (per part: textures, smoothness, alpha clip) so engine materials are built from data.
- Lip-sync data may use `_L/_R`; ARKit rigs use `Left/Right`. Convert names at one place (the player or the exporter).
- Engine import settings (blendshape normals, compression, tangents): see `engine-unity.md`.

## 18. Gates and review before the user sees a face

- **Automatic identity test** (`scripts/identity_check.py`): render the neutral rig and the original from the same
  cameras; measure SSIM inside the face oval and MediaPipe landmark error in mm (scaled by the eye distance). Always measure the previous approach the
  same way as the baseline. Targets proposed: SSIM > 0.95, landmark error < 0.5 mm; reached in practice: SSIM 0.89–0.92.
- **MediaPipe blendshape scores off renders** are a cheap sanity check: compare the neutral's eyeBlink score with the
  original's (0.05–0.08 vs 0.13–0.17 flagged eyes rendered more open than the scan).
- **Fixed review protocol:** poses neutral, jawOpen 0.5, smile, blink, funnel, pucker in front and 3/4; eye close-ups
  neutral, blink, half-blink, squint, wide. Three-column sheets: original | previous approach | new approach.
- **Before/after sheets** at identical poses and cameras for every texture or geometry fix (neutral, scream mix, sob,
  blink, smile, eye close-ups).
- **Check in the target renderer, not only the DCC preview.** Back-face culling, reflection probes, normal-map strength
  and eye materials differ.
- **Archive a rejected version completely** (recordings, shot lists, sources, engine export, a README with the user's
  verdict) for A/B and rollback.
- **Make every step undoable:** `.pre_<step>` backups, re-run guards, dry-run flags that write outside the engine
  project.

## 19. Known limits to tell the user up front

- Scan-exact heads still carry a few folds next to rims (the gate is ≤ 2× template, not zero) and slivers near rims.
- Deep-set painted eyes are hardest: half-blink lids can cross the iris, sockets stay dark.
- A red nostril triangle can show in mouthFunnel on template topology.
- Iris placement differs by ~1–2 mm front, 2–4 mm at 3/4: gaze lines are close, not exact.
