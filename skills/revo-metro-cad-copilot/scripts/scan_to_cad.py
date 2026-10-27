#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Revo Metro 3D Scan-to-CAD & Engineering Drawing Generator
=========================================================
Automatically converts Revo Metro (.revo / .ply / .stl / .obj) scan data into:
  1. Cleaned & PCA-Aligned 3D Mesh (.stl, .ply, .obj)
  2. Reverse-Engineered Parametric 3D CAD Solid (.step, .iges, _parametric_cad.py)
  3. 2D/3D Engineering Drawings (.dxf, _blueprint.png, _blueprint.pdf, _blueprint.svg, _dimensions.json)

Powered by FreeCAD 1.1 (OpenCASCADE B-Rep kernel + ReverseEngineering + Mesh + Points)
and NumPy / SciPy / Matplotlib.
"""

import os
import sys
import subprocess

FREECAD_PYTHON = r"C:\Users\rogers\Applications\FreeCAD\bin\python.exe"

# Auto-relaunch under FreeCAD's bundled Python if current interpreter lacks FreeCAD/numpy
try:
    import numpy as np
    import FreeCAD
except ImportError:
    if os.path.isfile(FREECAD_PYTHON) and os.path.normcase(sys.executable) != os.path.normcase(FREECAD_PYTHON):
        res = subprocess.run([FREECAD_PYTHON, __file__] + sys.argv[1:])
        sys.exit(res.returncode)
    else:
        raise RuntimeError(
            f"需要 FreeCAD Python 环境 ({FREECAD_PYTHON}) 以运行 OpenCASCADE CAD 建模与点云逆向算法。"
        )

import argparse
import glob
import json
import math
from datetime import datetime
from scipy.spatial import cKDTree, ConvexHull, Delaunay
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle as MplCircle, Polygon as MplPolygon, Rectangle

import Part
import Mesh
import Points
import ReverseEngineering

# Configure Matplotlib Chinese font support on Windows
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

APPDATA_ROAMING = os.path.expandvars(r"%APPDATA%\RevoMetro")
PROJECTS_DIR = os.path.join(APPDATA_ROAMING, "Projects")


def resolve_input_file(args):
    """Resolve input path from direct file path or Revo Metro project/node name."""
    if args.input and os.path.isfile(args.input):
        if args.input.lower().endswith(".revo"):
            return resolve_from_revo(args.input, args.node)
        return os.path.abspath(args.input), os.path.splitext(os.path.basename(args.input))[0]

    if args.project:
        revo_candidates = glob.glob(os.path.join(PROJECTS_DIR, args.project, "*.revo"))
        if not revo_candidates and os.path.isfile(args.project):
            revo_candidates = [args.project]
        if not revo_candidates:
            raise FileNotFoundError(f"未找到工程: {args.project} (搜索目录: {PROJECTS_DIR})")
        return resolve_from_revo(revo_candidates[0], args.node)

    raise ValueError("请提供 --input <文件路径> 或 --project <Revo工程名称>。")


def resolve_from_revo(revo_path, target_node=None):
    """Select the best scan node file (preferring fuse_mesh.ply > raw_preview.ply) from a .revo project."""
    proj_dir = os.path.dirname(os.path.abspath(revo_path))
    with open(revo_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    nodes = meta.get("nodes", [])
    if not nodes:
        raise RuntimeError(f"工程 {revo_path} 中没有任何扫描节点。")

    best_file = None
    best_name = None
    best_score = -1

    for node in nodes:
        name = node.get("name", "")
        guid = node.get("guid", "")
        if target_node and target_node not in (name, guid):
            continue
        node_dir = os.path.join(proj_dir, "data", guid)
        fuse_ply = os.path.join(node_dir, "fuse_mesh.ply")
        raw_ply = os.path.join(node_dir, "raw_preview.ply")
        prop_path = os.path.join(node_dir, "property.rvproj")
        vf_count = 0
        if os.path.isfile(prop_path):
            try:
                with open(prop_path, "r", encoding="utf-8") as pf:
                    vf_count = json.load(pf).get("vf_count", 0)
            except Exception:
                pass

        if os.path.isfile(fuse_ply):
            score = 1_000_000 + vf_count + os.path.getsize(fuse_ply)
            if score > best_score:
                best_score = score
                best_file = fuse_ply
                best_name = f"{meta.get('name', 'Project')}_{name}"
        elif os.path.isfile(raw_ply):
            score = vf_count + os.path.getsize(raw_ply)
            if score > best_score:
                best_score = score
                best_file = raw_ply
                best_name = f"{meta.get('name', 'Project')}_{name}"

    if not best_file:
        raise FileNotFoundError(f"在工程 {revo_path} 中未找到可用的 .ply 点云或网格文件。")
    return best_file, best_name


def parse_ply_vertices_numpy(filepath):
    """Fast native NumPy parser for ASCII and binary_little_endian PLY vertex data."""
    TYPE_MAP = {
        "char": "i1", "int8": "i1",
        "uchar": "u1", "uint8": "u1",
        "short": "<i2", "int16": "<i2",
        "ushort": "<u2", "uint16": "<u2",
        "int": "<i4", "int32": "<i4",
        "uint": "<u4", "uint32": "<u4",
        "float": "<f4", "float32": "<f4",
        "double": "<f8", "float64": "<f8",
    }
    with open(filepath, "rb") as f:
        fmt = "ascii"
        num_vertices = 0
        in_vertex = False
        vertex_props = []
        while True:
            line = f.readline()
            if not line:
                break
            s = line.decode("ascii", errors="ignore").strip()
            if s.startswith("format "):
                fmt = s.split()[1]
            elif s.startswith("element "):
                parts = s.split()
                if parts[1] == "vertex":
                    num_vertices = int(parts[2])
                    in_vertex = True
                else:
                    in_vertex = False
            elif s.startswith("property ") and in_vertex:
                parts = s.split()
                if parts[1] != "list":
                    dtype_str = TYPE_MAP.get(parts[1], "<f4")
                    vertex_props.append((parts[2], dtype_str))
            elif s == "end_header":
                break

        if num_vertices <= 0:
            raise RuntimeError(f"PLY 文件不包含有效顶点: {filepath}")

        if "binary_little_endian" in fmt:
            dt = np.dtype(vertex_props)
            raw_bytes = f.read(num_vertices * dt.itemsize)
            arr = np.frombuffer(raw_bytes, dtype=dt, count=num_vertices)
            pts = np.column_stack([
                arr["x"].astype(np.float64),
                arr["y"].astype(np.float64),
                arr["z"].astype(np.float64),
            ])
            return pts
        else:
            # ASCII PLY
            prop_names = [p[0] for p in vertex_props]
            ix, iy, iz = prop_names.index("x"), prop_names.index("y"), prop_names.index("z")
            pts_list = []
            for _ in range(num_vertices):
                row = f.readline().decode("ascii", errors="ignore").split()
                if len(row) > max(ix, iy, iz):
                    pts_list.append([float(row[ix]), float(row[iy]), float(row[iz])])
            return np.array(pts_list, dtype=np.float64)


def load_points_and_mesh(filepath):
    """Load 3D points (Nx3 numpy array) and optional FreeCAD Mesh from .ply/.stl/.obj."""
    ext = os.path.splitext(filepath)[1].lower()
    fc_mesh = None
    pts = None

    if ext in (".stl", ".obj"):
        fc_mesh = Mesh.Mesh(filepath)
        if fc_mesh.CountPoints > 0:
            pts = np.array([[p.x, p.y, p.z] for p in fc_mesh.Points], dtype=np.float64)
    elif ext == ".ply":
        # Try loading as Mesh first to check if it has faces
        try:
            m = Mesh.Mesh(filepath)
            if m.CountFacets > 0:
                fc_mesh = m
                pts = np.array([[p.x, p.y, p.z] for p in m.Points], dtype=np.float64)
        except Exception:
            pass

        if pts is None or len(pts) == 0:
            pts = parse_ply_vertices_numpy(filepath)

    if pts is None or len(pts) == 0:
        raise RuntimeError(f"无法从文件读取有效 3D 点数据: {filepath}")

    # Filter out NaNs or infinities
    valid_mask = np.isfinite(pts).all(axis=1)
    pts = pts[valid_mask]
    return pts, fc_mesh


def clean_point_cloud(pts, remove_base=False, isolate=True, sor_k=16, sor_std=2.0,
                      remove_markers=True, marker_max_mm=12.0, marker_flatness_mm=0.6):
    """Clean point cloud: Statistical Outlier Removal, optional RANSAC base plane slicing, marker-disc removal
    and main cluster isolation."""
    raw_count = len(pts)
    if raw_count < 30:
        return pts, {"raw_points": raw_count, "cleaned_points": raw_count}

    # Subsample if huge (>150,000 pts) for fast KDTree processing
    work_pts = pts
    if len(work_pts) > 150000:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(work_pts), size=150000, replace=False)
        work_pts = work_pts[idx]

    # 1. Statistical Outlier Removal (SOR)
    tree = cKDTree(work_pts)
    dists, _ = tree.query(work_pts, k=min(sor_k + 1, len(work_pts)))
    mean_dists = np.mean(dists[:, 1:], axis=1)
    median_d = np.median(mean_dists)
    std_d = np.std(mean_dists)
    inlier_mask = mean_dists <= (median_d + sor_std * std_d)
    work_pts = work_pts[inlier_mask]

    # 2. Optional RANSAC Base Plane Removal (slice off flat desktop/turntable)
    base_removed_count = 0
    if remove_base and len(work_pts) > 200:
        rng = np.random.default_rng(42)
        best_normal = None
        best_d = 0.0
        best_inliers = None
        tol = max(0.8, float(median_d * 2.5))
        sample_pts = work_pts[rng.choice(len(work_pts), size=min(5000, len(work_pts)), replace=False)]
        for _ in range(120):
            tri = sample_pts[rng.choice(len(sample_pts), size=3, replace=False)]
            v1 = tri[1] - tri[0]
            v2 = tri[2] - tri[0]
            n = np.cross(v1, v2)
            norm = np.linalg.norm(n)
            if norm < 1e-8:
                continue
            n = n / norm
            d = -np.dot(n, tri[0])
            plane_dists = np.abs(sample_pts @ n + d)
            inliers = np.sum(plane_dists < tol)
            if best_inliers is None or inliers > best_inliers:
                best_inliers = inliers
                best_normal = n
                best_d = d

        # Only remove if the plane is near the bottom/top boundary and accounts for 10%~65% of points
        if best_normal is not None and (0.10 * len(sample_pts) < best_inliers < 0.65 * len(sample_pts)):
            signed = work_pts @ best_normal + best_d
            # Ensure normal points toward the majority of the object
            if np.median(signed) < 0:
                best_normal = -best_normal
                best_d = -best_d
                signed = -signed
            keep_mask = signed > tol
            if np.sum(keep_mask) > 100:
                base_removed_count = int(len(work_pts) - np.sum(keep_mask))
                work_pts = work_pts[keep_mask]

    # 3. Connected-Component Isolation via KDTree Graph (removes floating marker blocks & background clutter)
    isolated_removed_count = 0
    marker_clusters_removed = 0
    if isolate and len(work_pts) > 100:
        tree2 = cKDTree(work_pts)
        d2, _ = tree2.query(work_pts, k=min(8, len(work_pts)))
        cluster_radius = max(2.5, float(np.percentile(d2[:, -1], 92) * 3.0))
        # Connected components via BFS on KDTree ball graph (downsampled if >40k)
        if len(work_pts) > 40000:
            rng = np.random.default_rng(42)
            sub_idx = rng.choice(len(work_pts), size=40000, replace=False)
            sub_pts = work_pts[sub_idx]
        else:
            sub_pts = work_pts

        stree = cKDTree(sub_pts)
        visited = np.zeros(len(sub_pts), dtype=bool)
        clusters = []
        for i in range(len(sub_pts)):
            if not visited[i]:
                queue = [i]
                visited[i] = True
                comp = [i]
                head = 0
                while head < len(queue):
                    curr = queue[head]
                    head += 1
                    neighbors = stree.query_ball_point(sub_pts[curr], r=cluster_radius)
                    for nb in neighbors:
                        if not visited[nb]:
                            visited[nb] = True
                            queue.append(nb)
                            comp.append(nb)
                clusters.append(comp)

        # Drop marker-sticker discs: small (<= marker_max_mm across) and flat (thin along the smallest PCA axis)
        if remove_markers and len(clusters) > 1:
            kept = []
            for c in clusters:
                cp = sub_pts[c]
                if len(cp) >= 6:
                    sv = np.linalg.svd(cp - cp.mean(axis=0), compute_uv=False) / math.sqrt(len(cp))
                    extent = float(np.ptp(cp, axis=0).max())
                    if extent <= marker_max_mm and sv[2] < marker_flatness_mm:
                        marker_clusters_removed += 1
                        continue
                kept.append(c)
            if kept:
                clusters = kept

        clusters.sort(key=len, reverse=True)
        # Keep largest cluster (and any cluster that has at least 20% of the largest cluster's points)
        main_size = len(clusters[0])
        valid_indices = []
        for c in clusters:
            if len(c) >= max(30, int(0.18 * main_size)):
                valid_indices.extend(c)
        valid_sub_pts = sub_pts[valid_indices]
        if len(work_pts) > 40000:
            vtree = cKDTree(valid_sub_pts)
            dist_to_valid, _ = vtree.query(work_pts, k=1)
            keep = dist_to_valid <= cluster_radius
            isolated_removed_count = int(len(work_pts) - np.sum(keep))
            work_pts = work_pts[keep]
        else:
            isolated_removed_count = int(len(sub_pts) - len(valid_sub_pts))
            work_pts = valid_sub_pts

    stats = {
        "raw_points": int(raw_count),
        "cleaned_points": int(len(work_pts)),
        "base_plane_points_removed": int(base_removed_count),
        "isolated_noise_points_removed": int(isolated_removed_count),
        "marker_disc_clusters_removed": int(marker_clusters_removed),
    }
    return work_pts, stats


def align_points_pca(pts):
    """
    Align point cloud to canonical engineering axes via PCA:
      - Primary longest axis -> X
      - Secondary axis -> Y
      - Shortest/normal axis -> Z (with bottom resting on Z = 0, XY centered at 0,0)
    Returns (aligned_pts, rotation_matrix_3x3, translation_pre, translation_post).
    """
    centroid = np.mean(pts, axis=0)
    centered = pts - centroid
    # SVD on covariance
    cov = np.cov(centered, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(cov)
    # Sort eigenvalues descending: largest variance = X, middle = Y, smallest = Z
    order = np.argsort(eigvals)[::-1]
    R = eigvecs[:, order].T  # rows are new X, Y, Z unit vectors
    if np.linalg.det(R) < 0:
        R[2, :] = -R[2, :]

    rotated = centered @ R.T
    # Center XY at midpoint of bounding box, and place bottom at Z = 0
    mins = np.min(rotated, axis=0)
    maxs = np.max(rotated, axis=0)
    mid_xy = 0.5 * (mins[:2] + maxs[:2])
    shift_post = np.array([-mid_xy[0], -mid_xy[1], -mins[2]], dtype=np.float64)
    aligned = rotated + shift_post
    return aligned, R, -centroid, shift_post


def fit_circle_2d_kasa(xy):
    """Algebraic Kåsa circle fit on Nx2 points. Returns (cx, cy, radius, residual_rms)."""
    if len(xy) < 6:
        return None
    x = xy[:, 0]
    y = xy[:, 1]
    A = np.column_stack([2 * x, 2 * y, np.ones_like(x)])
    b = x ** 2 + y ** 2
    try:
        sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
        cx, cy, c = sol
        r_sq = c + cx ** 2 + cy ** 2
        if r_sq <= 0:
            return None
        r = math.sqrt(r_sq)
        dists = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        rms = float(np.sqrt(np.mean((dists - r) ** 2)))
        return float(cx), float(cy), float(r), rms
    except Exception:
        return None


def simplify_polygon_rdp(pts_2d, epsilon=0.6):
    """Ramer-Douglas-Peucker 2D polyline simplification."""
    if len(pts_2d) < 3:
        return pts_2d
    dmax = 0.0
    index = 0
    p1 = pts_2d[0]
    p2 = pts_2d[-1]
    line_vec = p2 - p1
    line_len = np.linalg.norm(line_vec)
    for i in range(1, len(pts_2d) - 1):
        if line_len < 1e-8:
            d = np.linalg.norm(pts_2d[i] - p1)
        else:
            d = abs(np.cross(line_vec, p1 - pts_2d[i])) / line_len
        if d > dmax:
            index = i
            dmax = d
    if dmax > epsilon:
        rec1 = simplify_polygon_rdp(pts_2d[: index + 1], epsilon)
        rec2 = simplify_polygon_rdp(pts_2d[index:], epsilon)
        return np.vstack([rec1[:-1], rec2])
    else:
        return np.vstack([p1, p2])


def extract_engineering_features(aligned_pts):
    """
    Extract comprehensive engineering dimensions, cross-section contours, outer profile type,
    and internal circular holes/bosses from the aligned point cloud.
    """
    mins = np.min(aligned_pts, axis=0)
    maxs = np.max(aligned_pts, axis=0)
    dims = maxs - mins
    length_x = float(dims[0])
    width_y = float(dims[1])
    height_z = float(dims[2])

    # Extract 2D cross-section slices at 20%, 50%, 80% of Z height
    slice_ratios = [0.20, 0.50, 0.80]
    slice_band = max(0.8, 0.04 * height_z)
    sections = []

    for ratio in slice_ratios:
        z_val = float(mins[2] + ratio * height_z)
        mask = np.abs(aligned_pts[:, 2] - z_val) <= slice_band
        xy = aligned_pts[mask, :2]
        if len(xy) >= 12:
            try:
                hull = ConvexHull(xy)
                hull_pts = xy[hull.vertices]
                closed = np.vstack([hull_pts, hull_pts[0]])
                simp = simplify_polygon_rdp(closed, epsilon=max(0.4, 0.008 * max(length_x, width_y)))
                sections.append({
                    "z_ratio": ratio,
                    "z_mm": round(z_val, 3),
                    "point_count": int(len(xy)),
                    "contour_xy": [[round(float(p[0]), 3), round(float(p[1]), 3)] for p in simp],
                })
            except Exception:
                pass

    # Fallback outer contour from XY projection if mid-slices are sparse
    xy_all = aligned_pts[:, :2]
    hull_all = ConvexHull(xy_all)
    outer_hull_pts = xy_all[hull_all.vertices]
    outer_closed = np.vstack([outer_hull_pts, outer_hull_pts[0]])
    outer_simplified = simplify_polygon_rdp(
        outer_closed, epsilon=max(0.5, 0.01 * max(length_x, width_y))
    )

    # Check if outer boundary is circular/cylindrical
    c_fit = fit_circle_2d_kasa(outer_hull_pts)
    is_outer_cylindrical = False
    outer_circle_info = None
    if c_fit:
        cx, cy, r, rms = c_fit
        if rms < 0.035 * r and abs(length_x - width_y) < 0.12 * max(length_x, width_y):
            is_outer_cylindrical = True
            outer_circle_info = {
                "center_x_mm": round(cx, 3),
                "center_y_mm": round(cy, 3),
                "diameter_mm": round(2.0 * r, 3),
                "fit_rms_mm": round(rms, 3),
            }

    # Detect circular through-holes / cavities in the XY projection using a 2D occupancy grid
    detected_holes = []
    grid_res = 120
    x_edges = np.linspace(mins[0], maxs[0], grid_res + 1)
    y_edges = np.linspace(mins[1], maxs[1], grid_res + 1)
    H, _, _ = np.histogram2d(xy_all[:, 0], xy_all[:, 1], bins=[x_edges, y_edges])
    # Find empty interior cells inside the convex hull
    x_centers = 0.5 * (x_edges[:-1] + x_edges[1:])
    y_centers = 0.5 * (y_edges[:-1] + y_edges[1:])
    cell_w = float(x_edges[1] - x_edges[0])
    cell_h = float(y_edges[1] - y_edges[0])
    tree_2d = cKDTree(xy_all)

    # Local maxima of distance-to-nearest-point inside bounding box (candidate hole centers)
    gx, gy = np.meshgrid(x_centers, y_centers, indexing="ij")
    grid_pts = np.column_stack([gx.ravel(), gy.ravel()])
    dists_2d, _ = tree_2d.query(grid_pts, k=1)
    dist_map = dists_2d.reshape(grid_res, grid_res)

    min_hole_r = max(2.5, 0.04 * min(length_x, width_y))
    max_hole_r = 0.42 * min(length_x, width_y)

    for i in range(4, grid_res - 4):
        for j in range(4, grid_res - 4):
            r_cand = float(dist_map[i, j])
            if min_hole_r <= r_cand <= max_hole_r:
                patch = dist_map[i - 3 : i + 4, j - 3 : j + 4]
                if r_cand >= np.max(patch) and r_cand > 0:
                    cx_cand = float(x_centers[i])
                    cy_cand = float(y_centers[j])
                    # Verify points form a ring around (cx_cand, cy_cand) at radius [r_cand, r_cand * 1.35]
                    d_ring = np.sqrt((xy_all[:, 0] - cx_cand) ** 2 + (xy_all[:, 1] - cy_cand) ** 2)
                    ring_pts = xy_all[(d_ring >= r_cand * 0.95) & (d_ring <= r_cand * 1.30)]
                    if len(ring_pts) >= 24:
                        angles = np.arctan2(ring_pts[:, 1] - cy_cand, ring_pts[:, 0] - cx_cand)
                        # Check angular coverage across 8 octants
                        octants = np.unique(np.floor((angles + np.pi) / (np.pi / 4)))
                        if len(octants) >= 6:
                            fit = fit_circle_2d_kasa(ring_pts)
                            if fit:
                                fcx, fcy, fr, frms = fit
                                if min_hole_r <= fr <= max_hole_r and frms < 0.20 * fr:
                                    # Deduplicate close hole centers
                                    if not any(math.hypot(fcx - h["center_x_mm"], fcy - h["center_y_mm"]) < fr for h in detected_holes):
                                        detected_holes.append({
                                            "center_x_mm": round(fcx, 3),
                                            "center_y_mm": round(fcy, 3),
                                            "radius_mm": round(fr, 3),
                                            "diameter_mm": round(2.0 * fr, 3),
                                            "fit_rms_mm": round(frms, 3),
                                        })

    detected_holes.sort(key=lambda h: h["diameter_mm"], reverse=True)
    detected_holes = detected_holes[:8]

    return {
        "bounding_box_mm": {
            "length_x": round(length_x, 3),
            "width_y": round(width_y, 3),
            "height_z": round(height_z, 3),
            "x_min": round(float(mins[0]), 3),
            "x_max": round(float(maxs[0]), 3),
            "y_min": round(float(mins[1]), 3),
            "y_max": round(float(maxs[1]), 3),
            "z_min": round(float(mins[2]), 3),
            "z_max": round(float(maxs[2]), 3),
        },
        "is_outer_cylindrical": is_outer_cylindrical,
        "outer_cylinder": outer_circle_info,
        "outer_contour_xy": [[round(float(p[0]), 3), round(float(p[1]), 3)] for p in outer_simplified],
        "cross_sections": sections,
        "detected_holes": detected_holes,
    }


def export_cleaned_meshes(aligned_pts, orig_mesh, R, shift_pre, shift_post, out_prefix):
    """Export cleaned & aligned 3D mesh to .stl, .ply, and .obj."""
    stl_path = f"{out_prefix}_cleaned.stl"
    ply_path = f"{out_prefix}_cleaned.ply"
    obj_path = f"{out_prefix}_cleaned.obj"

    final_mesh = None
    if orig_mesh is not None and orig_mesh.CountFacets > 0:
        # Apply rigid transformation to existing mesh
        m = orig_mesh.copy()
        mat = FreeCAD.Matrix()
        # Combine x_aligned = R * (x + shift_pre) + shift_post = R*x + (R*shift_pre + shift_post)
        t_total = R @ shift_pre + shift_post
        mat.A11, mat.A12, mat.A13, mat.A14 = float(R[0, 0]), float(R[0, 1]), float(R[0, 2]), float(t_total[0])
        mat.A21, mat.A22, mat.A23, mat.A24 = float(R[1, 0]), float(R[1, 1]), float(R[1, 2]), float(t_total[1])
        mat.A31, mat.A32, mat.A33, mat.A34 = float(R[2, 0]), float(R[2, 1]), float(R[2, 2]), float(t_total[2])
        m.transform(mat)
        try:
            if m.CountFacets > 500:
                m.removeComponents(max(50, int(0.02 * m.CountFacets)))
            m.fillupHoles(20)
            m.smooth(2)
        except Exception:
            pass
        final_mesh = m
    else:
        # Reconstruct surface mesh from point cloud via FreeCAD ReverseEngineering or Alpha-Filtered Delaunay
        pc = Points.Points([FreeCAD.Vector(float(p[0]), float(p[1]), float(p[2])) for p in aligned_pts])
        try:
            m = ReverseEngineering.poissonReconstruction(Points=pc, OctreeDepth=7)
            if m.CountFacets > 0:
                m.smooth(2)
                final_mesh = m
        except Exception:
            pass

        if final_mesh is None or final_mesh.CountFacets == 0:
            try:
                # High-resolution Alpha-Filtered 2.5D Delaunay surface triangulation
                rng = np.random.default_rng(42)
                max_mesh_pts = 15000
                if len(aligned_pts) > max_mesh_pts:
                    idx = rng.choice(len(aligned_pts), size=max_mesh_pts, replace=False)
                    sub_pts = aligned_pts[idx]
                else:
                    sub_pts = aligned_pts

                tri2d = Delaunay(sub_pts[:, :2])
                simplices = tri2d.simplices
                p0 = sub_pts[simplices[:, 0]]
                p1 = sub_pts[simplices[:, 1]]
                p2 = sub_pts[simplices[:, 2]]
                e0 = np.linalg.norm(p1 - p0, axis=1)
                e1 = np.linalg.norm(p2 - p1, axis=1)
                e2 = np.linalg.norm(p0 - p2, axis=1)
                max_edge = np.maximum(np.maximum(e0, e1), e2)
                # Alpha threshold: filter out long spanning triangles across concavities/holes
                alpha_thresh = max(3.5, float(np.percentile(max_edge, 90) * 2.2))
                valid_tris = simplices[max_edge <= alpha_thresh]

                if len(valid_tris) > 100:
                    facets = [
                        [
                            (float(sub_pts[s[0], 0]), float(sub_pts[s[0], 1]), float(sub_pts[s[0], 2])),
                            (float(sub_pts[s[1], 0]), float(sub_pts[s[1], 1]), float(sub_pts[s[1], 2])),
                            (float(sub_pts[s[2], 0]), float(sub_pts[s[2], 1]), float(sub_pts[s[2], 2])),
                        ]
                        for s in valid_tris
                    ]
                    m = Mesh.Mesh(facets)
                    m.fillupHoles(15)
                    m.smooth(2)
                    final_mesh = m
            except Exception:
                pass

        if final_mesh is None or final_mesh.CountFacets == 0:
            # Ultimate Fallback: 3D Convex Hull mesh
            hull3d = ConvexHull(aligned_pts)
            facets = []
            for simplex in hull3d.simplices:
                p0, p1, p2 = aligned_pts[simplex[0]], aligned_pts[simplex[1]], aligned_pts[simplex[2]]
                facets.append([
                    (float(p0[0]), float(p0[1]), float(p0[2])),
                    (float(p1[0]), float(p1[1]), float(p1[2])),
                    (float(p2[0]), float(p2[1]), float(p2[2])),
                ])
            final_mesh = Mesh.Mesh(facets)

    final_mesh.write(stl_path)
    final_mesh.write(ply_path)
    final_mesh.write(obj_path)
    return {
        "stl": stl_path,
        "ply": ply_path,
        "obj": obj_path,
        "vertex_count": int(final_mesh.CountPoints),
        "facet_count": int(final_mesh.CountFacets),
        "volume_mm3": round(float(final_mesh.Volume), 2),
        "area_mm2": round(float(final_mesh.Area), 2),
    }


def build_and_export_cad_solid(features, out_prefix):
    """
    Build a parametric B-Rep 3D CAD solid in FreeCAD Part from extracted features
    (outer contour extrusion/lofting or cylinder + boolean hole cuts), and export
    to .step, .iges, and a standalone editable _parametric_cad.py script.
    """
    step_path = f"{out_prefix}_cad.step"
    iges_path = f"{out_prefix}_cad.iges"
    py_path = f"{out_prefix}_parametric_cad.py"

    bbox = features["bounding_box_mm"]
    height_z = max(0.5, float(bbox["height_z"]))
    holes = features["detected_holes"]
    contour = features["outer_contour_xy"]

    # Build base solid
    if features["is_outer_cylindrical"] and features["outer_cylinder"]:
        oc = features["outer_cylinder"]
        r = max(0.5, float(oc["diameter_mm"]) * 0.5)
        cx = float(oc["center_x_mm"])
        cy = float(oc["center_y_mm"])
        solid = Part.makeCylinder(r, height_z, FreeCAD.Vector(cx, cy, 0.0), FreeCAD.Vector(0, 0, 1))
    else:
        # Extrude the simplified 2D outer contour polygon
        pts_3d = [FreeCAD.Vector(float(p[0]), float(p[1]), 0.0) for p in contour]
        if (pts_3d[0] - pts_3d[-1]).Length > 1e-4:
            pts_3d.append(pts_3d[0])
        wire = Part.makePolygon(pts_3d)
        face = Part.Face(wire)
        solid = face.extrude(FreeCAD.Vector(0, 0, height_z))

    # Cut detected cylindrical holes
    for h in holes:
        hr = max(0.2, float(h["radius_mm"]))
        hcx = float(h["center_x_mm"])
        hcy = float(h["center_y_mm"])
        cyl_cut = Part.makeCylinder(hr, height_z + 2.0, FreeCAD.Vector(hcx, hcy, -1.0), FreeCAD.Vector(0, 0, 1))
        try:
            solid = solid.cut(cyl_cut)
        except Exception:
            pass

    solid.exportStep(step_path)
    solid.exportIges(iges_path)

    # Generate editable standalone Python parametric CAD script
    script_code = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parametric 3D CAD Reconstruction Script (Auto-generated from Revo Metro 3D Scan)
================================================================================
You can modify the parametric dimensions below and run this script with:
  "{FREECAD_PYTHON}" "{os.path.basename(py_path)}"
to regenerate the STEP and IGES solid models.
"""
import FreeCAD
import Part

# === 1. Parametric Dimensions (Unit: mm) ===
LENGTH_X_MM = {bbox["length_x"]}
WIDTH_Y_MM = {bbox["width_y"]}
HEIGHT_Z_MM = {height_z}
IS_CYLINDRICAL = {features["is_outer_cylindrical"]}
OUTER_CYLINDER = {json.dumps(features["outer_cylinder"])}

# Simplified 2D Outer Profile Vertices (X, Y in mm)
OUTER_PROFILE_XY = {json.dumps(contour, indent=2)}

# Detected Cylindrical Through-Holes: [{{"center_x_mm", "center_y_mm", "diameter_mm", ...}}]
HOLES = {json.dumps(holes, indent=2)}


def build_solid():
    if IS_CYLINDRICAL and OUTER_CYLINDER:
        r = OUTER_CYLINDER["diameter_mm"] * 0.5
        solid = Part.makeCylinder(
            r, HEIGHT_Z_MM,
            FreeCAD.Vector(OUTER_CYLINDER["center_x_mm"], OUTER_CYLINDER["center_y_mm"], 0.0),
            FreeCAD.Vector(0, 0, 1)
        )
    else:
        vecs = [FreeCAD.Vector(p[0], p[1], 0.0) for p in OUTER_PROFILE_XY]
        if (vecs[0] - vecs[-1]).Length > 1e-4:
            vecs.append(vecs[0])
        wire = Part.makePolygon(vecs)
        face = Part.Face(wire)
        solid = face.extrude(FreeCAD.Vector(0, 0, HEIGHT_Z_MM))

    for h in HOLES:
        r_hole = h["diameter_mm"] * 0.5
        cutter = Part.makeCylinder(
            r_hole, HEIGHT_Z_MM + 2.0,
            FreeCAD.Vector(h["center_x_mm"], h["center_y_mm"], -1.0),
            FreeCAD.Vector(0, 0, 1)
        )
        solid = solid.cut(cutter)
    return solid


if __name__ == "__main__":
    shape = build_solid()
    shape.exportStep(r"{step_path}")
    shape.exportIges(r"{iges_path}")
    print("Regenerated STEP & IGES CAD models successfully.")
'''
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(script_code)

    return {
        "step": step_path,
        "iges": iges_path,
        "parametric_script": py_path,
    }


def export_dxf_drawing(features, out_prefix):
    """
    Write a standard AutoCAD R12/LT2 DXF file containing:
      - TOP_VIEW (XY outer profile + cross-section contours + detected circular holes)
      - FRONT_VIEW (XZ bounding & section projection offset in Y)
      - SIDE_VIEW (YZ bounding & section projection offset in X)
    """
    dxf_path = f"{out_prefix}_drawing.dxf"
    bbox = features["bounding_box_mm"]
    lx, wy, hz = bbox["length_x"], bbox["width_y"], bbox["height_z"]
    gap = max(20.0, 0.35 * max(lx, wy, hz))

    lines = [
        "0", "SECTION",
        "2", "ENTITIES",
    ]

    def add_line(x1, y1, x2, y2, layer="0", color=7):
        lines.extend([
            "0", "LINE",
            "8", layer,
            "62", str(color),
            "10", f"{x1:.4f}",
            "20", f"{y1:.4f}",
            "30", "0.0",
            "11", f"{x2:.4f}",
            "21", f"{y2:.4f}",
            "31", "0.0",
        ])

    def add_polyline_2d(pts, layer="OUTLINE", color=7):
        for i in range(len(pts) - 1):
            add_line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], layer=layer, color=color)

    def add_circle(cx, cy, r, layer="HOLES", color=1):
        lines.extend([
            "0", "CIRCLE",
            "8", layer,
            "62", str(color),
            "10", f"{cx:.4f}",
            "20", f"{cy:.4f}",
            "30", "0.0",
            "40", f"{r:.4f}",
        ])

    # 1. Top View (centered at 0, 0)
    add_polyline_2d(features["outer_contour_xy"], layer="TOP_OUTER_PROFILE", color=7)
    for sec in features["cross_sections"]:
        add_polyline_2d(sec["contour_xy"], layer=f"SECTION_Z_{int(sec['z_ratio']*100)}", color=5)
    for h in features["detected_holes"]:
        add_circle(h["center_x_mm"], h["center_y_mm"], h["radius_mm"], layer="TOP_HOLES", color=1)

    # 2. Front View (XZ placed above Top View at Y_offset = wy/2 + gap)
    y_off = bbox["y_max"] + gap
    x0, x1 = bbox["x_min"], bbox["x_max"]
    add_polyline_2d(
        [[x0, y_off], [x1, y_off], [x1, y_off + hz], [x0, y_off + hz], [x0, y_off]],
        layer="FRONT_VIEW",
        color=3,
    )
    for h in features["detected_holes"]:
        hx, hr = h["center_x_mm"], h["radius_mm"]
        add_line(hx - hr, y_off, hx - hr, y_off + hz, layer="FRONT_HIDDEN_HOLES", color=8)
        add_line(hx + hr, y_off, hx + hr, y_off + hz, layer="FRONT_HIDDEN_HOLES", color=8)

    # 3. Side View (YZ placed to the right of Top View at X_offset = lx/2 + gap)
    x_off = bbox["x_max"] + gap
    y0, y1 = bbox["y_min"], bbox["y_max"]
    add_polyline_2d(
        [[x_off, y0], [x_off + hz, y0], [x_off + hz, y1], [x_off, y1], [x_off, y0]],
        layer="SIDE_VIEW",
        color=4,
    )

    lines.extend(["0", "ENDSEC", "0", "EOF"])
    with open(dxf_path, "w", encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    return dxf_path


def render_engineering_blueprint(aligned_pts, features, mesh_stats, title_name, out_prefix):
    """
    Render a multi-view 2D/3D Engineering Drawing Blueprint (.png, .pdf, .svg) with:
      - Front View (XZ) + dimensional callouts
      - Top View (XY) + outer contour + hole diameter & coordinate callouts
      - Left Side View (YZ) + dimensional callouts
      - Multi-height Cross-Section Overlay (20%, 50%, 80% Z)
      - 3D Isometric Point Cloud / Surface View
      - Standard Engineering Title Block
    """
    png_path = f"{out_prefix}_blueprint.png"
    pdf_path = f"{out_prefix}_blueprint.pdf"
    svg_path = f"{out_prefix}_blueprint.svg"

    bbox = features["bounding_box_mm"]
    lx, wy, hz = bbox["length_x"], bbox["width_y"], bbox["height_z"]

    # Subsample points for fast scatter plotting
    rng = np.random.default_rng(42)
    plot_pts = aligned_pts
    if len(plot_pts) > 18000:
        plot_pts = plot_pts[rng.choice(len(plot_pts), size=18000, replace=False)]

    fig = plt.figure(figsize=(16, 11), dpi=180, facecolor="#F8FAFC")
    gs = fig.add_gridspec(2, 3, width_ratios=[1.15, 1.15, 1.2], height_ratios=[1.0, 1.0], hspace=0.28, wspace=0.26)

    # --- Panel 1: Front View (XZ) [Top-Left] ---
    ax_front = fig.add_subplot(gs[0, 0])
    ax_front.scatter(plot_pts[:, 0], plot_pts[:, 2], c=plot_pts[:, 1], cmap="Blues", s=1.2, alpha=0.55)
    rect_front = Rectangle((bbox["x_min"], bbox["z_min"]), lx, hz, fill=False, edgecolor="#0F172A", lw=1.5, ls="--")
    ax_front.add_patch(rect_front)
    # Dimension annotations
    ax_front.annotate(
        f"L = {lx:.2f} mm",
        xy=(0.0, bbox["z_max"] + 0.06 * max(lx, hz)),
        ha="center", va="bottom", fontsize=10, fontweight="bold", color="#B91C1C",
    )
    ax_front.annotate(
        f"H = {hz:.2f} mm",
        xy=(bbox["x_max"] + 0.05 * max(lx, hz), 0.5 * hz),
        ha="left", va="center", rotation=90, fontsize=10, fontweight="bold", color="#B91C1C",
    )
    ax_front.set_title("主视图 Front View (X-Z)", fontsize=12, fontweight="bold", pad=12)
    ax_front.set_xlabel("X (mm)")
    ax_front.set_ylabel("Z (mm)")
    ax_front.set_aspect("equal", adjustable="datalim")
    ax_front.grid(True, linestyle=":", alpha=0.5)

    # --- Panel 2: Left Side View (YZ) [Top-Middle] ---
    ax_side = fig.add_subplot(gs[0, 1])
    ax_side.scatter(plot_pts[:, 1], plot_pts[:, 2], c=plot_pts[:, 0], cmap="Teal", s=1.2, alpha=0.55) if "Teal" in plt.colormaps() else ax_side.scatter(plot_pts[:, 1], plot_pts[:, 2], c=plot_pts[:, 0], cmap="viridis", s=1.2, alpha=0.55)
    rect_side = Rectangle((bbox["y_min"], bbox["z_min"]), wy, hz, fill=False, edgecolor="#0F172A", lw=1.5, ls="--")
    ax_side.add_patch(rect_side)
    ax_side.annotate(
        f"W = {wy:.2f} mm",
        xy=(0.0, bbox["z_max"] + 0.06 * max(wy, hz)),
        ha="center", va="bottom", fontsize=10, fontweight="bold", color="#047857",
    )
    ax_side.set_title("左视图 Side View (Y-Z)", fontsize=12, fontweight="bold", pad=12)
    ax_side.set_xlabel("Y (mm)")
    ax_side.set_ylabel("Z (mm)")
    ax_side.set_aspect("equal", adjustable="datalim")
    ax_side.grid(True, linestyle=":", alpha=0.5)

    # --- Panel 3: Top View (XY) + Outer Contour + Hole Callouts [Bottom-Left] ---
    ax_top = fig.add_subplot(gs[1, 0])
    ax_top.scatter(plot_pts[:, 0], plot_pts[:, 1], c=plot_pts[:, 2], cmap="winter", s=1.0, alpha=0.35)
    contour_np = np.array(features["outer_contour_xy"])
    ax_top.plot(contour_np[:, 0], contour_np[:, 1], color="#1E293B", lw=2.0, label="外轮廓 (Outer Profile)")
    for idx, h in enumerate(features["detected_holes"], start=1):
        circ = MplCircle((h["center_x_mm"], h["center_y_mm"]), h["radius_mm"], fill=False, edgecolor="#DC2626", lw=1.8)
        ax_top.add_patch(circ)
        ax_top.plot(h["center_x_mm"], h["center_y_mm"], "+", color="#DC2626", markersize=7)
        ax_top.annotate(
            f"H{idx}: Ø{h['diameter_mm']:.2f}",
            xy=(h["center_x_mm"], h["center_y_mm"]),
            xytext=(h["center_x_mm"] + h["radius_mm"] + 2.0, h["center_y_mm"] + 2.0),
            fontsize=8.5, fontweight="bold", color="#DC2626",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#DC2626", alpha=0.85),
        )
    ax_top.set_title(f"俯视图 Top View (X-Y) | {lx:.2f} × {wy:.2f} mm", fontsize=12, fontweight="bold", pad=10)
    ax_top.set_xlabel("X (mm)")
    ax_top.set_ylabel("Y (mm)")
    ax_top.set_aspect("equal", adjustable="datalim")
    ax_top.grid(True, linestyle=":", alpha=0.5)

    # --- Panel 4: Multi-Height Z Cross-Section Slices [Bottom-Middle] ---
    ax_sec = fig.add_subplot(gs[1, 1])
    sec_colors = ["#2563EB", "#D97706", "#7C3AED"]
    for s_idx, sec in enumerate(features["cross_sections"]):
        c_pts = np.array(sec["contour_xy"])
        col = sec_colors[s_idx % len(sec_colors)]
        ax_sec.plot(
            c_pts[:, 0], c_pts[:, 1],
            lw=1.8, color=col,
            label=f"截面 Z={sec['z_mm']:.1f}mm ({int(sec['z_ratio']*100)}%)"
        )
    ax_sec.plot(contour_np[:, 0], contour_np[:, 1], color="#64748B", lw=1.0, ls="--", label="最大包络轮廓")
    ax_sec.set_title("多高度横截面轮廓图 Cross-Section Profiles", fontsize=12, fontweight="bold", pad=10)
    ax_sec.set_xlabel("X (mm)")
    ax_sec.set_ylabel("Y (mm)")
    ax_sec.set_aspect("equal", adjustable="datalim")
    ax_sec.legend(loc="upper right", fontsize=8.5)
    ax_sec.grid(True, linestyle=":", alpha=0.5)

    # --- Panel 5: 3D Isometric View [Top-Right] ---
    ax_3d = fig.add_subplot(gs[0, 2], projection="3d")
    p3d = plot_pts[::max(1, len(plot_pts) // 6000)]
    sc = ax_3d.scatter(p3d[:, 0], p3d[:, 1], p3d[:, 2], c=p3d[:, 2], cmap="coolwarm", s=1.5, alpha=0.8)
    ax_3d.set_title("3D 等轴测点云/模型视图 Isometric View", fontsize=12, fontweight="bold", pad=10)
    ax_3d.set_xlabel("X")
    ax_3d.set_ylabel("Y")
    ax_3d.set_zlabel("Z")
    plt.colorbar(sc, ax=ax_3d, shrink=0.65, pad=0.08, label="Z Height (mm)")

    # --- Panel 6: Engineering Specification & Title Block [Bottom-Right] ---
    ax_info = fig.add_subplot(gs[1, 2])
    ax_info.axis("off")
    hole_lines = []
    for i, h in enumerate(features["detected_holes"][:5], start=1):
        hole_lines.append(f"  • 孔 H{i}: Ø{h['diameter_mm']:.2f} mm @ (X={h['center_x_mm']:.2f}, Y={h['center_y_mm']:.2f})")
    if not hole_lines:
        hole_lines.append("  • 未检测到明显圆形通孔特征")

    outer_type_str = (
        f"回转圆柱特征 (外径 Ø{features['outer_cylinder']['diameter_mm']:.2f} mm)"
        if features["is_outer_cylindrical"] and features["outer_cylinder"]
        else f"多边形/自由曲面棱柱 (简化外轮廓 {len(features['outer_contour_xy'])-1} 顶点)"
    )

    info_text = (
        f"【工程图纸技术规格栏 / ENGINEERING TITLE BLOCK】\n"
        f"──────────────────────────────────────────────\n"
        f"零件名称 (Part Name) : {title_name}\n"
        f"生成时间 (Date)      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"扫描设备 (Scanner)   : Revopoint MetroX / MetroX Pro\n"
        f"单位制式 (Units)     : 毫米 (mm) | 第三角投影\n"
        f"──────────────────────────────────────────────\n"
        f"【核心包络尺寸 / Overall Dimensions】\n"
        f"  • 总长度 (Length X): {lx:.3f} mm\n"
        f"  • 总宽度 (Width Y) : {wy:.3f} mm\n"
        f"  • 总高度 (Height Z): {hz:.3f} mm\n"
        f"  • 外形类别 (Profile): {outer_type_str}\n"
        f"──────────────────────────────────────────────\n"
        f"【检测到的孔位/圆特征 / Cylindrical Features】\n"
        + "\n".join(hole_lines) + "\n"
        f"──────────────────────────────────────────────\n"
        f"【网格与体积统计 / Mesh & Volume Stats】\n"
        f"  • 顶点数 / 面片数 : {mesh_stats['vertex_count']:,} pts / {mesh_stats['facet_count']:,} tris\n"
        f"  • 表面积 (Area)   : {mesh_stats['area_mm2']:,.2f} mm²\n"
        f"  • 导出格式清单    : STEP, IGES, DXF, STL, OBJ, PLY, SVG, PDF"
    )
    ax_info.text(
        0.03, 0.97, info_text,
        transform=ax_info.transAxes,
        va="top", ha="left",
        fontsize=9.5,
        family="sans-serif",
        bbox=dict(boxstyle="round,pad=0.6", facecolor="#FFFFFF", edgecolor="#334155", lw=1.5),
    )

    fig.suptitle(
        f"Revopoint MetroX AI 逆向工程图纸 — {title_name}",
        fontsize=15, fontweight="bold", color="#0F172A", y=0.97
    )

    fig.savefig(png_path, bbox_inches="tight")
    fig.savefig(pdf_path, bbox_inches="tight")
    fig.savefig(svg_path, bbox_inches="tight")
    plt.close(fig)

    return {
        "png": png_path,
        "pdf": pdf_path,
        "svg": svg_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Revo Metro 3D Scan-to-CAD & Engineering Drawing Pipeline")
    parser.add_argument("--input", help="输入的 .ply / .stl / .obj / .revo 文件路径")
    parser.add_argument("--project", help="Revo Metro 工程名称 (自动从 %APPDATA%\\RevoMetro\\Projects 查找)")
    parser.add_argument("--node", help="指定工程内的扫描节点名称或 GUID (默认自动选最优节点)")
    parser.add_argument("--out-dir", default=r"d:\source\revopoint\output", help="输出目录路径")
    parser.add_argument("--name", help="自定义输出文件名前缀")
    parser.add_argument("--remove-base", action="store_true", help="自动通过 RANSAC 拟合切除底部桌面/转盘平面")
    parser.add_argument("--no-isolate", action="store_true", help="禁用连通域孤立点/外围标志块自动剔除")
    parser.add_argument("--keep-markers", action="store_true", help="保留孤立的标志点贴纸圆片簇 (默认剔除)")
    parser.add_argument("--marker-max-mm", type=float, default=12.0, help="标志点圆片簇的最大外径 (mm)，默认 12")
    args = parser.parse_args()

    input_file, auto_name = resolve_input_file(args)
    base_name = args.name if args.name else auto_name
    os.makedirs(args.out_dir, exist_ok=True)
    out_prefix = os.path.join(os.path.abspath(args.out_dir), base_name)

    # 1. Load 3D scan data
    pts, orig_mesh = load_points_and_mesh(input_file)

    # 2. Clean point cloud (SOR + optional base plane removal + connected component isolation)
    cleaned_pts, clean_stats = clean_point_cloud(
        pts, remove_base=args.remove_base, isolate=(not args.no_isolate),
        remove_markers=(not args.keep_markers), marker_max_mm=args.marker_max_mm
    )

    # 3. PCA Coordinate Alignment (flat base on XY plane, Z >= 0, longest axis on X)
    aligned_pts, R, shift_pre, shift_post = align_points_pca(cleaned_pts)

    # 4. Extract Engineering Features (bounding dimensions, cross-sections, holes, cylinder fit)
    features = extract_engineering_features(aligned_pts)

    # 5. Export Cleaned Meshes (.stl, .ply, .obj)
    mesh_outputs = export_cleaned_meshes(aligned_pts, orig_mesh, R, shift_pre, shift_post, out_prefix)

    # 6. Reconstruct & Export 3D CAD Solids (.step, .iges, _parametric_cad.py)
    cad_outputs = build_and_export_cad_solid(features, out_prefix)

    # 7. Export 2D/3D Engineering Drawings (.dxf, _blueprint.png, .pdf, .svg)
    dxf_path = export_dxf_drawing(features, out_prefix)
    blueprint_outputs = render_engineering_blueprint(aligned_pts, features, mesh_outputs, base_name, out_prefix)

    # 8. Save full structured engineering report JSON
    report_path = f"{out_prefix}_dimensions.json"
    report = {
        "source_file": input_file,
        "part_name": base_name,
        "cleaning_stats": clean_stats,
        "features": features,
        "outputs": {
            "cleaned_meshes": mesh_outputs,
            "cad_solids": cad_outputs,
            "dxf_drawing": dxf_path,
            "blueprints": blueprint_outputs,
            "dimensions_json": report_path,
        },
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
