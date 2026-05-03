"""
3D Support Optimizer v9.2 - Goal Programming Collision-Constrained Tree System
Features:
- FastAPI UI
- Material/nozzle selection
- Classic and tree supports
- Spherical angle scan: rho + theta
- Feasibility analysis
- Physics checks: compression + Euler buckling
- Better score function
- Tree support with outward branch heuristic to reduce model-intersection
- Analyze endpoint and export endpoint
"""

from typing import Literal, Dict, List, Tuple, Any, Optional
from dataclasses import dataclass
from enum import Enum
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from contextlib import contextmanager
import tempfile
import os
import uuid
import logging
import numpy as np
import trimesh
from sklearn.cluster import DBSCAN
from trimesh.transformations import rotation_matrix


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =========================================================
# ENUMS / CONSTANTS
# =========================================================

class SupportType(str, Enum):
    CLASSIC = "classic"
    TREE = "tree"


class FeasibilityStatus(str, Enum):
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"


@dataclass
class MaterialProperties:
    density_g_cm3: float
    compressive_strength_mpa: float
    elastic_modulus_mpa: float


MATERIALS: Dict[str, MaterialProperties] = {
    "PLA": MaterialProperties(1.24, 60.0, 3500.0),
    "ABS": MaterialProperties(1.04, 40.0, 2100.0),
    "PETG": MaterialProperties(1.27, 50.0, 2100.0),
    "TPU": MaterialProperties(1.20, 20.0, 80.0),
}

ALLOWED_NOZZLES = [0.2, 0.4, 0.6, 0.8]
DEFAULT_MATERIAL = "PLA"
DEFAULT_NOZZLE = 0.4
DEFAULT_SAFETY_FACTOR = 2.0


# =========================================================
# EXCEPTIONS
# =========================================================

class InvalidNozzleError(Exception):
    pass


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="3D Support Optimizer v9.0",
    description="Full single-file support optimizer with angle scan, physics, and tree support export.",
    version="9.2.0",
)


# =========================================================
# UTILS
# =========================================================

def validate_nozzle(nozzle_mm: float) -> float:
    nozzle_mm = float(nozzle_mm)
    if nozzle_mm not in ALLOWED_NOZZLES:
        raise InvalidNozzleError(
            f"nozzle_mm must be one of {ALLOWED_NOZZLES}"
        )
    return nozzle_mm


@contextmanager
def temporary_mesh_file():
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".stl")
        tmp_path = tmp.name
        tmp.close()
        yield tmp_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def spherical_to_rotations(rho_deg: float, theta_deg: float) -> Tuple[np.ndarray, np.ndarray]:
    rho_rad = np.radians(rho_deg)
    theta_rad = np.radians(theta_deg)
    rot_x = rotation_matrix(rho_rad, [1, 0, 0])
    rot_y = rotation_matrix(theta_rad, [0, 1, 0])
    return rot_x, rot_y


def safe_float(x, default=0.0):
    try:
        if np.isnan(x) or np.isinf(x):
            return default
        return float(x)
    except Exception:
        return default


def bezier_quad(p0, p1, p2, t):
    p0 = np.array(p0, dtype=float)
    p1 = np.array(p1, dtype=float)
    p2 = np.array(p2, dtype=float)
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2


def outward_unit_from_center(x: float, y: float, center_x: float, center_y: float) -> Tuple[float, float]:
    dx = float(x - center_x)
    dy = float(y - center_y)
    n = float(np.sqrt(dx * dx + dy * dy))
    if n < 1e-9:
        return 1.0, 0.0
    return dx / n, dy / n


def push_point_outside_xy_bbox(
    x: float,
    y: float,
    center_x: float,
    center_y: float,
    bounds: Optional[np.ndarray],
    margin: float = 8.0,
) -> Tuple[float, float]:
    """
    Push a point outside the model XY bounding box in radial direction.
    This is a cheap collision-avoidance approximation:
    tree trunks and branch control points stay outside the body footprint.
    """
    if bounds is None:
        return float(x), float(y)

    minx, miny = float(bounds[0][0]), float(bounds[0][1])
    maxx, maxy = float(bounds[1][0]), float(bounds[1][1])

    ux, uy = outward_unit_from_center(x, y, center_x, center_y)

    # If already outside expanded bbox, keep it.
    if (x < minx - margin or x > maxx + margin or y < miny - margin or y > maxy + margin):
        return float(x), float(y)

    candidates = []

    if abs(ux) > 1e-9:
        tx1 = ((minx - margin) - center_x) / ux
        tx2 = ((maxx + margin) - center_x) / ux
        if tx1 > 0:
            candidates.append(tx1)
        if tx2 > 0:
            candidates.append(tx2)

    if abs(uy) > 1e-9:
        ty1 = ((miny - margin) - center_y) / uy
        ty2 = ((maxy + margin) - center_y) / uy
        if ty1 > 0:
            candidates.append(ty1)
        if ty2 > 0:
            candidates.append(ty2)

    if not candidates:
        return float(x + ux * margin), float(y + uy * margin)

    t = min(candidates)
    return float(center_x + ux * t), float(center_y + uy * t)


def make_safe_branch_control_point(
    trunk_xyz: Tuple[float, float, float],
    target_xyz: Tuple[float, float, float],
    center_x: float,
    center_y: float,
    mesh_bounds: Optional[np.ndarray],
    outside_offset: float,
    curve_lift: float,
    collision_margin: float,
) -> Tuple[float, float, float]:
    """
    Make a Bezier control point that is pushed outside the model footprint.
    This keeps most of the branch in free space instead of through the model.
    """
    tx, ty, tz = trunk_xyz
    px, py, pz = target_xyz
    ux, uy = outward_unit_from_center(px, py, center_x, center_y)

    # Initial outward control point
    mx = (tx + px) / 2.0 + ux * outside_offset
    my = (ty + py) / 2.0 + uy * outside_offset
    mz = tz + (pz - tz) * 0.55 + curve_lift

    # Push control point outside XY bounding box.
    mx, my = push_point_outside_xy_bbox(
        mx, my, center_x, center_y, mesh_bounds, margin=collision_margin
    )

    return float(mx), float(my), float(mz)


def approximate_bezier_length(p0, p1, p2, samples: int = 10) -> float:
    pts = [bezier_quad(p0, p1, p2, t) for t in np.linspace(0, 1, samples)]
    return float(sum(np.linalg.norm(pts[i + 1] - pts[i]) for i in range(len(pts) - 1)))




# =========================================================
# FAST RAY ENGINE - V9.1
# =========================================================

_FAST_RAY_CACHE = {}


def get_fast_ray_engine(mesh: Optional[trimesh.Trimesh]):
    """
    Use pyembree/Embree ray intersector when installed.
    Fallback to mesh.ray otherwise.
    """
    if mesh is None:
        return None

    key = id(mesh)
    if key in _FAST_RAY_CACHE:
        return _FAST_RAY_CACHE[key]

    try:
        from trimesh.ray.ray_pyembree import RayMeshIntersector
        engine = RayMeshIntersector(mesh)
        _FAST_RAY_CACHE[key] = engine
        return engine
    except Exception:
        engine = mesh.ray
        _FAST_RAY_CACHE[key] = engine
        return engine


def ray_intersects_location_fast(mesh: Optional[trimesh.Trimesh], origins, directions, multiple_hits: bool = True):
    engine = get_fast_ray_engine(mesh)
    if engine is None:
        return [], [], []

    return engine.intersects_location(
        ray_origins=origins,
        ray_directions=directions,
        multiple_hits=multiple_hits
    )


# =========================================================
# RAYCAST COLLISION HELPERS - V9
# =========================================================

def ray_hits_mesh_between(mesh: Optional[trimesh.Trimesh], p0, p1, clearance_mm: float = 0.8) -> bool:
    """
    Return True if segment p0->p1 intersects the model before reaching target.
    If trimesh ray backend is unavailable, it safely returns False.
    """
    if mesh is None:
        return False
    p0 = np.array(p0, dtype=float)
    p1 = np.array(p1, dtype=float)
    vec = p1 - p0
    length = float(np.linalg.norm(vec))
    if length < 1e-6:
        return False
    direction = vec / length
    origin = p0 + direction * clearance_mm
    try:
        locations, _, _ = ray_intersects_location_fast(
            mesh,
            origins=[origin],
            directions=[direction],
            multiple_hits=True
        )
    except Exception:
        return False
    for loc in locations:
        dist = float(np.linalg.norm(loc - origin))
        if clearance_mm < dist < length - clearance_mm:
            return True
    return False


def curve_hits_mesh(mesh: Optional[trimesh.Trimesh], p0, p1, p2, clearance_mm: float = 0.8, samples: int = 8) -> bool:
    if mesh is None:
        return False
    pts = [bezier_quad(p0, p1, p2, t) for t in np.linspace(0, 1, samples)]
    for i in range(len(pts) - 1):
        if ray_hits_mesh_between(mesh, pts[i], pts[i + 1], clearance_mm=clearance_mm):
            return True
    return False


def find_safe_control_point(
    mesh: Optional[trimesh.Trimesh],
    trunk_xyz,
    target_xyz,
    center_x: float,
    center_y: float,
    mesh_bounds: Optional[np.ndarray],
    base_outside_offset: float,
    curve_lift: float,
    collision_margin: float,
    clearance_mm: float,
    max_attempts: int = 7,
):
    """
    Try larger outward/lifted Bezier control points until the branch curve
    is not intersecting the mesh.
    """
    tx, ty, tz = trunk_xyz
    px, py, pz = target_xyz
    ux, uy = outward_unit_from_center(px, py, center_x, center_y)
    best = None

    for attempt in range(max_attempts):
        factor = 1.0 + attempt * 0.45
        mx = (tx + px) / 2.0 + ux * base_outside_offset * factor
        my = (ty + py) / 2.0 + uy * base_outside_offset * factor
        mz = tz + (pz - tz) * 0.55 + curve_lift * factor

        mx, my = push_point_outside_xy_bbox(
            mx, my, center_x, center_y, mesh_bounds, margin=collision_margin * factor
        )
        control = (float(mx), float(my), float(mz))
        best = control

        if not curve_hits_mesh(mesh, trunk_xyz, control, target_xyz, clearance_mm=clearance_mm, samples=9):
            return control

    return best


def filter_points_needing_support_by_downray(
    mesh: Optional[trimesh.Trimesh],
    points: np.ndarray,
    bed_z: float,
    min_drop_mm: float = 1.0,
    surface_offset_mm: float = 0.35,
) -> np.ndarray:
    """
    Keep points where a vertical downward ray reaches the build plate without
    hitting the model. This removes many internal/self-supported points.
    """
    if mesh is None or len(points) == 0:
        return points

    origins = points + np.array([0.0, 0.0, surface_offset_mm])
    directions = np.tile(np.array([0.0, 0.0, -1.0]), (len(points), 1))

    try:
        locations, index_ray, _ = ray_intersects_location_fast(
            mesh,
            origins=origins,
            directions=directions,
            multiple_hits=True
        )
    except Exception:
        return points

    ray_to_hits = {}
    for loc, ridx in zip(locations, index_ray):
        ray_to_hits.setdefault(int(ridx), []).append(loc)

    keep = []
    for i, p in enumerate(points):
        if float(p[2] - bed_z) <= min_drop_mm:
            continue

        hits = ray_to_hits.get(i, [])
        real_hits = []
        for h in hits:
            dz = float(origins[i][2] - h[2])
            if dz > surface_offset_mm + 0.8:
                real_hits.append(h)

        if len(real_hits) == 0:
            keep.append(p)

    if len(keep) == 0:
        return points
    return np.array(keep)



# =========================================================
# GOAL PROGRAMMING + COLLISION HELPERS - V9.2
# =========================================================

BIG_M_DEFAULT = 1_000_000.0


def goal_programming_objective(
    coverage: float,
    volume: float,
    support_count: int,
    collision_count: int,
    disconnected_count: int,
    target_coverage: float = 0.35,
    target_volume: float = 180000.0,
    target_support_count: int = 180,
    w_coverage: float = 1000.0,
    w_volume: float = 0.002,
    w_support_count: float = 2.0,
    big_m_collision: float = BIG_M_DEFAULT,
    big_m_disconnected: float = BIG_M_DEFAULT,
) -> Dict[str, Any]:
    d_coverage_under = max(0.0, target_coverage - float(coverage))
    d_volume_over = max(0.0, float(volume) - float(target_volume))
    d_support_over = max(0.0, float(support_count) - float(target_support_count))

    z = (
        w_coverage * d_coverage_under
        + w_volume * d_volume_over
        + big_m_collision * float(collision_count)
        + big_m_disconnected * float(disconnected_count)
        + w_support_count * d_support_over
    )

    return {
        "Z": float(z),
        "deviations": {
            "d_coverage_under": float(d_coverage_under),
            "d_volume_over": float(d_volume_over),
            "d_collision": int(collision_count),
            "d_disconnected": int(disconnected_count),
            "d_support_over": float(d_support_over),
        },
        "targets": {
            "target_coverage": float(target_coverage),
            "target_volume": float(target_volume),
            "target_support_count": int(target_support_count),
        },
        "weights": {
            "w_coverage": float(w_coverage),
            "w_volume": float(w_volume),
            "w_support_count": float(w_support_count),
            "big_m_collision": float(big_m_collision),
            "big_m_disconnected": float(big_m_disconnected),
        },
    }


def collision_count_for_tree(tree: Dict[str, Any], mesh: Optional[trimesh.Trimesh], clearance_mm: float = 1.0) -> int:
    if mesh is None:
        return 0
    count = 0
    for b in tree.get("branches", []):
        p0 = [b["x1"], b["y1"], b["z1"]]
        p1 = [b["xm"], b["ym"], b["zm"]]
        p2 = [b["x2"], b["y2"], b["z2"]]
        radius = float(b.get("radius", 1.0))
        if curve_hits_mesh(mesh, p0, p1, p2, clearance_mm=clearance_mm + radius, samples=9):
            count += 1
    return int(count)


def disconnected_count_for_tree(tree: Dict[str, Any]) -> int:
    trunks = tree.get("trunks", [])
    valid = set(int(t.get("id", -999)) for t in trunks)
    disconnected = 0
    for b in tree.get("branches", []):
        if int(b.get("trunk_id", -999)) not in valid:
            disconnected += 1
    return int(disconnected)


def contact_point_with_normal_offset(surface_point, center_x: float, center_y: float, tip_gap_mm: float):
    px, py, pz = float(surface_point[0]), float(surface_point[1]), float(surface_point[2])
    ux, uy = outward_unit_from_center(px, py, center_x, center_y)
    n = np.array([ux, uy, 0.35], dtype=float)
    nn = np.linalg.norm(n)
    n = np.array([0.0, 0.0, 1.0]) if nn < 1e-9 else n / nn
    c = np.array([px, py, pz], dtype=float) + n * float(tip_gap_mm)
    return float(c[0]), float(c[1]), float(c[2])


# =========================================================
# GEOMETRY
# =========================================================

class GeometryProcessor:

    @staticmethod
    def detect_overhang_faces(mesh: trimesh.Trimesh, critical_angle_deg: float = 45.0) -> np.ndarray:
        normals = mesh.face_normals
        build_dir = np.array([0.0, 0.0, 1.0])
        cos_vals = np.dot(normals, build_dir)
        angles = np.degrees(np.arccos(np.clip(cos_vals, -1.0, 1.0)))
        return np.where(angles > 90.0 + critical_angle_deg)[0]

    @staticmethod
    def face_centers(mesh: trimesh.Trimesh, face_indices: np.ndarray) -> np.ndarray:
        if len(face_indices) == 0:
            return np.empty((0, 3))
        return mesh.triangles[face_indices].mean(axis=1)

    @staticmethod
    def downsample_points(points: np.ndarray, max_points: int = 3000) -> np.ndarray:
        if len(points) <= max_points:
            return points
        idx = np.random.choice(len(points), max_points, replace=False)
        return points[idx]

    @staticmethod
    def cluster_points(points: np.ndarray, eps: float = 2.0, min_samples: int = 1) -> np.ndarray:
        if len(points) == 0:
            return np.empty((0, 3))

        labels = DBSCAN(eps=eps, min_samples=min_samples).fit(points[:, :2]).labels_
        out = []

        for label in set(labels):
            if label == -1:
                continue
            c = points[labels == label]
            out.append([
                float(c[:, 0].mean()),
                float(c[:, 1].mean()),
                float(c[:, 2].max())
            ])

        if not out:
            return np.empty((0, 3))
        return np.array(out)

    @staticmethod
    def process_rotated_mesh(
        original_mesh: trimesh.Trimesh,
        rho_deg: float,
        theta_deg: float,
        critical_angle_deg: float = 45.0,
        sample_points: int = 3000,
    ) -> Tuple[trimesh.Trimesh, np.ndarray, float]:
        mesh = original_mesh.copy()

        rot_x, rot_y = spherical_to_rotations(rho_deg, theta_deg)
        mesh.apply_transform(rot_x)
        mesh.apply_transform(rot_y)

        bed_z = float(mesh.bounds[0][2])

        faces = GeometryProcessor.detect_overhang_faces(mesh, critical_angle_deg)
        points = GeometryProcessor.face_centers(mesh, faces)
        points = GeometryProcessor.downsample_points(points, sample_points)

        return mesh, points, bed_z


# =========================================================
# STRUCTURAL ANALYSIS
# =========================================================

class StructuralAnalyzer:

    @staticmethod
    def cylinder_area_mm2(radius_mm: float) -> float:
        return np.pi * radius_mm ** 2

    @staticmethod
    def cylinder_inertia_mm4(radius_mm: float) -> float:
        return np.pi * radius_mm ** 4 / 4.0

    @staticmethod
    def estimate_point_load_n(mesh_mass_g: float = 100.0, point_count: int = 1000) -> float:
        if point_count <= 0:
            return 0.0
        return (mesh_mass_g / 1000.0 * 9.81) / point_count

    @staticmethod
    def compression_check(force_n: float, radius_mm: float, material: str, safety_factor: float = 2.0) -> Dict[str, Any]:
        mat = MATERIALS[material]
        area = StructuralAnalyzer.cylinder_area_mm2(radius_mm)
        stress_mpa = force_n / area if area > 0 else float("inf")
        allowable = mat.compressive_strength_mpa / safety_factor
        ok = stress_mpa <= allowable
        return {
            "stress_mpa": safe_float(stress_mpa),
            "allowable_mpa": safe_float(allowable),
            "ok": bool(ok),
            "safety_margin_percent": safe_float(((allowable - stress_mpa) / allowable) * 100 if allowable > 0 else -100.0),
        }

    @staticmethod
    def buckling_check(force_n: float, radius_mm: float, length_mm: float, material: str, safety_factor: float = 2.0) -> Dict[str, Any]:
        mat = MATERIALS[material]

        if length_mm <= 0:
            return {
                "critical_load_n": 0.0,
                "required_load_n": safe_float(force_n * safety_factor),
                "ok": False,
                "safety_margin_percent": -100.0,
            }

        e_mpa = mat.elastic_modulus_mpa
        inertia = StructuralAnalyzer.cylinder_inertia_mm4(radius_mm)
        pcr = (np.pi ** 2 * e_mpa * inertia) / (length_mm ** 2)
        required = force_n * safety_factor

        ok = pcr >= required
        return {
            "critical_load_n": safe_float(pcr),
            "required_load_n": safe_float(required),
            "ok": bool(ok),
            "safety_margin_percent": safe_float(((pcr - required) / pcr) * 100 if pcr > 0 else -100.0),
        }

    @staticmethod
    def required_radius_for_force(
        force_n: float,
        length_mm: float,
        material: str,
        nozzle_mm: float,
        safety_factor: float = 2.0,
        visual_min_radius: float = 0.0,
    ) -> float:
        mat = MATERIALS[material]
        min_radius_nozzle = nozzle_mm * 2.5

        allowable = mat.compressive_strength_mpa / safety_factor
        if force_n <= 0 or allowable <= 0:
            r_comp = min_radius_nozzle
        else:
            r_comp = np.sqrt(force_n / (np.pi * allowable))

        e_mpa = mat.elastic_modulus_mpa
        if force_n <= 0 or length_mm <= 0 or e_mpa <= 0:
            r_buck = min_radius_nozzle
        else:
            required_i = (force_n * safety_factor * length_mm ** 2) / (np.pi ** 2 * e_mpa)
            r_buck = ((4.0 * required_i) / np.pi) ** 0.25

        return float(max(min_radius_nozzle, r_comp, r_buck, visual_min_radius))


# =========================================================
# SUPPORT GENERATION
# =========================================================

class SupportGenerator:

    @staticmethod
    def classic_supports(
        points: np.ndarray,
        radius: float = 1.5,
        min_height: float = 1.0,
        bed_z: float = 0.0,
        mesh_mass_g: float = 100.0,
    ) -> List[Dict[str, float]]:
        supports = []
        point_load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, len(points))

        for p in points:
            z_top = float(p[2])
            height = z_top - bed_z

            if height > min_height:
                supports.append({
                    "x": float(p[0]),
                    "y": float(p[1]),
                    "z_bottom": float(bed_z),
                    "z_top": z_top,
                    "radius": float(radius),
                    "height": float(height),
                    "length": float(height),
                    "force_n": float(point_load),
                })

        return supports

    @staticmethod
    def load_aware_tree_supports(
        points: np.ndarray,
        radius: float = 1.8,
        tree_eps: float = 8.0,
        branch_merge_eps: float = 2.5,
        max_branches_per_tree: int = 240,
        min_height: float = 1.0,
        bed_z: float = 0.0,
        material: str = "PLA",
        nozzle_mm: float = 0.4,
        mesh_mass_g: float = 100.0,
        safety_factor: float = 2.0,
        trunk_outside_offset: float = 10.0,
        branch_outside_offset: float = 25.0,
        branch_curve_lift: float = 5.0,
        mesh_bounds: Optional[np.ndarray] = None,
        mesh_for_raycast: Optional[trimesh.Trimesh] = None,
        collision_margin: float = 8.0,
        tip_gap_mm: float = 0.25,
        ray_clearance_mm: float = 0.8,
        downray_filter: bool = True,
    ) -> Dict[str, Any]:
        """
        Load-aware tree support.

        This is not true Cura collision detection.
        It uses outward midpoints to reduce branch segments passing through the mesh.
        """
        if len(points) == 0:
            return {
                "trunks": [],
                "branches": [],
                "physics": {
                    "all_ok": False,
                    "reason": "No overhang points detected",
                    "trunk_count": 0,
                    "branch_count": 0,
                }
            }

        validate_nozzle(nozzle_mm)

        if downray_filter:
            points = filter_points_needing_support_by_downray(
                mesh_for_raycast,
                points,
                bed_z=bed_z,
                min_drop_mm=min_height,
                surface_offset_mm=0.35,
            )

        if len(points) == 0:
            return {
                "trunks": [],
                "branches": [],
                "physics": {
                    "all_ok": False,
                    "reason": "No free overhang points after down-ray filter",
                    "trunk_count": 0,
                    "branch_count": 0,
                }
            }

        labels = DBSCAN(eps=tree_eps, min_samples=1).fit(points[:, :2]).labels_
        trunks = []
        branches = []

        point_load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, len(points))
        center_x = float(points[:, 0].mean())
        center_y = float(points[:, 1].mean())

        physics_ok = True

        for tree_id in set(labels):
            tree_points = points[labels == tree_id]
            if len(tree_points) == 0:
                continue

            branch_points = GeometryProcessor.cluster_points(
                tree_points,
                eps=branch_merge_eps,
                min_samples=1,
            )

            if len(branch_points) == 0:
                branch_points = tree_points

            if len(branch_points) > max_branches_per_tree:
                chosen = np.argsort(branch_points[:, 2])[-max_branches_per_tree:]
                branch_points = branch_points[chosen]

            raw_trunk_x = float(branch_points[:, 0].mean())
            raw_trunk_y = float(branch_points[:, 1].mean())

            dir_x = raw_trunk_x - center_x
            dir_y = raw_trunk_y - center_y
            norm = np.sqrt(dir_x ** 2 + dir_y ** 2)

            if norm > 1e-6:
                trunk_x = raw_trunk_x + (dir_x / norm) * trunk_outside_offset
                trunk_y = raw_trunk_y + (dir_y / norm) * trunk_outside_offset
            else:
                trunk_x = raw_trunk_x
                trunk_y = raw_trunk_y

            # Collision-aware heuristic:
            # keep trunk outside model XY footprint.
            trunk_x, trunk_y = push_point_outside_xy_bbox(
                trunk_x,
                trunk_y,
                center_x,
                center_y,
                mesh_bounds,
                margin=collision_margin,
            )

            z_min = float(branch_points[:, 2].min())
            z_p15 = float(np.percentile(branch_points[:, 2], 15))
            trunk_z_top = min(z_p15, z_min - 0.5)

            # If top is too close to bed, use a safer trunk height target.
            if trunk_z_top - bed_z <= min_height:
                trunk_z_top = bed_z + min_height + 0.5

            trunk_height = trunk_z_top - bed_z
            if trunk_height <= min_height:
                continue

            trunk_force = point_load * len(branch_points)

            trunk_radius = StructuralAnalyzer.required_radius_for_force(
                trunk_force,
                trunk_height,
                material,
                nozzle_mm,
                safety_factor,
                visual_min_radius=radius * 1.80,
            )

            compression = StructuralAnalyzer.compression_check(
                trunk_force,
                trunk_radius,
                material,
                safety_factor,
            )
            buckling = StructuralAnalyzer.buckling_check(
                trunk_force,
                trunk_radius,
                trunk_height,
                material,
                safety_factor,
            )

            trunk_ok = compression["ok"] and buckling["ok"]
            physics_ok = physics_ok and trunk_ok

            trunk_id = len(trunks)
            trunks.append({
                "id": int(trunk_id),
                "x": float(trunk_x),
                "y": float(trunk_y),
                "z_bottom": float(bed_z),
                "z_top": float(trunk_z_top),
                "radius": float(trunk_radius),
                "height": float(trunk_height),
                "length": float(trunk_height),
                "force_n": float(trunk_force),
                "point_count": int(len(branch_points)),
                "compression": compression,
                "buckling": buckling,
                "ok": bool(trunk_ok),
            })

            for p in branch_points:
                px, py, pz = float(p[0]), float(p[1]), float(p[2])
                if pz <= trunk_z_top:
                    continue

                dir_bx = px - center_x
                dir_by = py - center_y
                norm_b = np.sqrt(dir_bx ** 2 + dir_by ** 2)

                if norm_b < 1e-6:
                    dir_bx, dir_by = 1.0, 0.0
                else:
                    dir_bx /= norm_b
                    dir_by /= norm_b

                # V9.2: offset contact along approximate outward surface normal.
                contact_x, contact_y, contact_z = contact_point_with_normal_offset(
                    (px, py, pz),
                    center_x,
                    center_y,
                    tip_gap_mm,
                )

                # Curved branch control point is forced outside the model footprint.
                # V9: raycast-tested and pushed outward/lifted until it avoids the model.
                mid_x, mid_y, mid_z = find_safe_control_point(
                    mesh_for_raycast,
                    (trunk_x, trunk_y, trunk_z_top),
                    (contact_x, contact_y, contact_z),
                    center_x,
                    center_y,
                    mesh_bounds,
                    base_outside_offset=branch_outside_offset,
                    curve_lift=branch_curve_lift,
                    collision_margin=collision_margin,
                    clearance_mm=ray_clearance_mm,
                    max_attempts=7,
                )

                # Hard constraint: if the branch tube still intersects the mesh, skip this branch.
                if curve_hits_mesh(
                    mesh_for_raycast,
                    [trunk_x, trunk_y, trunk_z_top],
                    [mid_x, mid_y, mid_z],
                    [contact_x, contact_y, contact_z],
                    clearance_mm=ray_clearance_mm + max(0.6, radius * 0.35),
                    samples=10,
                ):
                    continue

                branch_length = approximate_bezier_length(
                    [trunk_x, trunk_y, trunk_z_top],
                    [mid_x, mid_y, mid_z],
                    [contact_x, contact_y, contact_z],
                    samples=12,
                )

                branch_force = point_load

                # Taper logic:
                # lower branch side thicker, upper contact side thinner.
                visual_radius = radius * 0.35 + 0.0035 * branch_length
                branch_radius = StructuralAnalyzer.required_radius_for_force(
                    branch_force,
                    branch_length,
                    material,
                    nozzle_mm,
                    safety_factor,
                    visual_min_radius=visual_radius,
                )

                compression_b = StructuralAnalyzer.compression_check(
                    branch_force,
                    branch_radius,
                    material,
                    safety_factor,
                )
                buckling_b = StructuralAnalyzer.buckling_check(
                    branch_force,
                    branch_radius,
                    branch_length,
                    material,
                    safety_factor,
                )

                branch_ok = compression_b["ok"] and buckling_b["ok"]
                physics_ok = physics_ok and branch_ok

                branches.append({
                    "tree_id": int(tree_id),
                    "trunk_id": int(trunk_id),

                    "x1": float(trunk_x),
                    "y1": float(trunk_y),
                    "z1": float(trunk_z_top),

                    "xm": float(mid_x),
                    "ym": float(mid_y),
                    "zm": float(mid_z),

                    "x2": float(contact_x),
                    "y2": float(contact_y),
                    "z2": float(contact_z),

                    "radius": float(branch_radius),
                    "height": float(contact_z - trunk_z_top),
                    "length": float(branch_length),
                    "force_n": float(branch_force),
                    "compression": compression_b,
                    "buckling": buckling_b,
                    "ok": bool(branch_ok),
                })

        return {
            "trunks": trunks,
            "branches": branches,
            "physics": {
                "all_ok": bool(physics_ok),
                "trunk_count": len(trunks),
                "branch_count": len(branches),
            }
        }


# =========================================================
# PHYSICS MOTOR
# =========================================================

class PhysicsMotor:

    @staticmethod
    def calculate_contact_points(supports: Dict[str, Any]) -> List[Tuple[float, float, float]]:
        contact_points = []

        for trunk in supports.get("trunks", []):
            if "x" in trunk and "y" in trunk and "z_top" in trunk:
                contact_points.append((
                    float(trunk["x"]),
                    float(trunk["y"]),
                    float(trunk["z_top"]),
                ))

        for branch in supports.get("branches", []):
            if "x2" in branch and "y2" in branch and "z2" in branch:
                contact_points.append((
                    float(branch["x2"]),
                    float(branch["y2"]),
                    float(branch["z2"]),
                ))
            elif "x" in branch and "y" in branch and "z_top" in branch:
                contact_points.append((
                    float(branch["x"]),
                    float(branch["y"]),
                    float(branch["z_top"]),
                ))

        return contact_points

    @staticmethod
    def calculate_coverage(points: np.ndarray, supports: Dict[str, Any], max_xy_distance: float = 8.0) -> float:
        if len(points) == 0:
            return 1.0

        contact_points = PhysicsMotor.calculate_contact_points(supports)
        if len(contact_points) == 0:
            return 0.0

        supported_count = 0

        for point in points:
            px, py, pz = point

            for sx, sy, sz in contact_points:
                d = np.sqrt((sx - px) ** 2 + (sy - py) ** 2)

                # Contact/support point should be close in XY.
                # In this simplified model branch endpoint equals overhang target.
                if d <= max_xy_distance and sz <= pz + 3.0:
                    supported_count += 1
                    break

        return float(supported_count / len(points))

    @staticmethod
    def calculate_support_volume(supports: Dict[str, Any]) -> float:
        volume = 0.0

        for trunk in supports.get("trunks", []):
            radius = trunk.get("radius", 1.0)
            height = trunk.get("height", trunk.get("length", 1.0))
            volume += np.pi * radius ** 2 * height

        for branch in supports.get("branches", []):
            radius = branch.get("radius", 0.5)
            length = branch.get("length", 10.0)
            volume += np.pi * radius ** 2 * length * 0.85

        return float(volume)

    @staticmethod
    def calculate_feasibility_score(
        volume: float,
        coverage: float,
        compression_ok: bool,
        buckling_ok: bool,
        support_count: int,
        min_coverage: float = 0.3,
    ) -> float:
        """
        Higher is better.
        This no longer clamps to zero before comparing.
        """
        coverage_score = coverage * 1000.0
        volume_penalty = volume * 0.003
        support_penalty = support_count * 0.8
        coverage_penalty = max(0.0, min_coverage - coverage) * 2000.0
        physics_penalty = 0.0 if (compression_ok and buckling_ok) else 100000.0

        return float(coverage_score - volume_penalty - support_penalty - coverage_penalty - physics_penalty)

    @staticmethod
    def assess_support_feasibility(
        supports: Dict[str, Any],
        overhang_points: np.ndarray,
        mesh_mass_g: float,
        material: str,
        nozzle_mm: float,
        safety_factor: float = 2.0,
        min_coverage: float = 0.3,
        max_xy_distance: float = 8.0,
    ) -> Dict[str, Any]:

        trunks = supports.get("trunks", [])
        branches = supports.get("branches", [])

        all_compression_ok = True
        all_buckling_ok = True
        physics_details = []

        for trunk in trunks:
            compression = StructuralAnalyzer.compression_check(
                trunk.get("force_n", 0.0),
                trunk.get("radius", 1.8),
                material,
                safety_factor,
            )
            buckling = StructuralAnalyzer.buckling_check(
                trunk.get("force_n", 0.0),
                trunk.get("radius", 1.8),
                trunk.get("height", trunk.get("length", 1.0)),
                material,
                safety_factor,
            )
            ok = compression["ok"] and buckling["ok"]
            all_compression_ok = all_compression_ok and compression["ok"]
            all_buckling_ok = all_buckling_ok and buckling["ok"]
            physics_details.append({
                "type": "trunk",
                "compression": compression,
                "buckling": buckling,
                "ok": ok,
            })

        for branch in branches:
            compression = StructuralAnalyzer.compression_check(
                branch.get("force_n", 0.0),
                branch.get("radius", 1.0),
                material,
                safety_factor,
            )
            buckling = StructuralAnalyzer.buckling_check(
                branch.get("force_n", 0.0),
                branch.get("radius", 1.0),
                branch.get("length", 1.0),
                material,
                safety_factor,
            )
            ok = compression["ok"] and buckling["ok"]
            all_compression_ok = all_compression_ok and compression["ok"]
            all_buckling_ok = all_buckling_ok and buckling["ok"]
            physics_details.append({
                "type": "branch",
                "compression": compression,
                "buckling": buckling,
                "ok": ok,
            })

        coverage = PhysicsMotor.calculate_coverage(overhang_points, supports, max_xy_distance)
        volume = PhysicsMotor.calculate_support_volume(supports)

        support_count_total = len(trunks) + len(branches)
        collision_count = int(supports.get("collision_count", 0))
        disconnected_count = int(supports.get("disconnected_count", disconnected_count_for_tree(supports)))

        gp = goal_programming_objective(
            coverage=coverage,
            volume=volume,
            support_count=support_count_total,
            collision_count=collision_count,
            disconnected_count=disconnected_count,
            target_coverage=min_coverage,
            target_volume=float(supports.get("target_volume", 180000.0)),
            target_support_count=int(supports.get("target_support_count", 180)),
            big_m_collision=float(supports.get("big_m_collision", BIG_M_DEFAULT)),
            big_m_disconnected=float(supports.get("big_m_disconnected", BIG_M_DEFAULT)),
        )

        score = -float(gp["Z"])

        is_feasible = (
            all_compression_ok
            and all_buckling_ok
            and coverage >= min_coverage
            and support_count_total > 0
            and collision_count == 0
            and disconnected_count == 0
        )

        return {
            "feasible": bool(is_feasible),
            "status": FeasibilityStatus.FEASIBLE if is_feasible else FeasibilityStatus.INFEASIBLE,
            "physics": {
                "all_compression_ok": bool(all_compression_ok),
                "all_buckling_ok": bool(all_buckling_ok),
                "details": physics_details,
            },
            "coverage": {
                "ratio": float(coverage),
                "percentage": float(coverage * 100.0),
                "min_required": float(min_coverage * 100.0),
                "ok": bool(coverage >= min_coverage),
            },
            "volume": {
                "total_mm3": float(volume),
                "estimate_g": float(volume * MATERIALS[material].density_g_cm3 / 1000.0),
            },
            "score": {
                "value": float(score),
                "status": "acceptable" if is_feasible else "unacceptable",
                "goal_programming": gp,
                "details": {
                    "objective_Z_minimize": float(gp["Z"]),
                    "volume_penalty": float(gp["deviations"]["d_volume_over"] * gp["weights"]["w_volume"]),
                    "support_penalty": float(gp["deviations"]["d_support_over"] * gp["weights"]["w_support_count"]),
                    "coverage_penalty": float(gp["deviations"]["d_coverage_under"] * gp["weights"]["w_coverage"]),
                    "collision_penalty": float(gp["deviations"]["d_collision"] * gp["weights"]["big_m_collision"]),
                    "disconnected_penalty": float(gp["deviations"]["d_disconnected"] * gp["weights"]["big_m_disconnected"]),
                    "physics_penalty": 0 if (all_compression_ok and all_buckling_ok) else 100000,
                },
            },
            "support_structure": {
                "trunk_count": len(trunks),
                "branch_count": len(branches),
                "total_count": support_count_total,
                "collision_count": collision_count,
                "disconnected_count": disconnected_count,
            },
        }


# =========================================================
# MESH GENERATION
# =========================================================

class MeshGenerator:

    @staticmethod
    def cylinder_between(p1: np.ndarray, p2: np.ndarray, radius: float = 2.0, sections: int = 16) -> Optional[trimesh.Trimesh]:
        p1 = np.array(p1, dtype=float)
        p2 = np.array(p2, dtype=float)

        direction = p2 - p1
        length = np.linalg.norm(direction)

        if length < 1e-6:
            return None

        cyl = trimesh.creation.cylinder(
            radius=radius,
            height=length,
            sections=sections,
        )

        transform = trimesh.geometry.align_vectors(
            np.array([0.0, 0.0, 1.0]),
            direction / length,
        )

        cyl.apply_transform(transform)
        cyl.apply_translation((p1 + p2) / 2.0)
        return cyl

    @staticmethod
    def base_plate(x: float, y: float, bed_z: float, radius: float = 5.0, height: float = 0.8, sections: int = 24) -> trimesh.Trimesh:
        plate = trimesh.creation.cylinder(
            radius=radius,
            height=height,
            sections=sections,
        )
        plate.apply_translation([x, y, bed_z + height / 2.0])
        return plate

    @staticmethod
    def tube_along_bezier(p0, p1, p2, radius_start: float, radius_end: Optional[float] = None, sections: int = 8, tube_sections: int = 10) -> List[trimesh.Trimesh]:
        if radius_end is None:
            radius_end = radius_start * 0.65

        pts = [bezier_quad(p0, p1, p2, t) for t in np.linspace(0, 1, sections)]
        meshes = []

        for i in range(len(pts) - 1):
            t = i / max(1, len(pts) - 2)
            r = radius_start * (1 - t) + radius_end * t
            cyl = MeshGenerator.cylinder_between(pts[i], pts[i + 1], r, tube_sections)
            if cyl is not None:
                meshes.append(cyl)

        return meshes

    @staticmethod
    def tree_support_mesh(tree: Dict[str, Any], bed_z: float, add_base: bool = True, smooth_branches: bool = True) -> Optional[trimesh.Trimesh]:
        meshes = []

        for t in tree.get("trunks", []):
            cyl = MeshGenerator.cylinder_between(
                [t["x"], t["y"], t["z_bottom"]],
                [t["x"], t["y"], t["z_top"]],
                t.get("radius", 1.8),
                sections=20,
            )
            if cyl is not None:
                meshes.append(cyl)

            if add_base:
                meshes.append(MeshGenerator.base_plate(
                    t["x"],
                    t["y"],
                    bed_z=bed_z,
                    radius=t.get("radius", 1.8) * 2.2,
                    height=max(0.6, t.get("radius", 1.8) * 0.25),
                ))

        for b in tree.get("branches", []):
            if smooth_branches:
                meshes.extend(MeshGenerator.tube_along_bezier(
                    [b["x1"], b["y1"], b["z1"]],
                    [b["xm"], b["ym"], b["zm"]],
                    [b["x2"], b["y2"], b["z2"]],
                    radius_start=b.get("radius", 1.0),
                    radius_end=b.get("radius", 1.0) * 0.65,
                    sections=8,
                    tube_sections=10,
                ))
            else:
                cyl1 = MeshGenerator.cylinder_between(
                    [b["x1"], b["y1"], b["z1"]],
                    [b["xm"], b["ym"], b["zm"]],
                    b.get("radius", 1.0),
                    sections=12,
                )
                cyl2 = MeshGenerator.cylinder_between(
                    [b["xm"], b["ym"], b["zm"]],
                    [b["x2"], b["y2"], b["z2"]],
                    b.get("radius", 1.0) * 0.85,
                    sections=12,
                )
                if cyl1 is not None:
                    meshes.append(cyl1)
                if cyl2 is not None:
                    meshes.append(cyl2)

        if len(meshes) == 0:
            return None

        return trimesh.util.concatenate(meshes)

    @staticmethod
    def classic_support_mesh(supports: List[Dict[str, float]], bed_z: float, add_base: bool = True) -> Optional[trimesh.Trimesh]:
        meshes = []

        for s in supports:
            cyl = MeshGenerator.cylinder_between(
                [s["x"], s["y"], s["z_bottom"]],
                [s["x"], s["y"], s["z_top"]],
                s.get("radius", 1.5),
                sections=16,
            )
            if cyl is not None:
                meshes.append(cyl)

            if add_base:
                meshes.append(MeshGenerator.base_plate(
                    s["x"],
                    s["y"],
                    bed_z=bed_z,
                    radius=s.get("radius", 1.5) * 2.2,
                    height=max(0.6, s.get("radius", 1.5) * 0.25),
                ))

        if len(meshes) == 0:
            return None

        return trimesh.util.concatenate(meshes)


# =========================================================
# ANGLE SCANNER
# =========================================================

@dataclass
class AngleScanResult:
    rho: float
    theta: float
    feasible: bool
    overhang_points: int
    coverage_percent: float
    volume_mm3: float
    support_mass_g: float
    score: float
    compression_ok: bool
    buckling_ok: bool
    support_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rho": self.rho,
            "theta": self.theta,
            "feasible": self.feasible,
            "overhang_points": self.overhang_points,
            "coverage_percent": self.coverage_percent,
            "volume_mm3": self.volume_mm3,
            "support_mass_g": self.support_mass_g,
            "score": self.score,
            "support_count": self.support_count,
            "physics": {
                "compression_ok": self.compression_ok,
                "buckling_ok": self.buckling_ok,
            }
        }


def build_supports_for_orientation(
    mesh: trimesh.Trimesh,
    points: np.ndarray,
    bed_z: float,
    support_type: str,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    tree_eps: float,
    branch_merge_eps: float,
    max_branches_per_tree: int,
    trunk_outside_offset: float,
    branch_outside_offset: float,
    branch_curve_lift: float,
    collision_margin: float = 10.0,
    tip_gap_mm: float = 0.25,
    ray_clearance_mm: float = 0.8,
    downray_filter: bool = True,
    target_volume: float = 180000.0,
    target_support_count: int = 180,
    big_m_collision: float = BIG_M_DEFAULT,
    big_m_disconnected: float = BIG_M_DEFAULT,
) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]]]:
    if support_type == "classic":
        supports_list = SupportGenerator.classic_supports(
            points,
            radius=support_radius,
            bed_z=bed_z,
            mesh_mass_g=mesh_mass_g,
        )
        return {"trunks": [], "branches": supports_list}, supports_list

    supports = SupportGenerator.load_aware_tree_supports(
        points,
        radius=support_radius,
        tree_eps=tree_eps,
        branch_merge_eps=branch_merge_eps,
        max_branches_per_tree=max_branches_per_tree,
        bed_z=bed_z,
        material=material,
        nozzle_mm=nozzle_mm,
        mesh_mass_g=mesh_mass_g,
        safety_factor=safety_factor,
        trunk_outside_offset=trunk_outside_offset,
        branch_outside_offset=branch_outside_offset,
        branch_curve_lift=branch_curve_lift,
        mesh_bounds=mesh.bounds,
        mesh_for_raycast=mesh,
        collision_margin=collision_margin,
        tip_gap_mm=tip_gap_mm,
        ray_clearance_mm=ray_clearance_mm,
        downray_filter=downray_filter,
    )
    supports["collision_count"] = collision_count_for_tree(
        supports,
        mesh,
        clearance_mm=ray_clearance_mm,
    )
    supports["disconnected_count"] = disconnected_count_for_tree(supports)
    supports["target_volume"] = float(target_volume)
    supports["target_support_count"] = int(target_support_count)
    supports["big_m_collision"] = float(big_m_collision)
    supports["big_m_disconnected"] = float(big_m_disconnected)
    return supports, None


def evaluate_orientation(
    original_mesh: trimesh.Trimesh,
    rho_deg: float,
    theta_deg: float,
    support_type: str,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    critical_angle_deg: float,
    min_coverage: float,
    sample_points: int,
    max_xy_distance: float,
    tree_eps: float,
    branch_merge_eps: float,
    max_branches_per_tree: int,
    trunk_outside_offset: float,
    branch_outside_offset: float,
    branch_curve_lift: float,
    collision_margin: float,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    downray_filter: bool,
    target_volume: float,
    target_support_count: int,
    big_m_collision: float,
    big_m_disconnected: float,
) -> AngleScanResult:
    try:
        mesh, points, bed_z = GeometryProcessor.process_rotated_mesh(
            original_mesh,
            rho_deg,
            theta_deg,
            critical_angle_deg,
            sample_points,
        )

        supports, _ = build_supports_for_orientation(
            mesh,
            points,
            bed_z,
            support_type,
            material,
            nozzle_mm,
            support_radius,
            mesh_mass_g,
            safety_factor,
            tree_eps,
            branch_merge_eps,
            max_branches_per_tree,
            trunk_outside_offset,
            branch_outside_offset,
            branch_curve_lift,
            collision_margin,
            tip_gap_mm,
            ray_clearance_mm,
            downray_filter,
            target_volume,
            target_support_count,
            big_m_collision,
            big_m_disconnected,
        )

        feasibility = PhysicsMotor.assess_support_feasibility(
            supports,
            points,
            mesh_mass_g,
            material,
            nozzle_mm,
            safety_factor,
            min_coverage,
            max_xy_distance,
        )

        stability_bonus = orientation_stability_bonus(mesh, bed_z, rho_deg, theta_deg)
        final_score = float(feasibility["score"]["value"]) + stability_bonus

        return AngleScanResult(
            rho=float(rho_deg),
            theta=float(theta_deg),
            feasible=bool(feasibility["feasible"]),
            overhang_points=int(len(points)),
            coverage_percent=float(feasibility["coverage"]["percentage"]),
            volume_mm3=float(feasibility["volume"]["total_mm3"]),
            support_mass_g=float(feasibility["volume"]["estimate_g"]),
            score=float(final_score),
            compression_ok=bool(feasibility["physics"]["all_compression_ok"]),
            buckling_ok=bool(feasibility["physics"]["all_buckling_ok"]),
            support_count=int(feasibility["support_structure"]["total_count"]),
        )

    except Exception as e:
        logger.error(f"Error evaluating rho={rho_deg}, theta={theta_deg}: {e}")
        return AngleScanResult(
            rho=float(rho_deg),
            theta=float(theta_deg),
            feasible=False,
            overhang_points=0,
            coverage_percent=0.0,
            volume_mm3=0.0,
            support_mass_g=0.0,
            score=-999999.0,
            compression_ok=False,
            buckling_ok=False,
            support_count=0,
        )



# =========================================================
# ADAPTIVE SEARCH + FLAT BASE BONUS
# =========================================================

def estimate_bed_contact_area(mesh: trimesh.Trimesh, bed_z: float, tolerance_mm: float = 0.8) -> float:
    """
    Approximate how much area touches the build plate.
    This gives a bonus to naturally flat-bottom orientations.
    """
    try:
        centers = mesh.triangles_center
        near_bed = centers[:, 2] <= bed_z + tolerance_mm
        if len(mesh.area_faces) != len(near_bed):
            return 0.0
        return float(mesh.area_faces[near_bed].sum())
    except Exception:
        return 0.0


def orientation_stability_bonus(mesh: trimesh.Trimesh, bed_z: float, rho_deg: float, theta_deg: float) -> float:
    """
    Higher score for flat contact and lower height.
    Also mildly rewards 0°/0° if the model really has a good base.
    """
    contact_area = estimate_bed_contact_area(mesh, bed_z)
    height = float(mesh.bounds[1][2] - mesh.bounds[0][2])

    # Contact area can be large; keep coefficient modest.
    contact_bonus = contact_area * 0.02
    height_penalty = height * 0.35

    # Mild orientation prior: flat original orientation is often preferred for flat-base models.
    zero_angle_bonus = 0.0
    if abs(rho_deg) < 1e-6 and abs(theta_deg) < 1e-6:
        zero_angle_bonus = 120.0

    return float(contact_bonus - height_penalty + zero_angle_bonus)


def choose_best_result(results: List[AngleScanResult]) -> AngleScanResult:
    feasible = [r for r in results if r.feasible]
    if feasible:
        return max(feasible, key=lambda r: r.score)
    return max(results, key=lambda r: (r.coverage_percent, r.score))


def adaptive_scan_orientations(
    original_mesh: trimesh.Trimesh,
    support_type: str,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    critical_angle_deg: float,
    min_coverage: float,
    sample_points: int,
    max_xy_distance: float,
    tree_eps: float,
    branch_merge_eps: float,
    max_branches_per_tree: int,
    trunk_outside_offset: float,
    branch_outside_offset: float,
    branch_curve_lift: float,
    collision_margin: float,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    downray_filter: bool,
    target_volume: float,
    target_support_count: int,
    big_m_collision: float,
    big_m_disconnected: float,
    initial_step: float = 60.0,
    min_step: float = 1.0,
    top_k: int = 2,
) -> List[AngleScanResult]:
    """
    Adaptive rho/theta scan.

    Logic:
    1) Start coarse: rho 0,60,120,180 and theta 0,60,90.
    2) If feasible exists, refine around top feasible candidates.
    3) If no feasible exists, refine around the best infeasible candidate.
    4) Step halves: 60 -> 30 -> 15 -> 7.5 -> ... -> 1.
    """
    evaluated: Dict[Tuple[float, float], AngleScanResult] = {}

    def norm_angle(v: float, lo: float, hi: float) -> float:
        return float(round(min(max(v, lo), hi), 6))

    def eval_pair(rho: float, theta: float):
        rho = norm_angle(rho, 0.0, 180.0)
        theta = norm_angle(theta, 0.0, 90.0)
        key = (rho, theta)
        if key in evaluated:
            return
        evaluated[key] = evaluate_orientation(
            original_mesh=original_mesh,
            rho_deg=rho,
            theta_deg=theta,
            support_type=support_type,
            material=material,
            nozzle_mm=nozzle_mm,
            support_radius=support_radius,
            mesh_mass_g=mesh_mass_g,
            safety_factor=safety_factor,
            critical_angle_deg=critical_angle_deg,
            min_coverage=min_coverage,
            sample_points=sample_points,
            max_xy_distance=max_xy_distance,
            tree_eps=tree_eps,
            branch_merge_eps=branch_merge_eps,
            max_branches_per_tree=max_branches_per_tree,
            trunk_outside_offset=trunk_outside_offset,
            branch_outside_offset=branch_outside_offset,
            branch_curve_lift=branch_curve_lift,
            collision_margin=collision_margin,
            tip_gap_mm=tip_gap_mm,
            ray_clearance_mm=ray_clearance_mm,
            downray_filter=downray_filter,
            target_volume=target_volume,
            target_support_count=target_support_count,
            big_m_collision=big_m_collision,
            big_m_disconnected=big_m_disconnected,
        )

    # Coarse grid. Include theta=90 even if step is 60.
    rho_values = sorted(set([0.0, initial_step, initial_step * 2, 180.0]))
    rho_values = [v for v in rho_values if 0.0 <= v <= 180.0]
    theta_values = sorted(set([0.0, initial_step, 90.0]))
    theta_values = [v for v in theta_values if 0.0 <= v <= 90.0]

    for r in rho_values:
        for t in theta_values:
            eval_pair(r, t)

    step = initial_step / 2.0

    while step >= min_step:
        current = list(evaluated.values())
        feasible = [r for r in current if r.feasible]

        if feasible:
            anchors = sorted(feasible, key=lambda r: r.score, reverse=True)[:top_k]
        else:
            anchors = sorted(current, key=lambda r: (r.coverage_percent, r.score), reverse=True)[:1]

        for a in anchors:
            local_rhos = [
                norm_angle(a.rho - step, 0.0, 180.0),
                norm_angle(a.rho, 0.0, 180.0),
                norm_angle(a.rho + step, 0.0, 180.0),
            ]
            local_thetas = [
                norm_angle(a.theta - step, 0.0, 90.0),
                norm_angle(a.theta, 0.0, 90.0),
                norm_angle(a.theta + step, 0.0, 90.0),
            ]

            for r in local_rhos:
                for t in local_thetas:
                    eval_pair(r, t)

        step = step / 2.0

    return list(evaluated.values())


# =========================================================
# API ROUTES
# =========================================================

@app.get("/")
def root():
    return {
        "message": "3D Support Optimizer v6.0 - Hybrid Full System",
        "endpoints": [
            "/analyze",
            "/generate-and-export",
            "/health",
        ]
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "version": "6.0.0",
        "supported_materials": list(MATERIALS.keys()),
        "supported_nozzles": ALLOWED_NOZZLES,
        "support_types": [x.value for x in SupportType],
        "ray_engine": "pyembree/embreex if installed, otherwise trimesh native",
        "objective": "weighted nonlinear goal programming with Big-M collision/connectivity penalties",
    }


@app.post("/analyze")
async def analyze_all_orientations(
    file: UploadFile = File(...),
    support_type: Literal["classic", "tree"] = "tree",
    material: Literal["PLA", "ABS", "PETG", "TPU"] = DEFAULT_MATERIAL,
    nozzle_mm: float = DEFAULT_NOZZLE,
    rho_step: float = 15.0,
    theta_step: float = 15.0,
    search_mode: Literal["adaptive", "full"] = "adaptive",
    adaptive_initial_step: float = 60.0,
    adaptive_min_step: float = 1.0,
    adaptive_top_k: int = 2,
    support_radius: float = 1.8,
    mesh_mass_g: float = 100.0,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
    critical_angle_deg: float = 45.0,
    min_coverage: float = 0.3,
    sample_points: int = 2000,
    max_xy_distance: float = 8.0,
    tree_eps: float = 8.0,
    branch_merge_eps: float = 2.5,
    max_branches_per_tree: int = 240,
    trunk_outside_offset: float = 10.0,
    branch_outside_offset: float = 35.0,
    branch_curve_lift: float = 8.0,
    collision_margin: float = 10.0,
    tip_gap_mm: float = 0.25,
    ray_clearance_mm: float = 0.8,
    downray_filter: bool = True,
    target_volume: float = 180000.0,
    target_support_count: int = 180,
    big_m_collision: float = BIG_M_DEFAULT,
    big_m_disconnected: float = BIG_M_DEFAULT,
):
    try:
        nozzle_mm = validate_nozzle(nozzle_mm)
    except InvalidNozzleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with temporary_mesh_file() as tmp_path:
        try:
            contents = await file.read()
            with open(tmp_path, "wb") as f:
                f.write(contents)

            original_mesh = trimesh.load_mesh(tmp_path)

            if search_mode == "adaptive":
                all_results = adaptive_scan_orientations(
                    original_mesh=original_mesh,
                    support_type=support_type,
                    material=material,
                    nozzle_mm=nozzle_mm,
                    support_radius=support_radius,
                    mesh_mass_g=mesh_mass_g,
                    safety_factor=safety_factor,
                    critical_angle_deg=critical_angle_deg,
                    min_coverage=min_coverage,
                    sample_points=sample_points,
                    max_xy_distance=max_xy_distance,
                    tree_eps=tree_eps,
                    branch_merge_eps=branch_merge_eps,
                    max_branches_per_tree=max_branches_per_tree,
                    trunk_outside_offset=trunk_outside_offset,
                    branch_outside_offset=branch_outside_offset,
                    branch_curve_lift=branch_curve_lift,
                    collision_margin=collision_margin,
                    tip_gap_mm=tip_gap_mm,
                    ray_clearance_mm=ray_clearance_mm,
                    downray_filter=downray_filter,
                    target_volume=target_volume,
                    target_support_count=target_support_count,
                    big_m_collision=big_m_collision,
                    big_m_disconnected=big_m_disconnected,
                    initial_step=adaptive_initial_step,
                    min_step=adaptive_min_step,
                    top_k=adaptive_top_k,
                )
            else:
                rho_angles = list(np.arange(0, 181, rho_step))
                theta_angles = list(np.arange(0, 91, theta_step))
                all_results: List[AngleScanResult] = []

                for rho in rho_angles:
                    for theta in theta_angles:
                        result = evaluate_orientation(
                            original_mesh=original_mesh,
                            rho_deg=float(rho),
                            theta_deg=float(theta),
                            support_type=support_type,
                            material=material,
                            nozzle_mm=nozzle_mm,
                            support_radius=support_radius,
                            mesh_mass_g=mesh_mass_g,
                            safety_factor=safety_factor,
                            critical_angle_deg=critical_angle_deg,
                            min_coverage=min_coverage,
                            sample_points=sample_points,
                            max_xy_distance=max_xy_distance,
                            tree_eps=tree_eps,
                            branch_merge_eps=branch_merge_eps,
                            max_branches_per_tree=max_branches_per_tree,
                            trunk_outside_offset=trunk_outside_offset,
                            branch_outside_offset=branch_outside_offset,
                            branch_curve_lift=branch_curve_lift,
                            collision_margin=collision_margin,
                            tip_gap_mm=tip_gap_mm,
                            ray_clearance_mm=ray_clearance_mm,
                            downray_filter=downray_filter,
                            target_volume=target_volume,
                            target_support_count=target_support_count,
                            big_m_collision=big_m_collision,
                            big_m_disconnected=big_m_disconnected,
                        )
                        all_results.append(result)

            feasible_results = [r for r in all_results if r.feasible]

            # Feasible: sort by score high to low.
            # Top attempts: coverage high to low.
            feasible_results.sort(key=lambda r: r.score, reverse=True)
            top_attempts = sorted(all_results, key=lambda r: (r.coverage_percent, r.score), reverse=True)[:10]

            overall_feasible = len(feasible_results) > 0
            response_data = {
                "overall_feasible": overall_feasible,
                "overall_status": "feasible" if overall_feasible else "infeasible",
                "filename": file.filename,
                "analysis_type": "spherical_coordinates",
                "parameters": {
                    "support_type": support_type,
                    "material": material,
                    "nozzle_mm": float(nozzle_mm),
                    "support_radius": float(support_radius),
                    "rho_range": "0-180°",
                    "theta_range": "0-90°",
                    "rho_step": float(rho_step),
                    "theta_step": float(theta_step),
                    "search_mode": search_mode,
                    "adaptive_initial_step": float(adaptive_initial_step),
                    "adaptive_min_step": float(adaptive_min_step),
                    "adaptive_top_k": int(adaptive_top_k),
                    "min_coverage": float(min_coverage),
                    "sample_points": int(sample_points),
                    "max_xy_distance": float(max_xy_distance),
                    "tree_eps": float(tree_eps),
                    "branch_merge_eps": float(branch_merge_eps),
                    "collision_margin": float(collision_margin),
                    "tip_gap_mm": float(tip_gap_mm),
                    "ray_clearance_mm": float(ray_clearance_mm),
                    "downray_filter": bool(downray_filter),
                    "target_volume": float(target_volume),
                    "target_support_count": int(target_support_count),
                    "big_m_collision": float(big_m_collision),
                    "big_m_disconnected": float(big_m_disconnected),
                },
                "summary": {
                    "total_orientations_tested": len(all_results),
                    "feasible_orientations": len(feasible_results),
                    "infeasible_orientations": len(all_results) - len(feasible_results),
                },
                "feasible_solutions": [r.to_dict() for r in feasible_results],
                "top_attempts": [r.to_dict() for r in top_attempts],
            }

            if feasible_results:
                response_data["best_solution"] = feasible_results[0].to_dict()
            else:
                response_data["best_attempt"] = top_attempts[0].to_dict() if top_attempts else None

            return JSONResponse(
                content=response_data,
                headers={
                    "X-Overall-Status": response_data["overall_status"],
                    "X-Feasible-Count": str(len(feasible_results)),
                }
            )

        except Exception as e:
            logger.error(f"Error in /analyze: {e}")
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/generate-and-export")
async def generate_and_export(
    file: UploadFile = File(...),
    support_type: Literal["classic", "tree"] = "tree",
    material: Literal["PLA", "ABS", "PETG", "TPU"] = DEFAULT_MATERIAL,
    nozzle_mm: float = DEFAULT_NOZZLE,
    rho_step: float = 15.0,
    theta_step: float = 15.0,
    search_mode: Literal["adaptive", "full"] = "adaptive",
    adaptive_initial_step: float = 60.0,
    adaptive_min_step: float = 1.0,
    adaptive_top_k: int = 2,
    support_radius: float = 1.8,
    mesh_mass_g: float = 100.0,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
    critical_angle_deg: float = 45.0,
    min_coverage: float = 0.3,
    add_base: bool = True,
    include_model: bool = True,
    sample_points: int = 2000,
    export_sample_points: int = 5000,
    max_xy_distance: float = 8.0,
    tree_eps: float = 8.0,
    branch_merge_eps: float = 2.5,
    max_branches_per_tree: int = 240,
    trunk_outside_offset: float = 10.0,
    branch_outside_offset: float = 35.0,
    branch_curve_lift: float = 8.0,
    collision_margin: float = 10.0,
    tip_gap_mm: float = 0.25,
    ray_clearance_mm: float = 0.8,
    downray_filter: bool = True,
    target_volume: float = 180000.0,
    target_support_count: int = 180,
    big_m_collision: float = BIG_M_DEFAULT,
    big_m_disconnected: float = BIG_M_DEFAULT,
    smooth_branches: bool = True,
    export_even_if_infeasible: bool = True,
):
    try:
        nozzle_mm = validate_nozzle(nozzle_mm)
    except InvalidNozzleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    with temporary_mesh_file() as tmp_path:
        try:
            contents = await file.read()
            with open(tmp_path, "wb") as f:
                f.write(contents)

            original_mesh = trimesh.load_mesh(tmp_path)

            if search_mode == "adaptive":
                all_results = adaptive_scan_orientations(
                    original_mesh=original_mesh,
                    support_type=support_type,
                    material=material,
                    nozzle_mm=nozzle_mm,
                    support_radius=support_radius,
                    mesh_mass_g=mesh_mass_g,
                    safety_factor=safety_factor,
                    critical_angle_deg=critical_angle_deg,
                    min_coverage=min_coverage,
                    sample_points=sample_points,
                    max_xy_distance=max_xy_distance,
                    tree_eps=tree_eps,
                    branch_merge_eps=branch_merge_eps,
                    max_branches_per_tree=max_branches_per_tree,
                    trunk_outside_offset=trunk_outside_offset,
                    branch_outside_offset=branch_outside_offset,
                    branch_curve_lift=branch_curve_lift,
                    collision_margin=collision_margin,
                    tip_gap_mm=tip_gap_mm,
                    ray_clearance_mm=ray_clearance_mm,
                    downray_filter=downray_filter,
                    target_volume=target_volume,
                    target_support_count=target_support_count,
                    big_m_collision=big_m_collision,
                    big_m_disconnected=big_m_disconnected,
                    initial_step=adaptive_initial_step,
                    min_step=adaptive_min_step,
                    top_k=adaptive_top_k,
                )
            else:
                rho_angles = list(np.arange(0, 181, rho_step))
                theta_angles = list(np.arange(0, 91, theta_step))
                all_results = []

                for rho in rho_angles:
                    for theta in theta_angles:
                        result = evaluate_orientation(
                            original_mesh=original_mesh,
                            rho_deg=float(rho),
                            theta_deg=float(theta),
                            support_type=support_type,
                            material=material,
                            nozzle_mm=nozzle_mm,
                            support_radius=support_radius,
                            mesh_mass_g=mesh_mass_g,
                            safety_factor=safety_factor,
                            critical_angle_deg=critical_angle_deg,
                            min_coverage=min_coverage,
                            sample_points=sample_points,
                            max_xy_distance=max_xy_distance,
                            tree_eps=tree_eps,
                            branch_merge_eps=branch_merge_eps,
                            max_branches_per_tree=max_branches_per_tree,
                            trunk_outside_offset=trunk_outside_offset,
                            branch_outside_offset=branch_outside_offset,
                            branch_curve_lift=branch_curve_lift,
                            collision_margin=collision_margin,
                            tip_gap_mm=tip_gap_mm,
                            ray_clearance_mm=ray_clearance_mm,
                            downray_filter=downray_filter,
                            target_volume=target_volume,
                            target_support_count=target_support_count,
                            big_m_collision=big_m_collision,
                            big_m_disconnected=big_m_disconnected,
                        )
                        all_results.append(result)

            feasible_results = [r for r in all_results if r.feasible]

            if feasible_results:
                best_result = max(feasible_results, key=lambda r: r.score)
            else:
                best_result = max(all_results, key=lambda r: (r.coverage_percent, r.score))

            if (not best_result.feasible) and (not export_even_if_infeasible):
                return JSONResponse(
                    status_code=400,
                    content={
                        "feasible": False,
                        "status": "infeasible",
                        "best_attempt": best_result.to_dict(),
                        "message": "No feasible solution found. Set export_even_if_infeasible=true to export best attempt.",
                    }
                )

            best_rho = best_result.rho
            best_theta = best_result.theta

            mesh, points, bed_z = GeometryProcessor.process_rotated_mesh(
                original_mesh,
                best_rho,
                best_theta,
                critical_angle_deg,
                export_sample_points,
            )

            supports, supports_list = build_supports_for_orientation(
                mesh=mesh,
                points=points,
                bed_z=bed_z,
                support_type=support_type,
                material=material,
                nozzle_mm=nozzle_mm,
                support_radius=support_radius,
                mesh_mass_g=mesh_mass_g,
                safety_factor=safety_factor,
                tree_eps=tree_eps,
                branch_merge_eps=branch_merge_eps,
                max_branches_per_tree=max_branches_per_tree,
                trunk_outside_offset=trunk_outside_offset,
                branch_outside_offset=branch_outside_offset,
                branch_curve_lift=branch_curve_lift,
                collision_margin=collision_margin,
                tip_gap_mm=tip_gap_mm,
                ray_clearance_mm=ray_clearance_mm,
                downray_filter=downray_filter,
                target_volume=target_volume,
                target_support_count=target_support_count,
                big_m_collision=big_m_collision,
                big_m_disconnected=big_m_disconnected,
            )

            if support_type == "classic":
                support_mesh = MeshGenerator.classic_support_mesh(
                    supports_list or [],
                    bed_z=bed_z,
                    add_base=add_base,
                )
            else:
                support_mesh = MeshGenerator.tree_support_mesh(
                    supports,
                    bed_z=bed_z,
                    add_base=add_base,
                    smooth_branches=smooth_branches,
                )

            if support_mesh is None:
                return JSONResponse(
                    status_code=400,
                    content={
                        "feasible": False,
                        "status": "infeasible",
                        "error": "Could not generate support mesh for selected orientation",
                        "best_attempt": best_result.to_dict(),
                    }
                )

            output_mesh = trimesh.util.concatenate([mesh, support_mesh]) if include_model else support_mesh

            output_name = f"hybrid_rho{best_rho:.0f}_theta{best_theta:.0f}_{support_type}_{uuid.uuid4().hex[:6]}.stl"
            output_path = os.path.join(tempfile.gettempdir(), output_name)
            output_mesh.export(output_path)

            return FileResponse(
                output_path,
                media_type="model/stl",
                filename=output_name,
                headers={
                    "X-Rho": str(best_rho),
                    "X-Theta": str(best_theta),
                    "X-Feasible": str(best_result.feasible),
                    "X-Support-Type": support_type,
                    "X-Score": str(best_result.score),
                    "X-Coverage": str(best_result.coverage_percent),
                }
            )

        except Exception as e:
            logger.error(f"Error in /generate-and-export: {e}")
            raise HTTPException(status_code=400, detail=str(e))


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
