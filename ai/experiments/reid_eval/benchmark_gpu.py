"""COPE 외형 임베더 GPU(RTX 4050) 추론 지연시간(배치 1·8·16) 및 VRAM 메모리 측정 (S15P31A101-463)."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from simai_ai.reid import COPEConfig, COPEEmbedder


def _percentiles_ms(samples_ms: Sequence[float]) -> dict[str, float]:
    arr = np.asarray(samples_ms, dtype=np.float64)
    p50, p95, p99 = np.percentile(arr, [50, 95, 99])
    return {
        "p50": round(float(p50), 3),
        "p95": round(float(p95), 3),
        "p99": round(float(p99), 3),
        "mean": round(float(np.mean(arr)), 3),
        "min": round(float(np.min(arr)), 3),
        "max": round(float(np.max(arr)), 3),
    }


def benchmark_cope_embedder(
    batch_sizes: Sequence[int] = (1, 8, 16),
    *,
    warmup_iters: int = 10,
    measure_iters: int = 40,
    device: str = "auto",
) -> dict[str, Any]:
    """배치 크기(1·8·16)별 COPE 추론 시간(ms)과 GPU 메모리(MB)를 측정한다."""
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

    cfg = COPEConfig.from_env(device=device, batch_size=max(batch_sizes))
    t_load0 = time.perf_counter()
    embedder = COPEEmbedder(cfg)
    if embedder.device.startswith("cuda"):
        torch.cuda.synchronize()
    load_time_ms = round((time.perf_counter() - t_load0) * 1000.0, 1)

    model_allocated_mb = (
        round(torch.cuda.memory_allocated() / (1024 * 1024), 2)
        if embedder.device.startswith("cuda")
        else 0.0
    )

    rng = np.random.default_rng(20261104)
    max_bs = max(batch_sizes)
    sample_crops = [
        rng.integers(0, 255, size=(240, 110, 3), dtype=np.uint8)
        for _ in range(max_bs)
    ]

    by_batch: dict[str, Any] = {}
    stage_latency_ms: dict[str, dict[str, float]] = {}
    peak_vram_overall_mb = model_allocated_mb

    for bs in batch_sizes:
        crops_bs = sample_crops[:bs]
        embedder_bs = COPEEmbedder(
            COPEConfig.from_env(device=embedder.device, batch_size=bs),
            model=embedder._model,
        )
        embedder_bs._device = embedder.device

        for _ in range(warmup_iters):
            _ = embedder_bs.embed(crops_bs)
        if embedder.device.startswith("cuda"):
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()

        e2e_times_ms: list[float] = []
        for _ in range(measure_iters):
            if embedder.device.startswith("cuda"):
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            _ = embedder_bs.embed(crops_bs)
            if embedder.device.startswith("cuda"):
                torch.cuda.synchronize()
            e2e_times_ms.append((time.perf_counter() - t0) * 1000.0)

        peak_alloc_mb = (
            round(torch.cuda.max_memory_allocated() / (1024 * 1024), 2)
            if embedder.device.startswith("cuda")
            else 0.0
        )
        peak_res_mb = (
            round(torch.cuda.max_memory_reserved() / (1024 * 1024), 2)
            if embedder.device.startswith("cuda")
            else 0.0
        )

        # PSS 품질 점수 포함 경로(embed_with_scores)도 측정한다.
        for _ in range(max(3, warmup_iters // 2)):
            _ = embedder_bs.embed_with_scores(crops_bs)
        if embedder.device.startswith("cuda"):
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()

        pss_times_ms: list[float] = []
        for _ in range(max(15, measure_iters // 2)):
            if embedder.device.startswith("cuda"):
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            _ = embedder_bs.embed_with_scores(crops_bs)
            if embedder.device.startswith("cuda"):
                torch.cuda.synchronize()
            pss_times_ms.append((time.perf_counter() - t0) * 1000.0)

        peak_alloc_pss_mb = (
            round(torch.cuda.max_memory_allocated() / (1024 * 1024), 2)
            if embedder.device.startswith("cuda")
            else 0.0
        )
        peak_vram_overall_mb = max(peak_vram_overall_mb, peak_alloc_mb, peak_alloc_pss_mb)

        e2e_stats = _percentiles_ms(e2e_times_ms)
        pss_stats = _percentiles_ms(pss_times_ms)
        stage_latency_ms[f"cope_embed_b{bs}"] = {
            "p50": e2e_stats["p50"],
            "p95": e2e_stats["p95"],
            "p99": e2e_stats["p99"],
        }
        stage_latency_ms[f"cope_embed_pss_b{bs}"] = {
            "p50": pss_stats["p50"],
            "p95": pss_stats["p95"],
            "p99": pss_stats["p99"],
        }

        by_batch[str(bs)] = {
            "batch_size": bs,
            "embed_ms": e2e_stats,
            "embed_per_crop_p50_ms": round(e2e_stats["p50"] / bs, 3),
            "embed_throughput_crops_per_s": round((bs * 1000.0) / max(e2e_stats["mean"], 1e-6), 1),
            "embed_with_pss_ms": pss_stats,
            "embed_with_pss_per_crop_p50_ms": round(pss_stats["p50"] / bs, 3),
            "vram_peak_allocated_mb": peak_alloc_mb,
            "vram_peak_allocated_with_pss_mb": peak_alloc_pss_mb,
            "vram_peak_reserved_mb": peak_res_mb,
        }

    return {
        "device": embedder.device,
        "gpu_name": torch.cuda.get_device_name(0) if embedder.device.startswith("cuda") else "CPU",
        "load_time_ms": load_time_ms,
        "model_allocated_mb": model_allocated_mb,
        "vram_peak_mb": peak_vram_overall_mb,
        "batches": by_batch,
        "stage_latency_ms": stage_latency_ms,
    }
