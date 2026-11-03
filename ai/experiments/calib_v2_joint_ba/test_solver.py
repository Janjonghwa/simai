"""Numerical regression: run in .venv-solver, with no torch/av/lightglue required.

No skip on missing numerical libraries: an import failure is a blocked test, not a pass.
"""

import json
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from control import CAMS, PAIRS
from numeric import Problem, ProjectedProblem, evaluate, project


def synthetic_scene():
    config = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    centers = np.array([[0., 0., 2.2], [6., 0., 2.2], [6., 6., 2.2], [0., 6., 2.2]])
    target = np.array([3., 3., 1.])
    rotations = []
    for center in centers:
        forward = target - center
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, [0., 0., 1.])
        right /= np.linalg.norm(right)
        rotations.append(np.vstack((right, np.cross(forward, right), forward)))
    rotations = np.array(rotations)
    intrinsics = np.tile([[1000., 0., 1152.], [0., 1000., 648.], [0., 0., 1.]], (4, 1, 1))
    config["heights_m"] = dict.fromkeys(CAMS, 2.2)
    rng = np.random.default_rng(449)
    tracks = []
    for a, b in PAIRS:
        points = rng.uniform([1., 1., 0.2], [5., 5., 2.], (32, 3))
        pixels = np.stack([project(points, c, intrinsics, rotations, centers)[0] for c in (a, b)], axis=1)
        for i, uv in enumerate(pixels):
            tracks.append({"id": f"{a}-{b}:{i}", "pair": [a, b], "uv": uv.tolist(),
                           "nodes": [[a, 1000 * b + i], [b, 1000 * a + i]],
                           "split": "holdout" if i % 4 == 0 else "train"})
    initial = rotations.copy()
    initial[1] = Rotation.from_euler("x", 2.5, degrees=True).as_matrix() @ initial[1]
    return config, tracks, intrinsics, rotations, initial, centers


class RotationSolverTests(unittest.TestCase):
    def test_stage_a_undoes_known_camera_rotation(self):
        config, tracks, K, truth, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        problem = Problem(train, K, initial, centers, 3, config, None)
        before = evaluate(tracks, K, initial, centers)
        R, C, result, info = problem.solve(initial, centers)
        after = evaluate(tracks, K, R, C)
        moved = np.degrees(Rotation.from_matrix(R[1] @ initial[1].T).magnitude())
        error = np.degrees(Rotation.from_matrix(R[1] @ truth[1].T).magnitude())
        self.assertTrue(result.success, result.message)
        self.assertGreater(moved, 1., "solver reports success without moving the perturbed camera")
        self.assertLess(error, 0.1, "Stage A must recover the known camera orientation")
        self.assertLess(info["final_cost"]["total"], 0.1 * info["initial_cost"]["total"])
        self.assertLess(after["pairs"]["cam_A-cam_B"]["median"],
                        0.1 * before["pairs"]["cam_A-cam_B"]["median"])
        np.testing.assert_array_equal(C, centers)
        np.testing.assert_array_equal(R[3], initial[3])

    def test_sparsity_covers_numerical_residual_dependencies(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        # Include rotation/center/height/floor/depth rows and an active depth barrier.
        train = [next(t for t in tracks if t["pair"] == list(pair) and t["split"] == "train")
                 for pair in PAIRS]
        config["confirmed_floor_tracks"] = [train[0]["id"]]
        for sigma in (None, config["center_sigma_B_m"]):
            with self.subTest(center_sigma=sigma):
                problem = Problem(train, K, initial, centers, 3, config, sigma)
                x = problem.pack(initial, centers)
                # Move the first point behind both of its cameras.
                x[problem.nc:problem.nc + 3] = [3., -30., 1.]
                pattern = problem.sparsity().toarray().astype(bool)
                for col in range(len(x)):
                    xp, xm = x.copy(), x.copy()
                    xp[col] += 1e-6
                    xm[col] -= 1e-6
                    derivative = (problem.residual(xp) - problem.residual(xm)) / 2e-6
                    self.assertFalse(np.any((np.abs(derivative) > 1e-4) & ~pattern[:, col]),
                                     f"missing Jacobian dependency in column {col}")


class ProjectedSolverTests(unittest.TestCase):
    def test_projected_stage_a_undoes_2_5_degree_camera_rotation(self):
        config, tracks, K, truth, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        problem = ProjectedProblem(train, K, initial, centers, 3, config, None)
        before = evaluate(tracks, K, initial, centers)
        R, C, result, info = problem.solve(initial, centers)
        after = evaluate(tracks, K, R, C)
        moved = np.degrees(Rotation.from_matrix(R[1] @ initial[1].T).magnitude())
        error = np.degrees(Rotation.from_matrix(R[1] @ truth[1].T).magnitude())
        self.assertTrue(result.success, result.message)
        self.assertFalse(info["adoption_forbidden"], info["adoption_blockers"])
        self.assertGreater(moved, 2., "projected solver stalled at its initial camera")
        self.assertLess(error, 0.1)
        self.assertLess(info["final_cost"]["total"], 0.1 * info["initial_cost"]["total"])
        self.assertLess(after["pairs"]["cam_B-cam_C"]["median"],
                        0.1 * before["pairs"]["cam_B-cam_C"]["median"])
        self.assertTrue(info["point_projection"]["converged"])
        self.assertLess(info["point_projection"]["max_point_gradient"], config["projected_point_gradient_tol"])
        # A residual finite-difference Jacobian would add 9 inner fits per njev.
        self.assertEqual(info["residual_calls"], result.nfev)
        np.testing.assert_array_equal(C, centers)
        np.testing.assert_array_equal(R[3], initial[3])

    def test_camera_only_solve_matches_joint_holdout(self):
        config, tracks, K, truth, initial, centers = synthetic_scene()
        # Noisy fit/holdout pixels; the two paths must optimize comparable objectives.
        rng = np.random.default_rng(101)
        for track in tracks:
            track["uv"] = (np.asarray(track["uv"]) + rng.normal(0, 0.15, (2, 2))).tolist()
        train = [t for t in tracks if t["split"] == "train"]
        for sigma in (None, config["center_sigma_B_m"]):
            with self.subTest(sigma=sigma):
                joint = Problem(train, K, initial, centers, 3, config, sigma)
                projected = ProjectedProblem(train, K, initial, centers, 3, config, sigma)
                self.assertEqual(len(projected.pack(initial, centers)), 9 if sigma is None else 18)
                Rj, Cj, rj, _ = joint.solve(initial, centers)
                Rp, Cp, rp, info = projected.solve(initial, centers)
                self.assertTrue(rj.success, rj.message)
                self.assertTrue(rp.success, rp.message)
                self.assertFalse(info["adoption_forbidden"], info["adoption_blockers"])
                self.assertGreater(info["residual_calls"], 0)
                self.assertGreater(info["seconds"], 0)
                self.assertTrue(info["point_projection"]["converged"])
                self.assertLess(info["final_cost"]["total"], info["initial_cost"]["total"])
                for pair in evaluate(tracks, K, Rj, Cj)["pairs"]:
                    a = evaluate(tracks, K, Rj, Cj)["pairs"][pair]["median"]
                    b = evaluate(tracks, K, Rp, Cp)["pairs"][pair]["median"]
                    self.assertLessEqual(abs(a - b), 0.05)
                self.assertLess(np.degrees(Rotation.from_matrix(Rp[1] @ truth[1].T).magnitude()), 0.1)
                np.testing.assert_array_equal(Rp[3], initial[3])
                np.testing.assert_array_equal(Cp[3], centers[3])
                if sigma is None:
                    np.testing.assert_array_equal(Cp, centers)

    def test_projected_point_gradient_and_joint_objective_agree(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        p = ProjectedProblem(train, K, initial, centers, 3, config, 0.02)
        x = p.pack(initial, centers)
        R, C, xyz = p.unpack(x)
        _, residual, jac = p.point_system(xyz, R, C)
        weights = p.point_weights(residual)
        gradient = np.einsum("nri,nr->ni", jac, weights * residual)
        self.assertLess(np.max(np.abs(gradient)), config["projected_point_gradient_tol"])
        joint = Problem(train, K, initial, centers, 3, config, 0.02)
        full = np.concatenate((x, xyz.ravel()))
        np.testing.assert_allclose(p.residual(x), joint.residual(full), atol=1e-9)
        for axis in range(3):
            plus, minus = xyz.copy(), xyz.copy()
            plus[:, axis] += 1e-6
            minus[:, axis] -= 1e-6
            numeric = (p.point_system(plus, R, C)[1] - p.point_system(minus, R, C)[1]) / 2e-6
            np.testing.assert_allclose(jac[:, :, axis], numeric, rtol=1e-5, atol=1e-5)

    def test_point_hessian_and_camera_cross_derivatives(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        config["confirmed_floor_tracks"] = [train[0]["id"]]
        p = ProjectedProblem(train, K, initial, centers, 3, config, 0.02)
        x = p.pack(initial, centers)
        # Nonzero rotvec tests the SO(3) left Jacobian, not just its identity at 0.
        x[:3] = [0.003, -0.004, 0.002]
        R, C = p.camera_poses(x)
        xyz = p.points_at_poses(R, C)
        xyz[0] = [3., -30., 1.]  # floor + active depth barriers
        _, residual, jp = p.point_system(xyz, R, C)
        self.assertTrue(np.any(p.point_weights(residual)[:, :4] < 1))
        self.assertTrue(np.any(residual[:, 4:6] < 0))
        hessian = p.point_hessian(xyz, R, C, residual, jp)
        jc, cross = p.fixed_point_camera_system(x, xyz, R, C, residual, jp)
        def point_gradient(points, rp, cp):
            _, r, j = p.point_system(points, rp, cp)
            return np.einsum("nri,nr->ni", j, p.point_weights(r) * r)
        for axis in range(3):
            plus, minus = xyz.copy(), xyz.copy()
            plus[:, axis] += 1e-6
            minus[:, axis] -= 1e-6
            numeric = (point_gradient(plus, R, C) - point_gradient(minus, R, C)) / 2e-6
            np.testing.assert_allclose(hessian[:, :, axis], numeric, rtol=2e-4, atol=2e-3)
        for column in range(p.nc):
            plus, minus = x.copy(), x.copy()
            plus[column] += 1e-6
            minus[column] -= 1e-6
            rp, cp = p.camera_poses(plus)
            rm, cm = p.camera_poses(minus)
            numeric = (p.point_system(xyz, rp, cp)[1] - p.point_system(xyz, rm, cm)[1]) / 2e-6
            np.testing.assert_allclose(jc[:, :, column], numeric, rtol=1e-5, atol=1e-5)
            numeric_cross = (point_gradient(xyz, rp, cp) - point_gradient(xyz, rm, cm)) / 2e-6
            np.testing.assert_allclose(cross[:, :, column], numeric_cross, rtol=2e-4, atol=2e-3)

    def test_profiled_jacobian_matches_residual_and_envelope_gradient(self):
        config, tracks, K, truth, _, centers = synthetic_scene()
        rng = np.random.default_rng(4404)
        for track in tracks:
            track["uv"] = (np.asarray(track["uv"]) + rng.normal(0, 0.15, (2, 2))).tolist()
        train = [t for t in tracks if t["split"] == "train"]
        # A single saturated component checks the Huber curvature in the implicit fit.
        train[0]["uv"][0][0] += 15.
        for sigma in (None, 0.02):
            with self.subTest(sigma=sigma):
                p = ProjectedProblem(train, K, truth, centers, 3, config, sigma)
                x = p.pack(truth, centers)
                x[:3] = [0.0003, -0.0004, 0.0002]
                residual = p.residual(x)
                jac = p.profiled_jacobian(x)
                R, C, xyz = p.unpack(x)
                _, local_r, jp = p.point_system(xyz, R, C)
                fixed, _ = p.fixed_point_camera_system(x, xyz, R, C, local_r, jp)
                weights = p.loss(residual ** 2)[1]
                gradient = jac.T @ (weights * residual)
                prior_rows = len(p.free) * p.dof + 4
                camera_priors = slice(4 * len(train), 4 * len(train) + prior_rows)
                envelope = np.einsum("nrj,nr->j", fixed, p.point_weights(local_r) * local_r)
                envelope += jac[camera_priors].T @ residual[camera_priors]
                np.testing.assert_allclose(gradient, envelope, rtol=1e-5, atol=1e-3)
                self.assertEqual(p.projection_info["schur_tangent_fallback_points"], 0)
                for column in range(p.nc):
                    plus, minus = x.copy(), x.copy()
                    plus[column] += 1e-5
                    minus[column] -= 1e-5
                    numeric = (p.residual(plus) - p.residual(minus)) / 2e-5
                    np.testing.assert_allclose(jac[:, column], numeric, rtol=3e-4, atol=3e-3)
                # Re-evaluation order must not alter the starting fit or stationarity.
                np.testing.assert_allclose(p.residual(x), residual, rtol=0, atol=1e-12)

    def test_point_budget_exhaustion_cannot_be_reported_as_converged(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        config["projected_point_max_iterations"] = 0
        config["projected_point_fallback_nfev"] = 1
        config["max_nfev"] = 1
        train = [t for t in tracks if t["split"] == "train"]
        p = ProjectedProblem(train, K, initial, centers, 3, config, None)
        _, _, _, info = p.solve(initial, centers)
        self.assertTrue(info["adoption_forbidden"])
        self.assertIn("point_projection_not_converged", info["adoption_blockers"])
        self.assertFalse(info["point_projection"]["converged"])
        self.assertGreater(info["point_projection"]["unconverged_points"], 0)

    def test_budget_exhaustion_is_marked_adoption_forbidden(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        config["max_nfev"] = 1
        train = [t for t in tracks if t["split"] == "train"]
        for cls in (Problem, ProjectedProblem):
            with self.subTest(solver=cls.__name__):
                p = cls(train, K, initial, centers, 3, config, None)
                _, _, result, info = p.solve(initial, centers)
                self.assertFalse(result.success)
                self.assertTrue(info["adoption_forbidden"])
                self.assertIn("solver_not_converged", info["adoption_blockers"])

    def test_point_jacobian_includes_floor_and_active_depth_barrier(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        config["confirmed_floor_tracks"] = [train[0]["id"]]
        p = ProjectedProblem(train, K, initial, centers, 3, config, 0.02)
        xyz = p.points_at_poses(initial, centers)
        xyz[0] = [3., -30., 1.]
        _, residual, jac = p.point_system(xyz, initial, centers)
        self.assertTrue(np.any(residual[:, 4:6] < 0))
        self.assertNotEqual(residual[0, 6], 0)
        for axis in range(3):
            plus, minus = xyz.copy(), xyz.copy()
            plus[:, axis] += 1e-6
            minus[:, axis] -= 1e-6
            numeric = (p.point_system(plus, initial, centers)[1] - p.point_system(minus, initial, centers)[1]) / 2e-6
            np.testing.assert_allclose(jac[:, :, axis], numeric, rtol=1e-5, atol=1e-5)

    def test_block_observability_matches_dense_full_ba_elimination(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for pair in PAIRS for t in
                 [t for t in tracks if t["pair"] == list(pair) and t["split"] == "train"][:4]]
        config["confirmed_floor_tracks"] = [train[0]["id"]]
        p = Problem(train, K, initial, centers, 3, config, 0.02)
        x = p.pack(initial, centers)
        x[p.nc:p.nc + 3] = [3., -30., 1.]
        jac = np.empty((len(p.residual(x)), len(x)))
        for j in range(len(x)):
            xp, xm = x.copy(), x.copy()
            xp[j] += 1e-6
            xm[j] -= 1e-6
            jac[:, j] = (p.residual(xp) - p.residual(xm)) / 2e-6 * 0.01
        diagnostics = p.observability(x)
        for name, rows in (("data_only_schur", 4 * len(train)), ("with_quadratic_priors_schur", len(jac))):
            jp, jc = jac[:rows, p.nc:], jac[:rows, :p.nc]
            reduced = jc - jp @ np.linalg.lstsq(jp, jc, rcond=1e-10)[0]
            np.testing.assert_allclose(diagnostics[name]["singular_values"],
                                       np.linalg.svd(reduced, compute_uv=False), rtol=1e-5, atol=1e-5)


class HeightPriorTests(unittest.TestCase):
    def test_height_is_quadratic_and_has_correct_center_derivative(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        for cls in (Problem, ProjectedProblem):
            with self.subTest(solver=cls.__name__):
                p = cls(train, K, initial, centers, 3, config, 0.02)
                x = p.pack(initial, centers)
                column = p.free.index(1) * p.dof + 5
                x[column] += 0.037
                row = 4 * len(train) + 6 * len(p.free) + 1
                residual = p.residual(x)
                self.assertAlmostEqual(residual[row], 3.7)
                rho = p.loss(residual ** 2)
                self.assertAlmostEqual(0.5 * rho[0, row], 6.845)
                self.assertEqual(rho[1, row], 1.)
                self.assertEqual(rho[2, row], 0.)
                xp, xm = x.copy(), x.copy()
                xp[column] += 1e-6
                xm[column] -= 1e-6
                derivative = (p.residual(xp)[row] - p.residual(xm)[row]) / 2e-6
                self.assertAlmostEqual(derivative, 100., places=5)
                audit = p.height_prior_summary(x)
                self.assertTrue(audit["cameras"]["cam_B"]["optimized"])
                self.assertAlmostEqual(audit["cameras"]["cam_B"]["residual_sigma"], 3.7)
                self.assertFalse(audit["cameras"]["cam_D"]["optimized"])

    def test_stage_a_heights_are_constant(self):
        config, tracks, K, _, initial, centers = synthetic_scene()
        train = [t for t in tracks if t["split"] == "train"]
        config["heights_m"]["cam_B"] -= 0.037
        for cls in (Problem, ProjectedProblem):
            p = cls(train, K, initial, centers, 3, config, None)
            x = p.pack(initial, centers)
            row = 4 * len(train) + 3 * len(p.free) + 1
            self.assertAlmostEqual(p.residual(x)[row], 3.7)
            x[:3] += 0.001
            self.assertAlmostEqual(p.residual(x)[row], 3.7)
            self.assertFalse(p.height_prior_summary(x)["cameras"]["cam_B"]["optimized"])


if __name__ == "__main__":
    unittest.main()
