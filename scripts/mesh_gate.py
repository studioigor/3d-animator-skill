"""Deformation-quality gate for a shape-keyed mesh: folded, tilted and sliver triangles per pose.

Runs inside Blender (headless), never modifies or saves the file:

  blender -b file.blend --python mesh_gate.py -- --mesh Head [--baseline-mesh TemplateHead]
          [--keys all|arkit|none|name,...] [--combo scream] [--combo "mine=jawOpen:0.7,eyeBlink:1"]
          [--exclude-group Interior[,Helpers]] [--include-group Face] [--rim-group Rim]
          [--base current|basis] [--depsgraph] [--json out.json]

Poses: the neutral, every selected shape key at 1.0, and combination poses. Built-in combos (ARKit names,
a name without a side expands to both Left and Right): scream, sob, halfblink, wide. --combo takes built-in
names (comma-separated, or all / none) or a custom name=key:weight,key:weight; repeat it for several.
Default: all built-in combos.

Per triangle (Blender's triangulation of the rest topology):
  fold    dot(triangle normal, smoothed vertex normal of the pose) where the vertex normals are area-weighted
          over the exterior faces and averaged over SMOOTH_PASSES neighbour passes.
          folded: dot < FOLD_DOT;  tilted: dot < TILT_DOT (includes folded).
  sliver  q = 4*sqrt(3)*area / sum(edge length^2) (1 = equilateral); sliver when q < SLIVER_Q.
Not counted: faces whose vertices are all in an --exclude-group (interior surfaces such as a mouth bag, or
helper geometry; they are also left out of the vertex normals), the RIM_RINGS face rings around open
boundary edges and around --rim-group vertices, and, with --include-group, faces outside that group (e.g.
the head region of a full-body mesh; the vertex normals still use every exterior face).

Gate per pose and metric: pass when count <= max(GATE_FACTOR * reference, GATE_FLOOR).
  With --baseline-mesh (a known-good mesh in the same file with the same key names) the reference is the
  baseline's count for the same pose. Without one, the reference is 0 for folded and this mesh's own
  neutral count for tilted and sliver (thin or steep triangles that exist by design are not penalised).
Key names match exactly, then case-insensitively, then across side conventions (Left/_L/.L/L).
Keys under test (selected by --keys or used by a combo) start at 0 in every pose. --base current (default)
holds every other key at its current value (needed when proportion keys, e.g. MakeHuman/MPFB macro targets,
hold the character's shape: gate such a mesh with --keys arkit, not all); --base basis zeroes them too.
Poses are summed from the shape-key deltas with numpy (exact for shape keys, honours relative keys and key
vertex groups). --depsgraph evaluates through Blender instead so drivers and corrective keys apply
(modifiers are switched off for the measurement, in memory only).
Prints a table, writes --json, exits 0 when every pose passes and 1 otherwise.

Limits: the fold test is local. The reference normals follow the pose itself, so a broad fold-over that
spans many more face rings than the smoothing reaches (typical after subdividing the cage) reads as smooth
surface; gate the cage, or raise --smooth-passes about 4x per subdivision level (12 after one level, 48
after two: the smoothing reaches about sqrt(passes) rings). The JSON also lists
rest_flipped per pose (triangles facing against their neutral direction): not gated, because lip rolls and
lid closes rotate skin past 90 degrees legitimately, but a jump there is worth a look.
"""
import argparse, json, math, sys, time

FOLD_DOT = 0.0          # folded: triangle normal points away from the smoothed surface normal
TILT_DOT = 0.5          # tilted: more than 60 degrees off the smoothed surface normal
SLIVER_Q = 0.1          # sliver: triangle quality below this (1.0 = equilateral)
SMOOTH_PASSES = 3       # neighbour-averaging passes for the reference vertex normals
RIM_RINGS = 2           # face rings excluded around open boundaries and the rim group
GROUP_WEIGHT = 0.5      # a vertex belongs to a vertex group at or above this weight
GROUP_FACE_RULE = "all"  # "all": a face is in a group when all its vertices are; "any": when one is
GATE_FACTOR = 2.0       # allowed multiple of the reference count
GATE_FLOOR = 2          # allowed count whatever the reference
GATE_METRICS = ("folded", "tilted", "sliver")  # metrics that decide pass/fail
WORST_N = 20            # worst triangles listed per pose in the JSON
DEGENERATE = 1e-12      # relative area below which a triangle has no usable normal

COMBOS = {
    "scream": {"jawOpen": .94, "eyeSquint": .88, "mouthStretch": .81, "cheekSquint": .78, "browDown": .73,
               "noseSneer": .73, "mouthLowerDown": .58, "mouthUpperUp": .58, "browInnerUp": .57, "eyeBlink": .47},
    "sob": {"browInnerUp": .85, "mouthFrown": .6, "eyeSquint": .45, "jawOpen": .25},
    "halfblink": {"eyeBlink": .5},
    "wide": {"eyeWide": 1.0},
}
ARKIT = ("browDownLeft browDownRight browInnerUp browOuterUpLeft browOuterUpRight cheekPuff cheekSquintLeft "
         "cheekSquintRight eyeBlinkLeft eyeBlinkRight eyeLookDownLeft eyeLookDownRight eyeLookInLeft eyeLookInRight "
         "eyeLookOutLeft eyeLookOutRight eyeLookUpLeft eyeLookUpRight eyeSquintLeft eyeSquintRight eyeWideLeft "
         "eyeWideRight jawForward jawLeft jawOpen jawRight mouthClose mouthDimpleLeft mouthDimpleRight "
         "mouthFrownLeft mouthFrownRight mouthFunnel mouthLeft mouthLowerDownLeft mouthLowerDownRight "
         "mouthPressLeft mouthPressRight mouthPucker mouthRight mouthRollLower mouthRollUpper mouthShrugLower "
         "mouthShrugUpper mouthSmileLeft mouthSmileRight mouthStretchLeft mouthStretchRight mouthUpperUpLeft "
         "mouthUpperUpRight noseSneerLeft noseSneerRight tongueOut").split()
SIDES = [("Left", "Right"), ("_L", "_R"), (".L", ".R"), ("_l", "_r"), (".l", ".r"), ("_left", "_right"),
         ("L", "R")]    # side-suffix conventions tried in this order

try:
    import bpy
    import numpy as np
except ImportError:
    sys.exit("mesh_gate.py runs inside Blender: blender -b file.blend --python mesh_gate.py -- --mesh Head")


# ---------- key names ----------

def resolve(token, names):
    """Key names for a requested token: exact, case-insensitive, then side-suffix variants."""
    if token in names:
        return [token]
    low = {n.lower(): n for n in names}
    if token.lower() in low:
        return [low[token.lower()]]
    for left, right in SIDES:                       # token carries a side: translate the convention
        for side, suf in ((0, left), (1, right)):
            if len(token) > len(suf) and token.endswith(suf):
                stem = token[:-len(suf)]
                for pair in SIDES:
                    hit = low.get((stem + pair[side]).lower())
                    if hit:
                        return [hit]
    for left, right in SIDES:                       # token without a side: both sides
        hits = [low[(token + s).lower()] for s in (left, right) if (token + s).lower() in low]
        if hits:
            return hits
    return []


# ---------- mesh data ----------

class MeshData:
    """Topology, exclusion masks and shape-key coordinates of one mesh object."""

    def __init__(self, obj, exclude_groups, include_group, rim_group, base):
        if obj.type != "MESH":
            raise SystemExit(f"'{obj.name}' is a {obj.type}, not a mesh")
        me = obj.data
        self.obj, self.name = obj, obj.name
        self.nv = len(me.vertices)
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        self.tris = np.empty(nt * 3, np.int64)
        me.loop_triangles.foreach_get("vertices", self.tris)
        self.tris = self.tris.reshape(-1, 3)
        self.tpoly = np.empty(nt, np.int64)
        me.loop_triangles.foreach_get("polygon_index", self.tpoly)
        npoly = len(me.polygons)
        ltot = np.empty(npoly, np.int64)
        me.polygons.foreach_get("loop_total", ltot)
        lvert = np.empty(len(me.loops), np.int64)
        me.loops.foreach_get("vertex_index", lvert)
        ledge = np.empty(len(me.loops), np.int64)
        me.loops.foreach_get("edge_index", ledge)
        lface = np.repeat(np.arange(npoly), ltot)
        self.warnings = []

        def group_mask(name):
            m = np.zeros(self.nv, bool)
            if not name:
                return m
            vg = obj.vertex_groups.get(name)
            if vg is None:
                self.warnings.append(f"{obj.name}: no vertex group '{name}'")
                return m
            gi = vg.index
            for v in me.vertices:
                for g in v.groups:
                    if g.group == gi and g.weight >= GROUP_WEIGHT:
                        m[v.index] = True
            return m

        def face_in(vmask):
            cnt = np.zeros(npoly, np.int64)
            np.add.at(cnt, lface, vmask[lvert])
            return (cnt == ltot) if GROUP_FACE_RULE == "all" else (cnt > 0)

        # faces in the exclude groups (interior, helpers) and outside the include group
        face_group = np.zeros(npoly, bool)
        for name in exclude_groups:
            face_group |= face_in(group_mask(name))
        face_out = ~face_in(group_mask(include_group)) if include_group else np.zeros(npoly, bool)
        # rings around open boundaries and the rim group
        ecount = np.bincount(ledge, minlength=len(me.edges))
        ev = np.empty(len(me.edges) * 2, np.int64)
        me.edges.foreach_get("vertices", ev)
        ev = ev.reshape(-1, 2)
        seed = np.zeros(self.nv, bool)
        seed[ev[ecount == 1].ravel()] = True
        seed |= group_mask(rim_group)
        face_rim = np.zeros(npoly, bool)
        cur = seed.copy()
        for _ in range(RIM_RINGS):
            hit = np.zeros(npoly, bool)
            np.logical_or.at(hit, lface, cur[lvert])
            face_rim |= hit
            cur[lvert[hit[lface]]] = True
        self.ext_tri = ~face_group[self.tpoly]                       # used for the reference normals
        not_rim = self.ext_tri & ~face_rim[self.tpoly]
        self.count_tri = not_rim & ~face_out[self.tpoly]                # counted in the gate
        self.excluded = {"triangles": int(nt), "interior_group": int((~self.ext_tri).sum()),
                         "rim_rings": int((self.ext_tri & ~not_rim).sum()),
                         "outside_include_group": int((not_rim & ~self.count_tri).sum()),
                         "counted": int(self.count_tri.sum()), "boundary_edges": int((ecount == 1).sum())}
        et = self.tris[self.ext_tri]
        e = np.concatenate([et[:, [0, 1]], et[:, [1, 2]], et[:, [2, 0]]])
        e = np.unique(np.sort(e, axis=1), axis=0)
        self.edges = np.concatenate([e, e[:, ::-1]])                 # both directions for neighbour sums

        # shape keys
        sk = me.shape_keys
        self.keys, self.delta, self.base_mix = [], {}, {}
        co = np.empty(self.nv * 3, np.float64)
        me.vertices.foreach_get("co", co)
        self.basis = co.reshape(-1, 3).copy()
        if sk:
            if not sk.use_relative:
                raise SystemExit(f"{obj.name}: absolute shape keys are not supported")
            kbs = sk.key_blocks
            raw = {}
            for kb in kbs:
                buf = np.empty(self.nv * 3, np.float64)
                kb.data.foreach_get("co", buf)
                raw[kb.name] = buf.reshape(-1, 3)
            ref = sk.reference_key
            self.basis = raw[ref.name].copy()
            for kb in kbs:
                if kb == ref:
                    continue
                d = raw[kb.name] - raw[kb.relative_key.name]
                if kb.vertex_group:
                    vg = obj.vertex_groups.get(kb.vertex_group)
                    w = np.zeros(self.nv)
                    if vg is not None:
                        for v in me.vertices:
                            for g in v.groups:
                                if g.group == vg.index:
                                    w[v.index] = g.weight
                    d = d * w[:, None]
                self.delta[kb.name] = d
                self.keys.append(kb.name)
                if base == "current" and not kb.mute and kb.value != 0:
                    self.base_mix[kb.name] = float(kb.value)

    def pose_co(self, weights):
        co = self.basis.copy()
        for k, w in weights.items():
            if w:
                co += w * self.delta[k]
        return co


def depsgraph_co(md, weights):
    """Evaluate the pose through the depsgraph (drivers apply); modifiers were switched off by the caller."""
    kbs = md.obj.data.shape_keys.key_blocks
    for kb in kbs[1:]:
        kb.value = weights.get(kb.name, 0.0)
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    ev = md.obj.evaluated_get(dg)
    m = ev.to_mesh()
    if len(m.vertices) != md.nv:
        ev.to_mesh_clear()
        raise SystemExit(f"{md.name}: evaluated mesh has {len(m.vertices)} vertices, not {md.nv}")
    co = np.empty(md.nv * 3, np.float64)
    m.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    return co.reshape(-1, 3)


# ---------- metrics ----------

def measure(md, co, passes=SMOOTH_PASSES, rest_n=None):
    t = md.tris
    a, b, c = co[t[:, 0]], co[t[:, 1]], co[t[:, 2]]
    cr = np.cross(b - a, c - a)
    area2 = np.linalg.norm(cr, axis=1)                                # twice the area
    l2 = ((b - a) ** 2).sum(1) + ((c - b) ** 2).sum(1) + ((a - c) ** 2).sum(1)
    ok = area2 > DEGENERATE * np.maximum(l2, 1e-30)
    n = np.zeros_like(cr)
    n[ok] = cr[ok] / area2[ok, None]
    vn = np.zeros_like(co)
    ext = md.ext_tri
    for k in range(3):
        np.add.at(vn, t[ext, k], cr[ext])
    vn /= np.maximum(np.linalg.norm(vn, axis=1), 1e-30)[:, None]
    for _ in range(passes):
        s = vn.copy()
        np.add.at(s, md.edges[:, 0], vn[md.edges[:, 1]])
        vn = s / np.maximum(np.linalg.norm(s, axis=1), 1e-30)[:, None]
    ref = vn[t[:, 0]] + vn[t[:, 1]] + vn[t[:, 2]]
    ref /= np.maximum(np.linalg.norm(ref, axis=1), 1e-30)[:, None]
    dot = np.where(ok, (n * ref).sum(1), 1.0)
    q = 2.0 * math.sqrt(3.0) * area2 / np.maximum(l2, 1e-30)          # = 4*sqrt(3)*A / sum(l^2)
    cnt = md.count_tri
    folded, tilted, sliver = cnt & (dot < FOLD_DOT), cnt & (dot < TILT_DOT), cnt & (q < SLIVER_Q)
    order = np.argsort(np.where(cnt, dot, 9.0))[:WORST_N]
    centers = (a + b + c) / 3.0
    worst = [{"poly": int(md.tpoly[i]), "dot": round(float(dot[i]), 3), "q": round(float(q[i]), 3),
              "center": [round(float(x), 5) for x in centers[i]]} for i in order if cnt[i] and dot[i] < TILT_DOT]
    res = {"folded": int(folded.sum()), "tilted": int(tilted.sum()), "sliver": int(sliver.sum()),
           "min_dot": round(float(dot[cnt].min()), 3) if cnt.any() else None,
           "min_q": round(float(q[cnt].min()), 3) if cnt.any() else None, "worst": worst, "_n": n}
    if rest_n is not None:                     # informational: triangles facing against their neutral direction
        res["rest_flipped"] = int((cnt & ok & ((n * rest_n).sum(1) < 0)).sum())
    return res


# ---------- poses ----------

def build_poses(a, keys):
    """List of (pose name, {requested token: weight}) plus notes on what did not resolve."""
    poses, notes = [("neutral", {})], []
    sel = a.keys.strip()
    if sel.lower() == "all":
        chosen = list(keys)
    elif sel.lower() == "arkit":
        chosen = [k for k in keys if k in ARKIT or k.lower() in {n.lower() for n in ARKIT}]
    elif sel.lower() == "none":
        chosen = []
    else:
        chosen = []
        for tok in [s.strip() for s in sel.split(",") if s.strip()]:
            hits = resolve(tok, keys)
            if not hits:
                notes.append(f"key '{tok}' not found")
            chosen += [h for h in hits if h not in chosen]
    poses += [(k, {k: 1.0}) for k in chosen]
    combos = {}
    for val in (a.combo if a.combo else ["all"]):
        if "=" in val:
            name, spec = val.split("=", 1)
            w = {}
            for part in spec.split(","):
                if ":" not in part:
                    raise SystemExit(f"--combo {val}: each entry must be key:weight")
                k, x = part.rsplit(":", 1)
                w[k.strip()] = float(x)
            combos[name.strip()] = w
        else:
            for name in [s.strip() for s in val.split(",") if s.strip()]:
                if name.lower() == "none":
                    continue
                if name.lower() == "all":
                    combos.update(COMBOS)
                elif name in COMBOS:
                    combos[name] = COMBOS[name]
                else:
                    raise SystemExit(f"unknown combo '{name}' (built in: {', '.join(COMBOS)}; "
                                     f"custom: --combo name=key:w,key:w)")
    poses += [(f"combo:{n}", w) for n, w in combos.items()]
    return poses, notes


def pose_weights(md, tokens):
    """Resolve the pose's tokens against this mesh: (full weights incl. the base mix, pose-only weights, missing)."""
    own, missing = {}, []
    for tok, x in tokens.items():
        hits = resolve(tok, md.keys)
        if not hits:
            missing.append(tok)
        for h in hits:
            own[h] = x
    full = dict(md.base_mix)
    full.update(own)
    return full, own, missing


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="mesh_gate.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", required=True, help="mesh object to gate")
    ap.add_argument("--baseline-mesh", help="known-good mesh object in the same file with the same key names")
    ap.add_argument("--keys", default="all", help="all | arkit | none | comma-separated key names")
    ap.add_argument("--combo", action="append", help="built-in names (scream,sob,halfblink,wide | all | none) "
                                                     "or name=key:w,key:w; repeatable")
    ap.add_argument("--exclude-group", help="vertex group(s) of faces to ignore, comma-separated "
                                            "(interior surfaces, helper geometry)")
    ap.add_argument("--include-group", help="count only faces inside this vertex group (e.g. the head region)")
    ap.add_argument("--rim-group", help="vertex group treated like an open boundary (rings excluded)")
    ap.add_argument("--base", choices=("current", "basis"), default="current",
                    help="starting key mix: the file's current key values, or the bare basis")
    ap.add_argument("--depsgraph", action="store_true", help="evaluate poses through Blender (drivers apply)")
    ap.add_argument("--smooth-passes", type=int, default=SMOOTH_PASSES,
                    help="neighbour passes for the reference normals (raise on dense meshes, see the notes)")
    ap.add_argument("--json", help="write the full result here")
    a = ap.parse_args(argv)
    t0 = time.time()

    objs = {}
    for role, name in (("mesh", a.mesh), ("baseline", a.baseline_mesh)):
        if name:
            o = bpy.data.objects.get(name)
            if o is None:
                meshes = [o.name for o in bpy.data.objects if o.type == "MESH"]
                raise SystemExit(f"no object '{name}' in {bpy.data.filepath or 'the scene'}; meshes: {meshes}")
            excl = [g.strip() for g in (a.exclude_group or "").split(",") if g.strip()]
            objs[role] = MeshData(o, excl, a.include_group, a.rim_group, a.base)
    md, bd = objs["mesh"], objs.get("baseline")
    poses, notes = build_poses(a, md.keys)
    for n in notes:
        print("warning:", n)
    for m in (md, bd):
        if m:
            for w in m.warnings:
                print("warning:", w)
            tested = {h for _, tokens in poses for tok in tokens for h in resolve(tok, m.keys)}
            m.base_mix = {k: v for k, v in m.base_mix.items() if k not in tested}   # keys under test start at 0
            if m.base_mix:
                print(f"note: {m.name} holds its current values of keys not under test: "
                      + ", ".join(f"{k}={v:.3g}" for k, v in m.base_mix.items()))

    saved_vis, saved_vals = [], []                                 # in-memory state for --depsgraph
    if a.depsgraph:
        for m in (md, bd):
            if m and m.obj.data.shape_keys:
                sk = m.obj.data.shape_keys
                if sk.animation_data and sk.animation_data.drivers:
                    print(f"note: {m.name} has {len(sk.animation_data.drivers)} driven key values; "
                          "they follow their drivers")
                saved_vis += [(mod, mod.show_viewport) for mod in m.obj.modifiers]
                saved_vals += [(kb, kb.value) for kb in sk.key_blocks]
        for mod, _ in saved_vis:
            mod.show_viewport = False

    rest = {}

    def evaluate(m, w):
        r = measure(m, depsgraph_co(m, w) if a.depsgraph else m.pose_co(w), a.smooth_passes, rest.get(m.name))
        rest.setdefault(m.name, r["_n"])       # the first pose evaluated per mesh is its neutral
        r.pop("_n")
        return r

    results, all_pass, own_neutral = [], True, None
    for pname, tokens in poses:
        w, own, missing = pose_weights(md, tokens)
        if tokens and len(missing) == len(tokens):
            results.append({"pose": pname, "skipped": f"no matching keys for {sorted(tokens)}"})
            continue
        r = {"pose": pname, "weights": own, **evaluate(md, w)}
        if missing:
            r["missing_keys"] = missing
        if pname == "neutral":
            own_neutral = r
        ref, ref_src = None, None
        if bd:
            bw, _, bmiss = pose_weights(bd, tokens)
            if not tokens or len(bmiss) < len(tokens):
                b = evaluate(bd, bw)
                r["baseline"] = {k: b[k] for k in ("folded", "tilted", "sliver", "min_dot", "min_q")}
                if bmiss:
                    r["baseline"]["missing_keys"] = bmiss
                ref, ref_src = b, "baseline"
        if ref is None:
            ref = {"folded": 0, "tilted": own_neutral["tilted"], "sliver": own_neutral["sliver"]}
            ref_src = "own neutral (folded: 0)"
        r["limit"] = {k: max(GATE_FACTOR * ref[k], GATE_FLOOR) for k in GATE_METRICS}
        r["reference"] = ref_src
        r["pass"] = all(r[k] <= r["limit"][k] for k in GATE_METRICS)
        all_pass &= r["pass"]
        results.append(r)

    for kb, v in saved_vals:                                        # restore in-memory state (never saved)
        kb.value = v
    for mod, v in saved_vis:
        mod.show_viewport = v

    # table
    print(f"\nmesh_gate  {md.name}  ({bpy.data.filepath})  baseline: {bd.name if bd else 'none'}")
    ex = md.excluded
    print(f"triangles {ex['triangles']}: counted {ex['counted']}, excluded group {ex['interior_group']}, "
          f"rim rings {ex['rim_rings']} ({ex['boundary_edges']} open boundary edges), "
          f"outside include group {ex['outside_include_group']}")
    if bd:
        bx = bd.excluded
        print(f"baseline {bd.name}: triangles {bx['triangles']}, counted {bx['counted']}")
    hdr = f"{'pose':34s} {'fold':>5s} {'tilt':>5s} {'slvr':>5s}  {'base f/t/s':>14s}  {'limit f/t/s':>16s}  result"
    print(hdr + "\n" + "-" * len(hdr))
    for r in results:
        if "skipped" in r:
            print(f"{r['pose'][:34]:34s} skipped: {r['skipped']}")
            continue
        bs = r.get("baseline")
        bstr = f"{bs['folded']}/{bs['tilted']}/{bs['sliver']}" if bs else "-"
        lim = r["limit"]
        lstr = "/".join(f"{lim[k]:g}" for k in ("folded", "tilted", "sliver") if k in lim)
        flag = "pass" if r["pass"] else "FAIL"
        extra = f"  (missing {','.join(r['missing_keys'])})" if r.get("missing_keys") else ""
        print(f"{r['pose'][:34]:34s} {r['folded']:5d} {r['tilted']:5d} {r['sliver']:5d}  {bstr:>14s}  "
              f"{lstr:>16s}  {flag}{extra}")
    nrun = sum(1 for r in results if "pass" in r)
    npass = sum(1 for r in results if r.get("pass"))
    nskip = len(results) - nrun
    print(f"\n{'PASS' if all_pass else 'FAIL'}: {npass} of {nrun} poses pass"
          + (f", {nskip} skipped" if nskip else "") + f" ({time.time() - t0:.1f}s)")

    if a.json:
        out = {"file": bpy.data.filepath, "mesh": md.name, "baseline_mesh": bd.name if bd else None,
               "settings": {"fold_dot": FOLD_DOT, "tilt_dot": TILT_DOT, "sliver_q": SLIVER_Q,
                            "smooth_passes": a.smooth_passes, "rim_rings": RIM_RINGS, "gate_factor": GATE_FACTOR,
                            "gate_floor": GATE_FLOOR, "gate_metrics": list(GATE_METRICS), "base": a.base,
                            "exclude_group": a.exclude_group, "include_group": a.include_group,
                            "rim_group": a.rim_group,
                            "evaluation": "depsgraph" if a.depsgraph else "numpy key sum"},
               "excluded": md.excluded, "baseline_excluded": bd.excluded if bd else None,
               "base_mix": md.base_mix, "warnings": md.warnings + notes + (bd.warnings if bd else []),
               "poses": results, "pass": all_pass}
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
        print("wrote", a.json)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
