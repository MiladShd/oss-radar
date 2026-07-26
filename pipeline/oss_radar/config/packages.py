"""The OSS Radar watchlist: curated AI / data / ML-infra packages.

Each entry is ``(pypi_name, primary_category)``. A primary category is the stable,
mutually-exclusive dashboard grouping. ``PACKAGE_CAPABILITIES`` supplies optional
cross-cutting tags without forcing packages into increasingly narrow categories.

Repo URLs are resolved at ingest time from PyPI metadata / ecosyste.ms;
``REPO_OVERRIDES`` covers the handful whose canonical repo is ambiguous or not
discoverable from package metadata.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

CATEGORIES = {
    "llm": "LLM tooling & inference",
    "agents": "Agent frameworks",
    "vectordb": "Vector databases & retrieval",
    "mlframework": "ML frameworks",
    "dataeng": "Data engineering",
    "mlops": "MLOps & serving",
}

# Curated, stable tags for capabilities that span multiple primary categories.
# GitHub topics are ingested separately as raw upstream metadata.
CAPABILITIES = {
    "inference_serving_runtime": (
        "Production inference servers, runtimes, API layers, and serving interfaces"
    ),
    "evaluation_observability": (
        "Model or application evaluation, experiment tracking, and production monitoring"
    ),
    "workflow_orchestration": (
        "Agent, data, or ML workflow definition, scheduling, and execution"
    ),
}

WATCHLIST: list[tuple[str, str]] = [
    # --- LLM tooling & inference ---
    ("transformers", "llm"),
    ("vllm", "llm"),
    ("openai", "llm"),
    ("anthropic", "llm"),
    ("litellm", "llm"),
    ("langchain", "llm"),
    ("langchain-core", "llm"),
    ("llama-index", "llm"),
    ("tiktoken", "llm"),
    ("sentence-transformers", "llm"),
    ("llama-cpp-python", "llm"),
    ("huggingface-hub", "llm"),
    ("tokenizers", "llm"),
    ("accelerate", "llm"),
    ("peft", "llm"),
    ("trl", "llm"),
    ("guidance", "llm"),
    ("outlines", "llm"),
    ("instructor", "llm"),
    ("dspy-ai", "llm"),
    ("faster-whisper", "llm"),
    ("sentencepiece", "llm"),
    ("einops", "llm"),
    # --- Agent frameworks ---
    ("langgraph", "agents"),
    ("crewai", "agents"),
    ("pyautogen", "agents"),
    ("smolagents", "agents"),
    ("haystack-ai", "agents"),
    ("semantic-kernel", "agents"),
    ("agno", "agents"),
    ("metagpt", "agents"),
    ("langflow", "agents"),
    ("browser-use", "agents"),
    ("e2b", "agents"),
    ("langsmith", "agents"),
    # --- Vector databases & retrieval ---
    ("chromadb", "vectordb"),
    ("qdrant-client", "vectordb"),
    ("pinecone-client", "vectordb"),
    ("weaviate-client", "vectordb"),
    ("pymilvus", "vectordb"),
    ("faiss-cpu", "vectordb"),
    ("lancedb", "vectordb"),
    ("redisvl", "vectordb"),
    ("txtai", "vectordb"),
    ("hnswlib", "vectordb"),
    # --- ML frameworks ---
    ("torch", "mlframework"),
    ("tensorflow", "mlframework"),
    ("jax", "mlframework"),
    ("scikit-learn", "mlframework"),
    ("xgboost", "mlframework"),
    ("lightgbm", "mlframework"),
    ("catboost", "mlframework"),
    ("keras", "mlframework"),
    ("pytorch-lightning", "mlframework"),
    ("onnx", "mlframework"),
    ("onnxruntime", "mlframework"),
    ("timm", "mlframework"),
    ("diffusers", "mlframework"),
    ("datasets", "mlframework"),
    ("statsmodels", "mlframework"),
    ("scipy", "mlframework"),
    ("numpy", "mlframework"),
    # --- Data engineering ---
    ("pandas", "dataeng"),
    ("polars", "dataeng"),
    ("pyarrow", "dataeng"),
    ("duckdb", "dataeng"),
    ("dask", "dataeng"),
    ("ray", "dataeng"),
    ("dbt-core", "dataeng"),
    ("great-expectations", "dataeng"),
    ("sqlalchemy", "dataeng"),
    ("prefect", "dataeng"),
    ("dagster", "dataeng"),
    ("apache-airflow", "dataeng"),
    ("ibis-framework", "dataeng"),
    ("sqlmesh", "dataeng"),
    ("pandera", "dataeng"),
    # --- MLOps & serving ---
    ("mlflow", "mlops"),
    ("wandb", "mlops"),
    ("dvc", "mlops"),
    ("bentoml", "mlops"),
    ("evidently", "mlops"),
    ("feast", "mlops"),
    ("zenml", "mlops"),
    ("optuna", "mlops"),
    ("gradio", "mlops"),
    ("streamlit", "mlops"),
    ("fastapi", "mlops"),
    ("ragas", "mlops"),
    ("deepeval", "mlops"),
    ("kfp", "mlops"),
]

# Package-level curated capabilities. Packages may have zero or several tags; the
# primary category above remains the backward-compatible ``category`` value.
PACKAGE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    # Inference / serving runtimes
    "vllm": ("inference_serving_runtime",),
    "litellm": ("inference_serving_runtime",),
    "llama-cpp-python": ("inference_serving_runtime",),
    "faster-whisper": ("inference_serving_runtime",),
    "onnxruntime": ("inference_serving_runtime",),
    "bentoml": ("inference_serving_runtime",),
    "gradio": ("inference_serving_runtime",),
    "streamlit": ("inference_serving_runtime",),
    "fastapi": ("inference_serving_runtime",),
    # Evaluation / observability
    "langsmith": ("evaluation_observability",),
    "mlflow": ("evaluation_observability",),
    "wandb": ("evaluation_observability",),
    "evidently": ("evaluation_observability",),
    "ragas": ("evaluation_observability",),
    "deepeval": ("evaluation_observability",),
    # Workflow orchestration
    "langgraph": ("workflow_orchestration",),
    "crewai": ("workflow_orchestration",),
    "pyautogen": ("workflow_orchestration",),
    "smolagents": ("workflow_orchestration",),
    "haystack-ai": ("workflow_orchestration",),
    "semantic-kernel": ("workflow_orchestration",),
    "agno": ("workflow_orchestration",),
    "metagpt": ("workflow_orchestration",),
    "langflow": ("workflow_orchestration",),
    "prefect": ("workflow_orchestration",),
    "dagster": ("workflow_orchestration",),
    "apache-airflow": ("workflow_orchestration",),
    "zenml": ("workflow_orchestration",),
    "kfp": ("workflow_orchestration",),
}

# pypi_name -> "owner/repo" for cases where metadata resolution is unreliable.
REPO_OVERRIDES: dict[str, str] = {
    "faiss-cpu": "facebookresearch/faiss",
    "pyautogen": "microsoft/autogen",
    "dspy-ai": "stanfordnlp/dspy",
    "faster-whisper": "SYSTRAN/faster-whisper",
    "llama-index": "run-llama/llama_index",
    "langchain": "langchain-ai/langchain",
    "langchain-core": "langchain-ai/langchain",
    "langgraph": "langchain-ai/langgraph",
    "huggingface-hub": "huggingface/huggingface_hub",
    "apache-airflow": "apache/airflow",
    "dbt-core": "dbt-labs/dbt-core",
    "pinecone-client": "pinecone-io/pinecone-python-client",
    "pytorch-lightning": "Lightning-AI/pytorch-lightning",
    "ibis-framework": "ibis-project/ibis",
    "haystack-ai": "deepset-ai/haystack",
}


def parse_capabilities(raw: str | Iterable[str] | None) -> list[str]:
    """Normalize configured capability tags and reject unknown values.

    Strings may be comma-separated for convenient config generation. Hyphens and
    spaces normalize to underscores; output is de-duplicated in input order.
    """
    values = raw.split(",") if isinstance(raw, str) else raw or ()
    parsed: list[str] = []
    for value in values:
        tag = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        if not tag:
            continue
        if tag not in CAPABILITIES:
            raise ValueError(f"unknown capability: {tag}")
        if tag not in parsed:
            parsed.append(tag)
    return parsed


def _round_robin_by_category(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Interleave category buckets while retaining the curated order within each."""
    buckets: dict[str, deque[tuple[str, str]]] = {
        category: deque() for category in CATEGORIES
    }
    for item in items:
        buckets[item[1]].append(item)

    ordered: list[tuple[str, str]] = []
    while any(buckets.values()):
        for bucket in buckets.values():
            if bucket:
                ordered.append(bucket.popleft())
    return ordered


def _prefer_unique_repositories(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Defer known monorepo siblings so limited demos cover more repositories."""
    unique: list[tuple[str, str]] = []
    deferred: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        name, _ = item
        # Unknown repositories use the unique PyPI name until ingest resolves them.
        repo_key = REPO_OVERRIDES.get(name, f"pypi:{name}").casefold()
        if repo_key in seen:
            deferred.append(item)
            continue
        seen.add(repo_key)
        unique.append(item)
    return unique + deferred


def get_watchlist(limit: int = 0) -> list[dict[str, object]]:
    """Return package metadata; limited runs are balanced across categories.

    ``limit <= 0`` means the full curated watchlist in its source order. Positive
    limits use deterministic round-robin sampling and prefer unique repositories,
    making demos and small experiments representative without changing production
    full-watchlist behavior.
    """
    if limit <= 0:
        items = WATCHLIST
    else:
        items = _prefer_unique_repositories(_round_robin_by_category(WATCHLIST))[:limit]

    return [
        {
            "name": name,
            "primary_category": primary_category,
            # Backward-compatible alias used by features, predictions, and dashboard code.
            "category": primary_category,
            "capabilities": parse_capabilities(PACKAGE_CAPABILITIES.get(name)),
            "repo_override": REPO_OVERRIDES.get(name, ""),
        }
        for name, primary_category in items
    ]
