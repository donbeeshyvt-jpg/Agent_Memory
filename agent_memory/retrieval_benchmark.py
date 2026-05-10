"""Reusable retrieval benchmark runner for parameter tuning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

from agent_memory.runtime import MemoryRuntime


@dataclass(slots=True)
class BenchmarkCase:
    query: str
    expected_paths: list[str]
    expected_keywords: list[str]


@dataclass(slots=True)
class BenchmarkVariant:
    name: str
    strategy: str
    use_mmr: bool | None
    mmr_lambda: float | None


def _norm_path(text: str) -> str:
    return text.replace("\\", "/").strip().lstrip("/")


def default_cases() -> list[BenchmarkCase]:
    return [
        BenchmarkCase(
            query="retrieval router mmr embedding",
            expected_paths=["00_System/08_Runtime_Profiles/retrieval_router.yaml"],
            expected_keywords=["mmr", "embedding", "default_strategy"],
        ),
        BenchmarkCase(
            query="llm route events ledger",
            expected_paths=["11_AI_Mirror/ingestion_logs/llm_route_events.jsonl"],
            expected_keywords=["llm", "route", "events"],
        ),
        BenchmarkCase(
            query="task board completion log",
            expected_paths=["10_Permanent/Facts/task_completion_log.md"],
            expected_keywords=["task", "completion", "checklist"],
        ),
    ]


def default_variants() -> list[BenchmarkVariant]:
    return [
        BenchmarkVariant(name="hybrid_mmr_on", strategy="hybrid", use_mmr=True, mmr_lambda=0.7),
        BenchmarkVariant(name="hybrid_mmr_off", strategy="hybrid", use_mmr=False, mmr_lambda=None),
        BenchmarkVariant(name="fts_only", strategy="fts", use_mmr=False, mmr_lambda=None),
        BenchmarkVariant(name="vector_mmr_on", strategy="vector", use_mmr=True, mmr_lambda=0.7),
    ]


def _strategy_bias(strategy: str) -> float:
    key = str(strategy).strip().lower()
    if key == "hybrid":
        return 0.03
    if key == "vector":
        return 0.015
    return 0.0


def _composite_score(*, any_hit: float, top1_hit: float, keyword_hit: float, avg_latency_ms: float, strategy: str) -> dict[str, float]:
    # Quality first, latency second: avoid selecting ultra-fast but semantically weak variants by default.
    quality = any_hit * 0.55 + top1_hit * 0.35 + keyword_hit * 0.10
    latency_score = 1.0 / (1.0 + max(0.0, float(avg_latency_ms)) / 60.0)
    strategy_bonus = _strategy_bias(strategy)
    total = quality * 0.86 + latency_score * 0.11 + strategy_bonus
    return {
        "quality_score": round(quality, 6),
        "latency_score": round(latency_score, 6),
        "strategy_bonus": round(strategy_bonus, 6),
        "total_score": round(total, 6),
    }


def load_cases_from_yaml(path: Path) -> list[BenchmarkCase]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("cases file 必須是 YAML object")
    rows = payload.get("cases", [])
    if not isinstance(rows, list):
        raise ValueError("cases file 的 cases 必須是 list")
    cases: list[BenchmarkCase] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "")).strip()
        if not query:
            continue
        expected_paths = item.get("expected_paths", [])
        expected_keywords = item.get("expected_keywords", [])
        if not isinstance(expected_paths, list):
            expected_paths = []
        if not isinstance(expected_keywords, list):
            expected_keywords = []
        cases.append(
            BenchmarkCase(
                query=query,
                expected_paths=[_norm_path(str(x)) for x in expected_paths if str(x).strip()],
                expected_keywords=[str(x).strip().lower() for x in expected_keywords if str(x).strip()],
            )
        )
    if not cases:
        raise ValueError("cases file 沒有可用案例")
    return cases


def run_benchmark(
    runtime: MemoryRuntime,
    *,
    cases: list[BenchmarkCase],
    variants: list[BenchmarkVariant],
    limit: int = 8,
    include_archived: bool = False,
    auto_reindex_each_query: bool = False,
) -> dict[str, Any]:
    if limit < 1:
        limit = 1
    results: list[dict[str, Any]] = []
    for variant in variants:
        variant_rows: list[dict[str, Any]] = []
        total_latency = 0.0
        top1_path_hit = 0
        any_path_hit = 0
        keyword_hit = 0
        total_cases = 0
        for case in cases:
            total_cases += 1
            start = perf_counter()
            hits = runtime.memory_search(
                query=case.query,
                max_results=limit,
                include_archived=include_archived,
                auto_reindex=auto_reindex_each_query,
                strategy=variant.strategy,
                use_mmr=variant.use_mmr,
                mmr_lambda=variant.mmr_lambda,
            )
            elapsed_ms = (perf_counter() - start) * 1000.0
            total_latency += elapsed_ms
            top_paths = [_norm_path(hit.path) for hit in hits]
            top1 = top_paths[0] if top_paths else ""
            expected = set(case.expected_paths)
            top1_ok = bool(top1 and top1 in expected) if expected else False
            any_ok = bool(expected.intersection(set(top_paths))) if expected else False
            if top1_ok:
                top1_path_hit += 1
            if any_ok:
                any_path_hit += 1

            text_blob = " ".join(
                [case.query]
                + [str(hit.path) for hit in hits]
                + [str(hit.snippet) for hit in hits]
            ).lower()
            kw_ok = False
            if case.expected_keywords:
                kw_ok = any(kw in text_blob for kw in case.expected_keywords if kw)
            if kw_ok:
                keyword_hit += 1
            variant_rows.append(
                {
                    "query": case.query,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "top_paths": top_paths,
                    "top_scores": [round(float(hit.score), 4) for hit in hits[: min(5, len(hits))]],
                    "top1_path_hit": top1_ok,
                    "any_path_hit": any_ok,
                    "keyword_hit": kw_ok,
                }
            )

        total_cases = max(total_cases, 1)
        results.append(
            {
                "variant": variant.name,
                "strategy": variant.strategy,
                "mmr": variant.use_mmr,
                "mmr_lambda": variant.mmr_lambda,
                "summary": {
                    "cases": total_cases,
                    "avg_latency_ms": round(total_latency / total_cases, 2),
                    "top1_path_hit_rate": round(top1_path_hit / total_cases, 3),
                    "any_path_hit_rate": round(any_path_hit / total_cases, 3),
                    "keyword_hit_rate": round(keyword_hit / total_cases, 3),
                },
                "rows": variant_rows,
            }
        )

    for row in results:
        summary = row.get("summary", {})
        score = _composite_score(
            any_hit=float(summary.get("any_path_hit_rate", 0.0)),
            top1_hit=float(summary.get("top1_path_hit_rate", 0.0)),
            keyword_hit=float(summary.get("keyword_hit_rate", 0.0)),
            avg_latency_ms=float(summary.get("avg_latency_ms", 0.0)),
            strategy=str(row.get("strategy", "")),
        )
        row["score"] = score

    sorted_results = sorted(
        results,
        key=lambda row: (
            float(row.get("score", {}).get("total_score", 0.0)),
            float(row.get("summary", {}).get("any_path_hit_rate", 0.0)),
            float(row.get("summary", {}).get("top1_path_hit_rate", 0.0)),
            float(row.get("summary", {}).get("keyword_hit_rate", 0.0)),
            -float(row.get("summary", {}).get("avg_latency_ms", 0.0)),
        ),
        reverse=True,
    )
    recommended = ""
    recommendation_reason = {}
    if sorted_results:
        top = sorted_results[0]
        recommended = str(top.get("variant", ""))
        recommendation_reason = {
            "variant": recommended,
            "strategy": str(top.get("strategy", "")),
            "score": top.get("score", {}),
            "summary": top.get("summary", {}),
            "mode": "quality_first_composite",
        }
    return {
        "variants": sorted_results,
        "recommended": recommended,
        "recommendation_reason": recommendation_reason,
    }
