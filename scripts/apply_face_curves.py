"""Apply face-curves/1 JSON (from face_capture.py or lipsync.py) to a character in Blender.

Shape keys on one or more meshes get the blendshape curves; optional head/neck/eye bones get the
angle curves as additive rotations. Keys are written at the scene frame rate (curves are resampled),
LINEAR interpolation, through fcurve.keyframe_points.add + foreach_set (fast for long takes).

Run headless with a normal startup (never --factory-startup: it can remove installed extensions):
  blender -b scene.blend --python apply_face_curves.py -- --curves line.json --mesh Head Teeth Tongue --save out.blend
  blender -b scene.blend --python apply_face_curves.py -- --curves line.json --mesh Head \\
      --armature Rig --head-bone head --neck-bone neck --eye-bones eye.L,eye.R --sound line.wav --save out.blend
  blender -b scene.blend --python apply_face_curves.py -- --curves line.json --mesh Head --dry-run

Name mapping (curve -> shape key, per mesh): --map entry, exact, case-insensitive, separators ignored,
side spelling (eyeBlinkLeft / eyeBlink_L / eyeBlink.L / EyeBlinkLeft / Eye_Blink_L / A14_Eye_Blink_Left),
then aliases (CC/Reallusion, VRoid/VRM, Fuse/Mixamo-style words such as Blink_Left, Fcl_EYE_Close_L,
Jaw_Open, Mouth_Smile_L, Brow_Raise_Inner_L). A side-less curve (cheekPuff) drives both sided keys
(Cheek_Puff_L/R); a Left/Right pair drives a side-less key (VRM 'blink') with its mean.
--map JSON: {"jawOpen": "MouthOpen", "mouthSmileLeft": ["Smile_L", "SmileWide_L"], "tongueOut": null}

Bones: the angle curves (degrees; +pitch chin up, +yaw to the character's left, +roll top of head to
the character's left shoulder) become rotations in each bone's local space. --head-axes auto (default)
derives the local axes from the bone's rest orientation, with the character's forward taken from the
eye bones (or --facing, default -Y, Blender's front view) and world +Z as up. An explicit spec names
the local axis a positive angle turns about, e.g. pitch=-X,yaw=Y,roll=-Z (a bone pointing up with
roll 0 in a -Y facing rig). --neck-share 0.35 gives the neck 35% of the head rotation.
If the armature already has animation, its action is pushed into an NLA track and the face layer is
added above it in COMBINE mode (additive); otherwise the face action becomes the active action and the
current static pose is kept as the base (--bone-mode to force either).

Never overwrites the opened .blend unless --save names it explicitly. Without --save nothing is saved.
"""
import sys, os, re, json, math, fnmatch, argparse

try:
    import bpy
    import numpy as np
    from mathutils import Quaternion, Vector, Matrix
except ImportError:
    sys.exit("apply_face_curves.py runs inside Blender: "
             "blender -b scene.blend --python apply_face_curves.py -- --curves line.json --mesh Head")

# ---------------------------------------------------------------- tunables
DEFAULT_NECK_SHARE = 0.35          # share of the head rotation given to the neck bone when one is set
DEFAULT_FACING = "-Y"              # character forward in world space when no eye bones tell it
ANGLE_CURVES = ("headPitch", "headYaw", "headRoll", "eyeYaw", "eyePitch")
ACTION_SUFFIX = "_face"            # default action name = <curve id> + this
NLA_TRACK_NAME = "FaceCurves"      # NLA track holding the additive face layer (bone-mode layer)
REPORT_UNUSED_LIMIT = 60           # print at most this many unused shape keys per mesh

ARKIT_52 = [
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight", "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight", "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight", "mouthFrownLeft", "mouthFrownRight", "mouthFunnel",
    "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthPressLeft", "mouthPressRight", "mouthPucker",
    "mouthRight", "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "noseSneerLeft", "noseSneerRight", "tongueOut",
]
SIDE_WORDS = {"l": "L", "left": "L", "r": "R", "right": "R"}
# side-agnostic word aliases: ARKit base (lowercase, no side) -> other rigs' bases
BASE_ALIASES = {
    "eyeblink": ["blink", "eyeclose", "eyeclosed", "eyesclosed", "eyesblink", "eyesclose"],
    "eyesquint": ["squint", "eyessquint"],
    "eyewide": ["eyeswide", "eyeopen", "eyesopen", "eyeswiden"],
    "jawopen": ["mouthopen", "jawdown", "openmouth", "mtha", "aa"],
    "jawforward": ["jawforeward", "jawfwd", "jawthrust"],
    "mouthsmile": ["smile", "mouthhappy"],
    "mouthfrown": ["frown", "mouthsad"],
    "mouthpucker": ["pucker", "mouthkiss", "kiss", "lipspucker"],
    "mouthfunnel": ["funnel", "lipsfunnel"],
    "mouthclose": ["mouthclosed", "lipsclose"],
    "mouthstretch": ["stretch", "lipstretch", "lipsstretch"],
    "mouthpress": ["lippress", "lipspress"],
    "mouthdimple": ["dimple"],
    "mouthrolllower": ["lowerlipin", "mouthbottomlipunder", "lipsrolllower"],
    "mouthrollupper": ["upperlipin", "mouthtoplipunder", "lipsrollupper"],
    "mouthshruglower": ["lowerlipout", "mouthbottomlipout"],
    "mouthshrugupper": ["upperlipout", "mouthtoplipout"],
    "mouthupperup": ["upperlipup", "mouthsnarlupper", "upperlipraise"],
    "mouthlowerdown": ["lowerlipdown", "mouthsnarllower", "lowerlipdepress"],
    "browinnerup": ["browraiseinner", "browsinnerup", "browinnerraise", "innerbrowraise", "browsin"],
    "browouterup": ["browraiseouter", "browsouterup", "browouterraise", "outerbrowraise", "browsup"],
    "browdown": ["browdrop", "browsdown", "browlower", "browlowerer"],
    "cheekpuff": ["puff", "cheeksblow", "cheekblow", "cheekspuff"],
    "cheeksquint": ["cheekraise", "cheekraiser", "cheeksquint"],
    "nosesneer": ["nosescrunch", "sneer", "nosewrinkle", "nosewrinkler"],
    "tongueout": ["tongueextend", "tongueoutward"],
}
# full-name aliases where the side sits in the middle (CC4 "Eye_L_Look_L" = left eye looks left)
NAME_ALIASES = {
    "eyeLookOutLeft": ["eyellookl"], "eyeLookInLeft": ["eyellookr"],
    "eyeLookOutRight": ["eyerlookr"], "eyeLookInRight": ["eyerlookl"],
    "eyeLookUpLeft": ["eyellookup"], "eyeLookUpRight": ["eyerlookup"],
    "eyeLookDownLeft": ["eyellookdown"], "eyeLookDownRight": ["eyerlookdown"],
}


def die(msg):
    print("ERROR: " + msg)
    sys.exit(1)


# ---------------------------------------------------------------- name matching
def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_name(name):
    """-> (base, side) with base lowercase without the side word, side 'L'/'R'/None."""
    s = re.split(r"[:|]", name)[-1]            # Maya namespaces
    side = None
    m = re.match(r"^(.+)\.(L|R|l|r|Left|Right|left|right)$", s)
    if m:                                       # eyeBlink.L
        s, side = m.group(1), SIDE_WORDS[m.group(2).lower()]
    s = s.split(".")[-1]                        # blendShape1.eyeBlinkLeft
    s = re.sub(r"^[A-Za-z]\d{1,3}_", "", s)    # CC 'A14_Eye_Blink_Left', 'T10_Tongue_Out'
    toks = [t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", s)]
    if toks and toks[0] == "fcl":
        toks = toks[1:]
    if side is None and len(toks) > 1 and toks[-1] in SIDE_WORDS:
        side = SIDE_WORDS[toks.pop()]
    elif side is None and len(toks) > 1 and toks[0] in ("left", "right"):
        side = SIDE_WORDS[toks.pop(0)]
    return "".join(toks), side


def alias_bases(base):
    return [base] + BASE_ALIASES.get(base, [])


class KeyIndex:
    def __init__(self, names):
        self.names = list(names)
        self.exact = set(self.names)
        self.lower, self.flat, self.parsed = {}, {}, {}
        for n in self.names:
            self.lower.setdefault(n.lower(), []).append(n)
            self.flat.setdefault(norm(n), []).append(n)
            self.parsed.setdefault(parse_name(n), []).append(n)

    def find(self, curve):
        if curve in self.exact:
            return [curve], "exact"
        if curve.lower() in self.lower:
            return self.lower[curve.lower()][:1], "case"
        if norm(curve) in self.flat:
            return self.flat[norm(curve)][:1], "separators"
        base, side = parse_name(curve)
        if (base, side) in self.parsed:
            return self.parsed[(base, side)][:1], "side-spelling"
        for a in NAME_ALIASES.get(curve, []):
            if a in self.flat:
                return self.flat[a][:1], "alias"
        for b in alias_bases(base)[1:]:
            if (b, side) in self.parsed:
                return self.parsed[(b, side)][:1], "alias"
        if side is None:  # side-less curve -> both sided keys
            for b in alias_bases(base):
                if (b, "L") in self.parsed and (b, "R") in self.parsed:
                    return [self.parsed[(b, "L")][0], self.parsed[(b, "R")][0]], "both-sides"
        return [], None


def build_mapping(curves, mesh_objs, custom, exclude):
    """-> {mesh: [(source, key, how)]}, where source is a curve name or ('mean', L, R)."""
    names = [c for c in curves if c not in ANGLE_CURVES and not any(fnmatch.fnmatchcase(c, p) for p in exclude)
             and not (c in custom and custom[c] is None)]          # null in --map = skip on purpose
    result, unused = {}, {}
    for ob in mesh_objs:
        kbs = ob.data.shape_keys.key_blocks
        ref = ob.data.shape_keys.reference_key.name
        idx = KeyIndex([k.name for k in kbs if k.name != ref])
        used, rows = set(), []
        for c in names:
            if c in custom:
                tgt = custom[c]
                tgt = [] if tgt is None else ([tgt] if isinstance(tgt, str) else list(tgt))
                keys, how = [k for k in tgt if k in idx.exact], "map"
            else:
                keys, how = idx.find(c)
            for k in keys:
                if k in used:
                    continue
                used.add(k)
                rows.append((c, k, how))
        # Left/Right curve pairs -> side-less keys (mean)
        pairs = {}
        for c in names:
            b, s = parse_name(c)
            if s:
                pairs.setdefault(b, {})[s] = c
        for b, lr in pairs.items():
            if len(lr) != 2:
                continue
            for ab in alias_bases(b):
                for k in idx.parsed.get((ab, None), []):
                    if k not in used and not any(c in custom for c in lr.values()):
                        used.add(k)
                        rows.append((("mean", lr["L"], lr["R"]), k, "pair-mean"))
        result[ob.name] = rows
        unused[ob.name] = [n for n in idx.names if n not in used]
    return result, unused, names


# ---------------------------------------------------------------- animation data helpers
LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value


class ActionWriter:
    """Creates an action for one ID and gives fcurves (slotted actions in 4.4+, legacy before)."""

    def __init__(self, id_data, name, id_type):
        self.action = bpy.data.actions.new(name)
        self.slot = None
        self.groups = {}
        if hasattr(self.action, "layers"):
            self.slot = self.action.slots.new(id_type=id_type, name=id_data.name[:60])
            strip = self.action.layers.new("Layer").strips.new(type="KEYFRAME")
            self.bag = strip.channelbag(self.slot, ensure=True)
            self.fcurves = self.bag.fcurves
        else:
            self.bag = None
            self.fcurves = self.action.fcurves

    def fcurve(self, path, index=0, group=None):
        if self.bag is None:
            return self.fcurves.new(path, index=index, action_group=group or "")
        fc = self.fcurves.new(path, index=index)
        if group:
            if group not in self.groups:
                self.groups[group] = self.bag.groups.new(group)
            try:
                fc.group = self.groups[group]
            except (AttributeError, TypeError):
                pass
        return fc

    def assign(self, id_data):
        ad = id_data.animation_data or id_data.animation_data_create()
        ad.action = self.action
        if self.slot is not None:
            ad.action_slot = self.slot


def write_keys(fc, frames, values):
    n = len(frames)
    kp = fc.keyframe_points
    kp.add(n)
    co = np.empty(2 * n, dtype=np.float32)
    co[0::2] = frames
    co[1::2] = values
    kp.foreach_set("co", co)
    try:
        kp.foreach_set("interpolation", np.full(n, LINEAR, dtype=np.int32))
    except (TypeError, RuntimeError):
        for k in kp:
            k.interpolation = "LINEAR"
    fc.update()
    return n


# ---------------------------------------------------------------- bones
def parse_axes(spec, names):
    """'pitch=-X,yaw=Y,roll=-Z' -> {name: Vector} in bone local space."""
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        k, _, v = part.partition("=")
        m = re.match(r"^([+-]?)([XYZxyz])$", v.strip())
        if k.strip() not in names or not m:
            die("bad axis spec %r (expected e.g. pitch=-X,yaw=Y,roll=-Z)" % part)
        vec = Vector((0, 0, 0))
        vec["XYZ".index(m.group(2).upper())] = -1.0 if m.group(1) == "-" else 1.0
        out[k.strip()] = vec
    missing = [n for n in names if n not in out]
    if missing:
        die("axis spec %r lacks %s" % (spec, ", ".join(missing)))
    return out


def character_frame(arm, head_bone, eye_bones, facing):
    """World-space (left, up, forward) of the character."""
    up = Vector((0, 0, 1))
    fwd = None
    how = "--facing " + facing
    if head_bone and eye_bones:
        mw = arm.matrix_world
        h = mw @ arm.data.bones[head_bone].head_local
        e = sum((mw @ arm.data.bones[b].head_local for b in eye_bones), Vector()) / len(eye_bones)
        d = e - h
        d.z = 0.0
        if d.length > 1e-5:
            fwd, how = d.normalized(), "eye bones"
    if fwd is None:
        m = re.match(r"^([+-]?)([XYxy])$", facing)
        if not m:
            die("--facing must be one of -Y,+Y,-X,+X")
        fwd = Vector((0, 0, 0))
        fwd["XY".index(m.group(2).upper())] = -1.0 if m.group(1) == "-" else 1.0
    left = up.cross(fwd).normalized()
    return left, up, fwd, how


def auto_axes(arm, bone_name, frame):
    left, up, fwd = frame
    rw = arm.matrix_world.to_quaternion().to_matrix() @ arm.data.bones[bone_name].matrix_local.to_3x3().normalized()
    inv = rw.transposed()
    # +pitch (chin up) turns about the character's right axis, +yaw about up, +roll about backward
    return {"pitch": (inv @ -left).normalized(), "yaw": (inv @ up).normalized(), "roll": (inv @ -fwd).normalized()}


def axis_label(v):
    i = max(range(3), key=lambda k: abs(v[k]))
    return ("-" if v[i] < 0 else "+") + "XYZ"[i] + ("" if abs(v[i]) > 0.98 else "~")


def delta_quats(axes, pitch, yaw, roll):
    out = []
    for p, y, r in zip(pitch, yaw, roll):
        q = Quaternion(axes["yaw"], math.radians(y))
        q = q @ Quaternion(axes["pitch"], math.radians(p))
        if "roll" in axes:
            q = q @ Quaternion(axes["roll"], math.radians(r))
        out.append(q)
    return out


def key_bone(writer, pb, quats, base, frames):
    """Key a pose bone's rotation channels from per-frame delta quaternions composed on `base`."""
    path = 'pose.bones["%s"]' % bpy.utils.escape_identifier(pb.name)
    mode = pb.rotation_mode
    n = 0
    if mode == "QUATERNION":
        vals, prev = [], None
        for q in quats:
            r = base @ q
            if prev is not None and r.dot(prev) < 0:
                r.negate()
            vals.append(tuple(r))
            prev = r
        vals = np.array(vals)
        for i in range(4):
            n += write_keys(writer.fcurve(path + ".rotation_quaternion", i, pb.name), frames, vals[:, i])
    elif mode == "AXIS_ANGLE":
        vals = []
        for q in quats:
            ax, ang = (base @ q).to_axis_angle()
            vals.append((ang, ax.x, ax.y, ax.z))
        vals = np.array(vals)
        for i in range(4):
            n += write_keys(writer.fcurve(path + ".rotation_axis_angle", i, pb.name), frames, vals[:, i])
    else:
        vals, prev = [], None
        for q in quats:
            e = (base @ q).to_euler(mode, prev) if prev is not None else (base @ q).to_euler(mode)
            vals.append(tuple(e))
            prev = e
        vals = np.array(vals)
        for i in range(3):
            n += write_keys(writer.fcurve(path + ".rotation_euler", i, pb.name), frames, vals[:, i])
    return n


def current_rotation(pb):
    if pb.rotation_mode == "QUATERNION":
        return pb.rotation_quaternion.copy()
    if pb.rotation_mode == "AXIS_ANGLE":
        a = pb.rotation_axis_angle
        return Quaternion(Vector((a[1], a[2], a[3])), a[0])
    return pb.rotation_euler.to_quaternion()


def push_down_active(ad):
    """Move the armature's active action into an NLA track so the face layer can sit above it."""
    act = ad.action
    if act is None:
        return None
    slot = getattr(ad, "action_slot", None)
    tr = ad.nla_tracks.new()
    tr.name = "Base"
    st = tr.strips.new(act.name, int(math.floor(act.frame_range[0])), act)
    if slot is not None and hasattr(st, "action_slot"):
        try:
            st.action_slot = slot
        except (AttributeError, TypeError, RuntimeError):
            pass
    ad.action = None
    return act.name


# ---------------------------------------------------------------- main
def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="apply_face_curves.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--curves", required=True, help="face-curves/1 JSON")
    ap.add_argument("--mesh", nargs="+", default=[], help="mesh object(s) with shape keys (space or comma separated)")
    ap.add_argument("--map", help="JSON {curve: key | [keys] | null} custom name map")
    ap.add_argument("--scale", default="", help="per-curve factors, globs allowed: jawOpen=0.8,mouthSmile*=1.2")
    ap.add_argument("--exclude", default="", help="curve globs to skip, e.g. 'eyeLook*' when eye bones drive the eyes")
    ap.add_argument("--armature", help="armature object for head/neck/eye rotations")
    ap.add_argument("--head-bone")
    ap.add_argument("--neck-bone")
    ap.add_argument("--neck-share", type=float, default=DEFAULT_NECK_SHARE,
                    help="share of the head angles given to the neck (default %(default)s)")
    ap.add_argument("--eye-bones", help="L,R eye bone names")
    ap.add_argument("--head-axes", default="auto", help="auto | pitch=-X,yaw=Y,roll=-Z (local axis per positive angle)")
    ap.add_argument("--eye-axes", default="auto", help="auto | pitch=-X,yaw=Z")
    ap.add_argument("--facing", default=DEFAULT_FACING, help="character forward in world space without eye bones")
    ap.add_argument("--head-scale", type=float, default=1.0, help="multiply head/neck angles")
    ap.add_argument("--eye-scale", type=float, default=1.0, help="multiply eye angles")
    ap.add_argument("--bone-mode", choices=["auto", "active", "layer"], default="auto",
                    help="active: face action replaces the armature action; layer: NLA COMBINE layer above it")
    ap.add_argument("--start-frame", type=int, help="scene frame of curve t=0 (default frame_start + lead_in)")
    ap.add_argument("--action-name", help="base action name (default <id>%s)" % ACTION_SUFFIX)
    ap.add_argument("--sound", help="audio file to add as a sequencer strip starting at --start-frame")
    ap.add_argument("--set-range", action="store_true", help="set the scene frame range to the keyed range")
    ap.add_argument("--save", help="save the result to this .blend (required to keep anything)")
    ap.add_argument("--report", help="write a JSON report of the mapping and key counts")
    ap.add_argument("--dry-run", action="store_true", help="only print the mapping")
    a = ap.parse_args(argv)
    a.mesh = [m for part in a.mesh for m in part.split(",") if m]
    return a


def pairs(text):
    out = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        k, sep, v = part.partition("=")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            sep = ""
        if not sep:
            die("bad --scale entry %r (expected name=factor)" % part)
    return out


def main():
    a = parse_args()
    data = json.load(open(a.curves))
    if data.get("format") != "face-curves/1":
        print("WARNING: format is %r, expected face-curves/1" % data.get("format"))
    curves = {k: np.asarray(v, float) for k, v in data["curves"].items()}
    if not curves:
        die("no curves in %s" % a.curves)
    fps = float(data.get("fps", 60))
    lead_in = float(data.get("lead_in", 0.0))
    n = len(next(iter(curves.values())))
    t_curve = np.arange(n) / fps - lead_in
    sc = bpy.context.scene
    sfps = sc.render.fps / sc.render.fps_base
    start = a.start_frame if a.start_frame is not None else sc.frame_start + int(math.ceil(lead_in * sfps - 1e-6))
    f0 = int(math.ceil(start + t_curve[0] * sfps - 1e-6))
    f1 = int(math.floor(start + t_curve[-1] * sfps + 1e-6))
    frames = np.arange(f0, f1 + 1, dtype=float)
    t_frames = (frames - start) / sfps
    scale = pairs(a.scale)
    exclude = [p for p in a.exclude.split(",") if p]
    custom = json.load(open(a.map)) if a.map else {}

    def sampled(name):
        v = np.interp(t_frames, t_curve, curves[name]) if name in curves else np.zeros(len(frames))
        for pat, f in scale.items():
            if fnmatch.fnmatchcase(name, pat):
                v = v * f
        return v

    meshes = []
    for nm in a.mesh:
        ob = bpy.data.objects.get(nm)
        if ob is None or ob.type != "MESH":
            cands = [o.name for o in bpy.data.objects if o.type == "MESH" and o.data.shape_keys]
            die("mesh %r not found; meshes with shape keys: %s" % (nm, ", ".join(cands) or "none"))
        if not ob.data.shape_keys:
            print("WARNING: %s has no shape keys, skipped" % nm)
            continue
        meshes.append(ob)
    mapping, unused, shape_curves = build_mapping(curves, meshes, custom, exclude)
    mapped_curves = set()
    for rows in mapping.values():
        for src, _, _ in rows:
            mapped_curves.update(src[1:] if isinstance(src, tuple) else [src])
    unmapped = [c for c in shape_curves if c not in mapped_curves]
    for c, tgt in custom.items():
        keys = [] if tgt is None else ([tgt] if isinstance(tgt, str) else list(tgt))
        missing = [k for k in keys if not any(k in ob.data.shape_keys.key_blocks for ob in meshes)]
        if missing:
            print("WARNING: --map %s -> %s: no such shape key on the given meshes" % (c, ", ".join(missing)))
        if c not in curves and c not in ARKIT_52:
            print("WARNING: --map names curve %r, which is neither in the JSON nor an ARKit name" % c)
    report = {"curves": a.curves, "id": data.get("id"), "scene_fps": sfps, "curve_fps": fps, "lead_in": lead_in,
              "start_frame": start, "frame_range": [f0, f1], "meshes": {}, "unmapped_curves": unmapped, "bones": {}}

    print("face curves: %s  %d samples @ %g fps, lead_in %.3f s -> scene %g fps, frames %d..%d (t=0 at %d)"
          % (data.get("id"), n, fps, lead_in, sfps, f0, f1, start))
    base_name = a.action_name or ((data.get("id") or "face") + ACTION_SUFFIX)
    total_keys = 0
    for ob in meshes:
        rows = mapping[ob.name]
        kbs = ob.data.shape_keys.key_blocks
        print("mesh %s: %d keys mapped, %d unused" % (ob.name, len(rows), len(unused[ob.name])))
        for src, k, how in rows:
            label = "mean(%s,%s)" % src[1:] if isinstance(src, tuple) else src
            print("   %-22s -> %-26s [%s]" % (label, k, how))
        if unused[ob.name]:
            u = unused[ob.name]
            print("   unused keys: %s%s" % (", ".join(u[:REPORT_UNUSED_LIMIT]), " ..." if len(u) > REPORT_UNUSED_LIMIT else ""))
        mrep = {"mapped": [["mean(%s,%s)" % s[1:] if isinstance(s, tuple) else s, k, h] for s, k, h in rows],
                "unused_keys": unused[ob.name], "clipped": {}}
        report["meshes"][ob.name] = mrep
        if a.dry_run or not rows:
            continue
        key = ob.data.shape_keys
        prev = key.animation_data.action if key.animation_data else None
        if prev is not None:
            prev.use_fake_user = True
            print("   previous shape-key action %r kept with a fake user" % prev.name)
        wr = ActionWriter(key, "%s.%s" % (base_name, ob.name), "KEY")
        for src, k, how in rows:
            v = 0.5 * (sampled(src[1]) + sampled(src[2])) if isinstance(src, tuple) else sampled(src)
            kb = kbs[k]
            lo, hi = kb.slider_min, kb.slider_max
            clipped = int(np.sum((v < lo - 1e-6) | (v > hi + 1e-6)))
            if clipped:
                mrep["clipped"][k] = clipped
            v = np.clip(v, lo, hi)
            path = 'key_blocks["%s"].value' % bpy.utils.escape_identifier(k)
            total_keys += write_keys(wr.fcurve(path, 0, "Face"), frames, v)
        wr.assign(key)
        mrep["action"] = wr.action.name
        if mrep["clipped"]:
            print("   clipped to slider range: %s" % mrep["clipped"])
    if unmapped:
        print("unmapped curves (%d): %s" % (len(unmapped), ", ".join(unmapped)))

    # ---- bones
    angle_present = [c for c in ANGLE_CURVES if c in curves and np.any(np.abs(curves[c]) > 1e-6)]
    if a.armature:
        arm = bpy.data.objects.get(a.armature)
        if arm is None or arm.type != "ARMATURE":
            die("armature %r not found" % a.armature)
        eyes = [b for b in (a.eye_bones or "").split(",") if b]
        if eyes and len(eyes) != 2:
            die("--eye-bones expects two names: L,R")
        for b in [a.head_bone, a.neck_bone] + eyes:
            if b and b not in arm.pose.bones:
                die("bone %r not in %s; bones: %s" % (b, arm.name, ", ".join(bb.name for bb in arm.data.bones)))
        left, up, fwd, how = character_frame(arm, a.head_bone, eyes, a.facing)
        report["bones"]["facing"] = {"forward": list(fwd), "from": how}
        ad = arm.animation_data
        # an earlier run of this script is replaced, not stacked: its action is tagged "face_curves"
        prev_face = ad.action if (ad and ad.action and ad.action.get("face_curves") is not None) else None
        face_track = next((t for t in ad.nla_tracks if t.name == NLA_TRACK_NAME), None) if ad else None
        other_anim = bool(ad and ((ad.action and prev_face is None) or any(t != face_track for t in ad.nla_tracks)))
        mode = a.bone_mode if a.bone_mode != "auto" else ("layer" if other_anim else "active")
        report["bones"]["mode"] = mode
        jobs = []  # (bone, axes, pitch, yaw, roll)
        hp, hy, hr = (sampled(c) * a.head_scale for c in ("headPitch", "headYaw", "headRoll"))
        share = min(max(a.neck_share, 0.0), 1.0) if (a.neck_bone and a.head_bone) else (1.0 if a.neck_bone else 0.0)
        for bone, part in ((a.neck_bone, share), (a.head_bone, 1.0 - share)):
            if bone and part > 0:
                axes = auto_axes(arm, bone, (left, up, fwd)) if a.head_axes == "auto" else \
                    parse_axes(a.head_axes, ("pitch", "yaw", "roll"))
                jobs.append((bone, axes, hp * part, hy * part, hr * part, part))
        if eyes:
            ep, ey = (sampled(c) * a.eye_scale for c in ("eyePitch", "eyeYaw"))
            for bone in eyes:
                axes = auto_axes(arm, bone, (left, up, fwd)) if a.eye_axes == "auto" else \
                    parse_axes(a.eye_axes, ("pitch", "yaw"))
                axes.pop("roll", None)
                jobs.append((bone, axes, ep, ey, np.zeros(len(frames)), 1.0))
        if eyes and any(c.startswith("eyeLook") for c in mapped_curves):
            print("WARNING: eye bones and eyeLook* shape keys both move the eyes; add --exclude 'eyeLook*' "
                  "if the keys rotate the eyeballs (keep them if they only shape the lids)")
        print("armature %s: bone mode %s, forward from %s (%s)" % (arm.name, mode, how,
                                                                  ", ".join("%+.2f" % c for c in fwd)))
        if jobs and not a.dry_run:
            wr = ActionWriter(arm, "%s.%s" % (base_name, arm.name), "OBJECT")
            wr.action["face_curves"] = str(data.get("id") or "")
            old_base = dict(prev_face.get("face_curves_base", {})) if prev_face else {}
            bases = {}
            for bone, axes, p, y, r, part in jobs:
                pb = arm.pose.bones[bone]
                if mode != "active":
                    base = Quaternion()                       # pure delta, combined by the NLA
                elif prev_face is not None:                   # re-apply: reuse the pose stored by the earlier run
                    base = Quaternion(old_base[bone]) if bone in old_base else Quaternion()
                else:
                    base = current_rotation(pb)               # keep the current static pose under the face
                bases[bone] = list(base)
                nk = key_bone(wr, pb, delta_quats(axes, p, y, r), base, frames)
                total_keys += nk
                lab = ", ".join("%s=%s" % (k, axis_label(v)) for k, v in axes.items())
                print("   %-14s share %.2f  axes %s  (%s, %d keys)" % (bone, part, lab, pb.rotation_mode, nk))
                report["bones"][bone] = {"share": part, "axes": {k: list(v) for k, v in axes.items()},
                                         "rotation_mode": pb.rotation_mode, "keys": nk}
            wr.action["face_curves_base"] = bases
            if prev_face is not None:
                prev_face.use_fake_user = True
                print("   previous face action %r replaced (kept with a fake user)" % prev_face.name)
            if mode == "layer":
                ad = arm.animation_data or arm.animation_data_create()
                if prev_face is not None:
                    ad.action = None
                pushed = push_down_active(ad)
                tr = face_track or ad.nla_tracks.new()
                tr.name = NLA_TRACK_NAME
                for old in list(tr.strips):                  # replace an earlier face layer
                    if old.action:
                        old.action.use_fake_user = True
                    tr.strips.remove(old)
                st = tr.strips.new(wr.action.name, int(frames[0]), wr.action)
                st.blend_type = "COMBINE"
                st.extrapolation = "NOTHING"
                if wr.slot is not None and hasattr(st, "action_slot"):
                    st.action_slot = wr.slot
                report["bones"]["nla"] = {"pushed_down": pushed, "track": tr.name}
                print("   NLA: %s, face layer in track %r (COMBINE)"
                      % ("base action %r pushed into track 'Base'" % pushed if pushed else "no active action",
                         tr.name))
            else:
                wr.assign(arm)
            report["bones"]["action"] = wr.action.name
    elif angle_present:
        print("note: angle curves %s not applied (pass --armature with --head-bone/--neck-bone/--eye-bones)"
              % ", ".join(angle_present))

    # ---- sound, range, save
    if a.sound and not a.dry_run:
        snd = os.path.abspath(a.sound)
        if not os.path.isfile(snd):
            die("sound not found: %s" % snd)
        se = sc.sequence_editor or sc.sequence_editor_create()
        coll = se.strips if hasattr(se, "strips") else se.sequences
        all_strips = se.strips_all if hasattr(se, "strips_all") else se.sequences_all
        ch = max([s.channel for s in all_strips] + [0]) + 1
        strip = coll.new_sound(os.path.basename(snd), snd, ch, int(start))
        sc.sync_mode = "AUDIO_SYNC"
        report["sound"] = {"strip": strip.name, "channel": ch, "frame_start": int(start)}
        print("sound: %s on channel %d at frame %d" % (os.path.basename(snd), ch, start))
    if a.set_range and not a.dry_run:
        sc.frame_start, sc.frame_end = f0, f1
    report["keys_written"] = total_keys
    print("keys written: %d" % total_keys)
    if a.report:
        with open(a.report, "w") as fh:
            json.dump(report, fh, indent=1)
    if a.save and not a.dry_run:
        out = os.path.abspath(a.save)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=out, check_existing=False)
        print("saved %s" % out)
    elif not a.dry_run:
        print("not saved (pass --save out.blend to keep the result; the opened file is never overwritten implicitly)")


if __name__ == "__main__":
    main()
