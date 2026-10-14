# Facial animation: lip sync, emotion, capture and playback

Read this when a face has to speak, emote or follow a performance: generating lip sync from voice takes, capturing a
face from video, applying curves in Blender, or writing the engine-side player. The face must already have its shapes
(ARKit-style 52 or a subset) and eye bones; building those is in `facial-rig.md`.

Numbers are measured on production takes unless marked **(estimate)**.

## 1. The shared format: face-curves/1

Every tool in this skill reads and writes the same JSON, so lip sync, video capture and hand edits are interchangeable.

```json
{ "format": "face-curves/1", "id": "line_042", "fps": 60, "lead_in": 0.25, "tail": 0.40,
  "frames": 391, "duration": 5.86, "source": "lipsync",
  "curves": { "jawOpen": [..], "mouthClose": [..], "eyeBlinkLeft": [..], "...": [..],
              "eyeYaw": [..], "eyePitch": [..], "headPitch": [..], "headYaw": [..], "headRoll": [..] },
  "conventions": { ... }, "words": [..], "visemes": [..], "qa": { ... } }
```

- **Time base:** curve index `i` is at `t = i / fps − lead_in` seconds of audio (or video) time. Index 0 is `lead_in`
  before the audio starts. `frames = round((lead_in + duration + tail) × fps) + 1`. To play: start the curves `lead_in`
  before the audio, or start both together and skip the first `lead_in × fps` frames. Word, phoneme and viseme times
  are plain audio time.
- **Why a lead-in and tail:** 0.25 s before and 0.40 s after hold the line's emotion, so the clip can be cut into shots
  and the face is already "in" the line when the sound starts.
- **Blendshape curves** are 0..1 with full ARKit names (`eyeBlinkLeft`, `mouthSmileRight`). Convert `_L/_R` names from
  other tools once, at load.
- **Angles in degrees:** `eyeYaw eyePitch` (both eyes) and additive `headPitch headYaw headRoll`. +pitch = chin up /
  look up (a nod is negative), +yaw = turn or look to the character's left, +roll = top of the head toward the
  character's left shoulder. Head motion is soft-clipped to ±4°.
- **mouthClose semantics differ between tools.** Lip sync writes mouthClose = 1 for "lips sealed" (with jawOpen ≤ 0.02).
  A strict ARKit rig treats mouthClose as "lips meet relative to an open jaw" and wants mouthClose ≤ jawOpen. Drive such a
  rig with `min(mouthClose, jawOpen)` (or write the strict form at export): the seal then comes from jaw ≈ 0 plus
  mouthPress, and mouthClose never pushes the lips through each other.

## 2. Lip sync from voice takes

`scripts/lipsync.py` turns a voice take and its script line into face-curves/1 at 60 fps. Needs numpy and scipy;
whisper, espeak-ng and matplotlib are optional.

```bash
python3 scripts/lipsync.py --audio take.wav --text "Hold on [whispering] did you hear that?" --lang en --out take.json
python3 scripts/lipsync.py --audio take.wav --text "[радостно] Привет! Как дела?" --lang ru --out take.json --csv --plots
python3 scripts/lipsync.py --manifest lines.csv --out-dir anim/     # id,audio,text[,lang,emotion,speaker,direction,words]
python3 scripts/lipsync.py --audio take.wav --text "..." --words words.json --out take.json   # skip whisper
python3 scripts/lipsync.py --list-presets
```

- **Word timing:** `--whisper auto|openai|mlx|none` (`--whisper-model`, `--retranscribe`), or `--words words.json` with
  `[{"w","t0","t1"}]`. With none, script words are spread over the voiced spans by phoneme count; phrases still land
  correctly (word error up to ~0.4 s inside a phrase) and `alignment.method` says `vad-distribute`. Whisper results are
  cached in `<out>/whisper_cache/` (size + mtime + model + backend + language).
- **G2P:** `--lang` (default `en`), `--g2p auto|rules|espeak`, `--espeak-voice`, `--stress-dict` for Russian (§4).
- **Acting:** bracket tags in the text drive the emotion layer (§9); `--emotion preset[:intensity]` adds a preset to
  the whole line (default intensity 0.8); `--direction "looks at the screen"` (or a manifest column) adds direction
  modifiers.
- **Output:** `--strict-arkit-close` writes `min(mouthClose, jawOpen)`; `--csv` a frame-per-row CSV; `--plots` QA
  plots when matplotlib is present. Batch runs write `index.json` and a `qa.md` table (`--qa-only` rebuilds them,
  `--only id1,id2` limits a run) and exit non-zero if any line fails QA (§14).
- **Install:** `python3 scripts/fetch_library.py --python-deps core,lipsync` (openai-whisper pulls torch, ~2 GB; on
  Apple Silicon `mlx-whisper` is lighter and timed words within 20 ms of it).
- **Deterministic:** randomness is seeded by a crc32 of the line id (`--seed` to vary), so re-renders match.
- **Tested:** on 8 lines against a reference implementation (exact match with the same stress dictionary); all QA
  checks pass (closures 16/16 on an English test take, chatter 0).

## 3. Word timing: whisper plus the script

- **The script is the ground truth; whisper only times it.** Run whisper (large-v3-turbo class) with
  `word_timestamps=True`, the language set, and **no initial prompt**: prompting with the script made whisper hallucinate
  on a scream.
- **Normalise both sides:** lowercase, letters only, digits to words, language folding (ё→е), and collapse runs of 3+
  letters so a written "A-a-a-a-a" matches a transcribed "AAAA".
- **Align with Needleman–Wunsch** over script words and whisper tokens, with 1:1, 1:2 and 2:1 moves and gaps, scored by
  Levenshtein similarity (≥ 0.60 = clean match). Absorb repeated whisper tokens into the word they repeat; ignore
  hallucinated extras (subtitle-style sign-offs appear on quiet tails). Interpolate unmatched script words between
  neighbours by phoneme count (~0.075 s per phone).
- **Refine with voice activity (RMS at 200 Hz, 20 ms window):** voiced when RMS > clip p95 − 30 dB, above the noise
  floor + 10 dB and above −60 dB absolute. Bridge gaps < 45 ms (stop closures); drop islands < 30 ms.
  - Trim word edges to voiced audio.
  - Drop short or much quieter islands at a word start (≤ 0.10 s separated by ≥ 0.12 s, or ≤ 0.35 s and 12 dB quieter):
    those are inhales, clicks or sobs.
  - Extend words through contiguous voicing: 0.35 s back (0.08 s after a breath or laugh tag), 0.18 s forward, and to
    the end of the voiced tail (≤ 0.60 s) for phrase-final words (shouts, trailing "...").
  - Move a word boundary onto a real pause next to it (up to 0.25 s back / 0.30 s forward; minimum pause 0.06 s at
    punctuation, 0.15 s elsewhere). Whisper often starts the next word inside the previous word's tail.
  - Close inter-word gaps < 0.06 s (connected speech).
- **Uncovered voiced audio ≥ 120 ms becomes a nonverbal event** (breath: exhale, sob, gasp; or laugh), typed from the
  nearest tag. It gets its own mouth shape (§5).
- Accuracy after refinement: words within a few tens of ms. Phoneme times inside a word are estimates from duration
  weights, not forced alignment.

## 4. Phonemes (G2P) per language

Rule-based G2P is enough for lip sync: the mouth needs the right viseme class and relative length, not dictionary
phonology.

- **Duration weights** spread phonemes over the word span: stressed vowel 2.4, unstressed 1.4, reduced 1.0; consonants
  0.6–1.5 (sibilants and affricates longest, j shortest).
- **Vowel strength** scales the viseme: 1.0 stressed, 0.8 unstressed, 0.6 reduced; × 1.2 for words written in capitals
  (emphasis).
- **Russian (built-in rules):** iotation (е/ё/ю/я after a vowel, ь/ъ or word start → j + vowel, else they palatalise the
  consonant), ё always stressed, clitics unstressed, unstressed о → а in the first pretonic syllable or word-initially
  else ə, unstressed е/я after a soft consonant → и, rewrites (что→што, -ого→-ово, -тся/-ться→ца, сч→щ, стн→сн),
  regressive voicing assimilation, final devoicing, geminates as one long phone, a scream's repeated vowel as one long
  stressed vowel, and a word ending in "!" or "?" keeps its last vowel open. Stress needs a dictionary: supply
  `--stress-dict` (one word per line, `+` before the stressed vowel) or mark stress inline with a combining acute
  (за́мок); about 200 function words are built in; unknown words fall back to penultimate stress and are listed in
  `g2p.stress_fallback` so you can add them. Fallback stress only affects о/а reduction and vowel length.
- **Other languages:** with `espeak-ng` on PATH, IPA from `espeak-ng -q --ipa -v <lang>` maps onto the visemes; ˈ/ˌ give
  vowel strength, ː lengthens. English without espeak uses a rough grapheme fallback (digraphs, vowel teams, silent e,
  function words): good enough for closures and vowel openness, not for subtle vowels.
- **TH** is its own viseme (English dental fricatives); languages without it never produce it.

## 5. Visemes and their control vectors

15 Oculus visemes plus `sil`. Russian mapping as an example: м/б/п→PP, ф/в→FF, ш/щ/ж/ч→CH, с/з/ц→SS, т/д→DD, н/л→nn,
к/г/х→kk, р→RR, й→ih, а/ə→aa, э/е→E, и/ы→ih, о→oh, у/ю→ou. Equal neighbours merge. Speech-layer targets before
loudness and effort scaling (paired names apply to both sides):

| Viseme | Controls |
|---|---|
| PP | jawOpen 0, mouthClose 1.0, mouthPress .25, mouthRollLower .10 |
| FF | jawOpen .06, mouthRollLower .50, mouthUpperUp .25, mouthStretch .10, mouthClose .10 |
| TH | jawOpen .12, mouthStretch .06, mouthLowerDown .10 |
| DD | jawOpen .14, mouthStretch .10, mouthLowerDown .10, mouthUpperUp .05 |
| kk | jawOpen .18, mouthStretch .12 |
| CH | jawOpen .10, mouthFunnel .45, mouthPucker .25, mouthUpperUp .15, mouthLowerDown .15 |
| SS | jawOpen .05, mouthStretch .30, mouthSmile .12, mouthUpperUp .15, mouthLowerDown .15 |
| nn | jawOpen .10, mouthStretch .08 |
| RR | jawOpen .16, mouthFunnel .15, mouthPucker .05 |
| aa | jawOpen .60, mouthLowerDown .25, mouthUpperUp .10, mouthStretch .08 |
| E | jawOpen .38, mouthStretch .30, mouthSmile .12, mouthLowerDown .20, mouthUpperUp .10 |
| ih | jawOpen .22, mouthStretch .38, mouthSmile .18, mouthUpperUp .10, mouthLowerDown .10 |
| oh | jawOpen .45, mouthFunnel .55, mouthPucker .20 |
| ou | jawOpen .18, mouthPucker .70, mouthFunnel .35 |
| breath (nonverbal) | jawOpen .16, mouthFunnel .15, mouthPucker .05, mouthLowerDown .05 |
| laugh (nonverbal) | jawOpen .32, mouthSmile .30, mouthStretch .15, mouthUpperUp .10 |

## 6. Coarticulation, anticipation and filtering

- **Consonants borrow the lips of the next vowel** (falling back to the previous one), weighted by a dominance pair
  (lip, jaw), 1 = keeps its own shape: PP (1.0, 0.9), FF (0.9, 0.7), CH (0.9, 0.6), SS (0.6, 0.7), TH (0.4, 0.5),
  DD (0.3, 0.5), nn (0.3, 0.5), RR (0.4, 0.5), kk (0.2, 0.4). So a t before u is rounded, while PP, FF and CH keep their
  shapes. Lip controls borrowed: funnel, pucker, smile, stretch.
- **Anticipate:** step targets switch 55 ms before each acoustic segment. The mouth visibly moves before the sound; on
  time looks late.
- **Filter at 8× the output rate (480 Hz)** so 20–40 ms phonemes survive: a critically damped filter of two equal
  one-pole stages, 90% in 65 ms for the lips and 85 ms for the heavier jaw. Motion starts ~55 ms before the sound and
  is half-way ~25 ms before it. This is what prevents jaw flapping on fast syllables. Never key visemes as steps.

## 7. Jaw amplitude

- `jawOpen = filtered jaw target × vocal effort × loudness gain`, loudness gain 0.45 → 1.15 over the clip's voiced range
  (gamma 0.6, zero-lag gaussian smoothing σ 25 ms on a 30 ms RMS envelope), then a soft knee from 0.75 up to a max 0.95.
- **Vocal effort** comes from tags (whisper 0.6–0.75, shout 1.35, pain 1.5) × the clip's absolute level^0.4 against a
  reference line level (p95 RMS ≈ −15 dBFS). Whispers keep a small jaw while the lips still articulate.
- Loudness is normalised per clip, so very dynamic takes are judged relative to themselves; effort tags carry absolute
  intensity.

## 8. Closures are applied last

Nothing may open sealed lips, so closures overwrite everything else at the end:
- **PP (p/b/m):** mouthClose = 1 with jawOpen ≤ 0.02 from 15 ms before the acoustic onset, held ≥ 50 ms, smoothstep
  ramps 50 ms in / 40 ms out, plus mouthPress (.30 p, .25 b, .15 m).
- **FF (f/v):** lower lip to the upper teeth: mouthRollLower .50 with jawOpen ≤ 0.10, weight .85, hold ≥ 35 ms, ramps
  35 ms.
- Also write both into the pre-filter targets 60 ms ahead, so the jaw is already closing when the seal begins.
- **Rest:** silences relax to jawOpen 0.03 plus the emotion's rest jaw (awe, fear, breathless leave it slightly open).
  Lead-in and tail hold the line's emotion.

## 9. Emotion layer

Additive presets on top of speech, each with offsets plus behaviour: blink interval, saccade hold and size, gaze bias,
head mobility, vocal effort, rest jaw, tremble, blink suppression, intensity arc (`flat`, `build` grows over the
segment, `voice` follows loudness so shouts peak with the voice), rhythmic bob, roll bias.

| Preset | Key offsets (at intensity 1) | Behaviour |
|---|---|---|
| calm | browInnerUp .04, smile .03, squint .04 | blink 2.8–5 s |
| dry | browDown .18, press .22, squint .15 | mobility .8 |
| smirk | smile L .22 / R .10, cheekSquint L .12 | asymmetric |
| firm | browDown .45, press .35, squint .22 | steady gaze |
| excited | brow up .30–.35, eyeWide .22, smile .22 | blink 2–3.5 s, mobility 1.3, voice arc |
| awe | browInnerUp .55, outer .50, eyeWide .35 | blink 4–7 s, gaze up 4°, rest jaw .07, build arc |
| mesmerized | browInnerUp .35, eyeWide .18 | blink 5–8 s, mobility .4 |
| nervous | browInnerUp .45, smile .14, frown .10, stretch .12 | blink 1.5–2.5 s, fast saccades |
| nervous_laugh | smile .45, cheekSquint .35, squint .25 | event-type |
| puzzled | browDown L .35, browOuterUp R .50 | head roll 2° |
| hushed | browInnerUp .20 | effort .65, mobility .6 |
| fear | browInnerUp .75, eyeWide .55, stretch .28 | blink 1.5–2.5 s, tremble .25, build arc |
| fear_freeze | browInnerUp .60, eyeWide .50 | mobility 0: rigid head |
| terror | brows .75–.90, eyeWide .90, stretch .45 | blinks suppressed, effort 1.35, rest jaw .15 |
| shout | browDown .45, eyeWide .55, stretch .35, sneer .18 | blinks suppressed, effort 1.35 |
| alarm | browDown .35, eyeWide .40 | effort 1.15 |
| pain | browDown .70, squint .85, blink .45, cheekSquint .75, sneer .70, stretch .70 | effort 1.5, tremble .45 |
| sobbing | browInnerUp .85, frown .60, squint .45 | gaze down 6°, tremble 1.0 |
| hysterical | browInnerUp .90, eyeWide .45, stretch .40, frown .35 | tremble .6 |
| breathless | browInnerUp .30 | rest jaw .13, breathing bob .6° at .9 Hz |
| strain | browDown .55, squint .40, sneer .30, stretch .32 | effort 1.25 |
| weak | browInnerUp .40, blink .25, frown .15 | effort .65, mobility .5 |
| stunned | browInnerUp .55, eyeWide .30 | blink 4–7 s, mobility .3 |

- **Tags map to presets.** Split each bracket tag on commas; the first matching keyword rule per chunk wins (order
  matters: "screams in pain" before "screams"). Keywords are bilingual in `scripts/lipsync.py`. A tag starts a segment at
  the word it precedes; a mid-line tag changes state there.
- **Event tags** (a nervous laugh, an exhale, a sigh) last only until the next words + 0.4 s, then the earlier state
  returns.
- Crossfade preset weights with a zero-phase critically damped filter (~0.20 s). Mouth offsets back off while the lips
  are rounded. Summed shapes get a soft knee above 0.8 (not mouthClose, eyeBlink or jaw).
- Sobbing, hysteria, pain and fear add a 6–11 Hz chin and jaw tremble (mouthRollLower .12, jaw ±.025 at tremble 1).
- Untagged lines default to calm at 0.4. Direction remarks ("looks at the device", "running") add modifiers: gaze down
  8° and head −2°; wider, faster look-around; a running bob (0.9° at 2.6 Hz).

## 10. Eyes and head

- **Blinks:** both eyes together, 45–60 ms quadratic close, 0–20 ms hold, 75–100 ms ease-out (120–180 ms total).
  Interval from the dominant preset: 2.8–5 s calm, 1.5–2.5 s nervous or fear, 4–8 s awe, mesmerized or stunned. Extra
  blinks at phrase pauses. Suppressed while shout, terror or pain dominates.
- **Saccades:** 0.2–2 s holds, 2–6° jumps, 25–40 ms moves, kept within 4.5° of the preset's gaze bias.
- **Head:** slow drift; nods of 0.9–1.6° on the stressed syllable of phrase-initial and longer words (× 1.8 on capitals);
  tilt + chin up on questions; breathing or running bobs. Everything scales by preset mobility. Soft clip ±4°.
- Eyes with no line playing return to rest, so a gaze never goes stale. Body look-at layering (spine/head/eye shares and
  clamps) is in `procedural.md`.

## 11. Performance capture from video

`scripts/face_capture.py` reads a webcam, phone or rendered video and writes face-curves/1 with all 52 blendshapes
plus head and eye angles, using MediaPipe Face Landmarker (VIDEO mode, blendshapes + transformation matrices).

```bash
python3 scripts/fetch_library.py --get face_landmarker --python-deps core,face
python3 scripts/face_capture.py take.mp4 -o take.json --preview take_qa.mp4
python3 scripts/face_capture.py selfie.mov -o selfie.json --mirror --neutral-frames 0:20
python3 scripts/face_capture.py reel.mp4 -o reel.json --crop 420,0,1080,1080 --start 2 --end 9.5
python3 scripts/face_capture.py cg_render.mp4 -o cg.json --auto-gain 'eyeBlink*'
```

- **Timestamps from the container** (ffprobe per-frame times), not frame index: phone video is often variable frame
  rate. Tested on a VFR file with irregular gaps: every pose stayed aligned.
- **Neutral calibration:** per-channel offsets from neutral frames (`--neutral-frames 0:15` frames, `1.0s:2.0s`
  seconds, `auto` = median of the first 0.5 s with a face, or `none`). Ask the performer to start each take with 1 s of
  relaxed neutral face, eyes open, looking at the lens.
- **Gains:** `--gain jawOpen=1.2,mouthSmile*=0.9`; `--auto-gain 'eyeBlink*'` scales chosen channels so their peak
  reaches ~0.95 (capped ×3). Values clamp to 0..1.
- **What MediaPipe under-reads:** mouthClose, cheekPuff, cheekSquint, noseSneer and jawForward stay near 0; tongueOut
  is never produced; blinks read weak on CG or small eyes (on a CG reel blinks peaked at 0.35 until auto-gain; then 6
  blinks matched the raw peaks); eyeLookUp barely moves. Head angles read 10–25% small (20° yaw measured as 16°).
  Key the missing channels by hand or take them from lip sync.
- **Filtering:** One Euro per channel, run forward and backward so it adds no lag (`--causal` for forward only),
  lighter on eyeBlink so blinks stay crisp. Gaps up to 0.25 s are interpolated; longer ones ease to neutral; the `qa`
  block lists them (detection rate, dropouts, per-channel max, blink count). Measured on a 34 s CG reel: 99.8%
  detection, two 1-frame dropouts at cuts.
- **Eye angles** are estimates from the eyeLook* scores (`--eye-deg 28,22` degrees per unit yaw/pitch, **estimate**),
  faded out during blinks because closing lids read as looking down.
- **`--mirror`** for flipped selfie video: swaps Left/Right channels and negates yaw and roll. Left/Right always mean
  the character's own sides.
- `--audio-offset` aligns capture to a separately recorded voice; `--lead-in`/`--tail` add margins like lip sync's.
- Capture gives performance; lip sync gives clean closures. A good combination: capture for brows, eyes, head and
  cheeks, lip sync for jaw, mouthClose, funnel and pucker.

## 12. Applying curves in Blender

`scripts/apply_face_curves.py` keys face-curves/1 onto shape keys and bones, headless, with a normal startup:

```bash
blender -b scene.blend --python scripts/apply_face_curves.py -- --curves take.json --mesh Head Teeth Tongue \
  --armature Rig --head-bone head --neck-bone neck --neck-share 0.35 --eye-bones eye.L,eye.R \
  --sound take.wav --set-range --report report.json --save out.blend
blender -b scene.blend --python scripts/apply_face_curves.py -- --curves take.json --mesh Head --dry-run
```

- **Name matching per mesh:** `--map` entry, exact, case-insensitive, separators ignored, side spellings
  (`eyeBlinkLeft`, `eyeBlink_L`, `eyeBlink.L`, `Eye_Blink_L`, `A14_Eye_Blink_Left`), then aliases for CC/Reallusion,
  VRoid/VRM and Fuse/Mixamo-style names. A side-less curve (cheekPuff) drives both sided keys; a Left/Right pair drives a
  side-less key (VRM `blink`) with its mean. `--scale name=factor`, `--exclude 'eyeLook*'`. Read the mapped/unmapped
  report (`--dry-run` prints it without keying) before judging the motion.
- **Bones:** angles become rotations in each bone's local space. `--head-axes auto` derives axes from the rest
  orientation, with forward from the eye bones (or `--facing`, default −Y) and world +Z up; an explicit spec such as
  `pitch=-X,yaw=Y,roll=-Z` overrides it. Verify one known frame (a nod tips the chin down) before rendering long.
- **Layering:** if the rig already has animation, it goes into an NLA track and the face is added above it as an
  additive (COMBINE) layer; re-running replaces the earlier face layer instead of stacking.
- Keys at the scene fps (resampled), from `--start-frame`, honouring `lead_in`; linear; written with
  `keyframe_points.add` + `foreach_set`. Tested: values match the JSON to 1e-8, bone angles within 0.1°, all 52 ARKit
  curves map onto an MPFB head plus its lashes, eyes, teeth and tongue.
- `--sound` adds the WAV as a sequencer strip aligned to the curves, so a playblast is in sync.
- Saves only with `--save`, never over the opened file unless named. Headless Blender: never factory settings.

## 13. Runtime player rules (any engine)

- **Run after the body animation** (late update, after the animator/IK; one execution slot after body rigs) so the
  face's additive head and eye motion lands on the final body pose.
- **Clock from the audio source** (`AudioSource.time` or equivalent) while the voice plays; never from frame counts.
  Otherwise lips drift on hitches.
- **Additive head motion split ~35% neck / 65% head.** Undo your own previous offset if nothing re-animated the bones
  this frame, or the head winds up over time. Set the neck share to 0 when the neck is locked (helmets, collars).
- **Gaze relative to a rest captured at the bind pose** (head-in-root), never relative to whatever the body clip left.
- **Drive every face mesh that has a key of that name** (head, lashes, teeth, tongue), so jaw keys move the lower teeth
  and tongue in sync. ICT-FaceKit has no tongueOut; keep a template's.
- **Deterministic idle blink** when no line plays (every 2.2–4.6 s, 0.16 s close / 0.22 s open), seeded per character.
- **Expose `Evaluate(t)` for edit mode,** so timelines, scrubbing and offline renders show the same face as play mode.
  Offline renders that call the camera several times per editor tick need skinning recomputed per render
  (`engine-unity.md`).
- **Body gesture with speech:** blend a talking idle in only while standing and speaking (weight 0.85 × (1 − 0.5 ×
  upper-body pose weight)), window −0.3/+0.25 s around the line, fading in 0.45 s and out 0.6 s.
- **Subtitles:** if the production shows dialogue, every line needs a subtitle that names the speaker. Plan it with the
  player, from the same line data.

## 14. Lip-sync QA (automatic, every run)

| Check | Pass |
|---|---|
| PP closures | every p/b/m reaches mouthClose ≥ 0.9 for ≥ 40 ms (report the shortest hold) |
| Chatter | 0 events of two consecutive jaw reversals > 0.12, each within 3 frames |
| Range | shapes in [0, 1], head within ±4°, no NaN |
| Blinks | count plausible for duration and preset; 0 only for shout/terror/pain lines |
| Alignment | words matched / fuzzy / substituted / interpolated, extra whisper tokens listed |
| Max jaw | ≤ 0.95; whispers small, shouts high |

Exit non-zero when any line fails so batch runs can't silently ship a broken line. Plot representative lines
(loudness, words, mouth, eyes/brows, angles) and look at them.

## 15. The face lab and review protocol

- **Judge faces in a lab, not in the dressed shot:** neutral cyclorama; key, fill, two rims, a top pool and a backdrop
  light; a filmic tonemapper (ACES or similar), the same one the shots use. Physical camera presets: close-up 100 mm at 0.95 m (f/2.2), medium close-up 85 mm at
  1.45 m (f/2.4). The camera follows the eyes softly (60% of head motion, lerp 0.12) and focuses on the eyes every frame.
- **Render reels** at 2× supersampling, encode with the voice delayed by the pre-roll, burn in captions with the line,
  the speaker and the pipeline version. Users judge the video, not a live scene.
- **Head-locked views for creatures and generated heads:** front, three-quarter, profile and the gameplay angle (often
  from below), aimed from the head bone's bind pose, not from the model's forward on frame 1.
- **The take:** the performance, then each control swept 0 → 1 → 0, then every expression preset held.
- **Sheets beat scrubbing:** contact sheets of the problem window at 0.1–0.2 s steps; before/after side by side at the
  same frame and camera.
- **Contrast in performance:** key expressions on beats with fast attacks and holds; tremble only while screaming; don't
  hold a mouth open for seconds.

## 16. Failure → cause → fix

| Symptom | Usual cause | Fix |
|---|---|---|
| Lips late or mushy | Visemes keyed on the acoustic onset, or stepped | 55 ms anticipation + critically damped filter (§6) |
| Jaw flaps on fast speech | Raw per-phoneme targets | 480 Hz internal rate, 65/85 ms filters; check chatter = 0 |
| p/b/m don't close | Closures blended with later layers | Apply closures last, clamp jaw ≤ 0.02 (§8) |
| Lips push through each other | Strict ARKit rig fed "sealed" mouthClose | `min(mouthClose, jawOpen)` |
| Whisper line has a huge jaw | Loudness normalised per clip | Vocal effort from tags × absolute level (§7) |
| Words land early or late | Whisper starts words in the previous tail | VAD refinement and pause snapping (§3) |
| Scream mistimed or invented words | Script used as whisper prompt | No prompt; align to the script afterwards |
| Eyes look down or off | Body clips key eye bones from an old rest | Mask eyes out of body clips (`facial-rig.md` §10) |
| Head slowly drifts | Additive offset applied every frame without undo | Undo own previous offset |
| Jaw renders closed in offline stills | Skinning cached per editor tick | Recompute skinning per render (`engine-unity.md`) |
| Captured face mirrored | Selfie camera | `--mirror` |
| Captured mouth never seals | MediaPipe under-reads mouthClose | Take jaw/closure channels from lip sync |
| Teeth don't move with jaw | Player drives only the head mesh | Drive every mesh with that key name |
| Face frozen between lines | No idle layer | Deterministic idle blink + saccades |
