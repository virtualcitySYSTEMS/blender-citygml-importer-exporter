# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Geometry Utilities for CityGML ModelTyper
Enthält alle geometrischen Hilfsfunktionen und Algorithmen
"""

import bmesh
import math
import statistics
from mathutils import Vector, kdtree
from mathutils.bvhtree import BVHTree

# =========================
# Parameter
# =========================
ROOF_MAX_SLOPE_DEG   = 65.0     # θ zur Z-Achse
UPWARD_MIN_NZ        = 0.25     # n·ẑ >= 0.25 → aufwärts
VERTICAL_MIN_DEG     = 80.0     # Wand ~90°
HORIZ_COS            = 0.97     # |n·ẑ| ≥ 0.97 → nahezu horizontal

PAIR_MIN_T           = 0.05     # min Plattendicke (m)
PAIR_MAX_T_FACTOR    = 0.08     # max Dicke relativ zur BBox-Diagonale
PAIR_MAX_XY_DIST     = 0.40     # seitlicher Versatz (m)

RANSAC_DIST          = 0.05     # Planen-Inlier-Schwelle (m)
RANSAC_ITERS         = 250
RG_ANGLE_MAX         = 12.0     # Nachbarschaft: Normalähnlichkeit
RG_DIHED_MAX         = 25.0     # Nachbarschaft: Kantenwinkel
RG_H_CONT            = 0.25     # Nachbarschaft: Höhenkontinuität

GROUND_SUPPORT_H_ABS = 0.02     # relative Höhen-Diff. für „über" Ground
GROUND_BOTTOM_BAND   = 0.08     # Unteres Band der Höhe für Ground-Kandidaten

COPLANAR_ANG_MAX     = 3.0      # max Winkel zwischen Normalen in Gruppe (°)
COPLANAR_DIST_REL    = 0.01     # max Abstand zur Bezugs-Ebene relativ zur Diagonale

OFLOOR_TOP_MARGIN    = 0.02     # OuterFloor darf nicht im obersten +Z-Band liegen (rel. zur Z-Spanne)
MIN_ROOF_SLOPE_DEG  = 10.0      # Mindestneigung für Roof (gegen horizontale Decks)

GROUND_BAND_REL     = 0.03   # vorher 0.01
GROUND_MIN_AREA_REL = 0.0005 # vorher 0.001


# =========================
# Basic Utilities
# =========================

def bm_from_object(obj):
    """Create BMesh from object"""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    return bm

def bm_bounds_local(bm):
    """Get bounding box of BMesh"""
    mins = Vector((float("inf"),)*3)
    maxs = Vector((float("-inf"),)*3)
    for v in bm.verts:
        co = v.co
        mins.x = min(mins.x, co.x); mins.y = min(mins.y, co.y); mins.z = min(mins.z, co.z)
        maxs.x = max(maxs.x, co.x); maxs.y = max(maxs.y, co.y); maxs.z = max(maxs.z, co.z)
    return mins, maxs

def bbox_diag(mins, maxs):
    """Calculate bounding box diagonal"""
    return (maxs - mins).length

def estimate_grid_step(bm):
    """Estimate grid step size from edge lengths"""
    lens = []
    for i, e in enumerate(bm.edges):
        lens.append((e.verts[0].co - e.verts[1].co).length)
        if i >= 2000:
            break
    if not lens: return 0.5
    m = statistics.median(lens)
    return max(0.25, min(2.0, m))

def build_adjacency(bm):
    """Build face adjacency graph"""
    n = len(bm.faces)
    nbrs = [[] for _ in range(n)]
    dih = {}
    for e in bm.edges:
        lf = e.link_faces
        if len(lf) == 2:
            i = lf[0].index; j = lf[1].index
            nbrs[i].append(j); nbrs[j].append(i)
            try:
                ang = e.calc_face_angle() or 0.0
                deg = abs(math.degrees(ang))
            except Exception:
                n0 = lf[0].normal.normalized(); n1 = lf[1].normal.normalized()
                deg = math.degrees(n0.angle(n1))
            dih[(min(i,j), max(i,j))] = deg
    return nbrs, dih

def face_center_local(f):
    """Get face center"""
    return f.calc_center_median()

def face_is_horizontal(n):
    """Check if face normal is horizontal"""
    return abs(n.z) >= HORIZ_COS

def face_is_vertical(n):
    """Check if face normal is vertical"""
    theta = math.degrees(n.angle(Vector((0,0,1))))
    return theta >= VERTICAL_MIN_DEG


# =========================
# M1: Upper-/Lower-Envelope (Raycast)
# =========================
def raycast_envelopes(bm, step):
    """Detect top and bottom faces using raycasting"""
    mins, maxs = bm_bounds_local(bm)
    z_top = maxs.z + 1e-3
    z_bot = mins.z - 1e-3
    span = (maxs.z - mins.z) + 2e-3
    bvh = BVHTree.FromBMesh(bm, epsilon=0.0)
    top_hits = set()
    bot_hits = set()
    x = mins.x
    while x <= maxs.x + 1e-9:
        y = mins.y
        while y <= maxs.y + 1e-9:
            # oben → unten
            o_top = Vector((x, y, z_top))
            loc, normal, index, dist = bvh.ray_cast(o_top, Vector((0,0,-1)), span)
            if index is not None:
                top_hits.add(index)
            # unten → oben
            o_bot = Vector((x, y, z_bot))
            loc, normal, index, dist = bvh.ray_cast(o_bot, Vector((0,0, 1)), span)
            if index is not None:
                bot_hits.add(index)
            y += step
        x += step
    return top_hits, bot_hits


# =========================
# M2: Parallelflächen-Paarung (Deck/Unterseite)
# =========================
def detect_parallel_pairs(bm):
    """Detect parallel floor/ceiling pairs"""
    mins, maxs = bm_bounds_local(bm)
    diag = bbox_diag(mins, maxs)
    t_max = max(PAIR_MIN_T, PAIR_MAX_T_FACTOR * diag)

    up_faces, down_faces = [], []
    for f in bm.faces:
        n = f.normal.normalized()
        if face_is_horizontal(n):
            if n.z >= 0: up_faces.append(f)
            else:        down_faces.append(f)

    if not up_faces or not down_faces:
        return set(), set()

    kd = kdtree.KDTree(len(down_faces))
    for i, f in enumerate(down_faces):
        c = face_center_local(f)
        kd.insert((c.x, c.y, c.z), i)
    kd.balance()

    outer_floor = set()
    outer_ceiling = set()

    for fu in up_faces:
        cu = face_center_local(fu)
        for (co, idx, dist) in kd.find_n((cu.x, cu.y, cu.z), 8):
            fd = down_faces[idx]
            nd = fd.normal.normalized()
            if not face_is_horizontal(nd) or nd.z >= 0:
                continue
            dz = abs(cu.z - co.z)
            if dz < PAIR_MIN_T or dz > t_max:
                continue
            dxy = math.hypot(cu.x - co.x, cu.y - co.y)
            if dxy > PAIR_MAX_XY_DIST:
                continue
            nu = fu.normal.normalized()
            if abs(nu.z) < HORIZ_COS or abs(nd.z) < HORIZ_COS:
                continue
            if nu.z <= 0 or nd.z >= 0:
                continue
            outer_floor.add(fu.index)
            outer_ceiling.add(fd.index)
            break

    return outer_floor, outer_ceiling


# =========================
# M3: Region-Growing + RANSAC-Plane (für Dach)
# =========================
def plane_from_points(p0, p1, p2):
    """Compute plane from 3 points"""
    v1 = p1 - p0; v2 = p2 - p0
    n = v1.cross(v2)
    if n.length == 0: return None, None
    n.normalize()
    d = -n.dot(p0)
    return n, d

def point_plane_dist(n, d, p):
    """Distance from point to plane"""
    return abs(n.dot(p) + d)

def ransac_plane(points, iters=RANSAC_ITERS, thr=RANSAC_DIST):
    """RANSAC plane fitting"""
    import random
    best_inliers = []
    best_model = (Vector((0,0,1)), 0.0)
    if len(points) < 3:
        return best_model, best_inliers
    for _ in range(iters):
        i1, i2, i3 = random.sample(range(len(points)), 3)
        n, d = plane_from_points(points[i1], points[i2], points[i3])
        if n is None: continue
        inliers = [i for i, p in enumerate(points) if point_plane_dist(n, d, p) <= thr]
        if len(inliers) > len(best_inliers):
            best_inliers = inliers
            best_model = (n, d)
    return best_model, best_inliers

def build_face_points(bm, face_indices):
    """Build list of face center points"""
    return [bm.faces[i].calc_center_median() for i in face_indices]

def build_adjacency_metrics(bm, z_min, z_max):
    """Build adjacency with geometric metrics"""
    nbrs, dih = build_adjacency(bm)
    h = [0.0]*len(bm.faces)
    nz = [0.0]*len(bm.faces)
    theta = [0.0]*len(bm.faces)
    for f in bm.faces:
        c = f.calc_center_median()
        rel = (c.z - z_min) / max((z_max - z_min), 1e-6)
        h[f.index] = max(0.0, min(1.0, rel))
        if f.normal.length == 0:
            nz[f.index] = 0.0; theta[f.index] = 90.0
        else:
            n = f.normal.normalized()
            nz[f.index] = max(0.0, n.z)
            theta[f.index] = math.degrees(n.angle(Vector((0,0,1))))
    return nbrs, dih, h, nz, theta

def grow_roof_regions(bm, seed_mask):
    """Grow roof regions from seed faces"""
    mins, maxs = bm_bounds_local(bm)
    z_min, z_max = mins.z, maxs.z
    nbrs, dih, h, nz, theta = build_adjacency_metrics(bm, z_min, z_max)

    visited = [False]*len(bm.faces)
    regions = []
    for i in range(len(bm.faces)):
        if not seed_mask[i] or visited[i]: continue
        reg = []
        stack = [i]; visited[i] = True
        while stack:
            a = stack.pop()
            reg.append(a)
            na = bm.faces[a].normal.normalized()
            for b in nbrs[a]:
                if visited[b]: continue
                nb = bm.faces[b].normal.normalized()
                ang = math.degrees(na.angle(nb))
                dihed = dih.get((min(a,b), max(a,b)), 0.0)
                if ang <= RG_ANGLE_MAX and dihed <= RG_DIHED_MAX and theta[b] < VERTICAL_MIN_DEG and abs(h[b] - h[a]) <= RG_H_CONT:
                    visited[b] = True
                    stack.append(b)
        if reg:
            regions.append(reg)

    accepted = set()
    for reg in regions:
        pts = build_face_points(bm, reg)
        (n, d), inliers = ransac_plane(pts)
        if not inliers: continue
        med_nz = statistics.median(nz[i] for i in reg)
        med_theta = statistics.median(theta[i] for i in reg)
        # Dach nie nach unten
        if med_nz >= UPWARD_MIN_NZ and med_theta <= ROOF_MAX_SLOPE_DEG:
            accepted.update(reg)
    return accepted


# =========================
# Ground Detection
# =========================
def detect_ground_baseplates_multi(bm):
    """
    Detect ground surfaces (horizontal, bottom, coplanar groups)
    """
    bm.faces.ensure_lookup_table()
    mins, maxs = bm_bounds_local(bm)
    z_span = max(1e-6, (maxs.z - mins.z))
    z_cut = mins.z + GROUND_BAND_REL * z_span

    # Kandidaten: horizontal, n.z >= 0, und mit Mindesteckpunkt nahe unten
    cand = [f.index for f in bm.faces
            if face_is_horizontal(bm.faces[f.index].normal.normalized())
            and min(v.co.z for v in bm.faces[f.index].verts) <= z_cut]

    if not cand:
        return set()

    # Coplanar-Gruppen im Kandidaten-Set
    diag = bbox_diag(mins, maxs)
    dist_thr = max(1e-5, COPLANAR_DIST_REL * diag)
    nbrs, _ = build_adjacency(bm)

    visited = set()
    groups = []
    for i in cand:
        if i in visited:
            continue
        seed = bm.faces[i]
        ns = seed.normal.normalized()
        cs = seed.calc_center_median()
        ds = -ns.dot(cs)

        stack = [i]
        grp = []
        visited.add(i)
        while stack:
            a = stack.pop()
            grp.append(a)
            for b in nbrs[a]:
                if b in visited or b not in cand:
                    continue
                fb = bm.faces[b]
                nb = fb.normal.normalized()
                ang = math.degrees(ns.angle(nb))
                if ang > COPLANAR_ANG_MAX:
                    continue
                cb = fb.calc_center_median()
                if abs(ns.dot(cb) + ds) > dist_thr:
                    continue
                visited.add(b)
                stack.append(b)
        if grp:
            groups.append(grp)

    if not groups:
        return set()

    # Kleine Inseln filtern
    extent_xy = max(1e-6, (maxs.x - mins.x) * (maxs.y - mins.y))
    min_area = GROUND_MIN_AREA_REL * extent_xy

    ground_all = set()
    for ids in groups:
        a = sum(bm.faces[k].calc_area() for k in ids)
        if a >= min_area:
            ground_all.update(ids)
    return ground_all


def refine_ground_faces(bm, labels, bot_hits):
    """Refine ground face classification using support detection"""
    mins, maxs = bm_bounds_local(bm)
    z_min, z_max = mins.z, maxs.z
    z_span = max(1e-6, (z_max - z_min))
    bottom_cut = z_min + GROUND_BOTTOM_BAND * z_span
    nbrs, _ = build_adjacency(bm)

    for f in bm.faces:
        i = f.index
        n = f.normal.normalized()
        c = f.calc_center_median()
        if not face_is_horizontal(n):
            continue
        if i not in bot_hits:
            continue
        if c.z > bottom_cut:
            continue

        # Support: Nachbarn höher und nicht horizontal
        support = 0
        for j in nbrs[i]:
            cj = bm.faces[j].calc_center_median()
            nj = bm.faces[j].normal.normalized()
            if cj.z > c.z + GROUND_SUPPORT_H_ABS * z_span and not face_is_horizontal(nj):
                support += 1
        if support >= 1:
            labels[i] = "GroundSurface"
    return labels


# =========================
# Coplanar Grouping
# =========================
def unify_coplanar_groups(bm, labels):
    """Unify labels within coplanar face groups"""
    mins, maxs = bm_bounds_local(bm)
    diag = bbox_diag(mins, maxs)
    dist_thr = max(1e-5, COPLANAR_DIST_REL * diag)
    nbrs, _ = build_adjacency(bm)

    visited = [False]*len(bm.faces)
    for i in range(len(bm.faces)):
        if visited[i]: continue
        seed = bm.faces[i]
        ns = seed.normal.normalized()
        cs = seed.calc_center_median()
        ds = -ns.dot(cs)

        group = []
        stack = [i]; visited[i] = True
        while stack:
            a = stack.pop()
            group.append(a)
            for b in nbrs[a]:
                if visited[b]: 
                    continue
                fb = bm.faces[b]
                nb = fb.normal.normalized()
                # Winkel
                ang = math.degrees(ns.angle(nb))
                if ang > COPLANAR_ANG_MAX:
                    continue
                # Abstand zur Ebene des Seeds
                cb = fb.calc_center_median()
                dist = abs(ns.dot(cb) + ds)
                if dist > dist_thr:
                    continue
                visited[b] = True
                stack.append(b)

        if len(group) <= 1:
            continue

        # Mehrheitslabel übernehmen
        votes = {}
        for idx in group:
            lab = labels[idx]
            votes[lab] = votes.get(lab, 0) + 1
        new_label = max(votes.items(), key=lambda x: x[1])[0]
        for idx in group:
            labels[idx] = new_label
    return labels
