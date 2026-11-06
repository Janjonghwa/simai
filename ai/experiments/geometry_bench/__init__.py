"""Geometry & Texture Benchmark Suite (S15P31A101-528).

Compares 5 observation-only geometry & texture pipelines (Variants 0..4)
without any generative inpainting/hallucination (AGENTS.md Rules 2 & 7):
- Variant 0: Baseline (MapAnything 518 single-frame depth, TSDF 4 cm, 150k faces, texrecon)
- Variant 1: Fine + Multi-frame (Multi-frame MapAnything median depth, TSDF 1.5~2 cm, 500k+ faces, texrecon)
- Variant 2: Variant 1 + DA3 1008 hybrid depth (MapAnything pose fixed, sim3 + horizontal plane cleanup)
- Variant 3: Variant 1 mesh + OpenMVS RefineMesh (photometric surface refinement) + texrecon
- Variant 4: OpenMVS ReconstructMesh -> RefineMesh -> TextureMesh (replacing TSDF + texrecon)
"""
