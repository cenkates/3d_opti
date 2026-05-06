"""
3D Support Optimizer Beta V9.3 - Goal Programming Collision-Constrained Tree System
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
    title="3D Support Optimizer Beta V11.5 Collision Pruning",
    description="Beta V11.5: print-logic tree engine with collision-pruning, forced 0/0 orientation refinement, explicit infeasibility diagnostics, tapered supports, goal programming, and STL support export.",
    version="11.5.0-beta",
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
        max_branch_angle_deg: float = 70.0,
        min_cluster_points: int = 3,
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
            if len(tree_points) < min_cluster_points:
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

                # Hard constraint: branch must transfer load mostly downward.
                if max(
                    branch_vertical_angle_deg(
                        [trunk_x, trunk_y, trunk_z_top],
                        [mid_x, mid_y, mid_z],
                    ),
                    branch_vertical_angle_deg(
                        [mid_x, mid_y, mid_z],
                        [contact_x, contact_y, contact_z],
                    ),
                ) > max_branch_angle_deg:
                    continue

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

        coverage = coverage_v10(overhang_points, supports, max_xy_distance)
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
                "pruned_branches": int(supports.get("pruning", {}).get("pruned_branches", 0)),
                "pruned_trunks": int(supports.get("pruning", {}).get("pruned_trunks", 0)),
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
    collision_count: int = 0
    disconnected_count: int = 0
    pruned_branches: int = 0
    pruned_trunks: int = 0
    coverage_ok: bool = False
    reason: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        physics_ok = bool(self.compression_ok and self.buckling_ok)
        collision_ok = int(self.collision_count) == 0
        disconnected_ok = int(self.disconnected_count) == 0
        support_ok = int(self.support_count) > 0

        reason = self.reason
        if not reason:
            reasons = []
            if not self.coverage_ok:
                reasons.append("coverage_below_minimum")
            if not collision_ok:
                reasons.append("support_mesh_collision")
            if not disconnected_ok:
                reasons.append("disconnected_support_graph")
            if not physics_ok:
                reasons.append("physics_failure")
            if not support_ok:
                reasons.append("no_support_generated")
            reason = ", ".join(reasons) if reasons else "feasible"

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
            },
            "constraints": {
                "coverage_ok": bool(self.coverage_ok),
                "collision_count": int(self.collision_count),
                "collision_ok": bool(collision_ok),
                "disconnected_count": int(self.disconnected_count),
                "disconnected_ok": bool(disconnected_ok),
                "pruned_branches": int(self.pruned_branches),
                "pruned_trunks": int(self.pruned_trunks),
                "physics_ok": bool(physics_ok),
                "support_ok": bool(support_ok),
                "reason": reason,
            },
            "error": self.error,
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
    max_branch_angle_deg: float = 70.0,
    min_cluster_points: int = 3,
    auto_density: bool = True,
    density_grid_size: float = 6.0,
    density_min_points_per_cell: int = 2,
    max_density_supports: int = 160,
    density_target_coverage: float = 0.15,
    support_strategy: str = "pro",
    pro_grid_size: float = 8.0,
    pro_min_points_per_cell: int = 1,
    pro_max_supports: int = 90,
    pro_interface: bool = True,
    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> Tuple[Dict[str, Any], Optional[List[Dict[str, Any]]]]:
    if support_type == "classic":
        if support_strategy == "pro":
            supports_list = pro_classic_grid_supports(
                mesh=mesh,
                overhang_points=points,
                radius=support_radius,
                bed_z=bed_z,
                mesh_mass_g=mesh_mass_g,
                grid_size=pro_grid_size,
                min_points_per_cell=pro_min_points_per_cell,
                max_supports=pro_max_supports,
                tip_gap_mm=tip_gap_mm,
                ray_clearance_mm=ray_clearance_mm,
                downray_filter=downray_filter,
            )
        else:
            supports_list = classic_supports_collision_safe(
                mesh=mesh,
                points=points,
                radius=support_radius,
                bed_z=bed_z,
                mesh_mass_g=mesh_mass_g,
                min_height=1.0,
                tip_gap_mm=tip_gap_mm,
                ray_clearance_mm=ray_clearance_mm,
                downray_filter=downray_filter,
            )
        classic_tree = {"trunks": [], "branches": supports_list, "interfaces": []}
        classic_tree["collision_count"] = 0
        classic_tree["disconnected_count"] = 0
        classic_tree["target_volume"] = float(target_volume)
        classic_tree["target_support_count"] = int(target_support_count)
        classic_tree["big_m_collision"] = float(big_m_collision)
        classic_tree["big_m_disconnected"] = float(big_m_disconnected)
        return classic_tree, supports_list

    if support_strategy == "pro":
        supports = v11_build_tree_from_growth(
            mesh=mesh,
            overhang_points=points,
            bed_z=bed_z,
            material=material,
            nozzle_mm=nozzle_mm,
            support_radius=support_radius,
            mesh_mass_g=mesh_mass_g,
            safety_factor=safety_factor,
            grid_size=pro_grid_size,
            min_points_per_cell=pro_min_points_per_cell,
            max_tips=pro_max_supports,
            tip_gap_mm=tip_gap_mm,
            ray_clearance_mm=ray_clearance_mm,
            downray_filter=downray_filter,
            max_branch_angle_deg=max_branch_angle_deg,
            add_interface=pro_interface,
            external_tree=external_tree,
            external_margin=external_margin,
            fast_mode=fast_mode,
            strict_collision=strict_collision,
        )
        supports = apply_taper_to_tree(
            supports,
            bed_z=bed_z,
            support_radius=support_radius,
            nozzle_mm=nozzle_mm,
        )
        supports = prune_colliding_supports_v11_5(
            supports,
            mesh=mesh,
            ray_clearance_mm=ray_clearance_mm,
            support_radius=support_radius,
            fast_mode=fast_mode,
            strict_collision=strict_collision,
        )
        supports["target_volume"] = float(target_volume)
        supports["target_support_count"] = int(target_support_count)
        supports["big_m_collision"] = float(big_m_collision)
        supports["big_m_disconnected"] = float(big_m_disconnected)
        return supports, None

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
        max_branch_angle_deg=max_branch_angle_deg,
        min_cluster_points=min_cluster_points,
    )
    supports = prune_unused_trunks(supports)
    supports = apply_taper_to_tree(
        supports,
        bed_z=bed_z,
        support_radius=support_radius,
        nozzle_mm=nozzle_mm,
    )

    if auto_density:
        center_x = float(points[:, 0].mean()) if len(points) else 0.0
        center_y = float(points[:, 1].mean()) if len(points) else 0.0
        supports = generate_density_tree_additions(
            mesh=mesh,
            all_points=points,
            existing_tree=supports,
            bed_z=bed_z,
            material=material,
            nozzle_mm=nozzle_mm,
            support_radius=support_radius,
            mesh_mass_g=mesh_mass_g,
            safety_factor=safety_factor,
            center_x=center_x,
            center_y=center_y,
            target_coverage=max(float(density_target_coverage), 0.0),
            max_xy_distance=8.0,
            density_grid_size=density_grid_size,
            density_min_points_per_cell=density_min_points_per_cell,
            max_density_supports=max_density_supports,
            collision_margin=collision_margin,
            tip_gap_mm=tip_gap_mm,
            ray_clearance_mm=ray_clearance_mm,
            max_branch_angle_deg=max_branch_angle_deg,
        )

    load_path_bad_count = support_tree_has_valid_load_paths(
        supports,
        max_branch_angle_deg=max_branch_angle_deg,
    )
    supports = prune_colliding_supports_v11_5(
        supports,
        mesh=mesh,
        ray_clearance_mm=ray_clearance_mm,
        support_radius=support_radius,
        fast_mode=fast_mode,
        strict_collision=strict_collision,
    )
    supports["collision_count"] = int(supports.get("collision_count", 0)) + int(load_path_bad_count)
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
    max_branch_angle_deg: float,
    min_cluster_points: int,
    auto_density: bool,
    density_grid_size: float,
    density_min_points_per_cell: int,
    max_density_supports: int,
    density_target_coverage: float,
    support_strategy: str,
    pro_grid_size: float,
    pro_min_points_per_cell: int,
    pro_max_supports: int,
    pro_interface: bool,
    external_tree: bool,
    external_margin: float,
    fast_mode: bool,
    strict_collision: bool,
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
            max_branch_angle_deg,
            min_cluster_points,
            auto_density,
            density_grid_size,
            density_min_points_per_cell,
            max_density_supports,
            density_target_coverage,
            support_strategy,
            pro_grid_size,
            pro_min_points_per_cell,
            pro_max_supports,
            pro_interface,
            external_tree,
            external_margin,
            fast_mode,
            strict_collision,
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

        structure = feasibility.get("support_structure", {})
        coverage_info = feasibility.get("coverage", {})

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
            support_count=int(structure.get("total_count", 0)),
            collision_count=int(structure.get("collision_count", 0)),
            disconnected_count=int(structure.get("disconnected_count", 0)),
            pruned_branches=int(structure.get("pruned_branches", 0)),
            pruned_trunks=int(structure.get("pruned_trunks", 0)),
            coverage_ok=bool(coverage_info.get("ok", False)),
            reason=infeasibility_reason_from_feasibility(feasibility),
        )

    except Exception as e:
        logger.exception(f"Error evaluating rho={rho_deg}, theta={theta_deg}: {e}")
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
            collision_count=0,
            disconnected_count=0,
            pruned_branches=0,
            pruned_trunks=0,
            coverage_ok=False,
            reason="exception",
            error=str(e),
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
    Print-logic orientation prior.

    The optimizer should not rotate a flat-base model into a strange angle just
    because coverage looks higher. This bonus rewards:
    - large bed contact area
    - low model height
    - staying close to the original orientation
    - especially rho=0, theta=0 when the model has a decent flat base
    """
    contact_area = estimate_bed_contact_area(mesh, bed_z)
    height = float(mesh.bounds[1][2] - mesh.bounds[0][2])

    contact_bonus = contact_area * 0.08
    height_penalty = height * 0.45

    rotation_penalty = 1.8 * abs(float(theta_deg)) + 0.55 * min(abs(float(rho_deg)), abs(180.0 - float(rho_deg)))

    zero_angle_bonus = 0.0
    if abs(rho_deg) < 1e-6 and abs(theta_deg) < 1e-6:
        zero_angle_bonus = 450.0

    near_zero_bonus = max(0.0, 120.0 - 2.0 * abs(float(theta_deg)) - 0.6 * abs(float(rho_deg)))

    return float(contact_bonus - height_penalty - rotation_penalty + zero_angle_bonus + near_zero_bonus)

def choose_best_result(results: List[AngleScanResult]) -> AngleScanResult:
    """
    Prefer feasible solutions. Among infeasible ones, prefer fewer hard violations
    before raw score, so a high-coverage solution with many collisions does not
    look better than a cleaner support path.
    """
    feasible = [r for r in results if r.feasible]
    if feasible:
        return max(feasible, key=lambda r: r.score)

    return max(
        results,
        key=lambda r: (
            -int(getattr(r, "collision_count", 0)),
            -int(getattr(r, "disconnected_count", 0)),
            bool(getattr(r, "coverage_ok", False)),
            float(getattr(r, "score", -1e9)),
        ),
    )

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
    max_branch_angle_deg: float,
    min_cluster_points: int,
    auto_density: bool,
    density_grid_size: float,
    density_min_points_per_cell: int,
    max_density_supports: int,
    density_target_coverage: float,
    support_strategy: str,
    pro_grid_size: float,
    pro_min_points_per_cell: int,
    pro_max_supports: int,
    pro_interface: bool,
    external_tree: bool,
    external_margin: float,
    fast_mode: bool,
    strict_collision: bool,
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
            max_branch_angle_deg=max_branch_angle_deg,
            min_cluster_points=min_cluster_points,
            auto_density=auto_density,
            density_grid_size=density_grid_size,
            density_min_points_per_cell=density_min_points_per_cell,
            max_density_supports=max_density_supports,
            density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
            support_strategy=support_strategy,
            pro_grid_size=pro_grid_size,
            pro_min_points_per_cell=pro_min_points_per_cell,
            pro_max_supports=pro_max_supports,
            pro_interface=pro_interface,
            external_tree=external_tree,
            external_margin=external_margin,
            fast_mode=fast_mode,
            strict_collision=strict_collision,
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
            # Prefer clean / low-collision candidates before pure coverage.
            anchors = sorted(
                current,
                key=lambda r: (
                    -int(getattr(r, "collision_count", 0)),
                    -int(getattr(r, "disconnected_count", 0)),
                    bool(getattr(r, "coverage_ok", False)),
                    float(getattr(r, "coverage_percent", 0.0)),
                    float(getattr(r, "score", -1e9)),
                ),
                reverse=True,
            )[:max(1, top_k)]

        # Always keep original flat orientation in the refinement set.
        zero_candidate = evaluated.get((0.0, 0.0))
        if zero_candidate is not None and all(abs(a.rho) > 1e-6 or abs(a.theta) > 1e-6 for a in anchors):
            anchors.append(zero_candidate)

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
# BETA V9.3 / V10 SUPPORT TOPOLOGY HELPERS
# =========================================================

def branch_vertical_angle_deg(p0, p1) -> float:
    """
    Angle between branch direction and vertical axis.
    0° = vertical, 90° = horizontal.
    """
    p0 = np.array(p0, dtype=float)
    p1 = np.array(p1, dtype=float)
    v = p1 - p0
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return 90.0
    v = v / n
    vertical = np.array([0.0, 0.0, 1.0])
    return float(np.degrees(np.arccos(np.clip(abs(np.dot(v, vertical)), -1.0, 1.0))))


def prune_unused_trunks(tree: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove trunks with no branches. This fixes disconnected lonely pillars.
    """
    trunks = tree.get("trunks", [])
    branches = tree.get("branches", [])

    used_ids = set(int(b.get("trunk_id", -999)) for b in branches)
    kept_trunks = [t for t in trunks if int(t.get("id", -999)) in used_ids]

    old_to_new = {}
    for new_id, t in enumerate(kept_trunks):
        old_id = int(t.get("id", new_id))
        old_to_new[old_id] = new_id
        t["id"] = int(new_id)

    kept_branches = []
    for b in branches:
        old_id = int(b.get("trunk_id", -999))
        if old_id in old_to_new:
            b["trunk_id"] = int(old_to_new[old_id])
            kept_branches.append(b)

    tree["trunks"] = kept_trunks
    tree["branches"] = kept_branches
    return tree


def classic_supports_collision_safe(
    mesh: trimesh.Trimesh,
    points: np.ndarray,
    radius: float,
    bed_z: float,
    mesh_mass_g: float,
    min_height: float = 1.0,
    tip_gap_mm: float = 0.25,
    ray_clearance_mm: float = 0.8,
    downray_filter: bool = True,
) -> List[Dict[str, float]]:
    """
    Classic support with forbidden-zone logic:
    - optionally downray filters points
    - skips pillars whose centerline intersects mesh before the contact point
    """
    if downray_filter:
        points = filter_points_needing_support_by_downray(
            mesh,
            points,
            bed_z=bed_z,
            min_drop_mm=min_height,
            surface_offset_mm=0.35,
        )

    if len(points) == 0:
        return []

    supports = []
    point_load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, len(points))

    for p in points:
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        z_top = z - tip_gap_mm
        height = z_top - bed_z
        if height <= min_height:
            continue

        # Check centerline collision. Clearance approximates tube radius.
        p0 = [x, y, bed_z + 0.35]
        p1 = [x, y, z_top - 0.35]
        if ray_hits_mesh_between(
            mesh,
            p0,
            p1,
            clearance_mm=ray_clearance_mm + radius,
        ):
            continue

        supports.append({
            "x": x,
            "y": y,
            "z_bottom": float(bed_z),
            "z_top": float(z_top),
            "radius": float(radius),
            "height": float(height),
            "length": float(height),
            "force_n": float(point_load),
        })

    return supports


def support_tree_has_valid_load_paths(tree: Dict[str, Any], max_branch_angle_deg: float = 70.0) -> int:
    """
    Count branches that are too horizontal to be physically reasonable.
    """
    bad = 0
    for b in tree.get("branches", []):
        a1 = branch_vertical_angle_deg([b["x1"], b["y1"], b["z1"]], [b["xm"], b["ym"], b["zm"]])
        a2 = branch_vertical_angle_deg([b["xm"], b["ym"], b["zm"]], [b["x2"], b["y2"], b["z2"]])
        if max(a1, a2) > max_branch_angle_deg:
            bad += 1
    return int(bad)



# =========================================================
# BETA V9.4 ADAPTIVE SUPPORT DENSITY HELPERS
# =========================================================

def grid_density_points(
    points: np.ndarray,
    grid_size: float = 6.0,
    min_points_per_cell: int = 2,
    max_cells: int = 240,
) -> np.ndarray:
    """
    Partition overhang points into XY grid cells.
    Each dense cell becomes one representative support contact point.
    This solves the old problem: 5000 samples but only 5 trunks.
    """
    if len(points) == 0:
        return np.empty((0, 3))

    grid = {}
    for p in points:
        key = (int(np.floor(p[0] / grid_size)), int(np.floor(p[1] / grid_size)))
        grid.setdefault(key, []).append(p)

    reps = []
    for _, pts in grid.items():
        if len(pts) < min_points_per_cell:
            continue
        arr = np.array(pts)
        # use highest point in that cell as contact target
        idx = int(np.argmax(arr[:, 2]))
        reps.append(arr[idx])

    if len(reps) == 0:
        return np.empty((0, 3))

    reps = np.array(reps)

    # Prioritize higher unsupported points if too many cells.
    if len(reps) > max_cells:
        order = np.argsort(reps[:, 2])[-max_cells:]
        reps = reps[order]

    return reps


def calculate_coverage_for_contact_points(points: np.ndarray, contact_points: np.ndarray, max_xy_distance: float = 8.0) -> float:
    if len(points) == 0:
        return 1.0
    if len(contact_points) == 0:
        return 0.0

    supported = 0
    for p in points:
        dxy = np.sqrt((contact_points[:, 0] - p[0]) ** 2 + (contact_points[:, 1] - p[1]) ** 2)
        z_ok = contact_points[:, 2] <= p[2] + 3.0
        if np.any((dxy <= max_xy_distance) & z_ok):
            supported += 1

    return float(supported / len(points))


def extra_points_for_uncovered_regions(
    all_points: np.ndarray,
    current_contacts: np.ndarray,
    grid_size: float = 6.0,
    max_xy_distance: float = 8.0,
    min_points_per_cell: int = 2,
    max_extra_points: int = 160,
) -> np.ndarray:
    """
    Select representative points from regions not covered by existing contacts.
    """
    if len(all_points) == 0:
        return np.empty((0, 3))

    uncovered = []
    if len(current_contacts) == 0:
        uncovered = all_points
    else:
        for p in all_points:
            dxy = np.sqrt((current_contacts[:, 0] - p[0]) ** 2 + (current_contacts[:, 1] - p[1]) ** 2)
            z_ok = current_contacts[:, 2] <= p[2] + 3.0
            if not np.any((dxy <= max_xy_distance) & z_ok):
                uncovered.append(p)
        uncovered = np.array(uncovered) if len(uncovered) else np.empty((0, 3))

    if len(uncovered) == 0:
        return np.empty((0, 3))

    return grid_density_points(
        uncovered,
        grid_size=grid_size,
        min_points_per_cell=min_points_per_cell,
        max_cells=max_extra_points,
    )


def generate_density_tree_additions(
    mesh: trimesh.Trimesh,
    all_points: np.ndarray,
    existing_tree: Dict[str, Any],
    bed_z: float,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    center_x: float,
    center_y: float,
    target_coverage: float,
    max_xy_distance: float,
    density_grid_size: float,
    density_min_points_per_cell: int,
    max_density_supports: int,
    collision_margin: float,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    max_branch_angle_deg: float,
) -> Dict[str, Any]:
    """
    If generated tree coverage is too low, add direct safe mini tree supports
    at uncovered grid cells until density is sufficient.
    """
    contacts = np.array(PhysicsMotor.calculate_contact_points(existing_tree))
    current_coverage = calculate_coverage_for_contact_points(all_points, contacts, max_xy_distance)

    if current_coverage >= target_coverage:
        return existing_tree

    extra_points = extra_points_for_uncovered_regions(
        all_points,
        contacts,
        grid_size=density_grid_size,
        max_xy_distance=max_xy_distance,
        min_points_per_cell=density_min_points_per_cell,
        max_extra_points=max_density_supports,
    )

    if len(extra_points) == 0:
        return existing_tree

    point_load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, max(1, len(all_points)))
    next_id = len(existing_tree.get("trunks", []))

    for p in extra_points:
        px, py, pz = float(p[0]), float(p[1]), float(p[2])
        contact_x, contact_y, contact_z = contact_point_with_normal_offset((px, py, pz), center_x, center_y, tip_gap_mm)

        # Create an almost vertical trunk slightly outward from the contact.
        ux, uy = outward_unit_from_center(contact_x, contact_y, center_x, center_y)
        trunk_x = contact_x + ux * max(1.0, support_radius * 1.2)
        trunk_y = contact_y + uy * max(1.0, support_radius * 1.2)
        trunk_x, trunk_y = push_point_outside_xy_bbox(
            trunk_x, trunk_y, center_x, center_y, mesh.bounds, margin=collision_margin * 0.5
        )

        trunk_top_z = max(bed_z + 2.0, contact_z - max(2.5, support_radius * 2.0))
        trunk_height = trunk_top_z - bed_z
        if trunk_height <= 1.0:
            continue

        trunk_force = point_load
        trunk_radius = StructuralAnalyzer.required_radius_for_force(
            trunk_force, trunk_height, material, nozzle_mm, safety_factor, visual_min_radius=support_radius * 1.4
        )

        # Control point: mostly vertical, slight outward curve.
        mid_x = (trunk_x + contact_x) / 2.0 + ux * max(1.0, support_radius * 1.5)
        mid_y = (trunk_y + contact_y) / 2.0 + uy * max(1.0, support_radius * 1.5)
        mid_z = (trunk_top_z + contact_z) / 2.0 + max(1.0, support_radius)

        # Do not add physically too-horizontal branches.
        if max(
            branch_vertical_angle_deg([trunk_x, trunk_y, trunk_top_z], [mid_x, mid_y, mid_z]),
            branch_vertical_angle_deg([mid_x, mid_y, mid_z], [contact_x, contact_y, contact_z])
        ) > max_branch_angle_deg:
            continue

        # Hard collision check.
        if curve_hits_mesh(
            mesh,
            [trunk_x, trunk_y, trunk_top_z],
            [mid_x, mid_y, mid_z],
            [contact_x, contact_y, contact_z],
            clearance_mm=ray_clearance_mm + support_radius * 0.45,
            samples=8,
        ):
            continue

        branch_len = approximate_bezier_length(
            [trunk_x, trunk_y, trunk_top_z],
            [mid_x, mid_y, mid_z],
            [contact_x, contact_y, contact_z],
            samples=8,
        )
        branch_radius = StructuralAnalyzer.required_radius_for_force(
            point_load, branch_len, material, nozzle_mm, safety_factor, visual_min_radius=support_radius * 0.45
        )

        existing_tree.setdefault("trunks", []).append({
            "id": int(next_id),
            "tree_id": int(next_id),
            "x": float(trunk_x),
            "y": float(trunk_y),
            "z_bottom": float(bed_z),
            "z_top": float(trunk_top_z),
            "radius": float(trunk_radius),
            "height": float(trunk_height),
            "length": float(trunk_height),
            "force_n": float(point_load),
            "point_count": 1,
            "ok": True,
        })

        existing_tree.setdefault("branches", []).append({
            "tree_id": int(next_id),
            "trunk_id": int(next_id),
            "x1": float(trunk_x),
            "y1": float(trunk_y),
            "z1": float(trunk_top_z),
            "xm": float(mid_x),
            "ym": float(mid_y),
            "zm": float(mid_z),
            "x2": float(contact_x),
            "y2": float(contact_y),
            "z2": float(contact_z),
            "radius": float(branch_radius),
            "height": float(contact_z - trunk_top_z),
            "length": float(branch_len),
            "force_n": float(point_load),
            "ok": True,
        })
        next_id += 1

        contacts = np.array(PhysicsMotor.calculate_contact_points(existing_tree))
        current_coverage = calculate_coverage_for_contact_points(all_points, contacts, max_xy_distance)
        if current_coverage >= target_coverage:
            break

    return prune_unused_trunks(existing_tree)



# =========================================================
# BETA V10 PRO SUPPORT GENERATOR
# Cura / Prusa inspired grid + interface supports
# =========================================================

def pro_grid_support_points(
    points: np.ndarray,
    grid_size: float = 5.0,
    min_points_per_cell: int = 1,
    max_points: int = 500,
) -> np.ndarray:
    """
    Cura/Prusa-like support density:
    partition overhang area into XY cells and use one representative support per cell.
    """
    if len(points) == 0:
        return np.empty((0, 3))

    cells = {}
    for p in points:
        key = (int(np.floor(p[0] / grid_size)), int(np.floor(p[1] / grid_size)))
        cells.setdefault(key, []).append(p)

    reps = []
    for _, pts in cells.items():
        if len(pts) < min_points_per_cell:
            continue
        arr = np.asarray(pts)
        # highest target in cell is safer for support interface
        reps.append(arr[int(np.argmax(arr[:, 2]))])

    if len(reps) == 0:
        return np.empty((0, 3))

    reps = np.asarray(reps)

    if len(reps) > max_points:
        # prioritize lower z overhangs slightly, then high density is capped
        order = np.argsort(reps[:, 2])[-max_points:]
        reps = reps[order]

    return reps


def pro_classic_grid_supports(
    mesh: trimesh.Trimesh,
    overhang_points: np.ndarray,
    radius: float,
    bed_z: float,
    mesh_mass_g: float,
    grid_size: float,
    min_points_per_cell: int,
    max_supports: int,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    downray_filter: bool,
) -> List[Dict[str, float]]:
    """
    Realistic classic support:
    - grid/density based, not every sample point
    - vertical columns from bed to just below overhang
    - collision filtered
    """
    pts = overhang_points
    if downray_filter:
        pts = filter_points_needing_support_by_downray(
            mesh, pts, bed_z=bed_z, min_drop_mm=1.0, surface_offset_mm=0.35
        )

    reps = pro_grid_support_points(
        pts,
        grid_size=grid_size,
        min_points_per_cell=min_points_per_cell,
        max_points=max_supports,
    )

    if len(reps) == 0:
        return []

    point_load = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, len(reps))
    supports = []

    for p in reps:
        x, y, z = float(p[0]), float(p[1]), float(p[2])
        z_top = z - tip_gap_mm
        height = z_top - bed_z
        if height <= 1.0:
            continue

        # Centerline collision check; allow final contact near top.
        if ray_hits_mesh_between(
            mesh,
            [x, y, bed_z + 0.5],
            [x, y, z_top - 0.5],
            clearance_mm=ray_clearance_mm + radius,
        ):
            continue

        supports.append({
            "x": x,
            "y": y,
            "z_bottom": float(bed_z),
            "z_top": float(z_top),
            "radius": float(radius),
            "height": float(height),
            "length": float(height),
            "force_n": float(point_load),
        })

    return supports


def pro_tree_grid_supports(
    mesh: trimesh.Trimesh,
    overhang_points: np.ndarray,
    bed_z: float,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    grid_size: float,
    min_points_per_cell: int,
    max_supports: int,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    downray_filter: bool,
    max_branch_angle_deg: float,
    add_interface: bool = True,
    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> Dict[str, Any]:
    """
    Beta V10.1 grouped tree engine.

    Difference from V10.0:
    - V10.0: every grid cell became its own vertical pillar.
    - V10.1: grid cells are grouped into trunk regions.
      Several contact points share one trunk, producing real tree-like branches.

    This is not a full Cura implementation, but it is much closer:
    contact cells -> grouped trunk centers -> short branch fan -> grounded trunk.
    """
    pts = overhang_points
    if downray_filter:
        pts = filter_points_needing_support_by_downray(
            mesh, pts, bed_z=bed_z, min_drop_mm=1.0, surface_offset_mm=0.35
        )

    reps = pro_grid_support_points(
        pts,
        grid_size=grid_size,
        min_points_per_cell=min_points_per_cell,
        max_points=max_supports,
    )

    trunks: List[Dict[str, Any]] = []
    branches: List[Dict[str, Any]] = []
    interfaces: List[Dict[str, Any]] = []

    if len(reps) == 0:
        return {
            "trunks": trunks,
            "branches": branches,
            "interfaces": interfaces,
            "physics": {
                "all_ok": False,
                "reason": "No grid support points",
                "trunk_count": 0,
                "branch_count": 0,
            },
        }

    # This is the critical slicer-like grouping control.
    # Larger grid_size -> fewer contact reps.
    # trunk_group_eps controls how many reps share one trunk.
    trunk_group_eps = max(grid_size * 1.8, support_radius * 4.0)

    labels = DBSCAN(eps=trunk_group_eps, min_samples=1).fit(reps[:, :2]).labels_
    unique_labels = sorted(set(labels))

    point_load_each = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, max(1, len(reps)))

    for label in unique_labels:
        group = reps[labels == label]
        if len(group) == 0:
            continue

        # Limit branches per trunk to avoid a dense forest.
        # Pick higher points first because they are usually more critical.
        max_branches_for_trunk = max(10, min(24, int(max_supports / max(1, len(unique_labels))) + 4))
        if len(group) > max_branches_for_trunk:
            order = np.argsort(group[:, 2])[-max_branches_for_trunk:]
            group = group[order]

        # Trunk is placed below the group center, not below every single contact.
        gx = float(np.mean(group[:, 0]))
        gy = float(np.mean(group[:, 1]))
        gz_min = float(np.min(group[:, 2]))
        gz_mean = float(np.mean(group[:, 2]))

        # Put trunk top below the lowest contact point in the group.
        trunk_top_z = gz_min - max(4.0, support_radius * 2.2)
        trunk_height = trunk_top_z - bed_z
        if trunk_height <= 1.0:
            continue

        # Try a few nearby XY candidates so trunk does not pass through model.
        candidate_offsets = [(0.0, 0.0)]
        for ang in np.linspace(0, 2 * np.pi, 8, endpoint=False):
            candidate_offsets.append((
                np.cos(ang) * max(1.5, grid_size * 0.35),
                np.sin(ang) * max(1.5, grid_size * 0.35),
            ))

        chosen_xy = None
        for ox, oy in candidate_offsets:
            tx = gx + ox
            ty = gy + oy

            if not ray_hits_mesh_between(
                mesh,
                [tx, ty, bed_z + 0.5],
                [tx, ty, trunk_top_z - 0.5],
                clearance_mm=ray_clearance_mm + support_radius * 0.9,
            ):
                chosen_xy = (float(tx), float(ty))
                break

        if chosen_xy is None:
            # If no collision-free trunk exists, skip this group.
            continue

        trunk_x, trunk_y = chosen_xy
        group_force = point_load_each * max(1, len(group))

        trunk_radius = StructuralAnalyzer.required_radius_for_force(
            group_force,
            trunk_height,
            material,
            nozzle_mm,
            safety_factor,
            visual_min_radius=support_radius * 1.45,
        )

        trunk_id = len(trunks)
        trunk = {
            "id": int(trunk_id),
            "tree_id": int(trunk_id),
            "x": float(trunk_x),
            "y": float(trunk_y),
            "z_bottom": float(bed_z),
            "z_top": float(trunk_top_z),
            "radius": float(trunk_radius),
            "height": float(trunk_height),
            "length": float(trunk_height),
            "force_n": float(group_force),
            "point_count": int(len(group)),
            "ok": True,
        }

        local_branches = []
        local_interfaces = []

        for p in group:
            px, py, pz = float(p[0]), float(p[1]), float(p[2])

            # Contact point is slightly outside the model surface.
            contact_x, contact_y, contact_z = contact_point_with_normal_offset(
                (px, py, pz), gx, gy, tip_gap_mm
            )

            # Branch control point: halfway between trunk and contact with slight lift.
            mid_x = (trunk_x + contact_x) / 2.0
            mid_y = (trunk_y + contact_y) / 2.0
            mid_z = (trunk_top_z + contact_z) / 2.0 + max(0.8, support_radius * 0.65)

            a1 = branch_vertical_angle_deg(
                [trunk_x, trunk_y, trunk_top_z],
                [mid_x, mid_y, mid_z],
            )
            a2 = branch_vertical_angle_deg(
                [mid_x, mid_y, mid_z],
                [contact_x, contact_y, contact_z],
            )

            # Tree branches cannot be extremely horizontal.
            if max(a1, a2) > max_branch_angle_deg:
                continue

            if curve_hits_mesh(
                mesh,
                [trunk_x, trunk_y, trunk_top_z],
                [mid_x, mid_y, mid_z],
                [contact_x, contact_y, contact_z],
                clearance_mm=ray_clearance_mm + support_radius * 0.55,
                samples=7,
            ):
                continue

            branch_len = approximate_bezier_length(
                [trunk_x, trunk_y, trunk_top_z],
                [mid_x, mid_y, mid_z],
                [contact_x, contact_y, contact_z],
                samples=7,
            )

            branch_radius = StructuralAnalyzer.required_radius_for_force(
                point_load_each,
                branch_len,
                material,
                nozzle_mm,
                safety_factor,
                visual_min_radius=support_radius * 0.50,
            )

            local_branches.append({
                "tree_id": int(trunk_id),
                "trunk_id": int(trunk_id),
                "x1": float(trunk_x),
                "y1": float(trunk_y),
                "z1": float(trunk_top_z),
                "xm": float(mid_x),
                "ym": float(mid_y),
                "zm": float(mid_z),
                "x2": float(contact_x),
                "y2": float(contact_y),
                "z2": float(contact_z),
                "radius": float(branch_radius),
                "height": float(contact_z - trunk_top_z),
                "length": float(branch_len),
                "force_n": float(point_load_each),
                "ok": True,
            })

            if add_interface:
                local_interfaces.append({
                    "x": float(contact_x),
                    "y": float(contact_y),
                    "z": float(contact_z),
                    "radius": float(max(support_radius * 1.10, grid_size * 0.18)),
                    "height": float(max(0.35, nozzle_mm)),
                })

        # Avoid lonely trunk pillars with no valid branches.
        if len(local_branches) == 0:
            continue

        trunks.append(trunk)
        branches.extend(local_branches)
        interfaces.extend(local_interfaces)

    tree = {
        "trunks": trunks,
        "branches": branches,
        "interfaces": interfaces,
        "physics": {
            "all_ok": True,
            "trunk_count": len(trunks),
            "branch_count": len(branches),
            "interface_count": len(interfaces),
            "engine": "grouped_tree_v10_1",
        },
    }

    tree = prune_unused_trunks(tree)
    tree["collision_count"] = collision_count_for_tree(tree, mesh, clearance_mm=ray_clearance_mm)
    tree["disconnected_count"] = disconnected_count_for_tree(tree)
    return tree


def pro_interface_mesh(interfaces: List[Dict[str, float]]) -> trimesh.Trimesh:
    """
    Thin contact/interface disks like slicer support roof/interface.
    """
    meshes = []
    for it in interfaces:
        cyl = trimesh.creation.cylinder(
            radius=float(it["radius"]),
            height=float(it["height"]),
            sections=24,
        )
        cyl.apply_translation([float(it["x"]), float(it["y"]), float(it["z"]) - float(it["height"]) / 2.0])
        meshes.append(cyl)

    if not meshes:
        return trimesh.Trimesh()

    return trimesh.util.concatenate(meshes)



# =========================================================
# BETA V10.2 COVERAGE FIX HELPERS
# =========================================================

def extract_support_contact_points_v10(tree: Dict[str, Any]) -> np.ndarray:
    """
    Robust contact extraction for both classic and tree supports.

    Old coverage logic missed some grouped-tree contacts. This function reads:
    - tree branch endpoints x2/y2/z2
    - interface disks x/y/z
    - classic vertical support tops x/y/z_top
    """
    pts = []

    for b in tree.get("branches", []):
        if all(k in b for k in ("x2", "y2", "z2")):
            pts.append([float(b["x2"]), float(b["y2"]), float(b["z2"])])
        elif all(k in b for k in ("x", "y", "z_top")):
            pts.append([float(b["x"]), float(b["y"]), float(b["z_top"])])

    for it in tree.get("interfaces", []):
        if all(k in it for k in ("x", "y", "z")):
            pts.append([float(it["x"]), float(it["y"]), float(it["z"])])

    if not pts:
        return np.empty((0, 3), dtype=float)

    return np.asarray(pts, dtype=float)


def coverage_v10(points: np.ndarray, supports: Dict[str, Any], max_xy_distance: float) -> float:
    contacts = extract_support_contact_points_v10(supports)
    return calculate_coverage_for_contact_points(points, contacts, max_xy_distance=max_xy_distance)



# =========================================================
# BETA V11 REAL TREE GROWTH ENGINE
# =========================================================

def v11_select_tip_points(
    points: np.ndarray,
    grid_size: float = 6.0,
    min_points_per_cell: int = 1,
    max_tips: int = 220,
) -> np.ndarray:
    """
    Select support tips from overhang regions.
    One tip per XY grid cell, prioritizing the highest point in the cell.
    """
    return pro_grid_support_points(
        points,
        grid_size=grid_size,
        min_points_per_cell=min_points_per_cell,
        max_points=max_tips,
    )


def v11_try_safe_curve(
    mesh: trimesh.Trimesh,
    p0,
    p2,
    center_xy,
    lift: float,
    clearance: float,
    samples: int = 8,
):
    """
    Try several Bezier control points. Return a collision-free mid point or None.
    """
    p0 = np.asarray(p0, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    cx, cy = center_xy

    ux, uy = outward_unit_from_center(float((p0[0] + p2[0]) / 2.0), float((p0[1] + p2[1]) / 2.0), cx, cy)

    candidates = []
    base_mid = (p0 + p2) / 2.0
    base_mid[2] += lift
    candidates.append(base_mid.copy())

    for scale in [1.0, 1.8, 2.8, 4.0]:
        m = base_mid.copy()
        m[0] += ux * lift * scale
        m[1] += uy * lift * scale
        candidates.append(m)

    for ang in np.linspace(0.0, 2.0 * np.pi, 8, endpoint=False):
        m = base_mid.copy()
        m[0] += np.cos(ang) * lift * 1.5
        m[1] += np.sin(ang) * lift * 1.5
        candidates.append(m)

    for mid in candidates:
        if not curve_hits_mesh(mesh, p0, mid, p2, clearance_mm=clearance, samples=samples):
            return [float(mid[0]), float(mid[1]), float(mid[2])]

    return None


def v11_downward_grow_nodes(
    mesh: trimesh.Trimesh,
    tips: np.ndarray,
    bed_z: float,
    center_x: float,
    center_y: float,
    step_down: float,
    merge_radius: float,
    max_branch_angle_deg: float,
    clearance: float,
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int]]]:
    """
    Grow branches from tips downward.

    nodes:
      {id, x, y, z, is_tip}
    edges:
      child -> parent, where parent is lower in Z.

    Algorithm:
      1) start with tip nodes
      2) repeatedly move each active node downward
      3) nearby nodes at similar/lower z merge
      4) stop when near bed
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Tuple[int, int]] = []

    for p in tips:
        nodes.append({
            "id": len(nodes),
            "x": float(p[0]),
            "y": float(p[1]),
            "z": float(p[2]),
            "is_tip": True,
        })

    active = list(range(len(nodes)))
    max_iters = 80

    for _ in range(max_iters):
        if not active:
            break

        next_active = []
        created_this_round = []

        # higher first, so merge decisions are stable
        active = sorted(active, key=lambda i: nodes[i]["z"], reverse=True)

        for nid in active:
            n = nodes[nid]
            if n["z"] <= bed_z + step_down * 1.5:
                continue

            # Preferred target is downward with slight inward/outward relaxation.
            tx = n["x"]
            ty = n["y"]
            tz = max(bed_z + step_down, n["z"] - step_down)

            # Merge with an existing lower/nearby growth node if possible.
            merge_candidate = None
            best_d = 1e18
            for oid, other in enumerate(nodes):
                if oid == nid:
                    continue
                if other["z"] >= n["z"] - 0.5:
                    continue
                dz = abs(other["z"] - tz)
                if dz > step_down * 1.2:
                    continue
                dxy = float(np.hypot(other["x"] - n["x"], other["y"] - n["y"]))
                if dxy < merge_radius and dxy < best_d:
                    merge_candidate = oid
                    best_d = dxy

            if merge_candidate is not None:
                target_id = merge_candidate
                target = nodes[target_id]
                angle = branch_vertical_angle_deg([n["x"], n["y"], n["z"]], [target["x"], target["y"], target["z"]])
                if angle <= max_branch_angle_deg:
                    mid = v11_try_safe_curve(
                        mesh,
                        [n["x"], n["y"], n["z"]],
                        [target["x"], target["y"], target["z"]],
                        (center_x, center_y),
                        lift=max(0.5, step_down * 0.2),
                        clearance=clearance,
                        samples=6,
                    )
                    if mid is not None:
                        edges.append((nid, target_id))
                        continue

            # Otherwise create a new lower node.
            # Slightly bias toward center as it descends, causing tree convergence.
            blend = 0.10
            tx = (1.0 - blend) * tx + blend * center_x
            ty = (1.0 - blend) * ty + blend * center_y

            angle = branch_vertical_angle_deg([n["x"], n["y"], n["z"]], [tx, ty, tz])
            if angle > max_branch_angle_deg:
                # If too angled, drop mostly vertically.
                tx = n["x"]
                ty = n["y"]

            mid = v11_try_safe_curve(
                mesh,
                [n["x"], n["y"], n["z"]],
                [tx, ty, tz],
                (center_x, center_y),
                lift=max(0.5, step_down * 0.2),
                clearance=clearance,
                samples=6,
            )
            if mid is None:
                # Last fallback: exact vertical if safe.
                if curve_hits_mesh(
                    mesh,
                    [n["x"], n["y"], n["z"]],
                    [n["x"], n["y"], (n["z"] + tz) / 2.0],
                    [n["x"], n["y"], tz],
                    clearance_mm=clearance,
                    samples=6,
                ):
                    continue
                tx, ty = n["x"], n["y"]

            new_id = len(nodes)
            nodes.append({
                "id": new_id,
                "x": float(tx),
                "y": float(ty),
                "z": float(tz),
                "is_tip": False,
            })
            edges.append((nid, new_id))
            next_active.append(new_id)
            created_this_round.append(new_id)

        # Merge newly created nodes horizontally to prevent a forest.
        if created_this_round:
            merged_active = []
            used = set()
            for nid in created_this_round:
                if nid in used:
                    continue
                group = [nid]
                used.add(nid)
                for oid in created_this_round:
                    if oid in used:
                        continue
                    if abs(nodes[oid]["z"] - nodes[nid]["z"]) <= step_down * 0.5:
                        if np.hypot(nodes[oid]["x"] - nodes[nid]["x"], nodes[oid]["y"] - nodes[nid]["y"]) <= merge_radius:
                            group.append(oid)
                            used.add(oid)

                if len(group) == 1:
                    merged_active.append(nid)
                else:
                    mx = float(np.mean([nodes[g]["x"] for g in group]))
                    my = float(np.mean([nodes[g]["y"] for g in group]))
                    mz = float(np.mean([nodes[g]["z"] for g in group]))
                    mid_id = len(nodes)
                    nodes.append({"id": mid_id, "x": mx, "y": my, "z": mz, "is_tip": False})
                    for g in group:
                        edges.append((g, mid_id))
                    merged_active.append(mid_id)

            next_active = merged_active

        active = next_active

    return nodes, edges


def v11_build_tree_from_growth(
    mesh: trimesh.Trimesh,
    overhang_points: np.ndarray,
    bed_z: float,
    material: str,
    nozzle_mm: float,
    support_radius: float,
    mesh_mass_g: float,
    safety_factor: float,
    grid_size: float,
    min_points_per_cell: int,
    max_tips: int,
    tip_gap_mm: float,
    ray_clearance_mm: float,
    downray_filter: bool,
    max_branch_angle_deg: float,
    add_interface: bool = True,
    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> Dict[str, Any]:
    """
    Real growth-based tree support:
    - select tips from overhangs
    - grow downward in steps
    - merge nearby paths
    - create grounded trunks at terminal nodes
    """
    pts = overhang_points
    if downray_filter:
        pts = filter_points_needing_support_by_downray(
            mesh, pts, bed_z=bed_z, min_drop_mm=1.0, surface_offset_mm=0.35
        )

    tips_raw = v11_select_tip_points(
        pts,
        grid_size=grid_size,
        min_points_per_cell=min_points_per_cell,
        max_tips=max_tips,
    )

    trunks: List[Dict[str, Any]] = []
    branches: List[Dict[str, Any]] = []
    interfaces: List[Dict[str, Any]] = []

    if len(tips_raw) == 0:
        return {
            "trunks": trunks,
            "branches": branches,
            "interfaces": interfaces,
            "physics": {"all_ok": False, "reason": "No tips selected"},
        }

    cx = float(np.mean(tips_raw[:, 0]))
    cy = float(np.mean(tips_raw[:, 1]))

    # Offset tips outward from surface.
    tips = []
    for p in tips_raw:
        tips.append(contact_point_with_normal_offset((p[0], p[1], p[2]), cx, cy, tip_gap_mm))
    tips = np.asarray(tips, dtype=float)

    step_down = max(5.0, grid_size * 0.85)
    merge_radius = max(grid_size * 1.5, support_radius * 5.0)
    clearance = ray_clearance_mm + support_radius * 0.55

    nodes, edges = v11_downward_grow_nodes(
        mesh=mesh,
        tips=tips,
        bed_z=bed_z,
        center_x=cx,
        center_y=cy,
        step_down=step_down,
        merge_radius=merge_radius,
        max_branch_angle_deg=max_branch_angle_deg,
        clearance=clearance,
    )

    if not nodes or not edges:
        return {
            "trunks": trunks,
            "branches": branches,
            "interfaces": interfaces,
            "physics": {"all_ok": False, "reason": "Growth produced no graph"},
        }

    # terminal nodes are parents with no outgoing lower edge
    children = set(c for c, p in edges)
    parents = set(p for c, p in edges)
    terminal_ids = sorted(list(parents - children))
    if not terminal_ids:
        terminal_ids = sorted(list(parents))

    # Create one trunk for each terminal node, from bed to terminal node.
    terminal_to_trunk = {}
    point_load_each = StructuralAnalyzer.estimate_point_load_n(mesh_mass_g, max(1, len(tips)))

    for tid in terminal_ids:
        n = nodes[tid]
        trunk_x, trunk_y = float(n["x"]), float(n["y"])
        if external_tree:
            trunk_x, trunk_y = outside_xy_bbox_point(
                trunk_x,
                trunk_y,
                mesh.bounds,
                margin=external_margin,
            )
        trunk_top_z = float(max(n["z"], bed_z + 2.0))
        height = trunk_top_z - bed_z
        if height <= 1.0:
            continue

        # Collision check for vertical trunk.
        if ray_hits_mesh_between(
            mesh,
            [trunk_x, trunk_y, bed_z + 0.5],
            [trunk_x, trunk_y, trunk_top_z - 0.5],
            clearance_mm=ray_clearance_mm + support_radius * 0.85,
        ):
            # Try nudging trunk outward.
            ux, uy = outward_unit_from_center(trunk_x, trunk_y, cx, cy)
            success = False
            for dist in [grid_size * 0.4, grid_size * 0.8, grid_size * 1.2]:
                tx = trunk_x + ux * dist
                ty = trunk_y + uy * dist
                if not ray_hits_mesh_between(
                    mesh,
                    [tx, ty, bed_z + 0.5],
                    [tx, ty, trunk_top_z - 0.5],
                    clearance_mm=ray_clearance_mm + support_radius * 0.85,
                ):
                    trunk_x, trunk_y = float(tx), float(ty)
                    success = True
                    break
            if not success:
                continue

        # Count approximate downstream tips for force.
        force = point_load_each * 4.0
        radius = StructuralAnalyzer.required_radius_for_force(
            force,
            height,
            material,
            nozzle_mm,
            safety_factor,
            visual_min_radius=support_radius * 1.5,
        )

        trunk_id = len(trunks)
        trunks.append({
            "id": int(trunk_id),
            "tree_id": int(trunk_id),
            "x": float(trunk_x),
            "y": float(trunk_y),
            "z_bottom": float(bed_z),
            "z_top": float(trunk_top_z),
            "radius": float(radius),
            "height": float(height),
            "length": float(height),
            "force_n": float(force),
            "point_count": 1,
            "ok": True,
        })
        terminal_to_trunk[tid] = trunk_id

    # helper to find closest trunk/terminal for a node
    def nearest_trunk_for_node(nid):
        if not terminal_to_trunk:
            return None
        n = nodes[nid]
        best_tid = None
        best_d = 1e18
        for term_id, trunk_id in terminal_to_trunk.items():
            t = nodes[term_id]
            d = np.hypot(float(n["x"]) - float(t["x"]), float(n["y"]) - float(t["y"])) + abs(float(n["z"]) - float(t["z"])) * 0.15
            if d < best_d:
                best_d = d
                best_tid = trunk_id
        return best_tid

    # Convert graph edges to branch bezier segments.
    for child, parent in edges:
        c = nodes[child]
        p = nodes[parent]
        trunk_id = terminal_to_trunk.get(parent, nearest_trunk_for_node(parent))
        if trunk_id is None:
            continue

        p0 = [float(c["x"]), float(c["y"]), float(c["z"])]
        p2 = [float(p["x"]), float(p["y"]), float(p["z"])]
        lift = max(0.5, step_down * 0.25)
        if external_tree:
            mid = external_branch_midpoint(p0, p2, mesh, lift=max(lift, external_margin * 0.45))
            if fast_bezier_hits_mesh(
                mesh,
                p0,
                mid,
                p2,
                clearance_mm=clearance + support_radius * 0.45,
                samples=adaptive_collision_samples(14, fast_mode),
                strict=strict_collision,
            ):
                mid = None
        else:
            mid = v11_try_safe_curve(mesh, p0, p2, (cx, cy), lift=lift, clearance=clearance, samples=6)
        if mid is None:
            continue

        if branch_vertical_angle_deg(p0, p2) > max_branch_angle_deg:
            continue

        if fast_bezier_hits_mesh(
            mesh,
            p0,
            mid,
            p2,
            clearance_mm=clearance + support_radius * 0.35,
            samples=adaptive_collision_samples(14, fast_mode),
            strict=strict_collision,
        ):
            continue

        length = approximate_bezier_length(p0, mid, p2, samples=6)
        physics_radius = StructuralAnalyzer.required_radius_for_force(
            point_load_each,
            length,
            material,
            nozzle_mm,
            safety_factor,
            visual_min_radius=support_radius * 0.48,
        )
        z_mid = (float(p0[2]) + float(p2[2])) / 2.0
        taper_radius = taper_radius_for_edge(
            z_mid=z_mid,
            bed_z=bed_z,
            top_z=max(float(np.max(tips[:, 2])), bed_z + 1.0),
            root_radius=max(support_radius * 1.8, nozzle_mm * 4.0),
            tip_radius=max(support_radius * 0.42, nozzle_mm * 1.35),
        )
        radius = max(float(physics_radius), float(taper_radius))

        branches.append({
            "tree_id": int(trunk_id),
            "trunk_id": int(trunk_id),
            "x1": float(p0[0]),
            "y1": float(p0[1]),
            "z1": float(p0[2]),
            "xm": float(mid[0]),
            "ym": float(mid[1]),
            "zm": float(mid[2]),
            "x2": float(p2[0]),
            "y2": float(p2[1]),
            "z2": float(p2[2]),
            "radius": float(radius),
            "height": float(abs(p0[2] - p2[2])),
            "length": float(length),
            "force_n": float(point_load_each),
            "ok": True,
        })

    if add_interface:
        for p in tips:
            interfaces.append({
                "x": float(p[0]),
                "y": float(p[1]),
                "z": float(p[2]),
                "radius": float(max(support_radius * 1.05, grid_size * 0.16)),
                "height": float(max(0.35, nozzle_mm)),
            })

    tree = {
        "trunks": trunks,
        "branches": branches,
        "interfaces": interfaces,
        "physics": {
            "all_ok": True,
            "engine": "v11_growth_tree",
            "tip_count": int(len(tips)),
            "node_count": int(len(nodes)),
            "edge_count": int(len(edges)),
            "trunk_count": len(trunks),
            "branch_count": len(branches),
        },
    }

    tree = prune_unused_trunks(tree)
    tree["collision_count"] = collision_count_for_tree(tree, mesh, clearance_mm=ray_clearance_mm)
    tree["disconnected_count"] = disconnected_count_for_tree(tree)
    return tree



# =========================================================
# BETA V11.1 LOAD TAPER + HARD COLLISION HELPERS
# =========================================================

def tapered_radius_by_height(
    z: float,
    bed_z: float,
    max_z: float,
    base_radius: float,
    tip_radius: float,
    power: float = 1.35,
) -> float:
    """
    Height-dependent radius function.

    z = bed_z -> base_radius
    z = max_z -> tip_radius
    """
    denom = max(1e-6, float(max_z) - float(bed_z))
    h = np.clip((float(z) - float(bed_z)) / denom, 0.0, 1.0)
    r = float(tip_radius) + (float(base_radius) - float(tip_radius)) * ((1.0 - h) ** float(power))
    return float(max(tip_radius, r))


def apply_taper_to_tree(
    tree: Dict[str, Any],
    bed_z: float,
    support_radius: float,
    nozzle_mm: float,
) -> Dict[str, Any]:
    """
    Make bottom thicker and top thinner.
    """
    z_values = []
    for t in tree.get("trunks", []):
        z_values.extend([float(t.get("z_bottom", bed_z)), float(t.get("z_top", bed_z))])
    for b in tree.get("branches", []):
        z_values.extend([float(b.get("z1", bed_z)), float(b.get("z2", bed_z))])

    max_z = max(z_values) if z_values else bed_z + 1.0

    base_radius = max(float(support_radius) * 1.75, float(nozzle_mm) * 4.0)
    tip_radius = max(float(support_radius) * 0.42, float(nozzle_mm) * 1.35)

    for t in tree.get("trunks", []):
        z_mid = (float(t.get("z_bottom", bed_z)) + float(t.get("z_top", bed_z))) / 2.0
        r = tapered_radius_by_height(
            z_mid,
            bed_z,
            max_z,
            base_radius,
            max(tip_radius, support_radius * 0.9),
        )
        t["radius"] = float(max(float(t.get("radius", 0.0)), r))

    for b in tree.get("branches", []):
        z_mid = (float(b.get("z1", bed_z)) + float(b.get("z2", bed_z))) / 2.0
        r = tapered_radius_by_height(
            z_mid,
            bed_z,
            max_z,
            base_radius * 0.75,
            tip_radius,
        )
        b["radius"] = float(max(tip_radius, r))

    return tree


def hard_collision_count_v11(
    tree: Dict[str, Any],
    mesh: trimesh.Trimesh,
    ray_clearance_mm: float,
    support_radius: float,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> int:
    """
    If any generated support crosses original mesh, count it as hard collision.
    Feasibility already requires collision_count == 0.
    """
    collisions = 0

    for b in tree.get("branches", []):
        if all(k in b for k in ("x1", "y1", "z1", "xm", "ym", "zm", "x2", "y2", "z2")):
            radius = float(b.get("radius", support_radius))
            if fast_bezier_hits_mesh(
                mesh,
                [b["x1"], b["y1"], b["z1"]],
                [b["xm"], b["ym"], b["zm"]],
                [b["x2"], b["y2"], b["z2"]],
                clearance_mm=float(ray_clearance_mm) + radius * 0.65,
                samples=adaptive_collision_samples(10, fast_mode),
                strict=strict_collision,
            ):
                collisions += 1

    for t in tree.get("trunks", []):
        radius = float(t.get("radius", support_radius))
        x = float(t.get("x", 0.0))
        y = float(t.get("y", 0.0))
        z0 = float(t.get("z_bottom", 0.0))
        z1 = float(t.get("z_top", 0.0))
        if z1 - z0 <= 1.0:
            continue
        if fast_segment_hits_mesh_cached(
            mesh,
            [x, y, z0 + 0.5],
            [x, y, z1 - 0.5],
            clearance_mm=float(ray_clearance_mm) + radius * 0.55,
        ):
            collisions += 1

    return int(collisions)




def bezier_points(p0, p1, p2, samples: int = 16) -> np.ndarray:
    """
    Return sampled points on a quadratic Bezier curve.

    B(t) = (1-t)^2 p0 + 2(1-t)t p1 + t^2 p2
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    p2 = np.asarray(p2, dtype=float)

    ts = np.linspace(0.0, 1.0, int(max(2, samples)))
    pts = []
    for t in ts:
        pts.append(((1.0 - t) ** 2) * p0 + 2.0 * (1.0 - t) * t * p1 + (t ** 2) * p2)

    return np.asarray(pts, dtype=float)


# =========================================================
# BETA V11.2 EXTERNAL TREE / FORBIDDEN ZONE HELPERS
# =========================================================

def outside_xy_bbox_point(
    x: float,
    y: float,
    bounds: np.ndarray,
    margin: float = 8.0,
) -> Tuple[float, float]:
    """
    Push an XY point outside model XY bounding box.
    This prevents tree supports from growing through hollow model interiors.
    """
    xmin, ymin = float(bounds[0][0]), float(bounds[0][1])
    xmax, ymax = float(bounds[1][0]), float(bounds[1][1])
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0

    dx = float(x) - cx
    dy = float(y) - cy

    # Choose dominant outward side.
    if abs(dx) >= abs(dy):
        ox = xmax + margin if dx >= 0 else xmin - margin
        oy = float(np.clip(y, ymin - margin, ymax + margin))
    else:
        oy = ymax + margin if dy >= 0 else ymin - margin
        ox = float(np.clip(x, xmin - margin, xmax + margin))

    return float(ox), float(oy)


def segment_samples_hit_mesh(
    mesh: trimesh.Trimesh,
    p0,
    p1,
    clearance_mm: float,
    samples: int = 12,
) -> bool:
    """
    Conservative forbidden-zone check:
    sample points along a support segment and check if they are inside or very near mesh.
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)

    for t in np.linspace(0.08, 0.92, samples):
        p = p0 * (1.0 - t) + p1 * t

        # Signed distance is expensive but available in trimesh.
        try:
            sd = trimesh.proximity.signed_distance(mesh, [p])[0]
            # Positive usually means inside for watertight meshes.
            if sd > -float(clearance_mm):
                return True
        except Exception:
            # Fallback: nearest surface distance.
            try:
                closest, dist, _ = trimesh.proximity.closest_point(mesh, [p])
                if float(dist[0]) < float(clearance_mm):
                    return True
            except Exception:
                pass

    return False


def bezier_forbidden_zone_hit(
    mesh: trimesh.Trimesh,
    p0,
    p1,
    p2,
    clearance_mm: float,
    samples: int = 18,
) -> bool:
    """
    Stricter check than centerline ray:
    both curve intersection and sampled forbidden-zone proximity.
    """
    if curve_hits_mesh(mesh, p0, p1, p2, clearance_mm=clearance_mm, samples=samples):
        return True

    curve = bezier_points(p0, p1, p2, samples=samples)
    for i in range(len(curve) - 1):
        if segment_samples_hit_mesh(mesh, curve[i], curve[i + 1], clearance_mm=clearance_mm, samples=3):
            return True

    return False


def external_branch_midpoint(p0, p2, mesh: trimesh.Trimesh, lift: float = 4.0) -> List[float]:
    """
    Pull midpoint outward from model center so branches do not dive into hollow interiors.
    """
    p0 = np.asarray(p0, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    bounds = mesh.bounds
    cx = float((bounds[0][0] + bounds[1][0]) / 2.0)
    cy = float((bounds[0][1] + bounds[1][1]) / 2.0)

    mid = (p0 + p2) / 2.0
    ux, uy = outward_unit_from_center(float(mid[0]), float(mid[1]), cx, cy)
    mid[0] += ux * max(lift, 2.0)
    mid[1] += uy * max(lift, 2.0)
    mid[2] += max(lift * 0.35, 0.8)
    return [float(mid[0]), float(mid[1]), float(mid[2])]


def taper_radius_for_edge(
    z_mid: float,
    bed_z: float,
    top_z: float,
    root_radius: float,
    tip_radius: float,
) -> float:
    """
    Radius function:
    bottom thick, top thin.
    """
    return tapered_radius_by_height(
        z=z_mid,
        bed_z=bed_z,
        max_z=top_z,
        base_radius=root_radius,
        tip_radius=tip_radius,
        power=1.55,
    )



# =========================================================
# BETA V11.3 OPTIMIZATION HELPERS
# =========================================================

_FAST_MESH_CACHE: Dict[int, Dict[str, Any]] = {}


def get_fast_mesh_cache(mesh: Optional[trimesh.Trimesh]) -> Dict[str, Any]:
    """
    Cache expensive per-mesh data.
    Safe because each rotated mesh object has its own id.
    """
    if mesh is None:
        return {}

    mid = id(mesh)
    if mid not in _FAST_MESH_CACHE:
        bounds = mesh.bounds
        center = np.array([
            (bounds[0][0] + bounds[1][0]) / 2.0,
            (bounds[0][1] + bounds[1][1]) / 2.0,
            (bounds[0][2] + bounds[1][2]) / 2.0,
        ], dtype=float)
        radius = float(np.linalg.norm(bounds[1] - bounds[0]) / 2.0)
        _FAST_MESH_CACHE[mid] = {
            "bounds": bounds,
            "center": center,
            "bbox_radius": radius,
            "ray": mesh.ray,
        }
    return _FAST_MESH_CACHE[mid]


def clear_fast_mesh_cache_if_large(max_items: int = 24) -> None:
    """
    Avoid memory growth during angle scans.
    """
    if len(_FAST_MESH_CACHE) > max_items:
        _FAST_MESH_CACHE.clear()


def bbox_segment_could_hit_mesh(mesh: trimesh.Trimesh, p0, p1, margin: float = 2.0) -> bool:
    """
    Very cheap AABB precheck before expensive ray/proximity tests.
    If segment bounding box does not overlap mesh bounding box, skip collision.
    """
    cache = get_fast_mesh_cache(mesh)
    bounds = cache.get("bounds", mesh.bounds)
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    mn = np.minimum(p0, p1) - margin
    mx = np.maximum(p0, p1) + margin

    if np.any(mx < bounds[0]) or np.any(mn > bounds[1]):
        return False
    return True


def fast_segment_hits_mesh_cached(
    mesh: Optional[trimesh.Trimesh],
    p0,
    p1,
    clearance_mm: float = 0.8,
    endpoint_trim_mm: float = 0.75,
) -> bool:
    """
    Faster segment-mesh collision:
    - cheap AABB precheck
    - cached ray engine
    - avoids expensive proximity unless needed elsewhere
    """
    if mesh is None:
        return False

    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    d = p1 - p0
    length = float(np.linalg.norm(d))
    if length <= max(clearance_mm, 1e-6):
        return False

    # Trim endpoints so intended contact point is not counted as collision.
    trim = min(endpoint_trim_mm, length * 0.25)
    q0 = p0 + d / length * trim
    q1 = p1 - d / length * trim

    if not bbox_segment_could_hit_mesh(mesh, q0, q1, margin=clearance_mm):
        return False

    direction = q1 - q0
    dist = float(np.linalg.norm(direction))
    if dist <= 1e-6:
        return False
    direction = direction / dist

    try:
        ray = get_fast_mesh_cache(mesh).get("ray", mesh.ray)
        locations, _, _ = ray.intersects_location(
            [q0],
            [direction],
            multiple_hits=False,
        )
        if len(locations) == 0:
            return False
        hit_dist = float(np.linalg.norm(locations[0] - q0))
        return hit_dist < dist
    except Exception:
        # Fall back to old checker if ray engine fails.
        try:
            return ray_hits_mesh_between(mesh, q0, q1, clearance_mm=clearance_mm)
        except Exception:
            return False


def fast_bezier_points(p0, p1, p2, samples: int = 10) -> np.ndarray:
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    t = np.linspace(0.0, 1.0, int(max(2, samples)))[:, None]
    return ((1.0 - t) ** 2) * p0 + 2.0 * (1.0 - t) * t * p1 + (t ** 2) * p2


def fast_bezier_hits_mesh(
    mesh: Optional[trimesh.Trimesh],
    p0,
    p1,
    p2,
    clearance_mm: float = 0.8,
    samples: int = 8,
    strict: bool = False,
) -> bool:
    """
    Optimized Bezier collision check.

    strict=False:
      checks sampled curve segments with cached ray engine only.
      This is much faster for scanning orientations.

    strict=True:
      after fast ray check, optional proximity forbidden-zone check is used
      by existing functions if available.
    """
    if mesh is None:
        return False

    curve = fast_bezier_points(p0, p1, p2, samples=samples)
    for i in range(len(curve) - 1):
        if fast_segment_hits_mesh_cached(
            mesh,
            curve[i],
            curve[i + 1],
            clearance_mm=clearance_mm,
            endpoint_trim_mm=0.6,
        ):
            return True

    if strict:
        try:
            # use stricter existing checker if defined
            if "bezier_forbidden_zone_hit" in globals():
                return bezier_forbidden_zone_hit(
                    mesh,
                    p0,
                    p1,
                    p2,
                    clearance_mm=clearance_mm,
                    samples=max(samples, 12),
                )
        except Exception:
            return False

    return False


def adaptive_collision_samples(base_samples: int, fast_mode: bool) -> int:
    return max(5, int(base_samples * 0.55)) if fast_mode else base_samples


def safe_goal_density_target(density_target_coverage: float, min_coverage: float) -> float:
    """
    The generator should not stop at 0.15 if feasibility requires 0.30.
    """
    return max(float(density_target_coverage), float(min_coverage))


def result_sort_key_v11_3(result: Any) -> float:
    """
    Prefer feasible, then lower goal objective / higher score.
    Keeps compatibility with existing AngleScanResult.
    """
    try:
        feasible_bonus = 1_000_000.0 if bool(result.feasible) else 0.0
        return feasible_bonus + float(result.score)
    except Exception:
        try:
            feasible_bonus = 1_000_000.0 if bool(result.get("feasible")) else 0.0
            return feasible_bonus + float(result.get("score", -1e9))
        except Exception:
            return -1e9



# =========================================================
# BETA V11.4 DIAGNOSTIC HELPERS
# =========================================================

def infeasibility_reason_from_feasibility(feasibility: Dict[str, Any]) -> str:
    reasons = []

    coverage = feasibility.get("coverage", {})
    physics = feasibility.get("physics", {})
    structure = feasibility.get("support_structure", {})

    if not bool(coverage.get("ok", False)):
        reasons.append("coverage_below_minimum")

    if not bool(physics.get("all_compression_ok", False)) or not bool(physics.get("all_buckling_ok", False)):
        reasons.append("physics_failure")

    if int(structure.get("total_count", 0)) <= 0:
        reasons.append("no_support_generated")

    if int(structure.get("collision_count", 0)) > 0:
        reasons.append(f"support_mesh_collision:{int(structure.get('collision_count', 0))}")

    if int(structure.get("disconnected_count", 0)) > 0:
        reasons.append(f"disconnected_support_graph:{int(structure.get('disconnected_count', 0))}")

    return ", ".join(reasons) if reasons else "feasible"



# =========================================================
# BETA V11.5 COLLISION PRUNING HELPERS
# =========================================================

def branch_hits_mesh_v11_5(
    branch: Dict[str, Any],
    mesh: trimesh.Trimesh,
    ray_clearance_mm: float,
    support_radius: float,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> bool:
    if not all(k in branch for k in ("x1", "y1", "z1", "xm", "ym", "zm", "x2", "y2", "z2")):
        return False

    radius = float(branch.get("radius", support_radius))
    return fast_bezier_hits_mesh(
        mesh,
        [branch["x1"], branch["y1"], branch["z1"]],
        [branch["xm"], branch["ym"], branch["zm"]],
        [branch["x2"], branch["y2"], branch["z2"]],
        clearance_mm=float(ray_clearance_mm) + radius * 0.65,
        samples=adaptive_collision_samples(10, fast_mode),
        strict=strict_collision,
    )


def trunk_hits_mesh_v11_5(
    trunk: Dict[str, Any],
    mesh: trimesh.Trimesh,
    ray_clearance_mm: float,
    support_radius: float,
) -> bool:
    radius = float(trunk.get("radius", support_radius))
    x = float(trunk.get("x", 0.0))
    y = float(trunk.get("y", 0.0))
    z0 = float(trunk.get("z_bottom", 0.0))
    z1 = float(trunk.get("z_top", 0.0))

    if z1 - z0 <= 1.0:
        return False

    return fast_segment_hits_mesh_cached(
        mesh,
        [x, y, z0 + 0.5],
        [x, y, z1 - 0.5],
        clearance_mm=float(ray_clearance_mm) + radius * 0.55,
    )


def prune_colliding_supports_v11_5(
    tree: Dict[str, Any],
    mesh: trimesh.Trimesh,
    ray_clearance_mm: float,
    support_radius: float,
    fast_mode: bool = True,
    strict_collision: bool = False,
) -> Dict[str, Any]:
    """
    Instead of generating supports and then simply declaring the whole solution
    infeasible, remove colliding branches/trunks first.

    Pipeline:
    1. Remove branches whose curved path intersects the original mesh.
    2. Remove trunks whose vertical body intersects the original mesh.
    3. Remove branches attached to removed trunks.
    4. Remove unused trunks and recompute collision/disconnected counts.

    This is the key difference from V11.4:
    collision is now handled by pruning before feasibility scoring.
    """
    if mesh is None:
        return tree

    branches = list(tree.get("branches", []))
    trunks = list(tree.get("trunks", []))

    kept_branches = []
    pruned_branches = 0

    for b in branches:
        if branch_hits_mesh_v11_5(
            b,
            mesh=mesh,
            ray_clearance_mm=ray_clearance_mm,
            support_radius=support_radius,
            fast_mode=fast_mode,
            strict_collision=strict_collision,
        ):
            pruned_branches += 1
            continue
        kept_branches.append(b)

    bad_trunk_ids = set()
    pruned_trunks = 0

    for t in trunks:
        tid = int(t.get("id", -999))
        if trunk_hits_mesh_v11_5(
            t,
            mesh=mesh,
            ray_clearance_mm=ray_clearance_mm,
            support_radius=support_radius,
        ):
            bad_trunk_ids.add(tid)
            pruned_trunks += 1

    if bad_trunk_ids:
        before = len(kept_branches)
        kept_branches = [
            b for b in kept_branches
            if int(b.get("trunk_id", -999)) not in bad_trunk_ids
        ]
        pruned_branches += before - len(kept_branches)

    kept_trunks = [
        t for t in trunks
        if int(t.get("id", -999)) not in bad_trunk_ids
    ]

    tree["branches"] = kept_branches
    tree["trunks"] = kept_trunks
    tree = prune_unused_trunks(tree)

    # After pruning, recalculate final hard constraints.
    tree["collision_count"] = hard_collision_count_v11(
        tree,
        mesh=mesh,
        ray_clearance_mm=ray_clearance_mm,
        support_radius=support_radius,
        fast_mode=fast_mode,
        strict_collision=strict_collision,
    )
    tree["disconnected_count"] = disconnected_count_for_tree(tree)

    tree["pruning"] = {
        "pruned_branches": int(pruned_branches),
        "pruned_trunks": int(pruned_trunks),
        "remaining_branches": int(len(tree.get("branches", []))),
        "remaining_trunks": int(len(tree.get("trunks", []))),
    }

    return tree


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
        "loaded_file_marker": "main_beta_v11_5_collision_pruning",
        "version": "11.5.0-beta",
        "supported_materials": list(MATERIALS.keys()),
        "supported_nozzles": ALLOWED_NOZZLES,
        "support_types": [x.value for x in SupportType],
        "ray_engine": "pyembree/embreex if installed, otherwise trimesh native",
        "objective": "weighted nonlinear goal programming with Big-M collision/connectivity penalties",
        "beta_v9_3_features": [
            "classic support forbidden-zone collision filter",
            "tree branch maximum angle constraint",
            "unused trunk pruning",
            "cluster minimum point threshold",
            "hard coverage and collision goals",
            "adaptive density supports",
            "pro grid support generator",
            "support interface layer",
            "grouped tree trunk engine",
            "v11 growth-based recursive tree engine",
            "height-based taper radius",
            "hard mesh-collision infeasibility",
            "external tree routing",
            "stricter forbidden-zone proximity checks"
        ],
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
    max_branch_angle_deg: float = 70.0,
    min_cluster_points: int = 3,
    auto_density: bool = True,
    density_grid_size: float = 6.0,
    density_min_points_per_cell: int = 2,
    max_density_supports: int = 160,
    density_target_coverage: float = 0.15,
    support_strategy: Literal["pro", "legacy"] = "pro",
    pro_grid_size: float = 8.0,
    pro_min_points_per_cell: int = 1,
    pro_max_supports: int = 90,
    pro_interface: bool = True,
    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
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
                    max_branch_angle_deg=max_branch_angle_deg,
                    min_cluster_points=min_cluster_points,
                    auto_density=auto_density,
                    density_grid_size=density_grid_size,
                    density_min_points_per_cell=density_min_points_per_cell,
                    max_density_supports=max_density_supports,
                    density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
                    support_strategy=support_strategy,
                    pro_grid_size=pro_grid_size,
                    pro_min_points_per_cell=pro_min_points_per_cell,
                    pro_max_supports=pro_max_supports,
                    pro_interface=pro_interface,
                    external_tree=external_tree,
                    external_margin=external_margin,
                    fast_mode=fast_mode,
                    strict_collision=strict_collision,
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
                            max_branch_angle_deg=max_branch_angle_deg,
                            min_cluster_points=min_cluster_points,
                            auto_density=auto_density,
                            density_grid_size=density_grid_size,
                            density_min_points_per_cell=density_min_points_per_cell,
                            max_density_supports=max_density_supports,
                            density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
                            support_strategy=support_strategy,
                            pro_grid_size=pro_grid_size,
                            pro_min_points_per_cell=pro_min_points_per_cell,
                            pro_max_supports=pro_max_supports,
                            pro_interface=pro_interface,
                            external_tree=external_tree,
                            external_margin=external_margin,
                            fast_mode=fast_mode,
                            strict_collision=strict_collision,
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
                    "max_branch_angle_deg": float(max_branch_angle_deg),
                    "min_cluster_points": int(min_cluster_points),
                    "auto_density": bool(auto_density),
                    "density_grid_size": float(density_grid_size),
                    "density_min_points_per_cell": int(density_min_points_per_cell),
                    "max_density_supports": int(max_density_supports),
                    "density_target_coverage": float(density_target_coverage),
                    "support_strategy": support_strategy,
                    "pro_grid_size": float(pro_grid_size),
                    "pro_min_points_per_cell": int(pro_min_points_per_cell),
                    "pro_max_supports": int(pro_max_supports),
                    "pro_interface": bool(pro_interface),
                    "external_tree": bool(external_tree),
                    "external_margin": float(external_margin),
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
    max_branch_angle_deg: float = 70.0,
    min_cluster_points: int = 3,
    auto_density: bool = True,
    density_grid_size: float = 6.0,
    density_min_points_per_cell: int = 2,
    max_density_supports: int = 160,
    density_target_coverage: float = 0.15,
    support_strategy: Literal["pro", "legacy"] = "pro",
    pro_grid_size: float = 8.0,
    pro_min_points_per_cell: int = 1,
    pro_max_supports: int = 90,
    pro_interface: bool = True,
    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
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
                    max_branch_angle_deg=max_branch_angle_deg,
                    min_cluster_points=min_cluster_points,
                    auto_density=auto_density,
                    density_grid_size=density_grid_size,
                    density_min_points_per_cell=density_min_points_per_cell,
                    max_density_supports=max_density_supports,
                    density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
                    support_strategy=support_strategy,
                    pro_grid_size=pro_grid_size,
                    pro_min_points_per_cell=pro_min_points_per_cell,
                    pro_max_supports=pro_max_supports,
                    pro_interface=pro_interface,
                    external_tree=external_tree,
                    external_margin=external_margin,
                    fast_mode=fast_mode,
                    strict_collision=strict_collision,
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
                            max_branch_angle_deg=max_branch_angle_deg,
                            min_cluster_points=min_cluster_points,
                            auto_density=auto_density,
                            density_grid_size=density_grid_size,
                            density_min_points_per_cell=density_min_points_per_cell,
                            max_density_supports=max_density_supports,
                            density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
                            support_strategy=support_strategy,
                            pro_grid_size=pro_grid_size,
                            pro_min_points_per_cell=pro_min_points_per_cell,
                            pro_max_supports=pro_max_supports,
                            pro_interface=pro_interface,
                            external_tree=external_tree,
                            external_margin=external_margin,
                            fast_mode=fast_mode,
                            strict_collision=strict_collision,
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
                max_branch_angle_deg=max_branch_angle_deg,
                min_cluster_points=min_cluster_points,
                auto_density=auto_density,
                density_grid_size=density_grid_size,
                density_min_points_per_cell=density_min_points_per_cell,
                max_density_supports=max_density_supports,
                density_target_coverage=max(float(density_target_coverage), float(min_coverage)),
                support_strategy=support_strategy,
                pro_grid_size=pro_grid_size,
                pro_min_points_per_cell=pro_min_points_per_cell,
                pro_max_supports=pro_max_supports,
                pro_interface=pro_interface,
                external_tree=external_tree,
                external_margin=external_margin,
                fast_mode=fast_mode,
                strict_collision=strict_collision,
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
# FIXED ORIENTATION ANALYSIS ENDPOINT
# Added for poster/data collection scans:
# Analyze exactly one rho/theta pair and return JSON.
# =========================================================

@app.post("/analyze-fixed")
async def analyze_fixed_orientation(
    file: UploadFile = File(...),
    rho: float = 0.0,
    theta: float = 0.0,

    support_type: str = "classic",
    material: str = "PLA",
    nozzle_mm: float = 0.4,
    support_radius: float = 1.8,
    mesh_mass_g: float = 100.0,
    safety_factor: float = 2.0,
    critical_angle_deg: float = 45.0,
    min_coverage: float = 0.3,
    sample_points: int = 500,
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
    big_m_collision: float = 1000000.0,
    big_m_disconnected: float = 1000000.0,

    max_branch_angle_deg: float = 70.0,
    min_cluster_points: int = 3,

    auto_density: bool = True,
    density_grid_size: float = 6.0,
    density_min_points_per_cell: int = 2,
    max_density_supports: int = 160,
    density_target_coverage: float = 0.15,

    support_strategy: str = "pro",
    pro_grid_size: float = 8.0,
    pro_min_points_per_cell: int = 1,
    pro_max_supports: int = 90,
    pro_interface: bool = True,

    external_tree: bool = True,
    external_margin: float = 8.0,
    fast_mode: bool = True,
    strict_collision: bool = False,
):
    """
    Analyze exactly one orientation.

    Use this endpoint for data collection and MATLAB graphs.
    Unlike /analyze, this does NOT run adaptive or full scan.
    It only evaluates the requested rho/theta pair.
    """
    import inspect

    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".stl") as tmp:
            tmp_path = tmp.name
            tmp.write(await file.read())

        original_mesh = trimesh.load(tmp_path, force="mesh")

        if isinstance(original_mesh, trimesh.Scene):
            original_mesh = trimesh.util.concatenate(tuple(original_mesh.geometry.values()))

        sig = inspect.signature(evaluate_orientation)

        kwargs = {
            "original_mesh": original_mesh,
            "rho_deg": float(rho),
            "theta_deg": float(theta),
            "support_type": support_type,
            "material": material,
            "nozzle_mm": nozzle_mm,
            "support_radius": support_radius,
            "mesh_mass_g": mesh_mass_g,
            "safety_factor": safety_factor,
            "critical_angle_deg": critical_angle_deg,
            "min_coverage": min_coverage,
            "sample_points": sample_points,
            "max_xy_distance": max_xy_distance,
            "tree_eps": tree_eps,
            "branch_merge_eps": branch_merge_eps,
            "max_branches_per_tree": max_branches_per_tree,
            "trunk_outside_offset": trunk_outside_offset,
            "branch_outside_offset": branch_outside_offset,
            "branch_curve_lift": branch_curve_lift,
            "collision_margin": collision_margin,
            "tip_gap_mm": tip_gap_mm,
            "ray_clearance_mm": ray_clearance_mm,
            "downray_filter": downray_filter,
            "target_volume": target_volume,
            "target_support_count": target_support_count,
            "big_m_collision": big_m_collision,
            "big_m_disconnected": big_m_disconnected,
            "max_branch_angle_deg": max_branch_angle_deg,
            "min_cluster_points": min_cluster_points,
            "auto_density": auto_density,
            "density_grid_size": density_grid_size,
            "density_min_points_per_cell": density_min_points_per_cell,
            "max_density_supports": max_density_supports,
            "density_target_coverage": density_target_coverage,
            "support_strategy": support_strategy,
            "pro_grid_size": pro_grid_size,
            "pro_min_points_per_cell": pro_min_points_per_cell,
            "pro_max_supports": pro_max_supports,
            "pro_interface": pro_interface,
            "external_tree": external_tree,
            "external_margin": external_margin,
            "fast_mode": fast_mode,
            "strict_collision": strict_collision,
        }

        # Keep compatibility if evaluate_orientation signature changes.
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in sig.parameters
        }

        result = evaluate_orientation(**kwargs)
        result_dict = result.to_dict() if hasattr(result, "to_dict") else result

        return {
            "filename": file.filename,
            "analysis_type": "fixed_orientation",
            "rho": float(rho),
            "theta": float(theta),
            "support_type": support_type,
            "material": material,
            "sample_points": int(sample_points),
            "result": result_dict,
        }

    except Exception as e:
        logger.exception(f"Error in /analyze-fixed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
