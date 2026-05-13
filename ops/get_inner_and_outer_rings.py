# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
# get_inner_and_outer_rings.py
import bmesh, math
from mathutils import Vector, Matrix

ANGLE_OPPOSE_MAX_DEG = 20.0
PLANE_DIST_EPS       = 0.2
MIN_INNER_AREA       = 0.10
INWARD_MIN_DOT       = 0.10
EPS                  = 1e-6

def _safe_face_normal(f):
    n = Vector(f.normal)
    if n.length > EPS: return n.normalized()
    vs = list(f.verts)
    if len(vs) < 3: return Vector((0,0,0))
    v0 = vs[0].co
    for i in range(1, len(vs)-1):
        c = (vs[i].co - v0).cross(vs[i+1].co - v0)
        if c.length > EPS: return c.normalized()
    return Vector((0,0,0))

def _object_center_world(obj_eval):
    bb = [obj_eval.matrix_world @ Vector(corner) for corner in obj_eval.bound_box]
    c = Vector((0,0,0))
    for p in bb: c += p
    return c / 8.0

def _build_projection_matrix(normal):
    z = normal.normalized()
    x = Vector((1,0,0)) if abs(z.x) < 0.9 else Vector((0,1,0))
    y = z.cross(x).normalized()
    x = y.cross(z).normalized()
    return Matrix((x, y, z)).transposed()

def _project2(normal, pts_world, center_world):
    P = _build_projection_matrix(normal)
    return [(P @ (p - center_world)).to_2d()[:] for p in pts_world]

def _signed_area2(pts):
    s = 0.0
    for i in range(len(pts)):
        x1,y1 = pts[i]; x2,y2 = pts[(i+1)%len(pts)]
        s += x1*y2 - x2*y1
    return 0.5*s

def _pip(pt, poly):
    x, y = pt; inside = False
    n = len(poly)
    for i in range(n):
        x1,y1 = poly[i]; x2,y2 = poly[(i+1)%n]
        if (y1>y) != (y2>y):
            xint = (x2-x1)*(y-y1)/(y2-y1+1e-18) + x1
            if x < xint: inside = not inside
    return inside

def _distance_point_plane(p, origin, normal):
    return abs((p - origin).dot(normal))

def detect_face_groups(obj_eval, mesh):
    """Rückgabe: Liste von Paaren [outer_dict, inner_dict] mit {index, center, area}"""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()

    obj_c = _object_center_world(obj_eval)

    faces = []
    for f in bm.faces:
        a = f.calc_area()
        if a <= EPS: continue
        n = _safe_face_normal(f)
        if n.length <= EPS: continue
        vsw = [obj_eval.matrix_world @ v.co for v in f.verts]
        cw  = sum(vsw, Vector((0,0,0))) / len(vsw)
        faces.append({
            "bm": f,
            "index": f.index,
            "area": float(a),
            "normal": n,
            "verts_world": vsw,
            "center_world": cw,
        })

    # Interior-Pre-Filter
    candidate_inners = []
    for d in faces:
        """ if d["area"] < MIN_INNER_AREA:
            continue """
        to_center = obj_c - d["center_world"]
        if to_center.length > EPS:
            to_center.normalize()
            """ if d["normal"].dot(to_center) < INWARD_MIN_DOT:
                continue """
        candidate_inners.append(d)

    pairs, used_inner = [], set()

    for inner in candidate_inners:
        if inner["index"] in used_inner:
            continue

        best, best_score = None, float("inf")
        for outer in faces:
            if outer["index"] == inner["index"]:
                continue
            try:
                ang = math.degrees(outer["normal"].angle(inner["normal"]))
            except Exception:
                ang = 180.0
            if abs(180.0 - ang) > ANGLE_OPPOSE_MAX_DEG:
                continue

            origin = outer["verts_world"][0]
            n_out = outer["normal"]
            if any(_distance_point_plane(p, origin, n_out) > PLANE_DIST_EPS for p in inner["verts_world"]):
                continue

            outer2 = _project2(n_out, outer["verts_world"], outer["center_world"])
            inner2 = _project2(n_out, inner["verts_world"], outer["center_world"])
            if not inner2:
                continue
            if not all(_pip(p, outer2) for p in inner2):
                continue

            ao = abs(_signed_area2(outer2))
            ai = abs(_signed_area2(inner2))
            if ai <= EPS or ao <= ai + 1e-9:
                continue

            score = ao - ai
            if score < best_score:
                best_score = score
                best = outer

        if best is not None:
            pairs.append(
                [
                    {"index": best["index"],
                     "center": (best["center_world"].x, best["center_world"].y, best["center_world"].z),
                     "area": best["area"]},
                    {"index": inner["index"],
                     "center": (inner["center_world"].x, inner["center_world"].y, inner["center_world"].z),
                     "area": inner["area"]},
                ]
            )
            used_inner.add(inner["index"])

    bm.free()
    return pairs
