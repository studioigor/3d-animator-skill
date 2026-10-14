#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lipsync.py - lip-sync and facial performance curves from a voice take and its script line.

Reads a voice clip plus its text (the ground truth; [bracketed] tags are acting directions) and writes
60 fps ARKit-named blendshape curves, eye and head angles, word / phoneme / viseme timing and the chosen
emotion as JSON (format "face-curves/1").

Pipeline per line
  1. word timing: whisper (openai-whisper or mlx-whisper, word_timestamps=True, no prompt) or a words file
     -> Needleman-Wunsch alignment onto the script words -> RMS voice-activity trim / extension.
     Without any word timing the script words are spread over the voiced spans by phoneme count.
  2. G2P: Russian rules (iotation, palatalisation, reduction, clusters, voicing; stress from ё, --stress-dict,
     built-in function words, else penultimate), espeak-ng IPA for any language, rough English letter rules
     when espeak-ng is missing
  3. phonemes -> 15 Oculus visemes -> coarticulated targets (dominance), switched 55 ms early
  4. critically damped articulators at 480 Hz; jaw = loudness x vocal effort with a soft knee;
     PP (p/b/m) seals and FF (f/v) lip-to-teeth applied last
  5. emotion layer from [tags] (English or Russian keywords) and an optional direction remark: 23 presets
  6. blinks, saccades, head nods - deterministic, seeded by crc32 of the line id
  7. QA: PP closures, jaw chatter, range; exit code 1 if a line fails

Usage
  python3 lipsync.py --audio take.wav --text "Line text [whispering] more text" --lang en --out line.json
  python3 lipsync.py --audio take.wav --text "[радостно] Привет! Как дела?" --lang ru --out line.json --csv --plots
  python3 lipsync.py --manifest lines.csv --out-dir anim/        # anim/<id>.json + index.json + qa.md
  python3 lipsync.py --audio take.wav --text "..." --words words.json --out line.json   # skip whisper
  python3 lipsync.py --audio take.wav --text "..." --whisper none --out line.json      # VAD only
  python3 lipsync.py --list-presets

Manifest: CSV with a header row (comma / semicolon / tab) or a JSON list of objects. Paths are relative
to the manifest file.
  id, audio, text           required (id defaults to the audio file stem)
  lang, emotion, speaker    optional; emotion = preset[:intensity][,preset2[:i]] added to the whole line
  direction, words          optional; direction = acting remark ("looks around", "оглядывается"),
                            words = a per-line words file (skips whisper for that line)
Words file: [{"w": "word", "t0": 0.31, "t1": 0.62}, ...] (also accepts word/start/end, a whisper result
with segments, or any JSON object with a "words" list).
Stress dictionary (--stress-dict, Russian): one word per line, '+' before the stressed vowel (пров+ерка);
'#' starts a comment. Words that fell back to penultimate stress are listed in g2p.stress_fallback.
Inline stress also works in the text: a combining acute after the vowel (за́мок).

Output JSON (format "face-curves/1")
  format, id, speaker, lang, text, raw, tags, tag_positions[{tag, word_index}], audio_file
  fps (60), duration (audio s), lead_in (0.25), tail (0.40), frames, time_base, conventions{...}
  shape_curves[], angle_curves[]      curve names in output order
  words[{w, t0, t1, status, heard}]   status: match / split / fuzzy / subst / interp / vad
  phonemes[{p, t0, t1}]               ' = palatalised, ˈ = stressed, @ = reduced (ru rules); IPA otherwise
  visemes[{v, t0, t1, w}]             sil PP FF TH DD kk CH SS nn RR aa E ih oh ou; w = vowel strength
  nonverbal[{type, t0, t1}]           voiced audio outside the words: breath / laugh
  emotion{name, intensity, label, direction, modifiers, effort_mean, level_factor, segments[], blinks[]}
  alignment{method, model, words, matched, fuzzy, substituted, interpolated, whisper_extra, whisper_text}
  g2p{method, lang, voice, stress_fallback[], unknown_symbols[]}
  loudness[]                          per-clip normalised loudness 0..1 on the curve frame grid
  qa{ok, pp_closures, pp_ok, closures[], max_jaw, silence_jaw, max_frame_delta_jaw, chatter, range_ok, blinks}
  curves{name: [floats]}              32 ARKit blendshapes (0..1) + eyeYaw eyePitch headPitch headYaw headRoll (deg)
Time base: curve index i is at t = i/fps - lead_in seconds relative to the first audio sample; so index 0
is 0.25 s before the audio. Word / phoneme / viseme / nonverbal times are plain audio seconds.
Signs: eyeYaw/headYaw + = to the character's left, eyePitch/headPitch + = up (a nod is negative),
headRoll + = top of the head toward the character's left shoulder. Head angles are additive, within +-4 deg.
mouthClose is a sealed-lips scale (1 on p/b/m with jawOpen <= 0.02); strict ARKit rigs want
min(mouthClose, jawOpen) - pass --strict-arkit-close to write that form.

Requires numpy and scipy. Optional: openai-whisper or mlx-whisper (word timing), espeak-ng (G2P for any
language), matplotlib (--plots), soundfile or ffmpeg (non-WAV audio).
"""

import argparse
import csv
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import warnings
import wave
import zlib
from pathlib import Path

try:
    import numpy as np
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import lfilter, resample_poly
except ImportError as _err:                                         # pragma: no cover
    sys.stderr.write("lipsync.py needs numpy and scipy (missing: {}).\n  pip install numpy scipy\n".format(_err))
    sys.exit(2)

__version__ = "1.0"
FORMAT = "face-curves/1"

# =====================================================================================
# GLOBAL TUNABLES
# =====================================================================================
FPS = 60
LEAD_IN = 0.25            # s of neutral/emotion before the audio (curve index 0)
TAIL = 0.40               # s after the audio ends
SUB = 8                   # articulators are simulated at FPS*SUB (480 Hz) so 20-40 ms phonemes survive

WHISPER_MODEL = "large-v3-turbo"          # openai-whisper model name (also mapped to an mlx-community repo)
MLX_REPOS = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo",   # mlx-whisper repo per model name
             "large-v3": "mlx-community/whisper-large-v3-mlx",
             "turbo": "mlx-community/whisper-large-v3-turbo"}
ESPEAK_VOICES = {"en": "en-us"}           # espeak-ng voice per language code (others: the code itself)

# --- voice activity / loudness -------------------------------------------------------
VAD_RATE = 200            # Hz
VAD_WIN = 0.020           # s RMS window for VAD
VAD_REL_DB = -30.0        # voiced if RMS > (clip p95 dB + VAD_REL_DB) ...
VAD_NOISE_DB = 10.0       # ... and > (noise floor = p3 of non-digital-silence frames) + this (capped at p95-26) ...
VAD_FLOOR_DB = -60.0      # ... and above this absolute floor
VAD_CLOSE_GAP = 0.045     # bridge unvoiced gaps shorter than this (stop closures inside words)
VAD_MIN_ISLAND = 0.030    # drop voiced islands shorter than this
ENV_WIN = 0.030           # s RMS window of the loudness envelope
ENV_SMOOTH = 0.025        # s gaussian sigma on the loudness that drives the jaw (zero lag)
LEVEL_REF_DB = -15.0      # p95 RMS of a "normal" line; quieter clips get a smaller jaw
LEVEL_WEIGHT = 0.4        # effort = tag_effort * level_factor^LEVEL_WEIGHT

# --- alignment -----------------------------------------------------------------------
ALIGN_OK = 0.60           # similarity >= this counts as a clean match
WORD_MIN_DUR = 0.06       # s, shortest word span after refinement
WORD_EXTEND_BACK = 0.35   # extend a word start back through contiguous uncovered voiced audio (max)
WORD_EXTEND_BACK_EVENT = 0.08   # ... after a breath / laugh tag (that audio is the event, not the word)
BLIP_MAX = 0.10           # a voiced island this short at a word edge ...
BLIP_GAP = 0.12           # ... separated by at least this much silence is not part of the word
BLIP_QUIET_MAX = 0.35     # a leading island up to this long ...
BLIP_QUIET_DB = 12.0      # ... and this many dB quieter than the word body is an inhale, not the word
WORD_EXTEND_FWD = 0.18    # extend a word end forward through uncovered voiced audio (max)
WORD_EXTEND_LAST = 0.60   # phrase-final words may run through the whole contiguous voiced tail (shouts, "...")
GAP_CLOSE = 0.06          # inter-word gaps shorter than this are closed (connected speech)
SNAP_BACK = 0.25          # a word boundary may move onto a pause this far before it ...
SNAP_FWD = 0.30           # ... or after it
SNAP_MIN_PHRASE = 0.06    # minimum pause at punctuation (comma, period ...)
SNAP_MIN_ANY = 0.15       # minimum pause elsewhere (shorter ones may be stop closures)
NV_MIN_DUR = 0.12         # uncovered voiced audio at least this long -> non-verbal event (breath/laugh)
SEC_PER_PHONE = 0.075     # duration estimate used when interpolating unmatched words
VAD_GUESS_GAP = 0.10      # no-whisper fallback: pauses at least this long separate voiced spans
VAD_GUESS_BREAK = (0.12, -0.03)   # no-whisper fallback: DP cost of a span break inside / at the end of a phrase
VAD_GUESS_EVENT = 0.15    # no-whisper fallback: cost scale of a voiced span left empty at a breath / laugh tag
VAD_GUESS_JOIN = 0.15     # no-whisper fallback: cost of each pause inside one group of words (>= mid-phrase break)
VAD_GUESS_RATE_TOL = 1.3  # no-whisper fallback: speech rate may differ this much per phrase at no cost

# --- articulation --------------------------------------------------------------------
NEUTRAL_JAW = 0.03        # relaxed jaw in silences
ANTICIPATION = 0.055      # viseme targets switch this long before the acoustic segment starts
ATTACK_90 = 0.065         # s to reach 90% of a step (critically damped articulator, lips)
JAW_ATTACK_90 = 0.085     # the jaw is heavier: slightly slower filter (prevents flapping on fast syllables)
JAW_LOUD_MIN = 0.45       # jaw gain at the quiet end of the voiced range ...
JAW_LOUD_MAX = 1.15       # ... and at the clip's loud end
JAW_LOUD_GAMMA = 0.6      # curve of the loudness -> jaw gain mapping
JAW_KNEE = 0.75           # soft knee: jaw values above this are compressed towards JAW_MAX
JAW_MAX = 0.95            # ceiling of the soft knee
PP_PRE = 0.015            # lips sealed this long before the acoustic onset of p/b/m
CLOSURE_LEAD = 0.060      # PP/FF step targets switch this long before the hold starts
PP_HOLD_MIN = 0.050       # minimum hold at mouthClose = 1
PP_RAMP_IN = 0.050        # smoothstep ramp into the seal
PP_RAMP_OUT = 0.040       # smoothstep ramp out of the seal
PP_JAW_MAX = 0.02         # jaw clamp while the lips are sealed
PP_PRESS = {"p": 0.30, "b": 0.25, "m": 0.15}   # mouthPress during the seal (by phoneme)
FF_HOLD_MIN = 0.035       # minimum lip-to-teeth hold for f/v
FF_RAMP = 0.035           # ramp in/out of the f/v contact
FF_WEIGHT = 0.85          # how hard the f/v override pulls (lower lip to upper teeth)
FF_ROLL = 0.50            # mouthRollLower target during f/v
FF_JAW_MAX = 0.10         # jaw clamp during f/v

# --- emotion / eyes / head -----------------------------------------------------------
EMO_T90 = 0.20            # emotion weight transitions (zero-phase critically damped, each pass)
HEAD_MAX = 4.0            # deg, soft clip of the additive head motion
SHAPE_KNEE = 0.80         # soft knee on the summed shapes (except mouthClose / eyeBlink / jaw)
SACC_RADIUS = 4.5         # deg, saccade offsets stay inside this circle around the gaze bias
TREMBLE_ROLL = 0.12       # chin tremble on mouthRollLower at tremble = 1
TREMBLE_JAW = 0.025       # jaw tremble (+-) at tremble = 1
FORCE_INTENSITY = 0.8     # default intensity of a preset forced with --emotion / the manifest emotion column

# =====================================================================================
# CONTROL NAMES (ARKit spelling; Left/Right = the character's own side)
# =====================================================================================
SHAPES = [
    "jawOpen", "mouthClose", "mouthFunnel", "mouthPucker",
    "mouthStretchLeft", "mouthStretchRight", "mouthSmileLeft", "mouthSmileRight",
    "mouthFrownLeft", "mouthFrownRight", "mouthPressLeft", "mouthPressRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthRollLower",
    "eyeBlinkLeft", "eyeBlinkRight", "eyeWideLeft", "eyeWideRight", "eyeSquintLeft", "eyeSquintRight",
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekSquintLeft", "cheekSquintRight", "noseSneerLeft", "noseSneerRight",
]
ANGLES = ["eyeYaw", "eyePitch", "headPitch", "headYaw", "headRoll"]   # degrees, + = character's left / up
# base names that expand to <name>Left + <name>Right in the tables below
PAIRED = {"mouthStretch", "mouthSmile", "mouthFrown", "mouthPress", "mouthUpperUp", "mouthLowerDown",
          "eyeBlink", "eyeWide", "eyeSquint", "browDown", "browOuterUp", "cheekSquint", "noseSneer"}


def expand(d):
    """{'mouthSmile': .2, 'browDownLeft': .3} -> both sides for paired base names."""
    out = {}
    for k, v in d.items():
        if k in PAIRED:
            out[k + "Left"] = out.get(k + "Left", 0.0) + v
            out[k + "Right"] = out.get(k + "Right", 0.0) + v
        else:
            out[k] = out.get(k, 0.0) + v
    for k in out:
        if k not in SHAPES:
            raise KeyError("unknown control " + k)
    return out


# =====================================================================================
# VISEMES -> CONTROLS  (speech layer targets, before loudness / effort scaling)
# =====================================================================================
VISEME_CONTROLS = {
    "sil": {},
    "PP": {"jawOpen": 0.00, "mouthClose": 1.00, "mouthPress": 0.25, "mouthRollLower": 0.10},
    "FF": {"jawOpen": 0.06, "mouthRollLower": 0.50, "mouthUpperUp": 0.25, "mouthStretch": 0.10, "mouthClose": 0.10},
    "TH": {"jawOpen": 0.12, "mouthStretch": 0.06, "mouthLowerDown": 0.10},
    "DD": {"jawOpen": 0.14, "mouthStretch": 0.10, "mouthLowerDown": 0.10, "mouthUpperUp": 0.05},
    "kk": {"jawOpen": 0.18, "mouthStretch": 0.12},
    "CH": {"jawOpen": 0.10, "mouthFunnel": 0.45, "mouthPucker": 0.25, "mouthUpperUp": 0.15, "mouthLowerDown": 0.15},
    "SS": {"jawOpen": 0.05, "mouthStretch": 0.30, "mouthSmile": 0.12, "mouthUpperUp": 0.15, "mouthLowerDown": 0.15},
    "nn": {"jawOpen": 0.10, "mouthStretch": 0.08},
    "RR": {"jawOpen": 0.16, "mouthFunnel": 0.15, "mouthPucker": 0.05},
    "aa": {"jawOpen": 0.60, "mouthLowerDown": 0.25, "mouthUpperUp": 0.10, "mouthStretch": 0.08},
    "E":  {"jawOpen": 0.38, "mouthStretch": 0.30, "mouthSmile": 0.12, "mouthLowerDown": 0.20, "mouthUpperUp": 0.10},
    "ih": {"jawOpen": 0.22, "mouthStretch": 0.38, "mouthSmile": 0.18, "mouthUpperUp": 0.10, "mouthLowerDown": 0.10},
    "oh": {"jawOpen": 0.45, "mouthFunnel": 0.55, "mouthPucker": 0.20},
    "ou": {"jawOpen": 0.18, "mouthPucker": 0.70, "mouthFunnel": 0.35},
}
VOWEL_VISEMES = {"aa", "E", "ih", "oh", "ou"}
# coarticulation: (lip dominance, jaw dominance). 1 = keeps its own shape, 0 = takes the
# neighbouring vowel's lip shape / jaw (lip rounding spreads backwards over consonants).
VISEME_DOMINANCE = {
    "PP": (1.0, 0.9), "FF": (0.9, 0.7), "CH": (0.9, 0.6), "SS": (0.6, 0.7), "TH": (0.4, 0.5),
    "DD": (0.3, 0.5), "nn": (0.3, 0.5), "kk": (0.2, 0.4), "RR": (0.4, 0.5),
}
LIP_CONTROLS = ["mouthFunnel", "mouthPucker", "mouthSmileLeft", "mouthSmileRight",
                "mouthStretchLeft", "mouthStretchRight"]
# non-verbal voiced events (audio not covered by words)
NONVERBAL_CONTROLS = {
    "breath": {"jawOpen": 0.16, "mouthFunnel": 0.15, "mouthPucker": 0.05, "mouthLowerDown": 0.05},
    "laugh": {"jawOpen": 0.32, "mouthSmile": 0.30, "mouthStretch": 0.15, "mouthUpperUp": 0.10},
}

# =====================================================================================
# PHONEMES -> VISEMES, DURATION WEIGHTS
# =====================================================================================
# Russian rule phones (internal ASCII labels)
PHONEME_TO_VISEME = {
    "p": "PP", "b": "PP", "m": "PP",
    "f": "FF", "v": "FF",
    "sh": "CH", "zh": "CH", "shch": "CH", "ch": "CH",
    "s": "SS", "z": "SS", "ts": "SS",
    "t": "DD", "d": "DD", "n": "nn", "l": "nn",
    "k": "kk", "g": "kk", "x": "kk",
    "r": "RR", "j": "ih",
    "a": "aa", "@": "aa", "o": "oh", "u": "ou", "e": "E", "i": "ih", "y": "ih",
}
PHONE_WEIGHT = {          # consonant duration weights of the Russian rule phones
    "p": 1.0, "b": 1.0, "t": 1.0, "d": 1.0, "k": 1.0, "g": 1.0,
    "m": 0.9, "n": 0.9, "l": 0.8, "r": 0.7, "j": 0.6,
    "f": 1.0, "v": 0.9, "s": 1.2, "z": 1.1, "sh": 1.25, "zh": 1.2, "shch": 1.5, "x": 1.1,
    "ts": 1.25, "ch": 1.25,
}
VOWEL_WEIGHT = {"S": 2.4, "U": 1.4, "R": 1.0}        # stressed / unstressed / reduced
VOWEL_STRENGTH = {"S": 1.0, "U": 0.8, "R": 0.6}      # jaw / lip scale of the viseme
CAPS_STRENGTH = 1.2                                   # ALL-CAPS word = emphasis
GEMINATE = 1.4                                        # weight factor of a doubled / long consonant
LONG_VOWEL = 1.5                                      # weight factor of an IPA long vowel (ː)
OFFGLIDE = 0.8                                        # weight factor of the second vowel of a diphthong

# IPA consonant -> (viseme, duration weight). Covers espeak-ng output for en/de/fr/es/it/ru and more.
IPA_CONSONANTS = {
    "p": ("PP", 1.0), "b": ("PP", 1.0), "m": ("PP", 0.9), "β": ("PP", 0.8), "ɓ": ("PP", 1.0),
    "f": ("FF", 1.0), "v": ("FF", 0.9), "ɱ": ("FF", 0.9), "ɸ": ("FF", 1.0), "ʋ": ("FF", 0.8),
    "θ": ("TH", 1.0), "ð": ("TH", 0.9),
    "t": ("DD", 1.0), "d": ("DD", 1.0), "ʈ": ("DD", 1.0), "ɖ": ("DD", 1.0),
    "n": ("nn", 0.9), "l": ("nn", 0.8), "ɫ": ("nn", 0.8), "ɲ": ("nn", 0.9), "ʎ": ("nn", 0.8),
    "ɳ": ("nn", 0.9), "ɭ": ("nn", 0.8), "ɬ": ("nn", 0.9), "ɮ": ("nn", 0.9), "ŋ": ("nn", 0.9),
    "k": ("kk", 1.0), "g": ("kk", 1.0), "ɡ": ("kk", 1.0), "q": ("kk", 1.0), "ɢ": ("kk", 1.0),
    "c": ("kk", 1.0), "ɟ": ("kk", 1.0), "ɴ": ("kk", 0.9),
    "x": ("kk", 1.1), "ɣ": ("kk", 1.0), "χ": ("kk", 1.1), "ʁ": ("kk", 0.8), "ʀ": ("kk", 0.8),
    "h": ("kk", 0.8), "ɦ": ("kk", 0.8), "ħ": ("kk", 0.9), "ʕ": ("kk", 0.9),
    "ʃ": ("CH", 1.25), "ʒ": ("CH", 1.2), "tʃ": ("CH", 1.25), "dʒ": ("CH", 1.25),
    "ʂ": ("CH", 1.25), "ʐ": ("CH", 1.2), "ɕ": ("CH", 1.3), "ʑ": ("CH", 1.2),
    "tɕ": ("CH", 1.25), "dʑ": ("CH", 1.25), "tʂ": ("CH", 1.25), "dʐ": ("CH", 1.25),
    "s": ("SS", 1.2), "z": ("SS", 1.1), "ts": ("SS", 1.25), "dz": ("SS", 1.2), "ç": ("SS", 1.1),
    "r": ("RR", 0.7), "ɾ": ("RR", 0.6), "ɹ": ("RR", 0.7), "ɻ": ("RR", 0.7), "ɽ": ("RR", 0.6),
}
IPA_LANG_CONSONANTS = {"en": {"ɾ": ("DD", 0.6)}}      # per-language overrides (English flap t reads as d)
IPA_GLIDES = {"j": "ih", "ʝ": "ih", "ɰ": "ih", "w": "ou", "ɥ": "ou", "ʍ": "ou"}   # semivowels, strength 0.6
IPA_VOWELS = {            # IPA vowel -> viseme (nasality, length and other diacritics are stripped first)
    "a": "aa", "ɑ": "aa", "ɐ": "aa", "æ": "aa", "ʌ": "aa", "ə": "aa", "ɶ": "oh",
    "e": "E", "ɛ": "E", "ɜ": "E", "ɘ": "E", "ɤ": "E",
    "i": "ih", "ɪ": "ih", "ɨ": "ih", "ᵻ": "ih", "ɯ": "ih",
    "o": "oh", "ɔ": "oh", "ɒ": "oh", "ɵ": "oh", "ø": "oh", "œ": "oh",
    "u": "ou", "ʊ": "ou", "y": "ou", "ʏ": "ou", "ʉ": "ou",
    "ɚ": "RR", "ɝ": "RR",
}
IPA_REDUCED = {"ə", "ɐ", "ᵻ", "ɚ", "ɘ"}                # unstressed reduced vowels (strength "R")
IPA_PP_BASE = {"β": "b", "ɓ": "b"}                     # PP press lookup key for non-ASCII labials
IPA_SKIP = set("ʰʷˠˤⁿˡʼˀʱ‿'-_.|‖↗↘ʔᵊ0123456789")       # symbols that carry no mouth shape

# =====================================================================================
# RUSSIAN G2P DATA
# =====================================================================================
# Built-in stress for frequent Russian function words, pronouns and short verbs ('+' before the stressed
# vowel; keys are matched with ё -> е). Everything else: --stress-dict, ё, else penultimate (reported).
RU_STRESS_BUILTIN = """
мен+я теб+я ег+о ем+у н+ами в+ами +ими себ+я себ+е соб+ой мо+я мо+и тво+я тво+и сво+я сво+и
н+аша н+аше н+аши в+аша в+аше в+аши +этот +эта +это +эти +этого +этому +этой +этих +этим тог+о том+у
к+ого ком+у чег+о чем+у как+ой как+ая как+ое как+ие как+их так+ой так+ая так+ое так+ие кот+орый кот+орая
кот+орое кот+орые кот+орого он+а он+о он+и сам+а сам+о с+ами всег+о всем+у н+есколько к+аждый друг+ой люб+ой
т+олько уж+е т+оже т+акже +очень м+ожно н+адо н+ужно нельз+я мн+ого немн+ого м+ало сейч+ас пот+ом тепер+ь
всегд+а никогд+а иногд+а давн+о туд+а сюд+а отт+уда отс+юда куд+а отк+уда зач+ем почем+у потом+у к+огда
тогд+а +если чт+обы хорош+о пл+охо ничег+о н+ечего никт+о к+ажется м+ожет б+ыло был+а б+ыли б+удет б+удут
б+уду вм+есте оп+ять пр+осто пр+ямо т+очно кон+ечно нав+ерное нав+ерно п+осле п+еред м+ежду +около вокр+уг
м+имо ч+ерез пр+отив р+ади вм+есто внутр+и снар+ужи сн+ова совс+ем ср+азу ск+оро д+альше наз+ад навер+х
м+ожешь м+ожем мог+у хоч+у х+очешь х+очет хот+им хот+ел хот+ела зн+аю зн+аешь зн+ает в+ижу в+идишь в+идит
говор+ю сказ+ал сказ+ала ид+и ид+у дав+ай дав+айте сл+ушай сл+ушайте смотр+и посмотр+и помог+и бег+и беж+им
сто+ять держ+ись скор+ее быстр+ее б+ыстро т+ихо гр+омко д+елать сд+елать д+елай д+умаю д+умать п+омню
понима+ю п+онял спас+ибо пож+алуйста здр+авствуйте прив+ет пок+а аг+а алл+о ог+о
од+ин одн+а одн+о чет+ыре в+осемь д+евять д+есять т+ысяча п+ервый втор+ой тр+етий
"""
# unstressed clitics (also skipped by head nods)
CLITICS = {"в", "во", "к", "ко", "с", "со", "у", "о", "об", "на", "не", "ни", "по", "за", "из", "от",
           "до", "же", "бы", "ли", "и", "а", "но", "то", "при", "для", "без", "под", "над"}
WORD_REWRITES = {"что": "што", "чтобы": "штобы", "конечно": "конешно", "скучно": "скушно"}   # spelling -> sound
G_TO_V_EXCEPT = {"много", "немного", "строго", "дорого", "убого", "полого", "ого"}   # -ого that keeps г
CLUSTER_RULES = [("ться", "ца"), ("тся", "ца"), ("сч", "щ"), ("зч", "щ"), ("стн", "сн"), ("здн", "зн"),
                 ("вств", "ств"), ("лнц", "нц"), ("рдц", "рц"), ("сш", "ш"), ("зж", "ж"), ("тч", "ч"),
                 ("дч", "ч")]     # consonant cluster simplifications, applied in order
RU_VOWELS = "аеёиоуыэюя"
IOTATED = {"е": "e", "ё": "o", "ю": "u", "я": "a"}
PLAIN_VOWEL = {"а": "a", "о": "o", "у": "u", "ы": "y", "э": "e", "и": "i"}
RU_CONS = {"б": "b", "в": "v", "г": "g", "д": "d", "ж": "zh", "з": "z", "й": "j", "к": "k", "л": "l",
           "м": "m", "н": "n", "п": "p", "р": "r", "с": "s", "т": "t", "ф": "f", "х": "x", "ц": "ts",
           "ч": "ch", "ш": "sh", "щ": "shch"}
ALWAYS_HARD = {"zh", "sh", "ts"}
VOICED_PAIR = {"b": "p", "d": "t", "g": "k", "v": "f", "z": "s", "zh": "sh"}
VOICELESS_PAIR = {v: k for k, v in VOICED_PAIR.items()}
DIGIT_WORDS = {           # digits spoken as words, per language (alignment + G2P)
    "ru": {"0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре", "5": "пять",
           "6": "шесть", "7": "семь", "8": "восемь", "9": "девять", "10": "десять"},
    "en": {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
           "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten"},
}

# =====================================================================================
# ENGLISH LETTER RULES (rough fallback when espeak-ng is missing; output is IPA)
# =====================================================================================
EN_WORDS = {              # frequent / irregular words -> IPA (ˈ before the stressed vowel)
    "a": "ə", "an": "ən", "the": "ðə", "of": "əv", "to": "tə", "and": "ənd", "or": "ɔɹ", "but": "bət",
    "as": "əz", "at": "ət", "in": "ɪn", "on": "ɑn", "by": "baɪ", "for": "fɚ", "from": "fɹəm", "with": "wɪð",
    "into": "ˈɪntuː", "is": "ɪz", "are": "ɑɹ", "was": "wəz", "were": "wɚ", "be": "bi", "been": "bɪn",
    "am": "æm", "has": "hæz", "have": "hæv", "had": "hæd", "do": "duː", "does": "dʌz", "did": "dɪd",
    "done": "dˈʌn", "will": "wɪl", "would": "wʊd", "could": "kʊd", "should": "ʃʊd", "can": "kæn",
    "must": "mʌst", "shall": "ʃæl", "i": "ˈaɪ", "you": "juː", "he": "hi", "she": "ʃi", "we": "wi",
    "they": "ðeɪ", "it": "ɪt", "me": "mi", "him": "hɪm", "her": "hɚ", "us": "ʌs", "them": "ðəm",
    "my": "maɪ", "your": "jɚ", "his": "hɪz", "its": "ɪts", "our": "aʊɚ", "their": "ðɛɹ", "this": "ðˈɪs",
    "that": "ðæt", "these": "ðˈiːz", "those": "ðˈoʊz", "there": "ðˈɛɹ", "here": "hˈɪɹ", "where": "wˈɛɹ",
    "then": "ðˈɛn", "than": "ðən", "though": "ðˈoʊ", "what": "wˈʌt", "who": "hˈuː", "whom": "hˈuːm",
    "whose": "hˈuːz", "why": "wˈaɪ", "when": "wˈɛn", "how": "hˈaʊ", "which": "wɪtʃ", "not": "nˈɑt",
    "no": "nˈoʊ", "yes": "jˈɛs", "so": "sˈoʊ", "now": "nˈaʊ", "one": "wˈʌn", "two": "tˈuː",
    "three": "θɹˈiː", "four": "fˈɔɹ", "five": "fˈaɪv", "six": "sˈɪks", "seven": "sˈɛvən",
    "eight": "ˈeɪt", "nine": "nˈaɪn", "ten": "tˈɛn", "zero": "zˈiɹoʊ", "once": "wˈʌns", "some": "sʌm",
    "come": "kˈʌm", "came": "kˈeɪm", "become": "bɪkˈʌm", "other": "ˈʌðɚ", "another": "ənˈʌðɚ",
    "mother": "mˈʌðɚ", "father": "fˈɑðɚ", "brother": "bɹˈʌðɚ", "love": "lˈʌv", "move": "mˈuːv",
    "lose": "lˈuːz", "give": "ɡˈɪv", "live": "lˈɪv", "get": "ɡˈɛt", "go": "ɡˈoʊ", "goes": "ɡˈoʊz",
    "gone": "ɡˈɔn", "know": "nˈoʊ", "said": "sˈɛd", "says": "sˈɛz", "again": "əɡˈɛn",
    "about": "əbˈaʊt", "above": "əbˈʌv", "away": "əwˈeɪ", "around": "əɹˈaʊnd", "alone": "əlˈoʊn",
    "along": "əlˈɔŋ", "across": "əkɹˈɔs", "ago": "əɡˈoʊ", "afraid": "əfɹˈeɪd", "all": "ˈɔːl",
    "any": "ˈɛni", "many": "mˈɛni", "very": "vˈɛɹi", "every": "ˈɛvɹi", "only": "ˈoʊnli", "over": "ˈoʊvɚ",
    "under": "ˈʌndɚ", "after": "ˈæftɚ", "before": "bɪfˈɔɹ", "behind": "bɪhˈaɪnd", "because": "bɪkˈʌz",
    "people": "pˈiːpəl", "water": "wˈɔtɚ", "world": "wˈɝld", "work": "wˈɝk", "word": "wˈɝd",
    "eye": "ˈaɪ", "eyes": "ˈaɪz", "great": "ɡɹˈeɪt", "break": "bɹˈeɪk", "head": "hˈɛd", "dead": "dˈɛd",
    "ready": "ɹˈɛdi", "heavy": "hˈɛvi", "friend": "fɹˈɛnd", "busy": "bˈɪzi", "women": "wˈɪmɪn",
    "woman": "wˈʊmən", "put": "pˈʊt", "push": "pˈʊʃ", "pull": "pˈʊl", "full": "fˈʊl", "door": "dˈɔɹ",
    "floor": "flˈɔɹ", "blood": "blˈʌd", "watch": "wˈɑtʃ", "want": "wˈɑnt", "ok": "oʊkˈeɪ",
    "okay": "oʊkˈeɪ", "oh": "ˈoʊ", "hello": "həlˈoʊ", "please": "plˈiːz", "sorry": "sˈɑɹi",
    "through": "θɹˈuː", "thought": "θˈɔt", "enough": "ɪnˈʌf", "maybe": "mˈeɪbi", "something": "sˈʌmθɪŋ",
    "nothing": "nˈʌθɪŋ", "everything": "ˈɛvɹiθɪŋ", "anything": "ˈɛniθɪŋ", "someone": "sˈʌmwʌn",
    "everyone": "ˈɛvɹiwʌn", "nobody": "nˈoʊbɑdi", "somebody": "sˈʌmbɑdi", "together": "təɡˈɛðɚ",
    "listen": "lˈɪsən", "often": "ˈɔfən", "answer": "ˈænsɚ", "build": "bˈɪld", "built": "bˈɪlt",
    "touch": "tˈʌtʃ", "young": "jˈʌŋ", "country": "kˈʌntɹi", "money": "mˈʌni", "month": "mˈʌnθ",
    "front": "fɹˈʌnt", "son": "sˈʌn", "sure": "ʃˈʊɹ", "girl": "ɡˈɝl", "begin": "bɪɡˈɪn",
    "idea": "aɪdˈiːə", "really": "ɹˈiːli", "quiet": "kwˈaɪət", "science": "sˈaɪəns", "alien": "ˈeɪliən",
    "don't": "dˈoʊnt", "won't": "wˈoʊnt", "can't": "kˈænt", "i'm": "ˈaɪm", "it's": "ɪts", "let's": "lˈɛts",
    "you're": "jʊɹ", "we're": "wɪɹ", "they're": "ðɛɹ", "what's": "wˈʌts", "there's": "ðˈɛɹz",
}
EN_SHORT = {"a": "æ", "e": "ɛ", "i": "ɪ", "o": "ɑ", "u": "ʌ", "y": "ɪ"}     # closed-syllable vowels
EN_LONG = {"a": "eɪ", "e": "iː", "i": "aɪ", "o": "oʊ", "u": "uː", "y": "aɪ"}   # magic-e / open vowels
EN_TEAMS = [("eigh", "eɪ"), ("ough", "ɔ"), ("augh", "ɔ"), ("igh", "aɪ"), ("eau", "oʊ"),   # vowel teams
            ("ee", "iː"), ("ea", "iː"), ("oo", "uː"), ("ou", "aʊ"), ("ow", "aʊ"), ("oa", "oʊ"),
            ("ai", "eɪ"), ("ay", "eɪ"), ("ei", "eɪ"), ("ey", "eɪ"), ("oi", "ɔɪ"), ("oy", "ɔɪ"),
            ("au", "ɔ"), ("aw", "ɔ"), ("ew", "uː"), ("ue", "uː"), ("ui", "uː"), ("ie", "iː"), ("oe", "oʊ")]
EN_R_VOWELS = {"a": "ɑɹ", "o": "ɔɹ", "e": "ɝ", "i": "ɝ", "u": "ɝ", "y": "ɝ"}   # vowel + r + consonant/end
EN_CONS = [("tch", "tʃ"), ("dge", "dʒ"), ("sch", "sk"), ("ch", "tʃ"), ("sh", "ʃ"), ("th", "θ"),   # digraphs
           ("ph", "f"), ("wh", "w"), ("ck", "k"), ("ng", "ŋ"), ("qu", "kw"), ("gh", ""), ("x", "ks"),
           ("j", "dʒ"), ("q", "k"), ("r", "ɹ"), ("y", "j")]
EN_SUFFIXES = [("tion", "ʃən"), ("sion", "ʒən"), ("cian", "ʃən"), ("ture", "tʃɚ"), ("cial", "ʃəl"),
               ("tial", "ʃəl"), ("ous", "əs"), ("ful", "fəl"), ("ness", "nəs"), ("less", "ləs"),
               ("ment", "mənt"), ("able", "əbəl"), ("ible", "əbəl")]          # unstressed suffix syllables
EN_STRESS_BEFORE = ("tion", "sion", "cian", "ic", "ical", "ity", "ial", "ian", "ious", "eous")  # stress before
EN_STRESS_LAST = ("ee", "eer", "ese", "ique", "oon")                          # stress on the last syllable
EN_PREFIXES = ("be", "de", "re", "un", "pre", "dis", "mis")                  # unstressed prefixes

# =====================================================================================
# LETTER FALLBACK (other languages without espeak-ng; very rough, output is IPA)
# =====================================================================================
LETTER_MULTI = [("tsch", "tʃ"), ("sch", "ʃ"), ("ch", "ʃ"), ("sh", "ʃ"), ("ph", "f"), ("th", "t"),
                ("qu", "k"), ("gn", "ɲ"), ("ck", "k"), ("ng", "ŋ"), ("ou", "u"), ("eau", "o"), ("au", "aʊ"),
                ("ei", "aɪ"), ("eu", "ɔɪ"), ("ie", "i")]
LETTER_LANG = {"de": {"z": "ts", "w": "v", "v": "f", "ch": "x", "ie": "iː"},      # per-language letter tweaks
               "es": {"j": "x", "z": "θ", "ll": "ʝ", "h": "", "v": "b", "ch": "tʃ", "ei": "ei", "eu": "eu",
                      "ou": "ou", "au": "au"},
               "it": {"z": "ts", "h": "", "ch": "k", "gli": "ʎ", "sc": "ʃ"},
               "fr": {"h": "", "j": "ʒ", "oi": "wa", "ai": "ɛ", "eu": "ø", "ch": "ʃ", "qu": "k", "au": "o"},
               "pt": {"h": "", "lh": "ʎ", "nh": "ɲ", "j": "ʒ", "ch": "ʃ"},
               "pl": {"w": "v", "sz": "ʂ", "cz": "tʂ", "rz": "ʐ", "ł": "w", "ch": "x"}}
LETTER_SINGLE = {"a": "a", "à": "a", "á": "a", "â": "a", "ã": "a", "ä": "ɛ", "å": "o", "e": "e", "é": "e",
                 "è": "ɛ", "ê": "ɛ", "ë": "e", "i": "i", "í": "i", "ì": "i", "î": "i", "ï": "i", "o": "o",
                 "ó": "o", "ò": "o", "ô": "o", "õ": "o", "ö": "ø", "u": "u", "ú": "u", "ù": "u", "û": "u",
                 "ü": "y", "y": "i", "ý": "i", "b": "b", "c": "k", "ç": "s", "d": "d", "f": "f", "g": "ɡ",
                 "h": "h", "j": "j", "k": "k", "l": "l", "m": "m", "n": "n", "ñ": "ɲ", "p": "p", "q": "k",
                 "r": "r", "s": "s", "ß": "s", "t": "t", "v": "v", "w": "w", "x": "ks", "z": "z", "ł": "w"}
LETTER_VOWELS = set("aàáâãäåeéèêëiíìîïoóòôõöuúùûüyý")
LETTER_ACCENTED = set("áéíóúàèìòù")                      # a written accent marks the stressed vowel
LANG_STRESS = {"de": "first", "nl": "first", "cs": "first", "sk": "first", "fi": "first", "hu": "first",
               "is": "first", "fr": "last", "tr": "last", "hy": "last"}   # default: penultimate syllable

# words that get no head nod (unstressed function words), per language
FUNCTION_WORDS = {
    "ru": CLITICS,
    "en": {"a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "from", "with", "and", "or", "but",
           "as", "is", "are", "was", "were", "be", "been", "am", "it", "its", "i", "you", "he", "she", "we",
           "they", "me", "him", "her", "us", "them", "my", "your", "his", "our", "their", "do", "does", "did",
           "have", "has", "had", "will", "would", "can", "could", "shall", "should", "may", "might", "must",
           "so", "if", "than", "that"},
    "de": {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "und", "oder", "aber",
           "zu", "in", "im", "an", "am", "auf", "mit", "von", "vom", "für", "ist", "es", "ich", "du", "er",
           "sie", "wir", "ihr", "so", "ja"},
    "fr": {"le", "la", "les", "l", "un", "une", "des", "de", "du", "d", "et", "ou", "à", "au", "aux", "en",
           "dans", "sur", "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "ne", "que", "qui",
           "se", "ce", "c", "j", "n", "s", "y"},
    "es": {"el", "la", "los", "las", "un", "una", "y", "o", "de", "del", "a", "al", "en", "con", "por",
           "para", "que", "se", "me", "te", "le", "lo", "mi", "tu", "su"},
    "it": {"il", "lo", "la", "i", "gli", "le", "un", "una", "uno", "e", "o", "di", "del", "della", "a", "al",
           "in", "con", "per", "che", "si", "mi", "ti", "ci", "vi"},
}

# =====================================================================================
# EMOTION PRESETS  (additive offsets at intensity 1; paired base names expand to Left/Right)
#   blink: interval range s | sacc_hold / sacc_amp: saccade hold s / jump deg | gaze: (yaw, pitch) deg bias
#   mobility: head motion scale | effort: vocal effort (jaw scale) | rest_jaw: extra jaw in silences
#   tremble: chin/jaw tremble | suppress: blink suppression | arc: flat / build / voice (follows loudness)
#   bob: (deg, Hz) rhythmic head motion (breathing, running) | roll: head tilt bias deg
# =====================================================================================
_P = dict
EMOTION_PRESETS = {
    "calm": _P(offsets={"browInnerUp": 0.04, "mouthSmile": 0.03, "eyeSquint": 0.04},
               blink=(2.8, 5.0), sacc_hold=(0.5, 1.2), sacc_amp=(2.0, 4.0), mobility=1.0, effort=1.0),
    "dry": _P(offsets={"browDown": 0.18, "mouthPress": 0.22, "eyeSquint": 0.15},
              blink=(2.6, 4.5), sacc_hold=(0.6, 1.3), sacc_amp=(2.0, 3.5), mobility=0.8, effort=0.95),
    "smirk": _P(offsets={"mouthSmileLeft": 0.22, "mouthSmileRight": 0.10, "cheekSquintLeft": 0.12,
                         "eyeSquintLeft": 0.08},
                blink=(2.6, 4.5), mobility=1.0),
    "firm": _P(offsets={"browDown": 0.45, "mouthPress": 0.35, "eyeSquint": 0.22, "cheekSquint": 0.08,
                        "noseSneer": 0.06, "mouthFrown": 0.08},
               blink=(3.0, 5.0), sacc_hold=(0.7, 1.4), sacc_amp=(1.5, 3.0), mobility=1.0, effort=1.05),
    "excited": _P(offsets={"browInnerUp": 0.30, "browOuterUp": 0.35, "eyeWide": 0.22, "mouthSmile": 0.22,
                           "cheekSquint": 0.08},
                  blink=(2.0, 3.5), sacc_hold=(0.3, 0.8), sacc_amp=(2.0, 5.0), mobility=1.3, effort=1.05, arc="voice"),
    "awe": _P(offsets={"browInnerUp": 0.55, "browOuterUp": 0.50, "eyeWide": 0.35, "mouthSmile": 0.14},
              blink=(4.0, 7.0), sacc_hold=(0.6, 1.4), sacc_amp=(2.0, 5.0), gaze=(0.0, 4.0), mobility=0.7,
              effort=0.85, rest_jaw=0.07, arc="build"),
    "mesmerized": _P(offsets={"browInnerUp": 0.35, "browOuterUp": 0.22, "eyeWide": 0.18, "mouthSmile": 0.08},
                     blink=(5.0, 8.0), sacc_hold=(1.0, 2.0), sacc_amp=(2.0, 3.0), gaze=(0.0, 1.0), mobility=0.4,
                     effort=0.85, rest_jaw=0.06),
    "nervous": _P(offsets={"browInnerUp": 0.45, "browDown": 0.05, "mouthSmile": 0.14, "mouthFrown": 0.10,
                           "mouthStretch": 0.12, "mouthPress": 0.12, "eyeWide": 0.10},
                  blink=(1.5, 2.5), sacc_hold=(0.3, 0.7), sacc_amp=(2.0, 5.0), mobility=1.15, arc="voice"),
    "nervous_laugh": _P(offsets={"mouthSmile": 0.45, "cheekSquint": 0.35, "eyeSquint": 0.25, "browInnerUp": 0.35,
                                 "mouthStretch": 0.10},
                        blink=(1.5, 2.5), sacc_hold=(0.3, 0.7), sacc_amp=(2.0, 4.0), mobility=1.3,
                        rest_jaw=0.08, arc="voice"),
    "puzzled": _P(offsets={"browDownLeft": 0.35, "browOuterUpRight": 0.50, "browInnerUp": 0.15,
                           "eyeSquintLeft": 0.18, "mouthPressLeft": 0.15, "mouthFrownRight": 0.10,
                           "mouthSmileLeft": 0.04, "noseSneerLeft": 0.05},
                  blink=(2.5, 4.0), sacc_hold=(0.5, 1.1), sacc_amp=(2.0, 4.0), mobility=0.9, roll=2.0),
    "hushed": _P(offsets={"browInnerUp": 0.20, "eyeWide": 0.08, "mouthPress": 0.06, "browDown": 0.05},
                 blink=(2.5, 4.0), sacc_hold=(0.4, 1.0), sacc_amp=(2.0, 4.0), mobility=0.6, effort=0.65),
    "fear": _P(offsets={"browInnerUp": 0.75, "browOuterUp": 0.35, "browDown": 0.08, "eyeWide": 0.55,
                        "mouthStretch": 0.28, "mouthFrown": 0.06},
               blink=(1.5, 2.5), sacc_hold=(0.2, 0.6), sacc_amp=(2.0, 6.0), mobility=0.45, effort=0.9,
               rest_jaw=0.06, tremble=0.25, arc="build"),
    "fear_freeze": _P(offsets={"browInnerUp": 0.60, "browOuterUp": 0.30, "eyeWide": 0.50, "mouthStretch": 0.18,
                               "mouthPress": 0.12},
                      blink=(4.5, 7.0), sacc_hold=(1.0, 2.0), sacc_amp=(1.0, 2.0), mobility=0.0, effort=0.85,
                      rest_jaw=0.04),
    "terror": _P(offsets={"browInnerUp": 0.90, "browOuterUp": 0.75, "eyeWide": 0.90, "mouthStretch": 0.45,
                          "noseSneer": 0.10, "mouthUpperUp": 0.15},
                 blink=(3.0, 5.0), suppress=1.0, sacc_hold=(0.25, 0.6), sacc_amp=(2.0, 6.0), mobility=1.4,
                 effort=1.35, rest_jaw=0.15, arc="voice"),
    "shout": _P(offsets={"browDown": 0.45, "browInnerUp": 0.45, "eyeWide": 0.55, "mouthStretch": 0.35,
                         "noseSneer": 0.18, "mouthUpperUp": 0.20, "cheekSquint": 0.10},
                blink=(3.0, 5.0), suppress=1.0, sacc_hold=(0.4, 0.9), sacc_amp=(2.0, 4.0), mobility=1.5,
                effort=1.35, rest_jaw=0.10, arc="voice"),
    "alarm": _P(offsets={"browDown": 0.35, "browInnerUp": 0.35, "eyeWide": 0.40, "mouthStretch": 0.12,
                         "mouthPress": 0.06},
                blink=(3.0, 5.0), suppress=0.6, sacc_hold=(0.4, 0.9), sacc_amp=(2.0, 4.0), mobility=1.3,
                effort=1.15, arc="voice"),
    "pain": _P(offsets={"browDown": 0.70, "browInnerUp": 0.55, "eyeSquint": 0.85, "eyeBlink": 0.45,
                        "cheekSquint": 0.75, "noseSneer": 0.70, "mouthStretch": 0.70, "mouthUpperUp": 0.45,
                        "mouthLowerDown": 0.30, "mouthFrown": 0.25},
               blink=(3.0, 5.0), suppress=1.0, sacc_hold=(1.5, 3.0), sacc_amp=(0.5, 1.0), mobility=1.6,
               effort=1.5, rest_jaw=0.20, tremble=0.45, arc="voice"),
    "sobbing": _P(offsets={"browInnerUp": 0.85, "browDown": 0.25, "mouthFrown": 0.60, "eyeSquint": 0.45,
                           "cheekSquint": 0.30, "mouthStretch": 0.22, "mouthUpperUp": 0.10, "noseSneer": 0.12},
                  blink=(1.2, 2.2), sacc_hold=(0.4, 1.0), sacc_amp=(2.0, 4.0), gaze=(0.0, -6.0), mobility=0.9,
                  rest_jaw=0.06, tremble=1.0, arc="voice"),
    "hysterical": _P(offsets={"browInnerUp": 0.90, "browOuterUp": 0.35, "browDown": 0.10, "eyeWide": 0.45,
                              "mouthStretch": 0.40, "mouthFrown": 0.35, "noseSneer": 0.10},
                     blink=(1.5, 2.5), sacc_hold=(0.2, 0.5), sacc_amp=(3.0, 6.0), mobility=1.4, effort=1.25,
                     rest_jaw=0.12, tremble=0.6, arc="voice"),
    "breathless": _P(offsets={"browInnerUp": 0.30, "mouthStretch": 0.10, "eyeSquint": 0.08},
                     blink=(2.0, 3.5), mobility=1.0, rest_jaw=0.13, bob=(0.6, 0.9)),
    "strain": _P(offsets={"browDown": 0.55, "browInnerUp": 0.15, "eyeSquint": 0.40, "cheekSquint": 0.35,
                          "noseSneer": 0.30, "mouthStretch": 0.32, "mouthUpperUp": 0.15},
                 blink=(2.0, 3.5), sacc_hold=(0.4, 0.9), sacc_amp=(2.0, 4.0), mobility=1.3, effort=1.25,
                 rest_jaw=0.10, arc="voice"),
    "weak": _P(offsets={"browInnerUp": 0.40, "eyeBlink": 0.25, "eyeSquint": 0.15, "mouthFrown": 0.15},
               blink=(2.5, 4.5), sacc_hold=(0.8, 1.6), sacc_amp=(1.0, 3.0), mobility=0.5, effort=0.65,
               rest_jaw=0.08),
    "stunned": _P(offsets={"browInnerUp": 0.55, "browOuterUp": 0.25, "eyeWide": 0.30, "mouthFrown": 0.10},
                  blink=(4.0, 7.0), sacc_hold=(1.0, 2.0), sacc_amp=(1.0, 3.0), mobility=0.3, effort=0.7,
                  rest_jaw=0.09),
}
PRESET_DEFAULTS = dict(offsets={}, blink=(2.8, 5.0), sacc_hold=(0.5, 1.2), sacc_amp=(2.0, 4.0), gaze=(0.0, 0.0),
                       mobility=1.0, effort=1.0, rest_jaw=0.0, tremble=0.0, suppress=0.0, arc="flat",
                       bob=(0.0, 0.0), roll=0.0)
for _n, _p in EMOTION_PRESETS.items():
    for _k, _v in PRESET_DEFAULTS.items():
        _p.setdefault(_k, _v)
    _p["offsets"] = expand(_p["offsets"])
PRESET_NAMES = list(EMOTION_PRESETS)

# Tag chunk (tag text split on commas) -> presets. First matching rule per chunk wins, so order matters.
#   ("kw1|kw2|...", [(preset, intensity)], explicit vocal effort or None, is_event)
# Keywords are substrings of the lowercased chunk, with ё folded to е (English and Russian).
# An "event" tag (laugh, exhale) only lasts until the next words start + 0.4 s, then the previous state returns.
TAG_RULES = [
    ("кричит от боли|крик боли|screams in pain|screaming in pain|cries out in pain|howls in pain",
     [("pain", 1.0)], 1.5, False),
    ("в ужасе|ужас|terrified|in terror|horrified|horror", [("terror", 1.0)], None, False),
    ("кричит|кричат|крик|орет|scream|shout|yell|cries out|bellow", [("shout", 1.0)], 1.35, False),
    ("истерик|hysteric", [("hysterical", 0.9)], 1.2, False),
    ("паник|panic", [("hysterical", 0.6), ("fear", 0.5)], 1.15, False),
    ("всхлип|плач|рыда|сквозь слезы|sob|crying|cries|in tears|tearful|weeping", [("sobbing", 0.9)], None, False),
    ("задыхаясь|запыхавшись|out of breath|breathless|panting", [("breathless", 0.5)], None, False),
    ("хрипло|hoarse|raspy|strained|straining", [("strain", 0.8)], 1.25, False),
    ("на бегу|while running|running", [("strain", 0.4)], None, False),
    ("дрожащим шепотом|trembling whisper|shaky whisper|quavering whisper", [("fear", 0.75)], 0.65, False),
    ("ошеломленным шепотом|stunned whisper", [("stunned", 0.85)], 0.65, False),
    ("ошеломленно|stunned|dazed|in shock|shocked", [("stunned", 0.75)], None, False),
    ("потрясенно|astonished|amazed|in amazement", [("awe", 0.9), ("stunned", 0.4)], None, False),
    ("со страхом и восторгом|fear and wonder|fearful awe", [("awe", 0.55), ("fear", 0.5)], None, False),
    ("восторженно|восхищ|in awe|awed|awestruck|in wonder|wonderstruck|rapturous", [("awe", 0.8)], None, False),
    ("заворож|mesmeri|spellbound|hypnoti", [("mesmerized", 0.85)], None, False),
    ("зачарован|enchanted|entranced", [("mesmerized", 0.9)], None, False),
    ("вслушиваясь|listening|listens", [("puzzled", 0.35), ("hushed", 0.5)], None, False),
    ("очень тихо|very quietly|very softly", [("fear", 0.45), ("firm", 0.35), ("hushed", 0.4)], 0.7, False),
    ("едва слышно|barely audible|barely a whisper|inaudibl", [("stunned", 0.5), ("fear", 0.35)], 0.6, False),
    ("полушепот|half-whisper|half whisper|under his breath|under her breath|under breath|sotto voce",
     [("hushed", 0.3)], 0.75, False),
    ("шепот|шепч|whisper", [("hushed", 0.45)], 0.7, False),
    ("твердо|firmly|resolute|determined|decisive", [("firm", 0.6)], None, False),
    ("жестко|harshly|harsh|sternly|stern|commanding", [("firm", 0.9)], 1.05, False),
    ("резко|sharply|abruptly|snaps|alarmed|urgent", [("alarm", 0.8)], 1.2, False),
    ("медленно|slowly", [], None, False),
    ("нервный смешок|nervous laugh|nervous chuckle|nervous giggle|awkward laugh", [("nervous_laugh", 0.9)],
     None, True),
    ("нервно-весело|nervously cheerful|forced cheer", [("nervous", 0.6), ("excited", 0.3)], None, False),
    ("неуверенно|uncertain|unsure|hesitant|hesitat", [("puzzled", 0.4), ("nervous", 0.4)], None, False),
    ("озадаченно|puzzled|confused|perplexed|baffled|wondering", [("puzzled", 0.75)], None, False),
    ("взволнованно|excited|eagerly|eager|thrilled", [("excited", 0.7)], 1.05, False),
    ("сухо|dryly|drily|deadpan", [("dry", 0.65)], None, False),
    ("коротко|curtly|tersely|briefly", [("dry", 0.5)], None, False),
    ("выдыхает|exhales|exhale|breathes out|sigh of relief", [("calm", 0.5)], None, True),
    ("вздыхает|sighs|sigh", [("weak", 0.3)], None, True),
    ("тяжело дышит|breathing heavily|breathes heavily|heavy breathing|pants", [("breathless", 0.8)], None, True),
    ("ахает|вздрагивает|gasps|gasp|inhales sharply", [("breathless", 0.6), ("alarm", 0.3)], None, True),
    ("смеется|смеясь|смех|хохоч|смешок|laughs|laughing|laugh|chuckle|giggle",
     [("nervous_laugh", 0.8), ("excited", 0.3)], None, True),
    ("нервно|nervous|anxious|uneasy|tense", [("nervous", 0.7)], None, False),
    ("страх|страшно|испуг|afraid|scared|fearful|frightened|fear", [("fear", 0.7)], None, False),
    ("боль|больно|in pain|pained|hurt|wincing", [("pain", 0.6)], 1.1, False),
    ("негромко|quietly|softly|low voice", [("calm", 0.5)], 0.85, False),
    ("тихо|hushed|hush", [("hushed", 0.35)], 0.8, False),
    ("ровно|evenly|steadily|steady", [("calm", 0.6)], None, False),
    ("спокойно|calmly|calm|relaxed|gently", [("calm", 0.6)], None, False),
    ("слабо|weakly|weak|faintly|feebly|exhausted", [("weak", 0.8)], 0.7, False),
    ("с хрипом|wheez|rasping", [("weak", 0.4)], None, False),
    ("усмех|ухмыл|иронично|саркастично|smirk|wry|ironic|sarcastic", [("smirk", 0.7)], None, False),
    ("радостно|весело|с улыбкой|happily|happy|cheerful|joyful|smiling", [("excited", 0.5), ("smirk", 0.3)],
     None, False),
    ("злобно|сердито|в ярости|angrily|angry|furious|rage", [("firm", 0.9), ("strain", 0.3)], 1.1, False),
    ("грустно|печально|sadly|mournful|sorrowful", [("weak", 0.5), ("sobbing", 0.25)], None, False),
    ("удивленно|удивлен|surprised|in surprise", [("excited", 0.4), ("stunned", 0.4)], None, False),
]
# Direction remark (acting note outside the text) -> presets for the whole line + optional modifiers.
DIRECTION_RULES = [
    ("с тенью улыбки|тень улыбки|wry smile|hint of a smile", [("smirk", 0.6)], []),
    ("командно|commanding|giving orders", [("firm", 0.55)], []),
    ("деловито|businesslike|matter-of-fact", [("firm", 0.25)], []),
    ("запыхавшись|out of breath|winded", [("breathless", 0.35)], []),
    ("не находит себе места|суетится|restless|fidgeting|can't stand still", [("excited", 0.2)], []),
    ("оглядывается|озирается|looks around|glances around|looking around", [], ["look_around"]),
    ("смотрит вниз|опускает глаза|смотрит на экран|смотрит на прибор|смотрит в телефон|"
     "looks down|looking down|eyes down|looks at the screen|looks at the device|reads from",
     [], ["look_down"]),
    ("не договаривает|обрывает фразу|trails off", [("nervous", 0.25)], []),
    ("не по себе|ill at ease|uneasy", [("nervous", 0.3)], []),
    ("теряет самообладание|loses composure|losing composure", [("stunned", 0.3)], []),
    ("берёт себя в руки|regains composure|pulls himself together|pulls herself together", [("firm", 0.3)], []),
    ("замерло|замерла|замер,|frozen|freezes|stock-still", [("fear_freeze", 0.6)], []),
    ("в шоке|in shock|shocked", [("stunned", 0.6)], []),
    ("на бегу|while running|running|on the run", [], ["running"]),
    ("не верит|disbelief|can't believe|cannot believe", [("stunned", 0.3)], []),
]
MODIFIERS = {   # applied on top of the presets
    "look_down": dict(gaze=(0.0, -8.0), head_pitch=-2.0),
    "look_around": dict(sacc_amp_mul=1.6, sacc_hold_mul=0.7, head_drift_mul=2.5),
    "running": dict(bob=(0.9, 2.6), roll_bob=0.5),
}
DEFAULT_PRESETS = [("calm", 0.4)]          # used when nothing else applies
# tags whose audio is a breath / laugh event (words next to them extend less into that audio)
BREATH_TAG_RE = re.compile(r"выдыха|вздыха|дыш|смеш|смех|смее|смея|хохоч|всхлип|задыха|плач|рыда|ахает|"
                           r"exhal|inhal|sigh|breath|pant|gasp|laugh|chuckl|giggl|sob|crying")
LAUGH_TAG_RE = re.compile(r"смеш|смех|смее|смея|хохоч|laugh|chuckl|giggl")   # nonverbal audio near it = laugh

COMBINING_ACUTE = "\u0301"


def fold(s):
    return s.lower().replace("ё", "е")


_TAG_RULES = [([fold(k) for k in kw.split("|")], pr, eff, ev) for kw, pr, eff, ev in TAG_RULES]
_DIRECTION_RULES = [([fold(k) for k in kw.split("|")], pr, md) for kw, pr, md in DIRECTION_RULES]
for _rules in (_TAG_RULES, _DIRECTION_RULES):
    for _r in _rules:
        for _name, _i in _r[1]:
            assert _name in EMOTION_PRESETS, _name

# =====================================================================================
# CONVENTIONS (written into every JSON and index.json)
# =====================================================================================
CONVENTIONS = {
    "time_base": "curve index i <-> t = i/fps - lead_in seconds relative to the first audio sample; "
                 "words, phonemes, visemes and nonverbal use plain audio seconds",
    "shapes": "blendshape curves are 0..1 weights with ARKit names",
    "left_right": "Left/Right = the character's own left/right (ARKit convention; mirrored on screen when the "
                  "character faces the camera)",
    "angles": "eyeYaw eyePitch headPitch headYaw headRoll are in degrees",
    "eyeYaw": "+ = both eyes turn to the character's left",
    "eyePitch": "+ = eyes look up",
    "headPitch": "+ = chin up (a nod is negative); additive, soft-clipped to +-4 deg",
    "headYaw": "+ = head turns to the character's left; additive, soft-clipped to +-4 deg",
    "headRoll": "+ = top of the head tilts toward the character's left shoulder; additive, soft-clipped to +-4 deg",
    "mouthClose": "sealed-lips scale: 1 on p/b/m with jawOpen clamped to <= 0.02 at the same time. "
                  "Strict ARKit rigs expect mouthClose <= jawOpen: drive them with min(mouthClose, jawOpen) "
                  "or rerun with --strict-arkit-close",
    "mouthClose_form": "sealed",
}

# =====================================================================================
# SMALL HELPERS
# =====================================================================================


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def crit_damp(x, rate, t90, zero_phase=False):
    """Critically damped 2nd-order low-pass (two identical 1-pole stages). t90 = 90% step time."""
    x = np.asarray(x, dtype=np.float64)
    if t90 <= 0 or len(x) < 2:
        return x.copy()
    tau = t90 / 3.89
    a = 1.0 - math.exp(-1.0 / (rate * tau))

    def one(sig):
        for _ in range(2):
            sig, _z = lfilter([a], [1.0, a - 1.0], sig, zi=[(1.0 - a) * sig[0]])
        return sig

    y = one(x)
    if zero_phase:
        y = one(y[::-1])[::-1]
    return y


def soft_knee(x, knee, top):
    x = np.asarray(x, dtype=np.float64)
    over = x > knee
    y = x.copy()
    y[over] = knee + (top - knee) * np.tanh((x[over] - knee) / (top - knee))
    return y


def seed_for(key, salt=""):
    return zlib.crc32((key + salt).encode("utf-8")) & 0xFFFFFFFF


def levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def collapse_runs(s):
    return re.sub(r"(.)\1{2,}", r"\1\1", s)


def similarity(a, b):
    a, b = collapse_runs(a), collapse_runs(b)
    if not a or not b:
        return 0.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def base_lang(lang):
    return (lang or "en").lower().replace("_", "-").split("-")[0]


def norm_word(w, lang="ru"):
    """Alignment key: lowercase, ё -> е, letters only, digits spoken (ru/en)."""
    w = w.lower().replace("ё", "е").replace(COMBINING_ACUTE, "")
    digits = DIGIT_WORDS.get(lang, {})
    if w.strip() in digits:
        return digits[w.strip()]
    if lang == "ru":
        return re.sub(r"[^а-я]", "", w)
    w = unicodedata.normalize("NFKD", w)
    return "".join(ch for ch in w if ch.isalnum() and not unicodedata.combining(ch))


def die(msg, code=2):
    sys.stderr.write("lipsync.py: " + msg + "\n")
    sys.exit(code)


# =====================================================================================
# TEXT
# =====================================================================================
_WCH = r"(?:[^\W_]|\u0301)"
TOKEN_RE = re.compile(r"\[([^\]]+)\]|(" + _WCH + r"+(?:[-'’]" + _WCH + r"+)*)")
PHRASE_PUNCT_RE = re.compile(r"[.!?…,—;:¿¡。！？]")


def parse_text(raw, lang, direction=""):
    """Script line -> words (with punctuation flags) and [tags] with the index of the word they precede."""
    raw = unicodedata.normalize("NFC", (raw or "").strip())
    words, tags = [], []
    toks = list(TOKEN_RE.finditer(raw))
    for k, t in enumerate(toks):
        if t.group(1) is not None:
            tags.append({"tag": t.group(1).strip(), "word_index": len(words)})
            continue
        w = t.group(2)
        plain = w.replace(COMBINING_ACUTE, "")
        nxt = toks[k + 1].start() if k + 1 < len(toks) else len(raw)
        punct = raw[t.end():nxt].strip()
        words.append({"w": plain, "norm": norm_word(w, lang), "low": w.lower(),
                      "caps": plain.isupper() and len(plain.replace("-", "")) > 1
                      and not re.fullmatch(r"[^\W\d_](?:-[^\W\d_])*", plain),
                      "punct": punct, "question": "?" in punct or "？" in punct,
                      "phrase_end": bool(PHRASE_PUNCT_RE.search(punct)) or k + 1 == len(toks)})
    text = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]\s*", "", raw)).strip().replace(COMBINING_ACUTE, "")
    return {"raw": raw, "text": text, "tags": tags, "words": words, "direction": (direction or "").strip()}


# =====================================================================================
# AUDIO
# =====================================================================================


def _to_float_mono(x):
    x = np.asarray(x)
    if x.dtype == np.uint8:
        x = (x.astype(np.float64) - 128.0) / 127.0
    elif np.issubdtype(x.dtype, np.integer):
        x = x.astype(np.float64) / float(np.iinfo(x.dtype).max)
    else:
        x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x


def _read_wave_stdlib(path):
    with wave.open(str(path), "rb") as f:
        sr, ch, sw, n = f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()
        raw = f.readframes(n)
    if sw == 1:
        x = np.frombuffer(raw, dtype=np.uint8)
    elif sw == 2:
        x = np.frombuffer(raw, dtype="<i2")
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = (v.astype(np.float64) / float((1 << 23) - 1))
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4")
    else:
        raise ValueError("unsupported sample width {}".format(sw))
    if ch > 1:
        x = np.asarray(x).reshape(-1, ch)
    return sr, _to_float_mono(x)


def _read_ffmpeg(path, sr=48000):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found")
    r = subprocess.run([exe, "-v", "error", "-nostdin", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr),
                        "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip())
    return sr, np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)


def load_audio(path):
    """-> (sample_rate, mono float64). WAV via scipy (or the wave module); other formats via soundfile or ffmpeg."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError("audio not found: {}".format(path))
    errors = []
    if path.suffix.lower() in (".wav", ".wave"):
        try:
            from scipy.io import wavfile
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sr, x = wavfile.read(str(path))
            return sr, _to_float_mono(x)
        except Exception as e:
            errors.append("scipy: {}".format(e))
        try:
            return _read_wave_stdlib(path)
        except Exception as e:
            errors.append("wave: {}".format(e))
    try:
        import soundfile
        x, sr = soundfile.read(str(path), dtype="float64", always_2d=False)
        return sr, _to_float_mono(x)
    except ImportError:
        errors.append("soundfile not installed (pip install soundfile)")
    except Exception as e:
        errors.append("soundfile: {}".format(e))
    try:
        return _read_ffmpeg(path)
    except Exception as e:
        errors.append("ffmpeg: {}".format(e))
    raise RuntimeError("cannot decode {} ({}). Use a PCM WAV, or install soundfile (pip install soundfile) "
                       "or ffmpeg.".format(path, "; ".join(errors)))


def rms_track(x, sr, times, win):
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    c = np.round(np.asarray(times) * sr).astype(np.int64)
    h = max(1, int(win * sr / 2))
    a = np.clip(c - h, 0, len(x))
    b = np.clip(c + h, 0, len(x))
    n = np.maximum(b - a, 1)
    return np.sqrt(np.maximum(cs[b] - cs[a], 0.0) / n)


def runs_of(mask):
    """[(start, end_exclusive)] of True runs."""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]])
    d = np.diff(m.astype(np.int8))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def close_gaps(mask, n):
    mask = mask.copy()
    for s, e in runs_of(~mask):
        if s > 0 and e < len(mask) and e - s <= n:
            mask[s:e] = True
    return mask


def drop_islands(mask, n):
    mask = mask.copy()
    for s, e in runs_of(mask):
        if e - s < n:
            mask[s:e] = False
    return mask


def analyse_audio(path):
    sr, x = load_audio(path)
    if len(x) < sr * 0.05:
        raise ValueError("audio is shorter than 50 ms: {}".format(path))
    dur = len(x) / sr
    tv = np.arange(int(dur * VAD_RATE) + 1) / VAD_RATE
    db = 20 * np.log10(rms_track(x, sr, tv, VAD_WIN) + 1e-9)
    valid = db > -90
    p95 = float(np.percentile(db[valid], 95)) if valid.any() else -90.0
    noise = float(np.percentile(db[valid], 3)) if valid.any() else -90.0
    thr = max(p95 + VAD_REL_DB, min(noise + VAD_NOISE_DB, p95 - 26.0), VAD_FLOOR_DB)
    voiced = drop_islands(close_gaps(db > thr, int(VAD_CLOSE_GAP * VAD_RATE)), int(VAD_MIN_ISLAND * VAD_RATE))
    level = float(np.clip(10 ** ((p95 - LEVEL_REF_DB) / 40.0), 0.6, 1.35))
    return {"sr": sr, "x": x, "dur": dur, "vad_t": tv, "vad_db": db, "voiced": voiced, "p95_db": p95,
            "thr_db": thr, "level_factor": level}


def loudness_at(audio, times, sigma_s=ENV_SMOOTH, rate=None):
    """Per-clip normalised loudness 0..1 (dB scale between VAD threshold and voiced p95)."""
    times = np.asarray(times, dtype=np.float64)
    db = 20 * np.log10(rms_track(audio["x"], audio["sr"], np.clip(times, 0, audio["dur"]), ENV_WIN) + 1e-9)
    vdb = audio["vad_db"][audio["voiced"]]
    ref = float(np.percentile(vdb, 95)) if len(vdb) else audio["p95_db"]
    lo = audio["thr_db"]
    env = np.clip((db - lo) / max(ref - lo, 1.0), 0.0, 1.0)
    env[(times < 0) | (times > audio["dur"])] = 0.0
    if sigma_s and len(times) > 1:
        r = rate or 1.0 / max(times[1] - times[0], 1e-6)
        env = gaussian_filter1d(env, sigma_s * r, mode="nearest")
    return np.clip(env, 0.0, 1.0)


# =====================================================================================
# WORD TIMING: whisper / words file / VAD guess
# =====================================================================================
_MODELS = {}


def _have_module(name):
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def pick_whisper(pref):
    """'auto' -> mlx on Apple Silicon if installed, else openai-whisper, else None (VAD fallback)."""
    have_openai, have_mlx = _have_module("whisper"), _have_module("mlx_whisper")
    if pref == "none":
        return None
    if pref == "openai":
        if not have_openai:
            die("openai-whisper is not installed.\n  pip install -U openai-whisper   (needs torch; ffmpeg recommended)")
        return "openai"
    if pref == "mlx":
        if not have_mlx:
            die("mlx-whisper is not installed (Apple Silicon only).\n  pip install -U mlx-whisper")
        return "mlx"
    if have_mlx and sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx"
    if have_openai:
        return "openai"
    return None


def mlx_repo(model):
    if "/" in model:
        return model
    return MLX_REPOS.get(model, "mlx-community/whisper-{}-mlx".format(model))


def _audio_16k(audio):
    sr = int(audio["sr"])
    g = math.gcd(16000, sr)
    return resample_poly(audio["x"], 16000 // g, sr // g).astype(np.float32)


def transcribe(backend, wav, audio, lang, model):
    warnings.filterwarnings("ignore")
    src = str(wav) if shutil.which("ffmpeg") else _audio_16k(audio)
    # No initial prompt on purpose: prompting with the script made whisper hallucinate on screams.
    if backend == "openai":
        import whisper
        if not hasattr(whisper, "load_model"):
            die("the installed 'whisper' module is not openai-whisper.\n  pip install -U openai-whisper")
        key = ("openai", model)
        if key not in _MODELS:
            dev = "cpu"
            try:
                import torch
                if torch.cuda.is_available():
                    dev = "cuda"
            except Exception:
                pass
            print("  loading openai-whisper {} on {}".format(model, dev), flush=True)
            _MODELS[key] = (whisper.load_model(model, device=dev), dev)
        m, dev = _MODELS[key]
        r = m.transcribe(src, language=lang, word_timestamps=True, fp16=(dev == "cuda"),
                         condition_on_previous_text=False, temperature=0.0)
    else:
        import mlx_whisper
        r = mlx_whisper.transcribe(src, path_or_hf_repo=mlx_repo(model), language=lang, word_timestamps=True,
                                   condition_on_previous_text=False, temperature=0.0)
    words = [{"w": w["word"].strip(), "t0": round(float(w["start"]), 3), "t1": round(float(w["end"]), 3),
              "p": round(float(w.get("probability", 0.0)), 3)}
             for s in r.get("segments", []) for w in s.get("words", [])]
    return {"text": r.get("text", "").strip(), "words": words}


def whisper_words(key, wav, audio, lang, backend, model, cache_dir, force=False):
    """Whisper word timestamps, cached in cache_dir/<key>.json (valid while size, mtime, model, lang match)."""
    st = wav.stat()
    cache = cache_dir / (re.sub(r"[^\w.-]", "_", key) + ".json")
    sig = {"backend": backend, "model": model, "lang": lang, "audio_size": st.st_size,
           "audio_mtime": int(st.st_mtime)}
    if cache.exists() and not force:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if all(c.get(k) == v for k, v in sig.items()):
                return c
        except Exception:
            pass
    r = transcribe(backend, wav, audio, lang, model)
    c = dict(sig, audio=wav.name, text=r["text"], words=r["words"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    return c


def load_words_file(path):
    """[{w,t0,t1}] | [{word,start,end}] | {"words": [...]} | whisper result with segments -> {text, words}."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        die("cannot read words file {}: {}".format(path, e))
    text = ""
    if isinstance(d, dict):
        text = d.get("text", "") if isinstance(d.get("text", ""), str) else ""
        if "words" in d:
            d = d["words"]
        elif "segments" in d:
            d = [w for s in d["segments"] for w in s.get("words", [])]
        else:
            die("words file {}: expected a list or an object with 'words'".format(path))
    out = []
    for w in d:
        word = w.get("w", w.get("word", w.get("text")))
        t0 = w.get("t0", w.get("start"))
        t1 = w.get("t1", w.get("end"))
        if word is None or t0 is None or t1 is None:
            continue
        out.append({"w": str(word).strip(), "t0": float(t0), "t1": float(t1)})
    return {"text": text or " ".join(w["w"] for w in out), "words": out}


def vad_word_guess(line, audio, phone_counts):
    """No word timing: spread the script words over the voiced spans by phoneme count (DP, breaks at pauses).
    A voiced span may stay empty (it becomes a nonverbal event); that is cheap where a breath / laugh tag sits.
    The speech rate is re-estimated from the spans that received words (two passes)."""
    dur = audio["dur"]
    voiced = close_gaps(audio["voiced"], int(VAD_GUESS_GAP * VAD_RATE))
    spans = [(s / VAD_RATE, e / VAD_RATE) for s, e in runs_of(voiced)] or [(0.0, dur)]
    n, m = len(line["words"]), len(spans)
    if n == 0:
        return {"text": "", "words": []}
    wts = [max(c, 1) for c in phone_counts]
    cum = np.concatenate([[0.0], np.cumsum(wts)])
    total = max(sum(e - s for s, e in spans), 1e-3)
    events = {t["word_index"] for t in line["tags"] if BREATH_TAG_RE.search(fold(t["tag"]))}
    INF = 1e18

    def span_len(q):
        return spans[q][1] - spans[q][0]

    def solve(rate):
        # state (span j, word i, previous span left empty?) -> cost
        D = np.full((m + 1, n + 1, 2), INF)
        D[0, 0, 0] = 0.0
        back = {}
        for j in range(m):
            for i in range(n + 1):
                for e in (0, 1):
                    cur = D[j, i, e]
                    if cur >= INF:
                        continue
                    c = cur + span_len(j) / total                  # leave span j without words
                    if c < D[j + 1, i, 1]:
                        D[j + 1, i, 1] = c
                        back[(j + 1, i, 1)] = (j, i, e)
                    # an empty span right before the words of a breath / laugh tag is that event: refund
                    refund = (1 - VAD_GUESS_EVENT) * span_len(j - 1) / total if (e and i in events) else 0.0
                    for j2 in range(j + 1, min(m, j + 4) + 1):     # a group may cover up to 4 voiced spans
                        d = sum(span_len(q) for q in range(j, j2))
                        for i2 in range(i + 1, n + 1):
                            ratio = abs(math.log(max(d, 1e-3) / max((cum[i2] - cum[i]) * rate, 1e-3)))
                            cost = d / total * max(0.0, ratio - math.log(VAD_GUESS_RATE_TOL)) \
                                + VAD_GUESS_JOIN * (j2 - j - 1) - refund
                            if i2 < n and j2 < m:                  # this group ends at a pause
                                cost += VAD_GUESS_BREAK[1] if line["words"][i2 - 1]["phrase_end"] \
                                    else VAD_GUESS_BREAK[0]
                            if cur + cost < D[j2, i2, 0]:
                                D[j2, i2, 0] = cur + cost
                                back[(j2, i2, 0)] = (j, i, e)
        end_refund = (1 - VAD_GUESS_EVENT) * span_len(m - 1) / total if n in events else 0.0
        key = (m, n, 0) if D[m, n, 0] <= D[m, n, 1] - end_refund else (m, n, 1)
        groups = []
        while key != (0, 0, 0):
            j, i, e = back[key]
            groups.append((j, key[0], i, key[1]))
            key = (j, i, e)
        return groups[::-1]

    def voiced_time(j, j2, x):
        """Position x (s of voiced time from the start of span j) -> clip time, skipping the pauses."""
        for q in range(j, j2):
            L = spans[q][1] - spans[q][0]
            if x <= L or q == j2 - 1:
                return spans[q][0] + min(x, L)
            x -= L
        return spans[j2 - 1][1]

    groups = solve(total / cum[-1])
    for _ in range(2):
        used = sum(sum(spans[q][1] - spans[q][0] for q in range(j, j2)) for j, j2, i, i2 in groups if i2 > i)
        groups = solve(max(used, 1e-3) / cum[-1])
    out = []
    for j, j2, i, i2 in groups:
        if i2 <= i:
            continue
        d = sum(spans[q][1] - spans[q][0] for q in range(j, j2))
        edges = [voiced_time(j, j2, x) for x in d * (cum[i:i2 + 1] - cum[i]) / (cum[i2] - cum[i])]
        edges[0], edges[-1] = spans[j][0], spans[j2 - 1][1]
        for k in range(i, i2):
            out.append({"w": line["words"][k]["w"], "t0": float(edges[k - i]), "t1": float(edges[k - i + 1])})
    return {"text": " ".join(w["w"] for w in line["words"]), "words": out}


# =====================================================================================
# ALIGNMENT  script words <- timed words
# =====================================================================================


def dp_align(S, W):
    """Needleman-Wunsch style with 1:1, 1:2, 2:1 and gaps. Returns list of (script_idx_list, whisper_idx_list, sim)."""
    n, m = len(S), len(W)
    NEG = -1e9
    D = np.full((n + 1, m + 1), NEG)
    B = {}
    D[0, 0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            cur = D[i, j]
            if cur <= NEG / 2:
                continue
            cand = []
            if i < n and j < m:
                cand.append((i + 1, j + 1, 2 * similarity(S[i], W[j]) - 0.6))
            if i < n and j + 1 < m:
                cand.append((i + 1, j + 2, 2 * similarity(S[i], W[j] + W[j + 1]) - 0.9))
            if i + 1 < n and j < m:
                cand.append((i + 2, j + 1, 2 * similarity(S[i] + S[i + 1], W[j]) - 0.9))
            if i < n:
                cand.append((i + 1, j, -0.5))
            if j < m:
                cand.append((i, j + 1, -0.35))
            for ni, nj, sc in cand:
                if cur + sc > D[ni, nj]:
                    D[ni, nj] = cur + sc
                    B[(ni, nj)] = (i, j)
    path = []
    i, j = n, m
    while (i, j) != (0, 0):
        pi, pj = B[(i, j)]
        si, wi = list(range(pi, i)), list(range(pj, j))
        sim = similarity("".join(S[k] for k in si), "".join(W[k] for k in wi)) if si and wi else 0.0
        path.append((si, wi, sim))
        i, j = pi, pj
    return path[::-1]


def align_words(line, wh, audio, phone_counts, lang):
    dur = audio["dur"]
    S = [w["norm"] for w in line["words"]]
    Wt = [w for w in wh["words"] if norm_word(w["w"], lang)]
    W = [norm_word(w["w"], lang) for w in Wt]
    n = len(S)
    out = [{"w": w["w"], "t0": None, "t1": None, "status": "interp", "sim": 0.0, "heard": ""} for w in line["words"]]
    extra = 0
    path = dp_align(S, W) if n and W else [([i], [], 0.0) for i in range(n)]
    last_script = None
    for si, wi, sim in path:
        if not si:                                    # whisper-only token
            # repetition of the previous word (e.g. a long scream split into many tokens): absorb
            if last_script is not None and out[last_script]["t1"] is not None and \
                    similarity(S[last_script], W[wi[0]]) >= 0.5:
                out[last_script]["t1"] = max(out[last_script]["t1"], Wt[wi[-1]]["t1"])
            else:
                extra += len(wi)
            continue
        if not wi:
            last_script = si[-1]
            continue
        t0, t1 = Wt[wi[0]]["t0"], Wt[wi[-1]]["t1"]
        heard = " ".join(Wt[k]["w"] for k in wi)
        status = "match" if sim >= ALIGN_OK else ("fuzzy" if sim >= 0.3 else "subst")
        if len(si) == 1:
            out[si[0]].update(t0=t0, t1=t1, status=status, sim=round(sim, 3), heard=heard)
        else:                                          # two script words heard as one token: split
            wts = [max(phone_counts[k], 1) for k in si]
            cut = t0 + (t1 - t0) * wts[0] / sum(wts)
            out[si[0]].update(t0=t0, t1=cut, status="split", sim=round(sim, 3), heard=heard)
            out[si[1]].update(t0=cut, t1=t1, status="split", sim=round(sim, 3), heard=heard)
        last_script = si[-1]

    vt, voiced = audio["vad_t"], audio["voiced"]

    def vidx(t):
        return int(np.clip(round(t * VAD_RATE), 0, len(voiced) - 1))

    # clamp + drop words whisper placed in silence
    for w in out:
        if w["t0"] is None:
            continue
        w["t0"], w["t1"] = float(np.clip(w["t0"], 0, dur)), float(np.clip(w["t1"], 0, dur))
        if w["t1"] - w["t0"] < 0.02 or not voiced[vidx(w["t0"]):vidx(w["t1"]) + 1].any():
            w.update(t0=None, t1=None, status="interp")

    vr = runs_of(voiced)
    v_on = vr[0][0] / VAD_RATE if vr else 0.0
    v_off = vr[-1][1] / VAD_RATE if vr else dur

    # interpolate unplaced runs of words (proportional to phoneme count)
    i = 0
    while i < n:
        if out[i]["t0"] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j]["t0"] is None:
            j += 1
        left = out[i - 1]["t1"] if i > 0 else v_on
        right = out[j]["t0"] if j < n else v_off
        need = sum(max(phone_counts[k], 1) for k in range(i, j)) * SEC_PER_PHONE
        lo_i, hi_j = i, j
        if right - left < 0.5 * need:              # no room: share the neighbours' spans
            if i > 0:
                lo_i = i - 1
                left = out[i - 1]["t0"]
            if j < n:
                hi_j = j + 1
                right = out[j]["t1"]
            if right - left < 0.5 * need:
                left, right = max(0.0, left - 0.5 * need), min(dur, right + 0.5 * need)
        ks = list(range(lo_i, hi_j))
        wts = np.array([max(phone_counts[k], 1) for k in ks], float)
        edges = left + (right - left) * np.concatenate([[0], np.cumsum(wts)]) / wts.sum()
        for q, k in enumerate(ks):
            out[k]["t0"], out[k]["t1"] = float(edges[q]), float(edges[q + 1])
        i = j

    # VAD trim + extension through uncovered voiced audio
    event_words = {t["word_index"] for t in line["tags"] if BREATH_TAG_RE.search(fold(t["tag"]))}
    for k, w in enumerate(out):
        a, b = vidx(w["t0"]), vidx(w["t1"])
        isl = [(s_ + a, e_ + a) for s_, e_ in runs_of(voiced[a:b + 1])]
        # drop short edge blips (inhale, click) separated from the word body by a real pause
        vdb = audio["vad_db"]

        def lead_blip(isl):
            d0 = (isl[0][1] - isl[0][0]) / VAD_RATE
            gap = (isl[1][0] - isl[0][1]) / VAD_RATE
            if d0 < BLIP_MAX and gap >= BLIP_GAP:
                return True
            body = max(vdb[s_:e_].max() for s_, e_ in isl[1:])
            # a much quieter island in front of the word (inhale / sob) - not the word itself
            return d0 < BLIP_QUIET_MAX and gap >= 0.04 and vdb[isl[0][0]:isl[0][1]].max() < body - BLIP_QUIET_DB

        while len(isl) >= 2 and lead_blip(isl):
            isl = isl[1:]
            w["blip_trim"] = True
        while len(isl) >= 2 and (isl[-1][1] - isl[-1][0]) / VAD_RATE < BLIP_MAX * 0.8 and \
                (isl[-1][0] - isl[-2][1]) / VAD_RATE >= BLIP_GAP:
            isl = isl[:-1]
            w["blip_trim"] = True
        if isl:
            first, last = isl[0][0], isl[-1][1] - 1
            w["t0"] = max(w["t0"], vt[first] - (0.04 if w.get("blip_trim") else 0.010))
            w["t1"] = min(w["t1"], vt[last] + 0.015)
        prev_t1 = out[k - 1]["t1"] if k > 0 else 0.0
        next_t0 = out[k + 1]["t0"] if k + 1 < n else dur
        q = vidx(w["t0"]) - 1
        lim = max(prev_t1, w["t0"] - (WORD_EXTEND_BACK_EVENT if k in event_words else WORD_EXTEND_BACK))
        while q >= 0 and voiced[q] and vt[q] > lim:
            q -= 1
        w["t0"] = max(min(w["t0"], vt[q + 1] if q + 1 < len(vt) else w["t0"]), lim)
        q = vidx(w["t1"]) + 1
        fwd = WORD_EXTEND_LAST if (k == n - 1 or line["words"][k]["phrase_end"]) else WORD_EXTEND_FWD
        if k + 1 in event_words:                       # a laugh / breath tag follows this word
            fwd = WORD_EXTEND_BACK_EVENT
        lim = min(next_t0, w["t1"] + fwd)
        while q < len(voiced) and voiced[q] and vt[q] < lim:
            q += 1
        w["t1"] = min(max(w["t1"], vt[q - 1] if q - 1 >= 0 else w["t1"]), lim)

    # monotonic, min duration, close tiny gaps
    for k in range(n):
        if k > 0 and out[k]["t0"] < out[k - 1]["t1"]:
            mid = 0.5 * (out[k]["t0"] + out[k - 1]["t1"])
            out[k - 1]["t1"] = out[k]["t0"] = mid
        if out[k]["t1"] - out[k]["t0"] < WORD_MIN_DUR:
            c = 0.5 * (out[k]["t0"] + out[k]["t1"])
            out[k]["t0"], out[k]["t1"] = max(0.0, c - WORD_MIN_DUR / 2), min(dur, c + WORD_MIN_DUR / 2)
    for k in range(1, n):
        if out[k]["t0"] < out[k - 1]["t1"]:
            out[k]["t0"] = out[k - 1]["t1"]
        elif out[k]["t0"] - out[k - 1]["t1"] < GAP_CLOSE:
            mid = 0.5 * (out[k]["t0"] + out[k - 1]["t1"])
            out[k - 1]["t1"] = out[k]["t0"] = mid
    # snap contiguous word boundaries onto a real pause next to them (whisper often starts the
    # next word inside the previous word's tail)
    gaps = [(s_ / VAD_RATE, e_ / VAD_RATE) for s_, e_ in runs_of(~voiced)]
    for k in range(1, n):
        a, b = out[k - 1], out[k]
        if b["t0"] - a["t1"] > 0.03:
            continue
        bnd = 0.5 * (a["t1"] + b["t0"])
        phrase = line["words"][k - 1]["phrase_end"]
        best = None
        for g0, g1 in gaps:
            L_ = g1 - g0
            if g1 < bnd - SNAP_BACK or g0 > bnd + SNAP_FWD:
                continue
            if g0 <= a["t0"] + 0.05 or g1 >= b["t1"] - 0.05:
                continue
            if not ((phrase and L_ >= SNAP_MIN_PHRASE) or L_ >= SNAP_MIN_ANY):
                continue
            dist = 0.0 if g0 <= bnd <= g1 else min(abs(g0 - bnd), abs(g1 - bnd))
            if best is None or dist < best[0]:
                best = (dist, g0, g1)
        if best is not None:
            a["t1"] = best[1] + 0.01
            b["t0"] = best[2] - 0.01
            a["snapped"] = b["snapped"] = True
    for w in out:
        w["t0"], w["t1"] = round(w["t0"], 4), round(max(w["t1"], w["t0"] + 0.02), 4)
    stats = {"words": n,
             "matched": sum(w["status"] in ("match", "split") for w in out),
             "fuzzy": sum(w["status"] == "fuzzy" for w in out),
             "substituted": sum(w["status"] == "subst" for w in out),
             "interpolated": sum(w["status"] == "interp" for w in out),
             "whisper_extra": extra, "whisper_text": wh.get("text", "")}
    return out, stats


# =====================================================================================
# G2P
# =====================================================================================


def _stress_table(text):
    """'+'-marked words (whitespace separated, '#' comments) -> {word_with_е: stressed vowel index}."""
    idx = {}
    for line_ in text.splitlines():
        for w in line_.split("#", 1)[0].split():
            if "+" not in w:
                continue
            w = w.lower()
            pos = w.index("+")
            idx[w.replace("+", "").replace("ё", "е")] = sum(1 for ch in w[:pos] if ch in RU_VOWELS)
    return idx


RU_STRESS_IDX = _stress_table(RU_STRESS_BUILTIN)


def load_stress_dict(path):
    try:
        return _stress_table(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        die("cannot read --stress-dict {}: {}".format(path, e))


def find_espeak():
    for exe in ("espeak-ng", "espeak"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def _phone(p, vis, vowel, weight, stress=None, caps=False, glide=False, base=None):
    q = {"p": p, "base": base or p, "vis": vis, "vowel": vowel, "weight": weight, "stress": stress,
         "soft": False, "glide": glide}
    if vowel:
        q["caps"] = caps
    return q


def ipa_to_phones(ipa, caps=False, lang="", unknown=None):
    """IPA string (espeak-ng style) -> phones. ˈ/ˌ mark the next vowel, ː lengthens, ʲ palatalises."""
    s = unicodedata.normalize("NFD", ipa).replace("c\u0327", "\u00e7")
    s = re.sub(r"\([a-z-]+\)", " ", s)                     # espeak language switch markers "(en)"
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    cons = dict(IPA_CONSONANTS)
    cons.update(IPA_LANG_CONSONANTS.get(lang, {}))
    phones, pending, i = [], None, 0
    after_vowel = False
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            after_vowel, i = False, i + 1
            continue
        if ch == "ˈ":
            pending, i = "S", i + 1
            continue
        if ch == "ˌ":
            pending, i = pending or "U", i + 1
            continue
        if ch in "ːˑ":
            if phones:
                f = (LONG_VOWEL if phones[-1]["vowel"] else GEMINATE) if ch == "ː" else 1.2
                phones[-1]["weight"] *= f
            i += 1
            continue
        if ch == "ʲ":
            if phones and not phones[-1]["vowel"]:
                phones[-1]["soft"] = True
            i += 1
            continue
        if ch in IPA_SKIP:
            i += 1
            continue
        sym = None
        for L in (2, 1):
            cand = s[i:i + L]
            if len(cand) == L and (cand in cons or cand in IPA_VOWELS or cand in IPA_GLIDES):
                sym = cand
                break
        if sym is None:
            if unknown is not None:
                unknown.add(ch)
            i += 1
            continue
        i += len(sym)
        if sym in IPA_VOWELS:
            if pending:
                lvl = pending
            elif after_vowel:                                # second element of a diphthong
                lvl = "R"
            else:
                lvl = "R" if sym in IPA_REDUCED else "U"
            w = VOWEL_WEIGHT[lvl] * (1.15 if caps and lvl == "S" else 1.0)
            if after_vowel and not pending:
                w *= OFFGLIDE
            phones.append(_phone(sym, IPA_VOWELS[sym], True, w, stress=lvl, caps=caps))
            pending, after_vowel = None, True
        elif sym in IPA_GLIDES:
            phones.append(_phone(sym, IPA_GLIDES[sym], False, PHONE_WEIGHT["j"], glide=True))
            after_vowel = False
        else:
            vis, w = cons[sym]
            phones.append(_phone(sym, vis, False, w, base=IPA_PP_BASE.get(sym, sym)))
            after_vowel = False
    return phones


def _en_restore_stem(stem):
    """'stopp' -> 'stop', 'mak' -> 'make' (rough inverse of -ing / -ed spelling changes)."""
    if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiouls":
        return stem[:-1]
    if re.fullmatch(r"[^aeiouy]*[aeiou][^aeiouwxy]", stem):
        return stem + "e"
    return stem


def en_rules_ipa(word):
    """Rough English spelling -> IPA (used only when espeak-ng is missing)."""
    w = word.lower().replace("’", "'")
    if w in EN_WORDS:
        return EN_WORDS[w]
    for suf, add in (("'s", "z"), ("'re", "ɚ"), ("'ll", "l"), ("'ve", "v"), ("'d", "d"), ("n't", "nt")):
        if w.endswith(suf) and len(w) > len(suf):
            return en_rules_ipa(w[:-len(suf)]) + add
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        return ""
    if w in EN_WORDS:
        return EN_WORDS[w]
    has_v = lambda x: re.search(r"[aeiouy]", x) is not None          # a suffix is only split off a real stem
    if len(w) > 4 and w.endswith("ing") and has_v(w[:-3]):
        return en_rules_ipa(_en_restore_stem(w[:-3])) + "ɪŋ"
    if (len(w) > 3 and w.endswith("ed") and w[-3] not in "aeiou" and has_v(w[:-2])) or \
            (len(w) > 3 and w.endswith("ied")):
        stem = w[:-3] + "y" if w.endswith("ied") else _en_restore_stem(w[:-2])
        last = stem.rstrip("e")[-1:] if stem else ""
        suf = "ɪd" if last in ("t", "d") else ("t" if last in ("p", "k", "s", "f", "x", "h", "c") else "d")
        return en_rules_ipa(stem) + suf
    if len(w) > 4 and w.endswith("ies"):
        return en_rules_ipa(w[:-3] + "y") + "z"
    if len(w) > 3 and w.endswith("es") and re.search(r"(s|x|z|ch|sh)$", w[:-2]) and has_v(w[:-2]):
        return en_rules_ipa(w[:-2]) + "ɪz"
    if len(w) > 3 and w.endswith("s") and not re.search(r"(ss|us|is|ous)$", w) and has_v(w[:-1]):
        stem = w[:-1]
        if re.search(r"(ce|ge|se|ze)$", stem):
            return en_rules_ipa(stem) + "ɪz"
        return en_rules_ipa(stem) + ("s" if stem[-1] in "ptkf" else "z")
    return _en_core(w)


def _en_core(w):
    word = w
    suffix = ""
    for suf, ipa in EN_SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf) + 1:
            w, suffix = w[:-len(suf)], ipa
            break
    magic = -1
    if len(w) >= 3 and w[-1] == "e" and w[-2] not in "aeiouy" and w[-3] in "aeiouy" and \
            (len(w) == 3 or w[-4] not in "aeiou"):
        magic, w = len(w) - 3, w[:-1]
    elif len(w) >= 3 and w.endswith("le") and w[-3] not in "aeiouy":
        w, suffix = w[:-2], "əl" + suffix
    elif len(w) >= 3 and w[-1] == "e" and any(c in "aeiouy" for c in w[:-1]):
        w = w[:-1]
    toks = []                                         # (ipa, is_nucleus)
    i, n = 0, len(w)
    while i < n:
        c, rest = w[i], w[i:]
        nxt = w[i + 1] if i + 1 < n else ""
        if i == 0 and rest[:2] in ("kn", "wr", "gn", "ps"):
            i += 1
            continue
        if rest in ("mb", "mn"):
            toks.append(("m", False))
            break
        if rest[:2] == "bt":
            toks.append(("t", False))
            i += 2
            continue
        is_vowel = c in "aeiou" or (c == "y" and i > 0 and (nxt == "" or nxt not in "aeiou"))
        if is_vowel:
            team = next(((L, ipa) for L, ipa in EN_TEAMS if rest.startswith(L)), None)
            if team:
                L, ipa = team
                if L == "ow" and i + 2 == n:
                    ipa = "oʊ"
                elif L == "ey" and i + 2 == n:
                    ipa = "i"
                elif L == "ie" and i + 2 == n and n <= 3:
                    ipa = "aɪ"
                toks.append((ipa, True))
                i += len(L)
                continue
            after = w[i + 1:i + 3]
            if nxt == "r" and (i + 2 >= n or w[i + 2] not in "aeiouyr"):
                toks.append((EN_R_VOWELS[c], True))
                i += 2
                continue
            if c == "y":
                ipa = ("i" if any(x in "aeiou" for x in w[:i]) else "aɪ") if i + 1 == n else "ɪ"
            elif c == "a" and after[:2] in ("ll", "lk", "lt"):
                ipa = "ɔ"
            elif c == "a" and i > 0 and w[i - 1] == "w":
                ipa = "ɑ"
            elif c == "o" and after[:2] in ("ld", "lt"):
                ipa = "oʊ"
            elif c == "i" and after[:2] in ("nd", "ld"):
                ipa = "aɪ"
            elif i == magic or (i + 1 == n and not any(x in "aeiouy" for x in w[:i])):
                ipa = EN_LONG[c]
            else:
                ipa = EN_SHORT[c]
            toks.append((ipa, True))
            i += 1
            continue
        if i > 0 and c == w[i - 1] and c not in "aeiou":      # doubled consonant letter
            i += 1
            continue
        dig = next(((L, ipa) for L, ipa in EN_CONS if rest.startswith(L)), None)
        if dig:
            L, ipa = dig
            if L == "gh" and i == 0:
                ipa = "ɡ"
            elif L == "x" and i == 0:
                ipa = "z"
            elif L == "ng" and i + 2 < n and w[i + 2] in "aeiouy" and len(word) > 5:
                ipa = "ŋɡ"
            toks.append((ipa, False))
            i += len(L)
            continue
        if c == "c":
            toks.append(("s" if nxt in ("e", "i", "y") else "k", False))
        elif c == "g":
            toks.append(("dʒ" if nxt in ("e", "i", "y") else "ɡ", False))
        elif c == "s":
            between = i > 0 and w[i - 1] in "aeiou" and nxt in ("a", "e", "i", "o", "u")
            toks.append(("z" if between else "s", False))
        elif c == "n" and nxt == "k":
            toks.append(("ŋ", False))
        elif c == "h":
            if nxt and nxt in "aeiouy":
                toks.append(("h", False))
        elif c in "bdfklmnptvwz":
            toks.append((c, False))
        i += 1
    nuclei = [k for k, (_, v) in enumerate(toks) if v]
    if suffix:
        toks.append((suffix, False))
    if not nuclei:
        return "".join(t for t, _ in toks)
    if len(nuclei) == 1:
        si = 0
    elif word.endswith(EN_STRESS_BEFORE):
        si = len(nuclei) - 1 if suffix else max(0, len(nuclei) - 2)
    elif word.startswith(EN_PREFIXES) and len(nuclei) >= 2:
        si = 1
    elif word.endswith(EN_STRESS_LAST):
        si = len(nuclei) - 1
    else:
        si = 0
    reduce_ = {"æ": "ə", "ɑ": "ə", "ʌ": "ə", "ɛ": "ə", "ɝ": "ɚ"}
    out = []
    for k, (t, v) in enumerate(toks):
        if v and k == nuclei[si]:
            out.append("ˈ" + t)
        elif v:
            out.append(reduce_.get(t, t))
        else:
            out.append(t)
    return "".join(out)


def letters_ipa(word, lang):
    """Very rough spelling -> IPA for languages without espeak-ng (fits es/it best)."""
    w = word.lower()
    table = dict(LETTER_SINGLE)
    multi = list(LETTER_MULTI)
    for k, v in LETTER_LANG.get(lang, {}).items():
        if len(k) == 1:
            table[k] = v
        else:
            multi = [(k, v)] + [m for m in multi if m[0] != k]
    multi.sort(key=lambda m: -len(m[0]))
    toks, i = [], 0                                   # (ipa, nucleus?, accented?)
    while i < len(w):
        c = w[i]
        m = next(((L, ipa) for L, ipa in multi if w.startswith(L, i)), None)
        if m:
            L, ipa = m
            toks.append((ipa, L[0] in LETTER_VOWELS and any(x in IPA_VOWELS for x in ipa), False))
            i += len(L)
            continue
        if c not in table:
            i += 1
            continue
        if i > 0 and c == w[i - 1]:                     # doubled letter: long vowel / geminate
            toks.append(("ː", False, False))
            i += 1
            continue
        ipa = table[c]
        if c == "c" and i + 1 < len(w) and w[i + 1] in "eiéèêí":
            ipa = "tʃ" if lang == "it" else ("θ" if lang == "es" else "s")
        if c == "g" and i + 1 < len(w) and w[i + 1] in "eiéèêí":
            ipa = "dʒ" if lang == "it" else ("x" if lang == "es" else ("ʒ" if lang in ("fr", "pt") else ipa))
        toks.append((ipa, c in LETTER_VOWELS, c in LETTER_ACCENTED))
        i += 1
    groups, cur = [], []                              # vowel runs = syllable nuclei
    for k, t in enumerate(toks):
        if t[1]:
            cur.append(k)
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    stressed = None
    acc = [k for k, t in enumerate(toks) if t[2]]
    if acc:
        stressed = acc[0]
    elif groups:
        rule = LANG_STRESS.get(lang, "penult")
        g = groups[0] if (rule == "first" or len(groups) == 1) else (groups[-1] if rule == "last" else groups[-2])
        strong = [k for k in g if toks[k][0] in ("a", "e", "o", "ɛ", "ø")]
        stressed = strong[0] if strong else g[0]
    return "".join(("ˈ" if k == stressed else "") + t[0] for k, t in enumerate(toks))


class G2P:
    """Word -> phones [{p, base, vis, vowel, weight, stress, soft, glide, caps}] for one language."""

    def __init__(self, lang, mode="auto", stress_dict=None, espeak_voice=None):
        self.lang = base_lang(lang)
        self.espeak = find_espeak()
        self.user_stress = stress_dict or {}
        if mode == "espeak":
            if not self.espeak:
                die("--g2p espeak: espeak-ng not found on PATH. Install it (brew install espeak-ng / "
                    "apt install espeak-ng / https://github.com/espeak-ng/espeak-ng/releases).")
            method = "espeak"
        elif mode == "rules":
            method = {"ru": "ru-rules", "en": "en-rules"}.get(self.lang, "letters")
        elif self.lang == "ru":
            method = "ru-rules"
        elif self.espeak:
            method = "espeak"
        elif self.lang == "en":
            method = "en-rules"
        else:
            method = "letters"
        self.method = method
        self.voice = espeak_voice or (lang if "-" in (lang or "") else ESPEAK_VOICES.get(self.lang, self.lang))
        self._ipa_cache = {}
        self.begin_line()

    def begin_line(self):
        self.fallback = []
        self.unknown = set()

    def info(self):
        label = {"ru-rules": "ru-rules", "en-rules": "en-rules (rough)", "letters": "letters (rough)",
                 "espeak": "espeak-ng IPA"}[self.method]
        return {"method": label, "lang": self.lang, "voice": self.voice if self.method == "espeak" else None,
                "stress_fallback": list(dict.fromkeys(self.fallback)), "unknown_symbols": sorted(self.unknown)}

    # --- espeak -------------------------------------------------------------------
    def espeak_ipa(self, text, voice=None):
        voice = voice or self.voice
        key = (voice, text)
        if key not in self._ipa_cache:
            try:
                r = subprocess.run([self.espeak, "-q", "--ipa", "-v", voice, text], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=30)
            except Exception as e:
                die("espeak-ng failed on {!r}: {}".format(text, e))
            if r.returncode != 0:
                die("espeak-ng -v {} failed: {}".format(voice, r.stderr.decode("utf-8", "replace").strip()))
            self._ipa_cache[key] = " ".join(r.stdout.decode("utf-8", "replace").split())
        return self._ipa_cache[key]

    # --- dispatch -----------------------------------------------------------------
    def word(self, low, caps=False):
        if self.method == "ru-rules":
            plain = low.replace(COMBINING_ACUTE, "")
            if plain.strip() in DIGIT_WORDS["ru"]:
                return g2p_word_ru(DIGIT_WORDS["ru"][plain.strip()], caps, self)
            if re.search(r"[а-яё]", plain):
                return g2p_word_ru(low, caps, self)
            if re.search(r"[a-z]", plain):                      # Latin word inside Russian text
                if self.espeak:
                    return ipa_to_phones(self.espeak_ipa(plain, "en-us"), caps, "en", self.unknown)
                return self._spelled(plain, caps, en_rules_ipa, "en")
            return []
        if self.method == "espeak":
            return self._spelled(low, caps, lambda part: self.espeak_ipa(part), self.lang)
        if self.method == "en-rules":
            return self._spelled(low, caps, en_rules_ipa, "en")
        return self._spelled(low, caps, lambda part: letters_ipa(part, self.lang), self.lang)

    def _spelled(self, low, caps, to_ipa, lang):
        plain = low.replace(COMBINING_ACUTE, "")
        digits = DIGIT_WORDS.get(lang, {})
        if plain.strip() in digits and self.method != "espeak":
            plain = digits[plain.strip()]
        parts = [p for p in plain.split("-") if p]
        if len(parts) > 1 and len(set(parts)) == 1 and len(parts[0]) == 1:      # "a-a-a-a" scream / "m-m-m"
            ph = letters_phones(parts[0], caps, lang, self.unknown)
            for q in ph:
                q["weight"] *= len(parts)
                if q["vowel"]:
                    q["stress"] = "S"
            return ph
        out = []
        for part in parts:
            m = re.search(r"(.)\1{2,}", part)                 # "Nooooo": collapse the run, lengthen the vowel
            stretch = 1.0
            if m:
                stretch = min(len(m.group(0)), 8) / 2.0
                part = re.sub(r"(.)\1{2,}", r"\1", part)
            ph = ipa_to_phones(to_ipa(part), caps, lang, self.unknown)
            if stretch > 1.0:
                vs = [q for q in ph if q["vowel"]]
                tgt = next((q for q in vs if q["stress"] == "S"), vs[0] if vs else None)
                if tgt is not None:
                    tgt["weight"] *= stretch
                    tgt["stress"] = "S"
            out += ph
        return out

    # --- Russian stress -------------------------------------------------------------
    def ru_stress(self, part, word):
        """part = original letters (may carry a combining acute), word = Cyrillic letters only."""
        if COMBINING_ACUTE in part:
            pre = part[:part.index(COMBINING_ACUTE)]
            return max(0, sum(1 for ch in pre if ch in RU_VOWELS) - 1)
        key = word.replace("ё", "е")
        if key in self.user_stress:
            return self.user_stress[key]
        if key in CLITICS:
            return -1
        if "ё" in word:
            return sum(1 for ch in word[:word.index("ё")] if ch in RU_VOWELS)
        if key in RU_STRESS_IDX:
            return RU_STRESS_IDX[key]
        nv = sum(1 for ch in word if ch in RU_VOWELS)
        if nv >= 2:
            self.fallback.append(word)
        return 0 if nv <= 1 else nv - 2


def letters_phones(letter, caps, lang, unknown):
    """One letter as a sound (for 'a-a-a' screams / 'm-m-m' hums), any script."""
    ru = {"а": "a", "о": "o", "у": "u", "э": "e", "ы": "ɨ", "и": "i", "е": "e", "я": "a", "ю": "u", "ё": "o",
          "м": "m", "н": "n", "х": "x", "ш": "ʃ", "с": "s", "ф": "f", "в": "v", "р": "r", "л": "l"}
    ipa = ru.get(letter) or LETTER_SINGLE.get(letter, "")
    return ipa_to_phones(("ˈ" if ipa in IPA_VOWELS else "") + ipa, caps, lang, unknown)


def g2p_part_ru(part, caps, g2p):
    """One orthographic Russian word (no hyphens), lowercase. Returns [{p, vowel, stress, weight, ...}]."""
    word = re.sub(r"[^а-яё]", "", part)
    if not word:
        return []
    stress = g2p.ru_stress(part, word)
    s = WORD_REWRITES.get(word, word)
    if len(s) > 3 and (s.endswith("ого") or s.endswith("его")) and s.replace("ё", "е") not in G_TO_V_EXCEPT:
        s = s[:-2] + "в" + s[-1]
    for a, b in CLUSTER_RULES:
        s = s.replace(a, b)
    ph = []
    vi = -1
    prev = None          # previous letter
    for k, ch in enumerate(s):
        if ch in "ьъ":
            if ch == "ь" and ph and not ph[-1]["vowel"]:
                ph[-1]["soft"] = True
            prev = ch
            continue
        if ch in RU_VOWELS:
            vi += 1
            after_vowel = prev is None or prev in RU_VOWELS or prev in "ьъ"
            soft_prev = False
            if ch in IOTATED:
                if after_vowel:
                    ph.append({"p": "j", "vowel": False, "weight": PHONE_WEIGHT["j"]})
                else:
                    soft_prev = True
                v = IOTATED[ch]
            elif ch == "и":
                if prev == "ь":
                    ph.append({"p": "j", "vowel": False, "weight": PHONE_WEIGHT["j"]})
                v = "i"
                soft_prev = not after_vowel
            else:
                v = PLAIN_VOWEL[ch]
            if soft_prev and ph and not ph[-1]["vowel"]:
                if ph[-1]["p"] in ALWAYS_HARD:
                    v = {"i": "y", "e": "e"}.get(v, v)
                else:
                    ph[-1]["soft"] = True
            if stress < 0:
                lvl = "R" if v == "o" else "U"
            elif vi == stress:
                lvl = "S"
            else:
                lvl = "U" if (vi == stress - 1 or k == 0) else "R"
            if lvl != "S":                           # vowel reduction (akanye / ikanye)
                after_soft = soft_prev and not (ph and ph[-1]["p"] in ALWAYS_HARD)
                if v == "o":
                    v = "a" if lvl == "U" else "@"
                elif v == "a":
                    v = "i" if (after_soft and vi < stress) else ("a" if lvl == "U" else "@")
                elif v == "e" and (after_soft or not after_vowel):
                    v = "i"
            w = VOWEL_WEIGHT[lvl] * (1.15 if caps and lvl == "S" else 1.0)
            if prev == ch:                            # doubled vowel letter (scream): lengthen
                ph[-1]["weight"] += w
            else:
                ph.append({"p": v, "vowel": True, "stress": lvl, "weight": w, "caps": caps})
            prev = ch
            continue
        if ch in RU_CONS:
            p = RU_CONS[ch]
            if prev == ch and ph and ph[-1]["p"] == p:   # geminate: one longer phone
                ph[-1]["weight"] *= GEMINATE
            else:
                ph.append({"p": p, "vowel": False, "weight": PHONE_WEIGHT[p]})
            prev = ch
    # voicing assimilation (regressive) and final devoicing - visemes are identical, labels nicer
    obstr_voiceless = set(VOICED_PAIR.values()) | {"x", "ts", "ch", "shch"}
    for k in range(len(ph) - 1, -1, -1):
        p = ph[k]["p"]
        if ph[k]["vowel"]:
            continue
        nxt = ph[k + 1]["p"] if k + 1 < len(ph) else None
        if nxt is None and p in VOICED_PAIR:
            ph[k]["p"] = VOICED_PAIR[p]
        elif nxt in obstr_voiceless and p in VOICED_PAIR:
            ph[k]["p"] = VOICED_PAIR[p]
        elif nxt in VOICED_PAIR and nxt != "v" and p in VOICELESS_PAIR:
            ph[k]["p"] = VOICELESS_PAIR[p]
    for q in ph:
        q.setdefault("stress", None)
        q.setdefault("soft", False)
        q["base"] = q["p"]
        q["vis"] = PHONEME_TO_VISEME[q["p"]]
        q["glide"] = q["p"] == "j"
    return ph


def g2p_word_ru(word_low, caps, g2p):
    parts = [p for p in word_low.split("-") if p]
    plain = [p.replace(COMBINING_ACUTE, "") for p in parts]
    if len(parts) > 1 and len(set(plain)) == 1 and len(plain[0]) == 1:      # "а-а-а-а-а" (scream)
        ph = g2p_part_ru(plain[0], caps, g2p)
        for q in ph:
            q["weight"] *= len(parts)
            if q["vowel"]:
                q["stress"] = "S"
        return ph
    out = []
    for p in parts:
        out += g2p_part_ru(p, caps, g2p)
    return out


def phone_label(q):
    return q["p"] + ("'" if q.get("soft") else "") + ("ˈ" if q.get("stress") == "S" else "")


# =====================================================================================
# PHONEME / VISEME TIMING
# =====================================================================================


def distribute_phonemes(words, phon_lists):
    phonemes = []
    for w, ph in zip(words, phon_lists):
        if not ph:
            continue
        t0, t1 = w["t0"], w["t1"]
        wts = np.array([q["weight"] for q in ph], float)
        if (t1 - t0) / len(ph) < 0.025:
            wts = np.ones(len(ph))
        edges = t0 + (t1 - t0) * np.concatenate([[0], np.cumsum(wts)]) / wts.sum()
        for k, q in enumerate(ph):
            phonemes.append({"p": phone_label(q), "base": q["base"], "vis": q["vis"], "glide": q.get("glide", False),
                             "t0": float(edges[k]), "t1": float(edges[k + 1]),
                             "stress": q.get("stress"), "caps": bool(q.get("caps"))})
    return phonemes


def build_visemes(phonemes, dur):
    segs = []
    for q in phonemes:
        v = q["vis"]
        if v in VOWEL_VISEMES and not q["glide"]:
            w = VOWEL_STRENGTH[q["stress"] or "U"] * (CAPS_STRENGTH if q["caps"] else 1.0)
        elif q["glide"]:
            w = 0.6
        else:
            w = 1.0
        if segs and segs[-1]["v"] == v and abs(segs[-1]["t1"] - q["t0"]) < 1e-6:
            segs[-1]["t1"] = q["t1"]
            segs[-1]["w"] = max(segs[-1]["w"], w)
            segs[-1]["phones"].append(q["base"])
        else:
            segs.append({"v": v, "t0": q["t0"], "t1": q["t1"], "w": w, "phones": [q["base"]]})
    # silences
    full = []
    t = 0.0
    for s in segs:
        if s["t0"] - t > 1e-4:
            full.append({"v": "sil", "t0": t, "t1": s["t0"], "w": 1.0, "phones": []})
        full.append(s)
        t = s["t1"]
    if dur - t > 1e-4:
        full.append({"v": "sil", "t0": t, "t1": dur, "w": 1.0, "phones": []})
    return full


def nonverbal_events(line, words, audio):
    voiced = close_gaps(audio["voiced"], int(0.10 * VAD_RATE))
    cover = np.zeros_like(voiced)
    for w in words:
        a = max(0, int((w["t0"] - 0.04) * VAD_RATE))
        b = min(len(cover), int((w["t1"] + 0.04) * VAD_RATE) + 1)
        cover[a:b] = True
    events = []
    for s, e in runs_of(voiced & ~cover):
        t0, t1 = s / VAD_RATE, e / VAD_RATE
        if t1 - t0 < NV_MIN_DUR:
            continue
        k = sum(1 for w in words if w["t1"] <= t0 + 1e-6)      # index of next word
        typ = "breath"
        for tg in line["tags"]:
            if tg["word_index"] in (k, k + 1) or (k == len(words) and tg["word_index"] == k):
                if LAUGH_TAG_RE.search(fold(tg["tag"])):
                    typ = "laugh"
        events.append({"type": typ, "t0": round(t0, 4), "t1": round(t1, 4)})
    return events


# =====================================================================================
# EMOTION
# =====================================================================================


def tag_presets(tag):
    presets, efforts, event = [], [], False
    for chunk in [c.strip() for c in re.split(r"[,;]", fold(tag)) if c.strip()]:
        for keys, pr, eff, ev in _TAG_RULES:
            if any(k in chunk for k in keys):
                presets += pr
                if eff is not None:
                    efforts.append(eff)
                event = event or ev
                break
    return presets, efforts, event


def merge_presets(lst):
    d = {}
    for name, inten in lst:
        d[name] = max(d.get(name, 0.0), inten)
    return d


def segment_effort(presets, efforts):
    if efforts:
        return min(efforts) if any(e < 1.0 for e in efforts) else max(efforts)
    if not presets:
        return 1.0
    tot = sum(presets.values())
    return sum(EMOTION_PRESETS[n]["effort"] * i for n, i in presets.items()) / max(tot, 1e-6)


def parse_forced(spec):
    """'fear_freeze:0.7,calm' -> [('fear_freeze', 0.7), ('calm', FORCE_INTENSITY)]."""
    out = []
    for part in [p.strip() for p in (spec or "").split(",") if p.strip()]:
        name, _, inten = part.partition(":")
        name = name.strip()
        if name not in EMOTION_PRESETS:
            die("unknown emotion preset '{}'. Presets: {}".format(name, ", ".join(PRESET_NAMES)))
        try:
            out.append((name, float(inten) if inten else FORCE_INTENSITY))
        except ValueError:
            die("bad emotion intensity in '{}'".format(part))
    return out


def build_emotion_segments(line, words, nv, dur, forced):
    base, mods = [], []
    direction = fold(line["direction"])
    for keys, pr, md in _DIRECTION_RULES:
        if any(k in direction for k in keys):
            base += pr
            mods += md
    base += forced
    start, end = -LEAD_IN, dur + TAIL
    tags = sorted(line["tags"], key=lambda t: t["word_index"])
    if not tags:
        pres = merge_presets(base or DEFAULT_PRESETS)
        segs = [{"t0": start, "t1": end, "tag": None, "presets": pres, "effort": segment_effort(pres, []),
                 "event": False}]
        return segs, mods

    def tag_time(k):
        if k <= 0:
            return start
        prev_end = words[k - 1]["t1"]
        nxt_start = words[k]["t0"] if k < len(words) else dur
        inside = [e for e in nv if e["t0"] >= prev_end - 1e-3 and e["t1"] <= nxt_start + 1e-3]
        if inside:
            return inside[0]["t0"] - 0.05
        return max(prev_end, nxt_start - 0.2)

    segs = []
    state = None                                     # last non-event segment presets/effort
    if tags[0]["word_index"] > 0:                    # words before the first tag: direction / default
        pres = merge_presets(base or DEFAULT_PRESETS)
        state = (pres, segment_effort(pres, []), None)
        segs.append({"t0": start, "t1": None, "tag": None, "presets": pres, "effort": state[1], "event": False})
    for q, tg in enumerate(tags):
        t0 = tag_time(tg["word_index"])
        pr, effs, event = tag_presets(tg["tag"])
        pres = merge_presets(pr + base) if (pr or base) else merge_presets(DEFAULT_PRESETS)
        eff = segment_effort(pres, effs)
        if event and state is not None:              # transient event on top of the running state
            pres = merge_presets(list(state[0].items()) + list(pres.items()))
            eff = state[1]
        if segs and segs[-1]["t1"] is None:
            segs[-1]["t1"] = t0
        segs.append({"t0": t0, "t1": None, "tag": tg["tag"], "presets": pres, "effort": eff, "event": event})
        if event:
            k = tg["word_index"]
            ev_end = (words[k]["t0"] if k < len(words) else dur) + 0.4
            nxt = tag_time(tags[q + 1]["word_index"]) if q + 1 < len(tags) else end
            if ev_end < nxt - 0.05:
                segs[-1]["t1"] = ev_end
                if state is not None:
                    back = state[0]
                    beff = state[1]
                else:
                    back = merge_presets(base or DEFAULT_PRESETS)
                    beff = segment_effort(back, [])
                segs.append({"t0": ev_end, "t1": None, "tag": state[2] if state else None, "presets": back,
                             "effort": beff, "event": False})
        else:
            state = (pres, eff, tg["tag"])
    segs[-1]["t1"] = end
    return segs, mods


def emotion_layer(segs, t_out, env_slow):
    N = len(t_out)
    W = {p: np.zeros(N) for p in PRESET_NAMES}
    eff = np.ones(N)
    for s in segs:
        m = (t_out >= s["t0"]) & (t_out < s["t1"] + (1e-9 if s["t1"] < t_out[-1] else 1.0))
        for p, inten in s["presets"].items():
            W[p][m] = inten
        eff[m] = s["effort"]
    for p in PRESET_NAMES:
        if W[p].any():
            W[p] = np.clip(crit_damp(W[p], FPS, EMO_T90, zero_phase=True), 0, 1.5)
    eff = crit_damp(eff, FPS, EMO_T90, zero_phase=True)
    # intensity arc
    for p in PRESET_NAMES:
        if not W[p].any():
            continue
        arc = EMOTION_PRESETS[p]["arc"]
        if arc == "voice":
            W[p] = W[p] * (0.8 + 0.25 * env_slow)
        elif arc == "build":
            for s in segs:
                if p in s["presets"]:
                    m = (t_out >= s["t0"]) & (t_out < s["t1"])
                    x = (t_out[m] - s["t0"]) / max(s["t1"] - s["t0"], 1e-3)
                    W[p][m] *= 0.85 + 0.2 * smoothstep(x)
    return W, eff


def dominant_at(W, i):
    best, bw = "calm", 0.15
    for p, arr in W.items():
        if arr[i] > bw:
            best, bw = p, arr[i]
    return best


# =====================================================================================
# ARTICULATION
# =====================================================================================
SPEECH_CONTROLS = sorted({c for v in list(VISEME_CONTROLS.values()) + list(NONVERBAL_CONTROLS.values())
                          for c in expand(v)} | {"jawOpen", "mouthClose"}, key=SHAPES.index)
_VIS_T = {v: expand(d) for v, d in VISEME_CONTROLS.items()}
_NV_T = {v: expand(d) for v, d in NONVERBAL_CONTROLS.items()}


def viseme_target(v, w):
    t = dict(_VIS_T[v])
    if v in VOWEL_VISEMES:
        for c in t:
            t[c] *= w if c == "jawOpen" else (0.6 + 0.4 * min(w, 1.2))
    return t


def coarticulated_targets(vis):
    """Per viseme target vector with lip rounding / jaw borrowed from neighbouring vowels."""
    targets = []
    for i, s in enumerate(vis):
        t = viseme_target(s["v"], s["w"])
        if s["v"] in VISEME_DOMINANCE:
            dl, dj = VISEME_DOMINANCE[s["v"]]
            nxt = prv = None
            for k in range(i + 1, len(vis)):
                if vis[k]["v"] == "sil" or vis[k]["t0"] - s["t1"] > 0.3:
                    break
                if vis[k]["v"] in VOWEL_VISEMES:
                    nxt = vis[k]
                    break
            for k in range(i - 1, -1, -1):
                if vis[k]["v"] == "sil" or s["t0"] - vis[k]["t1"] > 0.3:
                    break
                if vis[k]["v"] in VOWEL_VISEMES:
                    prv = vis[k]
                    break
            ref = nxt or prv
            if ref is not None:
                rt = viseme_target(ref["v"], ref["w"])
                for c in LIP_CONTROLS:
                    t[c] = dl * t.get(c, 0.0) + (1 - dl) * rt.get(c, 0.0)
                jaws = [viseme_target(r["v"], r["w"])["jawOpen"] for r in (prv, nxt) if r is not None]
                t["jawOpen"] = dj * t.get("jawOpen", 0.0) + (1 - dj) * 0.6 * min(jaws)
        targets.append(t)
    return targets


def window_weight(t, hs, he, ramp_in, ramp_out):
    w = np.zeros_like(t)
    w[(t >= hs) & (t <= he)] = 1.0
    m = (t >= hs - ramp_in) & (t < hs)
    w[m] = smoothstep((t[m] - (hs - ramp_in)) / ramp_in)
    m = (t > he) & (t <= he + ramp_out)
    w[m] = np.maximum(w[m], smoothstep(1.0 - (t[m] - he) / ramp_out))
    return w


def closure_windows(vis):
    """Hard lip seals for PP and lip-teeth contact for FF (acoustic time, not shifted)."""
    pp, ff = [], []
    for s in vis:
        if s["v"] == "PP":
            hs = s["t0"] - PP_PRE
            he = max(s["t1"] - 0.005, hs + PP_HOLD_MIN)
            ph = s["phones"]
            press = max(PP_PRESS.get(p, 0.2) for p in ph) if ph else 0.2
            pp.append({"t0": s["t0"], "t1": s["t1"], "hs": hs, "he": he, "press": press, "src": s})
        elif s["v"] == "FF":
            hs = s["t0"] - 0.010
            he = max(s["t1"] - 0.010, hs + FF_HOLD_MIN)
            ff.append({"t0": s["t0"], "t1": s["t1"], "hs": hs, "he": he, "src": s})
    return pp, ff


def articulate(vis, nv, audio, t_int, effort_int, rest_jaw_int, speech_act_int):
    R = FPS * SUB
    K = len(t_int)
    tg = {c: np.zeros(K) for c in SPEECH_CONTROLS}

    def idx(t):
        return int(np.clip(round((t + LEAD_IN) * R), 0, K))

    targets = coarticulated_targets(vis)
    pp, ff = closure_windows(vis)
    for s, t in zip(vis, targets):
        if s["v"] in ("sil", "PP", "FF"):
            continue
        a, b = idx(s["t0"] - ANTICIPATION), idx(s["t1"] - ANTICIPATION)
        for c in SPEECH_CONTROLS:
            tg[c][a:b] = t.get(c, 0.0)
    # lip closures are written last and aligned with their hold window, so the filtered jaw is
    # already near closed when the seal starts and starts opening right at the release
    closures = [(s, t) for s, t in zip(vis, targets) if s["v"] == "FF"] + \
               [(s, t) for s, t in zip(vis, targets) if s["v"] == "PP"]
    wins = {id(c_["src"]): c_ for c_ in pp + ff}
    for s, t in closures:
        c_ = wins[id(s)]
        a, b = idx(c_["hs"] - CLOSURE_LEAD), idx(c_["he"] - 0.010)
        for c in SPEECH_CONTROLS:
            tg[c][a:b] = t.get(c, 0.0)
    for e in nv:
        a, b = idx(e["t0"] - ANTICIPATION), idx(e["t1"] - ANTICIPATION)
        for c in SPEECH_CONTROLS:
            tg[c][a:b] = _NV_T[e["type"]].get(c, 0.0)
    sm = {c: crit_damp(tg[c], R, JAW_ATTACK_90 if c == "jawOpen" else ATTACK_90) for c in SPEECH_CONTROLS}

    env = loudness_at(audio, t_int + ANTICIPATION, rate=R)
    gain = JAW_LOUD_MIN + (JAW_LOUD_MAX - JAW_LOUD_MIN) * env ** JAW_LOUD_GAMMA
    jaw = soft_knee(sm["jawOpen"] * effort_int * gain, JAW_KNEE, JAW_MAX)
    floor = NEUTRAL_JAW + rest_jaw_int * (1.0 - 0.7 * speech_act_int)
    sm["jawOpen"] = np.maximum(jaw, floor)
    lip_scale = np.clip(0.8 + 0.2 * effort_int, 0.85, 1.1)
    for c in SPEECH_CONTROLS:
        if c not in ("jawOpen", "mouthClose"):
            sm[c] = sm[c] * lip_scale
    return sm, env


# =====================================================================================
# EYES / HEAD
# =====================================================================================


def blink_curve(key, t_out, W, words):
    rng = np.random.default_rng(seed_for(key, "blink"))
    N = len(t_out)
    end = t_out[-1]
    supp = np.zeros(N)
    for p, arr in W.items():
        if EMOTION_PRESETS[p]["suppress"] > 0:
            supp += arr * EMOTION_PRESETS[p]["suppress"]

    def i_at(t):
        return int(np.clip(round((t + LEAD_IN) * FPS), 0, N - 1))

    times = []
    t = -LEAD_IN + rng.uniform(0.15, 1.2)
    while t < end - 0.1:
        i = i_at(t)
        if supp[i] > 0.5:
            t += 0.25
            continue
        times.append(t)
        lo, hi = EMOTION_PRESETS[dominant_at(W, i)]["blink"]
        t += rng.uniform(lo, hi)
    # blinks at phrase pauses (people blink at syntactic breaks)
    for k in range(1, len(words)):
        g0, g1 = words[k - 1]["t1"], words[k]["t0"]
        if g1 - g0 >= 0.35 and rng.random() < 0.5:
            tb = g0 + 0.05
            if supp[i_at(tb)] <= 0.5 and all(abs(tb - x) > 1.0 for x in times):
                times.append(tb)
    times.sort()
    kept = []
    for x in times:
        if not kept or x - kept[-1] >= 0.6:
            kept.append(x)
    curve = np.zeros(N)
    for ts in kept:
        c = rng.uniform(0.045, 0.060)
        h = rng.uniform(0.0, 0.020)
        o = rng.uniform(0.075, 0.100)
        peak = round((ts + c + LEAD_IN) * FPS) / FPS - LEAD_IN         # peak lands on a frame
        s0 = peak - c
        y = np.zeros(N)
        m = (t_out >= s0) & (t_out <= peak)
        y[m] = ((t_out[m] - s0) / c) ** 2                               # fast down (accelerating)
        y[(t_out > peak) & (t_out <= peak + h)] = 1.0
        m = (t_out > peak + h) & (t_out <= peak + h + o)
        x = (t_out[m] - peak - h) / o
        y[m] = 1.0 - smoothstep(x)                                      # slower ease-out
        curve = np.maximum(curve, y)
    return curve, [round(x, 3) for x in kept]


def gaze_curves(key, t_out, W, mods):
    rng = np.random.default_rng(seed_for(key, "gaze"))
    N = len(t_out)
    amp_mul = hold_mul = 1.0
    for md in mods:
        amp_mul *= MODIFIERS[md].get("sacc_amp_mul", 1.0)
        hold_mul *= MODIFIERS[md].get("sacc_hold_mul", 1.0)
    off = np.zeros((N, 2))
    cur = rng.normal(0, 1.0, 2)
    t = -LEAD_IN
    i = 0
    while i < N:
        p = EMOTION_PRESETS[dominant_at(W, i)]
        hold = rng.uniform(*p["sacc_hold"]) * hold_mul
        amp = rng.uniform(*p["sacc_amp"]) * amp_mul
        ang = rng.uniform(0, 2 * np.pi)
        step = np.array([math.cos(ang), 0.6 * math.sin(ang)]) * amp
        tgt = cur + step
        if np.linalg.norm(tgt) > SACC_RADIUS * amp_mul:             # reflect back towards the centre
            tgt = cur - step
        if np.linalg.norm(tgt) > SACC_RADIUS * amp_mul:
            tgt = tgt / np.linalg.norm(tgt) * SACC_RADIUS * amp_mul
        d = 0.025 + 0.002 * amp                                     # saccade duration
        m = (t_out >= t) & (t_out < t + d)
        x = smoothstep((t_out[m] - t) / d)[:, None]
        off[m] = cur + (tgt - cur) * x
        m2 = (t_out >= t + d) & (t_out < t + d + hold)
        off[m2] = tgt
        cur = tgt
        t = t + d + hold
        i = int(np.searchsorted(t_out, t))
    bias = np.zeros((N, 2))
    for p, arr in W.items():
        bias += arr[:, None] * np.array(EMOTION_PRESETS[p]["gaze"])[None, :]
    for md in mods:
        if "gaze" in MODIFIERS[md]:
            bias += np.array(MODIFIERS[md]["gaze"])[None, :]
    bias = np.stack([crit_damp(bias[:, 0], FPS, 0.3, True), crit_damp(bias[:, 1], FPS, 0.3, True)], 1)
    g = off + bias
    return np.clip(g[:, 0], -25, 25), np.clip(g[:, 1], -20, 20)


def head_curves(key, t_out, W, words, phon_lists, vis_words, effort, mods, dur, func_words):
    rng = np.random.default_rng(seed_for(key, "head"))
    N = len(t_out)
    tot = np.zeros(N)
    mob = np.zeros(N)
    roll_bias = np.zeros(N)
    bob_amp = np.zeros(N)
    bob_f = 0.0
    for p, arr in W.items():
        pr = EMOTION_PRESETS[p]
        tot += arr
        mob += arr * pr["mobility"]
        roll_bias += arr * pr["roll"]
        if pr["bob"][0] > 0:
            bob_amp += arr * pr["bob"][0]
            bob_f = pr["bob"][1]
    rest = np.maximum(0.0, 1.0 - tot)
    mobility = (mob + rest) / (tot + rest)
    drift_mul = 1.0
    head_pitch_bias = 0.0
    run_bob = (0.0, 0.0)
    roll_bob = 0.0
    for md in mods:
        drift_mul *= MODIFIERS[md].get("head_drift_mul", 1.0)
        head_pitch_bias += MODIFIERS[md].get("head_pitch", 0.0)
        if "bob" in MODIFIERS[md]:
            run_bob = MODIFIERS[md]["bob"]
            roll_bob = MODIFIERS[md].get("roll_bob", 0.0)
    pitch, yaw, roll = np.zeros(N), np.zeros(N), np.zeros(N)
    # slow drift
    for arr, amp in ((pitch, 0.35), (yaw, 0.45), (roll, 0.25)):
        for _ in range(3):
            f = rng.uniform(0.08, 0.35)
            arr += amp / 2.0 * np.sin(2 * np.pi * f * t_out + rng.uniform(0, 2 * np.pi))
    pitch *= mobility * drift_mul
    yaw *= mobility * drift_mul
    roll *= mobility * drift_mul

    # nods on stressed syllables of phrase-initial / emphasised / long words
    def stressed_time(k):
        ph = phon_lists[k]
        w = words[k]
        if not ph:
            return w["t0"]
        wts = np.array([q["weight"] for q in ph], float)
        edges = w["t0"] + (w["t1"] - w["t0"]) * np.concatenate([[0], np.cumsum(wts)]) / wts.sum()
        for q, e in zip(ph, edges):
            if q.get("stress") == "S":
                return float(e)
        return w["t0"]

    last_nod = -10.0
    for k, w in enumerate(words):
        phrase_start = k == 0 or (w["t0"] - words[k - 1]["t1"] > 0.25) or vis_words[k - 1]["phrase_end"]
        caps = vis_words[k]["caps"]
        nsyl = sum(1 for q in phon_lists[k] if q["vowel"])
        content = vis_words[k]["low"].replace(COMBINING_ACUTE, "").replace("ё", "е") not in func_words
        if not content:
            continue
        if not (phrase_start or caps or (nsyl >= 2 and rng.random() < 0.35)):
            continue
        tc = stressed_time(k)
        if tc - last_nod < 0.45 and not caps:
            continue
        last_nod = tc
        i = int(np.clip(round((tc + LEAD_IN) * FPS), 0, N - 1))
        A = rng.uniform(0.9, 1.6) * mobility[i] * float(np.clip(effort[i], 0.6, 1.4)) ** 0.5
        if caps:
            A *= 1.8
        if A < 0.05:
            continue
        s0 = tc - 0.08
        att, rel = 0.12, 0.35
        y = np.zeros(N)
        m = (t_out >= s0) & (t_out < s0 + att)
        y[m] = smoothstep((t_out[m] - s0) / att)
        m = (t_out >= s0 + att) & (t_out < s0 + att + rel)
        y[m] = 1.0 - smoothstep((t_out[m] - s0 - att) / rel)
        pitch -= A * y                                      # + = chin up, a nod goes down
        yaw += A * 0.3 * rng.choice([-1, 1]) * y
        roll += A * 0.2 * rng.choice([-1, 1]) * y
    # questions: slight tilt + chin up towards the end of the phrase, held into the tail
    side = rng.choice([-1.0, 1.0])
    for k, w in enumerate(words):
        if vis_words[k]["question"]:
            s0, s1 = w["t0"] - 0.3, w["t1"]
            nxt = words[k + 1]["t0"] if k + 1 < len(words) else dur + TAIL + 1.0
            y = np.zeros(N)
            m = (t_out >= s0) & (t_out < s1)
            y[m] = smoothstep((t_out[m] - s0) / max(s1 - s0, 1e-3))
            m = (t_out >= s1) & (t_out < nxt)
            y[m] = 1.0
            m = (t_out >= nxt) & (t_out < nxt + 0.8)
            y[m] = 1.0 - smoothstep((t_out[m] - nxt) / 0.8)
            q_m = np.maximum(mobility, 0.3)
            roll += side * 2.0 * y * q_m
            pitch += 0.8 * y * q_m
    roll += side * roll_bias
    pitch += head_pitch_bias
    # breathing bob / running bob
    if bob_f > 0:
        pitch += bob_amp * np.sin(2 * np.pi * bob_f * t_out + rng.uniform(0, 6.28))
    if run_bob[0] > 0:
        ph0 = rng.uniform(0, 6.28)
        pitch += run_bob[0] * np.sin(2 * np.pi * run_bob[1] * t_out + ph0)
        roll += roll_bob * np.sin(np.pi * run_bob[1] * t_out + ph0)

    def clip(a):
        a = crit_damp(a, FPS, 0.06, zero_phase=True)
        return HEAD_MAX * np.tanh(a / HEAD_MAX)

    return clip(pitch), clip(yaw), clip(roll)


def tremble_noise(key, t_out, salt):
    rng = np.random.default_rng(seed_for(key, "tremble" + salt))
    y = np.zeros_like(t_out)
    for _ in range(3):
        f = rng.uniform(6.0, 11.0)
        y += np.sin(2 * np.pi * f * t_out + rng.uniform(0, 2 * np.pi) + 0.6 * np.sin(2 * np.pi * 0.7 * t_out))
    return y / 3.0


# =====================================================================================
# LINE PROCESSING
# =====================================================================================


class Context:
    """Options shared by all lines of a run."""

    def __init__(self, args):
        self.args = args
        self.default_lang = args.lang
        self.whisper = pick_whisper(args.whisper)
        self.whisper_model = args.whisper_model
        self.stress = load_stress_dict(args.stress_dict) if args.stress_dict else {}
        self._g2p = {}
        if args.whisper == "auto" and self.whisper is None and not args.words:
            print("  no whisper backend installed (pip install openai-whisper | mlx-whisper): "
                  "words are spread over the voiced audio instead", flush=True)

    def g2p(self, lang):
        key = base_lang(lang) if "-" not in lang else lang
        if key not in self._g2p:
            self._g2p[key] = G2P(lang, self.args.g2p, self.stress, self.args.espeak_voice)
        return self._g2p[key]


def process_line(spec, ctx, cache_dir):
    """spec: {id, audio (Path), text, lang, emotion, speaker, direction, words, seed} -> record dict."""
    lid = spec["id"]
    lang = spec.get("lang") or ctx.default_lang
    lb = base_lang(lang)
    wav = Path(spec["audio"])
    audio = analyse_audio(wav)
    dur = audio["dur"]
    line = parse_text(spec["text"], lb, spec.get("direction", ""))
    if not line["words"]:
        raise ValueError("line {}: no words in the text".format(lid))
    forced = parse_forced(spec.get("emotion", ""))
    key = spec.get("seed") or lid
    g2p = ctx.g2p(lang)
    g2p.begin_line()

    phon_lists = [g2p.word(w["low"], w["caps"]) for w in line["words"]]
    for w, ph in zip(line["words"], phon_lists):          # "Anna!" - shouted/asked final vowel stays open
        if re.search(r"[!?！？]", w["punct"]):
            vs = [q for q in ph if q["vowel"]]
            if vs and vs[-1]["stress"] == "R":
                vs[-1]["stress"] = "U"
                vs[-1]["weight"] = VOWEL_WEIGHT["U"]
                if vs[-1]["p"] == "@":
                    vs[-1]["p"] = vs[-1]["base"] = "a"
    counts = [len(p) for p in phon_lists]

    # word timing
    words_src = spec.get("words")
    if words_src:
        wh = load_words_file(words_src)
        method, model = "words-file", None
    elif ctx.whisper:
        wh = whisper_words(lid, wav, audio, lb, ctx.whisper, ctx.whisper_model, cache_dir, ctx.args.retranscribe)
        method, model = "whisper-" + ctx.whisper, ctx.whisper_model
    else:
        wh = vad_word_guess(line, audio, counts)
        method, model = "vad-distribute (no word timing; script words spread over voiced spans by phoneme count)", None
    words, astats = align_words(line, wh, audio, counts, lb)
    if method.startswith("vad"):
        for w in words:
            if w["status"] in ("match", "split"):
                w["status"] = "vad"
        astats.update(matched=0, whisper_text="")
    astats["vad"] = sum(w["status"] == "vad" for w in words)
    astats = dict({"method": method, "model": model}, **astats)

    phonemes = distribute_phonemes(words, phon_lists)
    vis = build_visemes(phonemes, dur)
    nv = nonverbal_events(line, words, audio)

    N = int(round((LEAD_IN + dur + TAIL) * FPS)) + 1
    t_out = np.arange(N) / FPS - LEAD_IN
    R = FPS * SUB
    K = (N - 1) * SUB + 1
    t_int = np.arange(K) / R - LEAD_IN

    # emotion
    env_out = loudness_at(audio, t_out, sigma_s=0.0)
    env_slow = gaussian_filter1d(env_out, 0.15 * FPS, mode="nearest")
    segs, mods = build_emotion_segments(line, words, nv, dur, forced)
    W, eff_seg = emotion_layer(segs, t_out, env_slow)
    effort = eff_seg * audio["level_factor"] ** LEVEL_WEIGHT
    rest_jaw = sum(W[p] * EMOTION_PRESETS[p]["rest_jaw"] for p in PRESET_NAMES)
    tremble = sum(W[p] * EMOTION_PRESETS[p]["tremble"] for p in PRESET_NAMES)
    speech = np.zeros(N)
    for w in words:
        speech[(t_out >= w["t0"]) & (t_out <= w["t1"])] = 1.0
    speech = np.clip(gaussian_filter1d(speech, 0.05 * FPS), 0, 1)

    def up(a):
        return np.interp(t_int, t_out, a)

    sm, _env_int = articulate(vis, nv, audio, t_int, up(effort), up(rest_jaw), up(speech))
    cur = {c: np.zeros(N) for c in SHAPES}
    for c in SPEECH_CONTROLS:
        cur[c] = sm[c][::SUB].copy()

    # emotion offsets (mouth offsets give way to lip rounding)
    E = {c: np.zeros(N) for c in SHAPES}
    for p in PRESET_NAMES:
        if W[p].any():
            for c, v in EMOTION_PRESETS[p]["offsets"].items():
                E[c] += W[p] * v
    rounding = np.maximum(cur["mouthFunnel"], cur["mouthPucker"])
    for c in SHAPES:
        if c in ("eyeBlinkLeft", "eyeBlinkRight"):
            continue
        if c.startswith("mouth") and c != "mouthClose":
            cur[c] = cur[c] + E[c] * (1.0 - 0.6 * np.clip(rounding, 0, 1))
        else:
            cur[c] = cur[c] + E[c]

    # tremble (sobbing / trembling whisper): chin + jaw
    if tremble.max() > 1e-3:
        n1, n2 = tremble_noise(key, t_out, "a"), tremble_noise(key, t_out, "b")
        cur["mouthRollLower"] += TREMBLE_ROLL * tremble * (0.5 + 0.5 * n1)
        cur["jawOpen"] += TREMBLE_JAW * tremble * n2
        cur["mouthFrownLeft"] += 0.04 * tremble * n1
        cur["mouthFrownRight"] += 0.04 * tremble * n1

    # hard closures (after everything that could open the lips)
    pp, ff = closure_windows(vis)
    for c in ff:
        w = FF_WEIGHT * window_weight(t_out, c["hs"], c["he"], FF_RAMP, FF_RAMP)
        cur["mouthRollLower"] = np.maximum(cur["mouthRollLower"], FF_ROLL * w)
        cur["jawOpen"] = cur["jawOpen"] * (1 - w) + w * np.minimum(cur["jawOpen"], FF_JAW_MAX)
        for k in ("mouthUpperUpLeft", "mouthUpperUpRight"):
            cur[k] = np.maximum(cur[k], 0.2 * w)
    for c in pp:
        w = window_weight(t_out, c["hs"], c["he"], PP_RAMP_IN, PP_RAMP_OUT)
        cur["mouthClose"] = np.maximum(cur["mouthClose"], w)
        cur["jawOpen"] = cur["jawOpen"] * (1 - w) + w * np.minimum(cur["jawOpen"], PP_JAW_MAX)
        for k in ("mouthFunnel", "mouthPucker"):
            cur[k] *= (1 - 0.5 * w)
        for k in ("mouthStretchLeft", "mouthStretchRight", "mouthLowerDownLeft", "mouthLowerDownRight",
                  "mouthUpperUpLeft", "mouthUpperUpRight", "mouthRollLower"):
            cur[k] *= (1 - 0.8 * w)
        for k in ("mouthPressLeft", "mouthPressRight"):
            cur[k] = np.maximum(cur[k], c["press"] * w)
    # eyes
    blink, blink_times = blink_curve(key, t_out, W, words)
    for s in ("Left", "Right"):
        b = np.clip(cur["eyeBlink" + s] + E["eyeBlink" + s], 0, 1)
        cur["eyeBlink" + s] = b + (1 - b) * blink
        cur["eyeWide" + s] = cur["eyeWide" + s] * (1 - blink)
    eye_yaw, eye_pitch = gaze_curves(key, t_out, W, mods)
    head_pitch, head_yaw, head_roll = head_curves(key, t_out, W, words, phon_lists, line["words"], effort,
                                                  mods, dur, FUNCTION_WORDS.get(lb, set()))
    for c in SHAPES:
        v = np.nan_to_num(cur[c])
        if c not in ("mouthClose", "eyeBlinkLeft", "eyeBlinkRight", "jawOpen"):
            v = soft_knee(v, SHAPE_KNEE, 1.0)          # no flat plateaus when emotion + speech stack up
        cur[c] = np.clip(v, 0.0, 1.0)
    curves = {c: [round(float(v), 4) for v in cur[c]] for c in SHAPES}
    for name, arr in (("eyeYaw", eye_yaw), ("eyePitch", eye_pitch), ("headPitch", head_pitch),
                      ("headYaw", head_yaw), ("headRoll", head_roll)):
        curves[name] = [round(float(v), 3) for v in np.nan_to_num(arr)]

    # emotion summary
    seg_out = []
    for s in segs:
        dom = max(s["presets"].items(), key=lambda kv: kv[1]) if s["presets"] else ("calm", 0.4)
        seg_out.append({"t0": round(s["t0"], 3), "t1": round(s["t1"], 3), "tag": s["tag"], "event": s["event"],
                        "dominant": dom[0], "intensity": round(dom[1], 2), "effort": round(s["effort"], 2),
                        "presets": [{"name": k, "intensity": round(v, 2)} for k, v in
                                    sorted(s["presets"].items(), key=lambda kv: -kv[1])]})
    score = {}
    for s in seg_out:
        if s["event"]:
            continue
        span = s["t1"] - s["t0"]
        for p in s["presets"]:
            score[p["name"]] = score.get(p["name"], 0.0) + p["intensity"] * span
    line_dom = max(score.items(), key=lambda kv: kv[1])[0] if score else seg_out[0]["dominant"]
    line_int = max((p["intensity"] for s in seg_out for p in s["presets"] if p["name"] == line_dom), default=0.4)
    emotion = {"name": line_dom, "intensity": round(line_int, 2),
               "label": " -> ".join(d for i_, d in enumerate(s["dominant"] for s in seg_out)
                                    if i_ == 0 or d != seg_out[i_ - 1]["dominant"]),
               "direction": line["direction"], "forced": [{"name": n, "intensity": i} for n, i in forced],
               "modifiers": mods,
               "effort_mean": round(float(effort[(t_out >= 0) & (t_out <= dur)].mean()), 3),
               "level_factor": round(audio["level_factor"], 3), "segments": seg_out,
               "blinks": blink_times}

    qa = line_qa(curves, pp, t_out)
    conventions = dict(CONVENTIONS)
    if ctx.args.strict_arkit_close:
        curves["mouthClose"] = [round(min(a, b), 4) for a, b in zip(curves["mouthClose"], curves["jawOpen"])]
        conventions["mouthClose_form"] = "strict-arkit: mouthClose = min(sealed mouthClose, jawOpen); " \
                                         "qa closures are measured on the sealed scale"
    rec = {
        "format": FORMAT, "generator": "lipsync.py " + __version__,
        "id": lid, "speaker": spec.get("speaker", ""), "lang": lang, "text": line["text"], "raw": line["raw"],
        "tags": [t["tag"] for t in line["tags"]], "tag_positions": line["tags"],
        "audio_file": spec.get("audio_label", wav.name), "fps": FPS, "duration": round(dur, 4),
        "lead_in": LEAD_IN, "tail": TAIL, "frames": N,
        "time_base": CONVENTIONS["time_base"], "conventions": conventions,
        "shape_curves": SHAPES, "angle_curves": ANGLES,
        "words": [{"w": w["w"], "t0": w["t0"], "t1": w["t1"], "status": w["status"], "heard": w["heard"]}
                  for w in words],
        "phonemes": [{"p": q["p"], "t0": round(q["t0"], 4), "t1": round(q["t1"], 4)} for q in phonemes],
        "visemes": [{"v": s["v"], "t0": round(s["t0"], 4), "t1": round(s["t1"], 4), "w": round(s["w"], 2)}
                    for s in vis],
        "nonverbal": nv,
        "emotion": emotion,
        "alignment": astats,
        "g2p": g2p.info(),
        "loudness": [round(float(v), 3) for v in env_out],
        "qa": qa,
        "curves": curves,
    }
    return rec


# =====================================================================================
# QA
# =====================================================================================


def line_qa(curves, pp, t_out):
    mc = np.array(curves["mouthClose"])
    jaw = np.array(curves["jawOpen"])
    closures = []
    for c in pp:
        m = (t_out >= c["t0"] - 0.06) & (t_out <= c["t1"] + 0.04)
        seg = mc[m]
        run = best = 0
        for v in seg:
            run = run + 1 if v >= 0.9 else 0
            best = max(best, run)
        closures.append({"t": round(c["t0"], 3), "max": round(float(seg.max()) if len(seg) else 0.0, 3),
                         "hold_ms": round(best * 1000.0 / FPS), "ok": bool(len(seg) and seg.max() >= 0.9
                                                                          and best * 1000.0 / FPS >= 40)})
    # jaw chatter = flapping: two consecutive reversals, each > 0.12, each within 3 frames
    d = np.diff(jaw)
    ext = [i for i in range(1, len(d)) if d[i - 1] * d[i] < 0]
    chatter = sum(1 for a, b, c in zip(ext, ext[1:], ext[2:])
                  if b - a < 4 and c - b < 4 and abs(jaw[b] - jaw[a]) > 0.12 and abs(jaw[c] - jaw[b]) > 0.12)
    shapes = np.array([curves[c] for c in SHAPES])
    angles = {a: (float(np.min(curves[a])), float(np.max(curves[a]))) for a in ANGLES}
    head_ok = all(abs(v) <= HEAD_MAX + 1e-6 for a in ("headPitch", "headYaw", "headRoll") for v in angles[a])
    silence_jaw = float(np.median(jaw[t_out < -0.05])) if (t_out < -0.05).any() else 0.0
    q = {
        "pp_closures": len(pp), "pp_ok": sum(c["ok"] for c in closures), "closures": closures,
        "max_jaw": round(float(jaw.max()), 3), "silence_jaw": round(silence_jaw, 3),
        "max_frame_delta_jaw": round(float(np.abs(np.diff(jaw)).max()), 3),
        "chatter": chatter,
        "range_ok": bool(np.isfinite(shapes).all() and shapes.min() >= 0 and shapes.max() <= 1) and head_ok,
        "blinks": int(sum(1 for i in range(1, len(curves["eyeBlinkLeft"])) if curves["eyeBlinkLeft"][i] >= 0.95
                          and curves["eyeBlinkLeft"][i - 1] < 0.95)),
    }
    q["ok"] = bool(q["pp_ok"] == q["pp_closures"] and q["chatter"] == 0 and q["range_ok"])
    return {"ok": q.pop("ok"), **q}


def qa_failed(r):
    q = r["qa"]
    return q["pp_ok"] < q["pp_closures"] or q["chatter"] or not q["range_ok"]


def write_qa(records, path, plots):
    L = ["# Lip-sync QA", "",
         "Generated by `lipsync.py`. One row per line. *PP ok* = p/b/m closures whose mouthClose (sealed scale) "
         "reached >= 0.9 for >= 40 ms. *chatter* = jaw flapping, i.e. two consecutive reversals > 0.12 each "
         "within 3 frames (should be 0). Words: matched (or VAD-placed) / fuzzy / substituted / interpolated "
         "(+ extra whisper tokens).", "",
         "| ID | spk | lang | dur s | timing | words m/f/s/i (+x) | PP ok/total | min PP hold ms | max jaw | "
         "chatter | range | blinks | emotion | effort |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    bad = []
    for r in records:
        a, q = r["alignment"], r["qa"]
        holds = [c["hold_ms"] for c in q["closures"]]
        L.append("| {id} | {spk} | {lang} | {dur:.2f} | {meth} | {m}/{f}/{s}/{i} (+{x}) | {pok}/{pt} | {hold} | "
                 "{mj:.2f} | {ch} | {rg} | {bl} | {emo} | {eff:.2f} |".format(
                     id=r["id"], spk=r.get("speaker", ""), lang=r.get("lang", ""), dur=r["duration"],
                     meth=a.get("method", "").split(" ")[0], m=a["matched"] + a.get("vad", 0), f=a["fuzzy"], s=a["substituted"],
                     i=a["interpolated"], x=a["whisper_extra"], pok=q["pp_ok"], pt=q["pp_closures"],
                     hold=min(holds) if holds else "-", mj=q["max_jaw"], ch=q["chatter"],
                     rg="ok" if q["range_ok"] else "FAIL", bl=q["blinks"], emo=r["emotion"]["label"],
                     eff=r["emotion"]["effort_mean"]))
        if qa_failed(r):
            bad.append(r["id"])
    notes = []
    for r in records:
        a = r["alignment"]
        odd = [w for w in r["words"] if w["status"] not in ("match", "split", "vad")]
        if odd or a["whisper_extra"]:
            notes.append("- **{}** heard: \"{}\"; ".format(r["id"], a["whisper_text"][:90]) + ", ".join(
                "`{}`={} (heard `{}`)".format(w["w"], w["status"], w["heard"]) for w in odd) +
                (" +{} extra whisper token(s) ignored".format(a["whisper_extra"]) if a["whisper_extra"] else ""))
    if notes:
        L += ["", "## Alignment notes", ""] + notes
    fb = [(r["id"], r["g2p"]["stress_fallback"]) for r in records if r.get("g2p", {}).get("stress_fallback")]
    if fb:
        L += ["", "## Stress guessed (penultimate)", "",
              "Add these words to a `--stress-dict` file with `+` before the stressed vowel:", ""]
        L += ["- **{}**: {}".format(i, ", ".join(ws)) for i, ws in fb]
    unk = sorted({s for r in records for s in r.get("g2p", {}).get("unknown_symbols", [])})
    if unk:
        L += ["", "Unmapped IPA symbols (ignored): " + " ".join(unk)]
    L += ["", "## Summary", "",
          "- lines: {}".format(len(records)),
          "- PP closures: {} / {} ok".format(sum(r["qa"]["pp_ok"] for r in records),
                                             sum(r["qa"]["pp_closures"] for r in records)),
          "- lines failing a check: {}".format(", ".join(bad) if bad else "none"), ""]
    if plots:
        L += ["## Plots", ""] + ["![{0}]({1})".format(i, p) for i, p in plots] + [""]
    Path(path).write_text("\n".join(L), encoding="utf-8")
    return bad


def make_plots(records, out_paths):
    """records + {id: png path}. Skipped (with a note) when matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:                                       # pragma: no cover
        print("  matplotlib unavailable, skipping plots ({}). pip install matplotlib".format(e))
        return []
    done = []
    for r in records:
        if r["id"] not in out_paths:
            continue
        c = r["curves"]
        t = np.arange(r["frames"]) / r["fps"] - r["lead_in"]
        fig, ax = plt.subplots(4, 1, figsize=(14, 10), sharex=True,
                               gridspec_kw={"height_ratios": [1.0, 1.6, 1.2, 1.0]})
        ax[0].fill_between(t, r["loudness"], color="#9bb7d4", label="loudness (norm.)")
        for w in r["words"]:
            ax[0].axvspan(w["t0"], w["t1"], color="#f2c14e" if w["status"] in ("match", "split", "vad")
                          else "#e4572e", alpha=0.18)
            ax[0].text(0.5 * (w["t0"] + w["t1"]), 1.02, w["w"], ha="center", va="bottom", fontsize=8)
        for e in r["nonverbal"]:
            ax[0].axvspan(e["t0"], e["t1"], color="#76b041", alpha=0.25)
        ax[0].set_ylim(0, 1.25)
        ax[0].set_title("{} - {}   emotion: {}".format(r["id"], r["text"], r["emotion"]["label"]), fontsize=10)
        for s in r["visemes"]:
            if s["v"] == "PP":
                ax[1].axvspan(s["t0"], s["t1"], color="#c0392b", alpha=0.15)
            if s["v"] != "sil":
                ax[1].text(0.5 * (s["t0"] + s["t1"]), 1.03, s["v"], ha="center", fontsize=6, color="#555")
        for name, col in (("jawOpen", "#1f77b4"), ("mouthClose", "#d62728"), ("mouthFunnel", "#2ca02c"),
                          ("mouthPucker", "#17becf"), ("mouthStretchLeft", "#9467bd"), ("mouthRollLower", "#8c564b")):
            ax[1].plot(t, c[name], color=col, lw=1.3, label=name)
        ax[1].set_ylim(-0.02, 1.12)
        ax[1].legend(loc="upper right", fontsize=7, ncol=6)
        for name, col in (("eyeBlinkLeft", "#333333"), ("browInnerUp", "#ff7f0e"), ("browDownLeft", "#8c564b"),
                          ("eyeWideLeft", "#17becf"), ("eyeSquintLeft", "#bcbd22"), ("mouthSmileLeft", "#e377c2"),
                          ("mouthFrownLeft", "#7f7f7f")):
            ax[2].plot(t, c[name], color=col, lw=1.2, label=name)
        ax[2].set_ylim(-0.02, 1.05)
        ax[2].legend(loc="upper right", fontsize=7, ncol=7)
        for name, col in (("headPitch", "#1f77b4"), ("headYaw", "#ff7f0e"), ("headRoll", "#2ca02c"),
                          ("eyeYaw", "#9467bd"), ("eyePitch", "#d62728")):
            ax[3].plot(t, c[name], color=col, lw=1.1, label=name + " deg")
        ax[3].legend(loc="upper right", fontsize=7, ncol=5)
        for a in ax:
            a.axvline(0, color="k", lw=0.6, ls=":")
            a.axvline(r["duration"], color="k", lw=0.6, ls=":")
        ax[3].set_xlabel("seconds (0 = audio start)")
        fig.tight_layout()
        p = Path(out_paths[r["id"]])
        p.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(p), dpi=90)
        plt.close(fig)
        done.append((r["id"], p))
    return done


# =====================================================================================
# OUTPUT
# =====================================================================================


def write_record(rec, path, want_csv):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if want_csv:
        names = SHAPES + ANGLES
        with open(str(path.with_suffix(".csv")), "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["frame", "time"] + names)
            for i in range(rec["frames"]):
                wr.writerow([i, round(i / rec["fps"] - rec["lead_in"], 5)] + [rec["curves"][n][i] for n in names])


def write_index(records, out_dir):
    index = {
        "format": FORMAT, "generator": "lipsync.py " + __version__, "fps": FPS, "lead_in": LEAD_IN, "tail": TAIL,
        "time_base": CONVENTIONS["time_base"], "conventions": CONVENTIONS,
        "shape_curves": SHAPES, "angle_curves": ANGLES,
        "lines": [{"id": r["id"], "speaker": r.get("speaker", ""), "lang": r.get("lang", ""),
                   "json": r["id"] + ".json", "audio_file": r["audio_file"], "duration": r["duration"],
                   "frames": r["frames"], "emotion": r["emotion"]["name"], "emotion_label": r["emotion"]["label"],
                   "mouthClose_form": r["conventions"]["mouthClose_form"], "qa_ok": not qa_failed(r),
                   "text": r["text"]} for r in records],
    }
    (Path(out_dir) / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def print_line(rec):
    q, a = rec["qa"], rec["alignment"]
    print("{:10s} {:3s} {:5.2f}s words {}/{} PP {}/{} jaw {:.2f} {:5s} {}".format(
        rec["id"], rec["lang"], rec["duration"], a["matched"] + a.get("vad", 0),
        a["words"], q["pp_ok"], q["pp_closures"], q["max_jaw"], "ok" if not qa_failed(rec) else "FAIL",
        rec["emotion"]["label"]), flush=True)
    fb = rec["g2p"]["stress_fallback"]
    if fb:
        print("           stress guessed (add to --stress-dict): " + ", ".join(fb), flush=True)


def read_manifest(path):
    path = Path(path)
    if not path.exists():
        die("manifest not found: {}".format(path))
    txt = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        d = json.loads(txt)
        rows = d.get("lines", d) if isinstance(d, dict) else d
    else:
        first = txt.splitlines()[0] if txt else ""
        delim = "\t" if (path.suffix.lower() == ".tsv" or first.count("\t") > max(first.count(","), first.count(";"))) \
            else (";" if first.count(";") > first.count(",") else ",")
        rows = list(csv.DictReader(txt.splitlines(), delimiter=delim))
    out = []
    for k, r in enumerate(rows):
        r = {str(a).strip().lower(): (b.strip() if isinstance(b, str) else b) for a, b in r.items() if a}
        if not r.get("audio") or not r.get("text"):
            die("manifest row {}: 'audio' and 'text' are required".format(k + 1))
        audio = Path(r["audio"])
        if not audio.is_absolute():
            audio = path.parent / audio
        words = r.get("words") or None
        if words and not Path(words).is_absolute():
            words = str(path.parent / words)
        out.append({"id": r.get("id") or audio.stem, "audio": audio, "audio_label": r["audio"], "text": r["text"],
                    "lang": r.get("lang") or None, "emotion": r.get("emotion") or "", "speaker": r.get("speaker") or "",
                    "direction": r.get("direction") or "", "words": words, "seed": r.get("seed") or None})
    ids = [s["id"] for s in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        die("manifest has duplicate ids: {}".format(", ".join(sorted(dup))))
    return out


def list_presets():
    print("Emotion presets (force one with --emotion name[:intensity] or the manifest 'emotion' column):\n")
    for name, p in EMOTION_PRESETS.items():
        offs = {}
        for c, v in p["offsets"].items():
            b = c[:-4] if c.endswith("Left") else (c[:-5] if c.endswith("Right") else c)
            offs[b] = max(offs.get(b, 0.0), v)
        top = ", ".join("{} {:.2f}".format(c, v) for c, v in sorted(offs.items(), key=lambda kv: -kv[1])[:4])
        extra = []
        for k in ("rest_jaw", "tremble", "suppress"):
            if p[k]:
                extra.append("{} {}".format(k, p[k]))
        print("  {:14s} effort {:.2f}  blink {:.1f}-{:.1f}s  mobility {:.2f}  arc {:5s} {}  | {}".format(
            name, p["effort"], p["blink"][0], p["blink"][1], p["mobility"], p["arc"], " ".join(extra), top))
    print("\nTag keywords -> presets (first match per comma-separated chunk; * = event tag):\n")
    for kw, pr, eff, ev in TAG_RULES:
        print("  {}{:58s} -> {}{}".format("*" if ev else " ", kw[:58],
                                         ", ".join("{} {}".format(n, i) for n, i in pr) or "(no preset)",
                                         "  effort {}".format(eff) if eff else ""))


# =====================================================================================
# MAIN
# =====================================================================================


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("single line")
    g.add_argument("--audio", help="voice clip (WAV; other formats need soundfile or ffmpeg)")
    g.add_argument("--text", help="script line; [bracketed] parts are acting tags")
    g.add_argument("--out", help="output JSON (default: <audio stem>.json in the current folder)")
    g.add_argument("--id", help="line id (default: audio file stem); also the random seed key")
    g.add_argument("--speaker", default="", help="speaker name, copied into the output")
    g.add_argument("--direction", default="", help="acting remark, e.g. 'looks around' / 'оглядывается'")
    g.add_argument("--words", help="precomputed word timings JSON [{w,t0,t1}] (skips whisper); with --manifest: "
                                   "a folder of <id>.json")
    g = ap.add_argument_group("batch")
    g.add_argument("--manifest", help="lines.csv | lines.json with id, audio, text [, lang, emotion, speaker, "
                                      "direction, words]")
    g.add_argument("--out-dir", help="batch output folder (default: lipsync_out next to the manifest)")
    g.add_argument("--only", default="", help="comma separated ids from the manifest")
    g.add_argument("--qa-only", action="store_true", help="rebuild index.json / qa.md from the JSON in --out-dir")
    g = ap.add_argument_group("options")
    g.add_argument("--lang", default="en", help="language code (default en; ru uses the Russian rule G2P)")
    g.add_argument("--emotion", default="", help="force base preset(s): name[:intensity][,name2[:i]] "
                                                 "(default intensity {})".format(FORCE_INTENSITY))
    g.add_argument("--whisper", default="auto", choices=["auto", "openai", "mlx", "none"],
                   help="word timing backend (auto: mlx on Apple Silicon, else openai-whisper, else none)")
    g.add_argument("--whisper-model", default=WHISPER_MODEL, help="whisper model name or mlx HF repo "
                                                                 "(default %(default)s)")
    g.add_argument("--retranscribe", action="store_true", help="ignore the whisper cache")
    g.add_argument("--g2p", default="auto", choices=["auto", "rules", "espeak"],
                   help="auto: ru -> rules, other languages -> espeak-ng (en falls back to rough rules)")
    g.add_argument("--espeak-voice", help="espeak-ng voice (default: en -> en-us, else the language code)")
    g.add_argument("--stress-dict", help="Russian stress file: one word per line, '+' before the stressed vowel")
    g.add_argument("--strict-arkit-close", action="store_true",
                   help="write mouthClose as min(mouthClose, jawOpen) for strict ARKit rigs")
    g.add_argument("--csv", action="store_true", help="also write a frame-per-row CSV next to each JSON")
    g.add_argument("--plots", nargs="?", const="all", default=None, metavar="IDS",
                   help="QA plots (matplotlib): all lines, or a comma separated list of ids")
    g.add_argument("--seed", help="seed key for blinks / saccades / head (default: the line id)")
    g.add_argument("--list-presets", action="store_true", help="print the emotion presets and tag keywords")
    args = ap.parse_args(argv)

    if args.list_presets:
        list_presets()
        return 0

    if args.manifest or args.qa_only:
        if not args.out_dir and not args.manifest:
            die("--qa-only needs --out-dir")
        out_dir = Path(args.out_dir) if args.out_dir else Path(args.manifest).resolve().parent / "lipsync_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        specs = read_manifest(args.manifest) if args.manifest else []
        only = {s.strip() for s in args.only.split(",") if s.strip()}
        if not args.qa_only:
            ctx = Context(args)
            for spec in specs:
                if only and spec["id"] not in only:
                    continue
                if not spec["words"] and args.words and Path(args.words).is_dir():
                    cand = Path(args.words) / (spec["id"] + ".json")
                    spec["words"] = str(cand) if cand.exists() else None
                if args.seed:
                    spec["seed"] = args.seed
                if not spec["emotion"] and args.emotion:
                    spec["emotion"] = args.emotion
                try:
                    rec = process_line(spec, ctx, out_dir / "whisper_cache")
                except (FileNotFoundError, ValueError, RuntimeError) as e:
                    die("line {}: {}".format(spec["id"], e))
                write_record(rec, out_dir / (spec["id"] + ".json"), args.csv)
                print_line(rec)
        records = []
        order = [s["id"] for s in specs]
        files = sorted(out_dir.glob("*.json"), key=lambda p: (order.index(p.stem) if p.stem in order else 1e9, p.name))
        for p in files:
            if p.name == "index.json":
                continue
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(r, dict) and r.get("format") == FORMAT and (not order or r["id"] in order):
                records.append(r)
        write_index(records, out_dir)
        plots = []
        if args.plots:
            ids = [r["id"] for r in records] if args.plots == "all" else \
                [s.strip() for s in args.plots.split(",") if s.strip()]
            done = make_plots(records, {i: out_dir / "qa" / (i + ".png") for i in ids})
            plots = [(i, os.path.relpath(str(p), str(out_dir))) for i, p in done]
        bad = write_qa(records, out_dir / "qa.md", plots)
        print("{} lines -> {}  | QA problems: {}".format(len(records), out_dir, ", ".join(bad) if bad else "none"))
        return 1 if bad else 0

    if not args.audio or args.text is None:
        ap.error("give --audio and --text (single line), or --manifest (batch), or --list-presets")
    audio = Path(args.audio)
    lid = args.id or audio.stem
    out = Path(args.out) if args.out else Path.cwd() / (lid + ".json")
    if args.words and Path(args.words).is_dir():
        die("--words must be a file in single-line mode")
    spec = {"id": lid, "audio": audio, "audio_label": args.audio, "text": args.text, "lang": args.lang,
            "emotion": args.emotion, "speaker": args.speaker, "direction": args.direction, "words": args.words,
            "seed": args.seed}
    ctx = Context(args)
    try:
        rec = process_line(spec, ctx, out.resolve().parent / "whisper_cache")
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        die(str(e))
    write_record(rec, out, args.csv)
    print_line(rec)
    if args.plots:
        make_plots([rec], {lid: out.with_suffix(".png")})
    print("-> {}{}".format(out, " (+ .csv)" if args.csv else ""))
    return 1 if qa_failed(rec) else 0


if __name__ == "__main__":
    sys.exit(main())
