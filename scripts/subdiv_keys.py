"""Subdivide a shape-keyed mesh with Catmull-Clark and carry every shape key over exactly.

Runs inside Blender (headless):

  blender -b in.blend --python subdiv_keys.py -- --mesh Head --levels 1 --save out.blend
  blender -b in.blend --python subdiv_keys.py -- --mesh Head --levels 2            # dry run, nothing saved

Catmull-Clark is linear in the control points, so subdiv(basis + delta) = subdiv(basis) + subdiv(delta):
each key's absolute shape is pushed through the same subdivision once (one evaluation per key per level),
which keeps relative keys, key vertex groups, slider ranges, values, mutes and the drivers or action on
the key datablock. UVs are interpolated linearly (uv_smooth NONE) so textures painted on the coarse mesh
still line up; boundaries keep their corners (PRESERVE_CORNERS); edge creases are honoured; the limit
surface is off, so the result is the plain subdivision. Vertex groups come along; the object's other
modifiers and materials stay as they are. Custom split normals are not carried.

Every level is checked before anything is saved: the new mesh must have V + E + F vertices in the order
originals, one per edge, one per face, and the face points must sit on the face centroids. One key (the
one with the largest delta) is also re-derived as subdiv(delta) to confirm linearity; --verify-all does
this for every key.

The input file is never overwritten unless --save names it. Without --save nothing is written.
"""
import argparse, os, sys, time

POINT_TOL = 1e-5         # face-point check tolerance, as a fraction of the mesh bounding-box diagonal
LINEAR_TOL = 1e-5        # linearity check tolerance, as a fraction of the bounding-box diagonal
TMP_NAME = "_subdiv_keys_tmp"  # name of the temporary object used for evaluation (removed afterwards)

try:
    import bpy
    import numpy as np
except ImportError:
    sys.exit("subdiv_keys.py runs inside Blender: blender -b in.blend --python subdiv_keys.py -- --mesh Head")


def read_co(coll, n):
    buf = np.empty(n * 3, np.float64)
    coll.foreach_get("co", buf)
    return buf.reshape(-1, 3)


class Subdivider:
    """A temporary key-free copy of the mesh with one Catmull-Clark level; evaluates any vertex positions."""

    def __init__(self, me):
        self.me = me.copy()
        self.obj = bpy.data.objects.new(TMP_NAME, self.me)
        bpy.context.scene.collection.objects.link(self.obj)
        if self.me.shape_keys:
            self.obj.shape_key_clear()
        m = self.obj.modifiers.new("subdiv_keys", "SUBSURF")
        m.subdivision_type = "CATMULL_CLARK"
        m.levels = m.render_levels = 1
        m.uv_smooth = "NONE"
        m.boundary_smooth = "PRESERVE_CORNERS"
        m.use_limit_surface = False
        m.use_creases = True
        m.use_custom_normals = False
        self.n = len(me.vertices)

    def _evaluated(self, co):
        self.me.vertices.foreach_set("co", np.ascontiguousarray(co, np.float32).ravel())
        self.me.update()
        dg = bpy.context.evaluated_depsgraph_get()
        dg.update()
        return self.obj.evaluated_get(dg), dg

    def positions(self, co):
        ev, _ = self._evaluated(co)
        m = ev.to_mesh()
        out = read_co(m.vertices, len(m.vertices))
        ev.to_mesh_clear()
        return out

    def new_mesh(self, co):
        ev, dg = self._evaluated(co)
        return bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)

    def free(self):
        bpy.data.objects.remove(self.obj)          # in-memory temporary datablocks only
        bpy.data.meshes.remove(self.me)


def subdivide_once(obj, verify_all, level):
    me = obj.data
    V, E, F = len(me.vertices), len(me.edges), len(me.polygons)
    sk = me.shape_keys
    kbs = list(sk.key_blocks) if sk else []
    meta = [{"name": k.name, "value": k.value, "mute": k.mute, "rel": k.relative_key.name,
             "vgroup": k.vertex_group, "smin": k.slider_min, "smax": k.slider_max,
             "interp": k.interpolation} for k in kbs]
    key_co = [read_co(k.data, V) for k in kbs]
    mesh_co = read_co(me.vertices, V)
    diag = float(np.linalg.norm(mesh_co.max(0) - mesh_co.min(0))) or 1.0
    use_relative = sk.use_relative if sk else True
    anim = sk.animation_data if sk else None

    sub = Subdivider(me)
    try:
        new = sub.new_mesh(mesh_co)
        co_new = read_co(new.vertices, len(new.vertices))
        # ordering check: originals, then one point per edge, then one per face sitting on the face centroid
        if len(new.vertices) != V + E + F:
            raise SystemExit(f"level {level}: expected V+E+F = {V + E + F} vertices, got {len(new.vertices)}")
        ltot = np.empty(F, np.int64)
        me.polygons.foreach_get("loop_total", ltot)
        lvert = np.empty(len(me.loops), np.int64)
        me.loops.foreach_get("vertex_index", lvert)
        cent = np.zeros((F, 3))
        np.add.at(cent, np.repeat(np.arange(F), ltot), mesh_co[lvert])
        cent /= ltot[:, None]
        face_err = float(np.abs(co_new[V + E:] - cent).max()) if F else 0.0
        if face_err > POINT_TOL * diag:
            raise SystemExit(f"level {level}: face points are off the face centroids by {face_err:.3g} "
                             f"(vertex order is not originals/edges/faces); nothing saved")
        new_keys = [sub.positions(c) for c in key_co]
        # linearity check: subdiv(key) - subdiv(ref) == subdiv(key - ref)
        lin_err = 0.0
        if len(kbs) > 1:
            idx = {m["name"]: i for i, m in enumerate(meta)}
            cands = list(range(1, len(kbs)))
            if not verify_all:
                cands = [max(cands, key=lambda i: np.abs(key_co[i] - key_co[idx[meta[i]["rel"]]]).max())]
            zero = sub.positions(np.zeros_like(mesh_co))            # affine offset (exactly 0 for subdivision)
            for i in cands:
                r = idx[meta[i]["rel"]]
                lin = sub.positions(key_co[i] - key_co[r])
                lin_err = max(lin_err, float(np.abs((new_keys[i] - new_keys[r]) - (lin - zero)).max()))
            if lin_err > LINEAR_TOL * diag:
                raise SystemExit(f"level {level}: linearity check failed ({lin_err:.3g}); nothing saved")
    finally:
        sub.free()

    old_name, users = me.name, me.users
    obj.data = new
    new.name = old_name + f"_sd{level}"
    if len(new.materials) == 0:
        for mat in me.materials:
            new.materials.append(mat)
    if kbs:
        for i, m in enumerate(meta):
            kb = obj.shape_key_add(name=m["name"], from_mix=False)
            kb.data.foreach_set("co", new_keys[i].astype(np.float32).ravel())
        nkb = new.shape_keys.key_blocks
        new.shape_keys.use_relative = use_relative
        for m in meta:
            kb = nkb[m["name"]]
            kb.relative_key = nkb[m["rel"]]
            kb.vertex_group = m["vgroup"]
            kb.slider_min, kb.slider_max = m["smin"], m["smax"]
            kb.value, kb.mute, kb.interpolation = m["value"], m["mute"], m["interp"]
        if anim:
            copy_key_animation(anim, new.shape_keys)
    return {"level": level, "verts": [V, len(new.vertices)], "faces": [F, len(new.polygons)],
            "keys": len(kbs), "face_point_err": face_err, "linearity_err": lin_err,
            "old_mesh_users": users - 1}


def copy_key_animation(src, key):
    """Move drivers and the action from the old shape-key datablock to the new one."""
    dst = key.animation_data_create()
    try:
        if src.action:
            dst.action = src.action
            if hasattr(src, "action_slot") and src.action_slot:
                dst.action_slot = src.action_slot
        for fc in src.drivers:
            dst.drivers.from_existing(src_driver=fc)
    except (AttributeError, RuntimeError, TypeError) as e:
        print(f"warning: could not copy all key animation/drivers: {e}")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="subdiv_keys.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", required=True, help="mesh object to subdivide")
    ap.add_argument("--levels", type=int, default=1, help="Catmull-Clark levels (each one ~4x the faces)")
    ap.add_argument("--save", help="write the result to this .blend (the input only if named here)")
    ap.add_argument("--verify-all", action="store_true", help="linearity-check every key, not just one")
    a = ap.parse_args(argv)
    if a.levels < 1:
        raise SystemExit("--levels must be 1 or more")
    obj = bpy.data.objects.get(a.mesh)
    if obj is None or obj.type != "MESH":
        meshes = [o.name for o in bpy.data.objects if o.type == "MESH"]
        raise SystemExit(f"no mesh object '{a.mesh}'; meshes: {meshes}")
    if bpy.context.scene.collection.objects.get(TMP_NAME) or bpy.data.objects.get(TMP_NAME):
        raise SystemExit(f"an object named {TMP_NAME} already exists; rename it first")
    groups = [g.name for g in obj.vertex_groups]
    if any(m.type == "SUBSURF" for m in obj.modifiers):
        print("note: the object already has a Subdivision modifier; lower it to keep the same final density")
    t0 = time.time()
    for lv in range(1, a.levels + 1):
        r = subdivide_once(obj, a.verify_all, lv)
        print(f"level {lv}: {r['verts'][0]} -> {r['verts'][1]} verts, {r['faces'][0]} -> {r['faces'][1]} faces, "
              f"{r['keys']} keys, face-point err {r['face_point_err']:.2e}, linearity err {r['linearity_err']:.2e}"
              + (f", old mesh still used by {r['old_mesh_users']} other object(s)" if r["old_mesh_users"] else ""))
    lost = [g for g in groups if g not in [x.name for x in obj.vertex_groups]]
    if lost:
        print(f"warning: vertex groups lost: {lost}")
    print(f"vertex groups carried: {len(obj.vertex_groups)}  ({time.time() - t0:.1f}s)")
    if not a.save:
        print("dry run: nothing saved (pass --save out.blend)")
        return
    src = os.path.realpath(bpy.data.filepath) if bpy.data.filepath else ""
    dst = os.path.realpath(os.path.expanduser(a.save))
    if dst == src:
        print("note: --save names the input file; overwriting it as asked")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=dst, check_existing=False)
    print("SAVED", dst)


if __name__ == "__main__":
    main()
