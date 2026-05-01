from typing import Literal
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse
import tempfile
import os
import uuid
import numpy as np
import trimesh
from sklearn.cluster import DBSCAN
from trimesh.transformations import rotation_matrix

app = FastAPI()


@app.get("/")
def root():
    return {"message": "API is running"}


# =========================================================
# BASIC GEOMETRY
# =========================================================

def detect_overhang_faces(mesh, critical_angle_deg=45.0):
    normals = mesh.face_normals
    build_dir = np.array([0.0, 0.0, 1.0])

    cos_vals = normals @ build_dir
    angles = np.degrees(np.arccos(np.clip(cos_vals, -1.0, 1.0)))

    return np.where(angles > 90.0 + critical_angle_deg)[0]


def face_centers(mesh, face_indices):
    if len(face_indices) == 0:
        return np.empty((0, 3))

    return mesh.triangles[face_indices].mean(axis=1)


def downsample_points(points, max_points=4000):
    if len(points) <= max_points:
        return points

    idx = np.random.choice(len(points), max_points, replace=False)
    return points[idx]


def cluster_points(points, eps=1.5, min_samples=4):
    if len(points) == 0:
        return np.empty((0, 3))

    labels = DBSCAN(eps=eps, min_samples=min_samples).fit(points[:, :2]).labels_

    clustered = []

    for label in set(labels):
        if label == -1:
            continue

        cluster = points[labels == label]

        clustered.append([
            cluster[:, 0].mean(),
            cluster[:, 1].mean(),
            cluster[:, 2].max()
        ])

    if len(clustered) == 0:
        return np.empty((0, 3))

    return np.array(clustered)


# =========================================================
# CLASSIC SUPPORT
# =========================================================

def generate_classic_supports(points, radius=3.0, min_height=1.0):
    supports = []

    for p in points:
        z_top = float(p[2])

        if z_top > min_height:
            supports.append({
                "x": float(p[0]),
                "y": float(p[1]),
                "z_bottom": 0.0,
                "z_top": z_top,
                "radius": float(radius),
                "height": z_top
            })

    return supports


# =========================================================
# NEXT LEVEL TREE SUPPORT
# =========================================================

def generate_tree_supports(
    points,
    radius=3.0,
    tree_eps=18.0,
    min_height=5.0,
    max_branches_per_tree=12,
    branch_merge_eps=6.0
):
    if len(points) == 0:
        return {"trunks": [], "branches": []}

    labels = DBSCAN(eps=tree_eps, min_samples=1).fit(points[:, :2]).labels_

    trunks = []
    branches = []

    for label in set(labels):
        group = points[labels == label]

        branch_points = cluster_points(
            group,
            eps=branch_merge_eps,
            min_samples=1
        )

        if len(branch_points) == 0:
            continue

        if len(branch_points) > max_branches_per_tree:
            sorted_idx = np.argsort(branch_points[:, 2])[-max_branches_per_tree:]
            branch_points = branch_points[sorted_idx]

        trunk_x = float(branch_points[:, 0].mean())
        trunk_y = float(branch_points[:, 1].mean())

        trunk_top_z = float(np.percentile(branch_points[:, 2], 35))

        if trunk_top_z <= min_height:
            continue

        trunk_radius = radius * 1.4

        trunks.append({
            "x": trunk_x,
            "y": trunk_y,
            "z_bottom": 0.0,
            "z_top": trunk_top_z,
            "radius": float(trunk_radius)
        })

        for p in branch_points:
            px, py, pz = float(p[0]), float(p[1]), float(p[2])

            if pz <= trunk_top_z:
                continue

            mid_x = (trunk_x + px) / 2.0
            mid_y = (trunk_y + py) / 2.0
            mid_z = trunk_top_z + (pz - trunk_top_z) * 0.55

            branch_length = np.linalg.norm([
                px - trunk_x,
                py - trunk_y,
                pz - trunk_top_z
            ])

            branch_radius = radius * 0.45 + 0.01 * branch_length

            branches.append({
                "x1": trunk_x,
                "y1": trunk_y,
                "z1": trunk_top_z,

                "xm": float(mid_x),
                "ym": float(mid_y),
                "zm": float(mid_z),

                "x2": px,
                "y2": py,
                "z2": pz,

                "radius": float(branch_radius)
            })

    return {
        "trunks": trunks,
        "branches": branches
    }


def tree_to_support_points(tree):
    supports = []

    for t in tree["trunks"]:
        height = t["z_top"] - t["z_bottom"]

        if height > 0:
            supports.append({
                "x": t["x"],
                "y": t["y"],
                "z_bottom": t["z_bottom"],
                "z_top": t["z_top"],
                "radius": t["radius"],
                "height": height
            })

    for b in tree["branches"]:
        length1 = np.linalg.norm([
            b["xm"] - b["x1"],
            b["ym"] - b["y1"],
            b["zm"] - b["z1"]
        ])

        length2 = np.linalg.norm([
            b["x2"] - b["xm"],
            b["y2"] - b["ym"],
            b["z2"] - b["zm"]
        ])

        total_length = length1 + length2

        if total_length > 0:
            supports.append({
                "x": b["x2"],
                "y": b["y2"],
                "z_bottom": b["z1"],
                "z_top": b["z2"],
                "radius": b["radius"],
                "height": total_length
            })

    return supports


# =========================================================
# METRICS
# =========================================================

def support_volume(supports):
    total = 0.0

    for s in supports:
        total += np.pi * (s["radius"] ** 2) * s["height"]

    return float(total)


def coverage(points, supports, max_xy_distance=6.0):
    if len(points) == 0:
        return 1.0, 0

    if len(supports) == 0:
        return 0.0, len(points)

    supported = 0

    for p in points:
        px, py, pz = p

        for s in supports:
            dist = np.sqrt((s["x"] - px) ** 2 + (s["y"] - py) ** 2)

            if dist <= max_xy_distance and s["z_top"] >= pz:
                supported += 1
                break

    cov = supported / len(points)
    unsupported = len(points) - supported

    return float(cov), int(unsupported)


# =========================================================
# EVALUATION
# =========================================================

def evaluate_angle(
    original_mesh,
    angle,
    mode="classic",
    sample_points=3000,
    use_clustering=False,
    cluster_eps=1.5,
    min_samples=4,
    support_radius=3.0,
    min_height=1.0,
    critical_angle_deg=45.0,
    max_xy_distance=6.0,
    penalty_weight=10.0,
    tree_eps=18.0,
    min_feasible_coverage=0.5
):
    mesh = original_mesh.copy()
    mesh.apply_transform(rotation_matrix(np.radians(angle), [0, 1, 0]))

    overhang_faces = detect_overhang_faces(mesh, critical_angle_deg)
    points = face_centers(mesh, overhang_faces)

    points = downsample_points(points, sample_points)

    if use_clustering:
        support_points = cluster_points(
            points,
            eps=cluster_eps,
            min_samples=min_samples
        )
    else:
        support_points = points

    if mode == "classic":
        supports = generate_classic_supports(
            support_points,
            radius=support_radius,
            min_height=min_height
        )

    elif mode == "tree":
        tree = generate_tree_supports(
            support_points,
            radius=support_radius,
            tree_eps=tree_eps,
            min_height=min_height,
            max_branches_per_tree=12,
            branch_merge_eps=6.0
        )

        supports = tree_to_support_points(tree)

    else:
        raise ValueError("mode must be classic or tree")

    vol = support_volume(supports)

    cov, unsupported = coverage(
        points,
        supports,
        max_xy_distance=max_xy_distance
    )

    coverage_penalty = 0.0
    if cov < min_feasible_coverage:
        coverage_penalty = 1_000_000_000 * (min_feasible_coverage - cov)

    score = vol + penalty_weight * unsupported + coverage_penalty

    return {
        "angle": float(angle),
        "mode": mode,
        "overhang_sample_count": int(len(points)),
        "support_count": int(len(supports)),
        "volume": float(vol),
        "coverage": float(cov),
        "unsupported": int(unsupported),
        "coverage_penalty": float(coverage_penalty),
        "score": float(score),
        "is_feasible": bool(cov >= min_feasible_coverage)
    }


# =========================================================
# SMART OPTIMIZATION ENDPOINT
# =========================================================

@app.post("/smart-optimize")
async def smart_optimize(
    file: UploadFile = File(...),
    mode: Literal["classic", "tree"] = "classic",
    coarse_step: float = 60.0,
    refine_step: float = 15.0,
    sample_points_coarse: int = 1500,
    sample_points_refine: int = 3000,
    cluster_eps: float = 1.5,
    min_samples: int = 4,
    support_radius: float = 3.0,
    min_height: float = 1.0,
    critical_angle_deg: float = 45.0,
    max_xy_distance: float = 6.0,
    penalty_weight: float = 10.0,
    tree_eps: float = 18.0,
    min_feasible_coverage: float = 0.5
):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".stl") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        mesh = trimesh.load_mesh(tmp_path)

        coarse_results = []

        for angle in np.arange(0, 180, coarse_step):
            result = evaluate_angle(
                mesh,
                angle,
                mode=mode,
                sample_points=sample_points_coarse,
                use_clustering=False,
                cluster_eps=cluster_eps,
                min_samples=min_samples,
                support_radius=support_radius,
                min_height=min_height,
                critical_angle_deg=critical_angle_deg,
                max_xy_distance=max_xy_distance,
                penalty_weight=penalty_weight,
                tree_eps=tree_eps,
                min_feasible_coverage=min_feasible_coverage
            )

            coarse_results.append(result)

        coarse_feasible = [r for r in coarse_results if r["is_feasible"]]

        if coarse_feasible:
            coarse_best = min(coarse_feasible, key=lambda r: r["score"])
        else:
            coarse_best = min(coarse_results, key=lambda r: r["score"])

        best_angle = coarse_best["angle"]

        start = max(0, best_angle - coarse_step)
        end = min(179.999, best_angle + coarse_step)

        refine_results = []

        for angle in np.arange(start, end + 0.01, refine_step):
            result = evaluate_angle(
                mesh,
                angle,
                mode=mode,
                sample_points=sample_points_refine,
                use_clustering=False,
                cluster_eps=cluster_eps,
                min_samples=min_samples,
                support_radius=support_radius,
                min_height=min_height,
                critical_angle_deg=critical_angle_deg,
                max_xy_distance=max_xy_distance,
                penalty_weight=penalty_weight,
                tree_eps=tree_eps,
                min_feasible_coverage=min_feasible_coverage
            )

            refine_results.append(result)

        feasible_results = [r for r in refine_results if r["is_feasible"]]

        if feasible_results:
            refine_best = min(feasible_results, key=lambda r: r["score"])
        else:
            refine_best = min(refine_results, key=lambda r: r["score"])

        best_feasible = refine_best if refine_best["is_feasible"] else None

        return {
            "filename": file.filename,
            "mode": mode,
            "min_feasible_coverage": min_feasible_coverage,
            "feasible_solution_count": len(feasible_results),
            "is_refine_best_feasible": refine_best["is_feasible"],
            "best_feasible": best_feasible,
            "coarse_best": coarse_best,
            "refine_best": refine_best,
            "coarse_results": coarse_results,
            "refine_results": refine_results
        }

    except Exception as e:
        return {"error": str(e)}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# =========================================================
# MESH GENERATION
# =========================================================

def cylinder_between(p1, p2, radius=3.0, sections=24):
    p1 = np.array(p1, dtype=float)
    p2 = np.array(p2, dtype=float)

    direction = p2 - p1
    length = np.linalg.norm(direction)

    if length < 1e-6:
        return None

    try:
        cyl = trimesh.creation.cylinder(
            radius=radius,
            height=length,
            sections=sections
        )

        transform = trimesh.geometry.align_vectors(
            np.array([0.0, 0.0, 1.0]),
            direction / length
        )

        cyl.apply_transform(transform)
        cyl.apply_translation((p1 + p2) / 2.0)

        return cyl

    except Exception:
        return None


def base_plate(x, y, z=0.0, radius=8.0, height=1.0, sections=32):
    plate = trimesh.creation.cylinder(
        radius=radius,
        height=height,
        sections=sections
    )

    plate.apply_translation([x, y, z + height / 2.0])

    return plate


def classic_support_mesh(supports, add_base=True):
    meshes = []

    for s in supports:
        cyl = cylinder_between(
            [s["x"], s["y"], s["z_bottom"]],
            [s["x"], s["y"], s["z_top"]],
            s["radius"],
            sections=24
        )

        if cyl is not None and len(cyl.vertices) > 0:
            meshes.append(cyl)

        if add_base:
            plate = base_plate(
                s["x"],
                s["y"],
                radius=s["radius"] * 2.5,
                height=max(0.8, s["radius"] * 0.3)
            )

            meshes.append(plate)

    if len(meshes) == 0:
        return None

    return trimesh.util.concatenate(meshes)


def tree_support_mesh(tree, add_base=True):
    meshes = []

    for t in tree["trunks"]:
        cyl = cylinder_between(
            [t["x"], t["y"], t["z_bottom"]],
            [t["x"], t["y"], t["z_top"]],
            t["radius"],
            sections=24
        )

        if cyl is not None and len(cyl.vertices) > 0:
            meshes.append(cyl)

        if add_base:
            plate = base_plate(
                t["x"],
                t["y"],
                radius=t["radius"] * 2.6,
                height=max(0.8, t["radius"] * 0.35)
            )

            meshes.append(plate)

    for b in tree["branches"]:
        cyl1 = cylinder_between(
            [b["x1"], b["y1"], b["z1"]],
            [b["xm"], b["ym"], b["zm"]],
            b["radius"],
            sections=16
        )

        cyl2 = cylinder_between(
            [b["xm"], b["ym"], b["zm"]],
            [b["x2"], b["y2"], b["z2"]],
            b["radius"] * 0.85,
            sections=16
        )

        if cyl1 is not None and len(cyl1.vertices) > 0:
            meshes.append(cyl1)

        if cyl2 is not None and len(cyl2.vertices) > 0:
            meshes.append(cyl2)

    if len(meshes) == 0:
        return None

    return trimesh.util.concatenate(meshes)


# =========================================================
# EXPORT SUPPORT STL ENDPOINT
# =========================================================

@app.post("/export-support-stl")
async def export_support_stl(
    file: UploadFile = File(...),
    angle: float = 0.0,
    mode: Literal["classic", "tree"] = "classic",
    full_resolution: bool = False,
    export_sample_points: int = 5000,
    cluster_eps: float = 4.0,
    min_samples: int = 3,
    support_radius: float = 3.0,
    min_height: float = 5.0,
    critical_angle_deg: float = 45.0,
    tree_eps: float = 18.0,
    add_base: bool = True,
    max_branches_per_tree: int = 12,
    branch_merge_eps: float = 6.0
):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".stl") as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        mesh = trimesh.load_mesh(tmp_path)

        mesh.apply_transform(rotation_matrix(np.radians(angle), [0, 1, 0]))

        overhang_faces = detect_overhang_faces(mesh, critical_angle_deg)

        points = face_centers(mesh, overhang_faces)

        if len(points) == 0:
            return {"error": "No overhang points detected."}

        if not full_resolution:
            points = downsample_points(points, export_sample_points)

        clustered = cluster_points(
            points,
            eps=cluster_eps,
            min_samples=min_samples
        )

        if len(clustered) == 0:
            return {
                "error": "No clustered support points generated. Try decreasing min_samples or increasing cluster_eps."
            }

        if mode == "classic":
            supports = generate_classic_supports(
                clustered,
                radius=support_radius,
                min_height=min_height
            )

            if len(supports) == 0:
                return {
                    "error": "No classic supports generated. Try decreasing min_height."
                }

            mesh_out = classic_support_mesh(
                supports,
                add_base=add_base
            )

        else:
            tree = generate_tree_supports(
                clustered,
                radius=support_radius,
                tree_eps=tree_eps,
                min_height=min_height,
                max_branches_per_tree=max_branches_per_tree,
                branch_merge_eps=branch_merge_eps
            )

            if len(tree["trunks"]) == 0 and len(tree["branches"]) == 0:
                return {
                    "error": "No tree supports generated. Try increasing tree_eps or decreasing min_height."
                }

            mesh_out = tree_support_mesh(
                tree,
                add_base=add_base
            )

        if mesh_out is None or len(mesh_out.vertices) == 0:
            return {
                "error": "Support mesh could not be generated."
            }

        output_name = f"{mode}_support_{uuid.uuid4().hex[:8]}.stl"

        output_path = os.path.join(
            tempfile.gettempdir(),
            output_name
        )

        mesh_out.export(output_path)

        return FileResponse(
            output_path,
            media_type="model/stl",
            filename=output_name
        )

    except Exception as e:
        return {"error": str(e)}

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)