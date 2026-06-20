"""The OSS Radar watchlist: curated AI / data / ML-infra packages.

Each entry is ``(pypi_name, category)``. Repo URLs are resolved at ingest time from
PyPI metadata / ecosyste.ms; ``REPO_OVERRIDES`` covers the handful whose canonical
repo is ambiguous or not discoverable from package metadata.
"""

from __future__ import annotations

CATEGORIES = {
    "llm": "LLM tooling & inference",
    "agents": "Agent frameworks",
    "vectordb": "Vector databases & retrieval",
    "mlframework": "ML frameworks",
    "dataeng": "Data engineering",
    "mlops": "MLOps & serving",
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


def get_watchlist(limit: int = 0) -> list[dict[str, str]]:
    """Return the watchlist as dicts; ``limit`` 0 means all."""
    items = WATCHLIST if not limit else WATCHLIST[:limit]
    return [
        {"name": name, "category": cat, "repo_override": REPO_OVERRIDES.get(name, "")}
        for name, cat in items
    ]
