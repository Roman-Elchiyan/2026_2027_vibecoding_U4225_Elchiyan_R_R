"""Check already-downloaded MiniLM locally, without Telegram, HTTP or paid API."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter


def emit(payload, output=None):
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


def run(args):
    path = args.model_dir.expanduser().resolve()
    weights = sorted(path.glob("*.safetensors")) if path.is_dir() else []
    if not weights and (path / "pytorch_model.bin").is_file():
        weights = [path / "pytorch_model.bin"]
    if not (path / "config.json").is_file() or not weights:
        emit(dict(status="missing_local_model", local_directory=str(path),
                  download_attempted=False, paid_calls=0), args.output)
        return 2

    # Set offline mode before importing HF/SentenceTransformers.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    started = perf_counter()
    try:
        import torch
        from sentence_transformers import CrossEncoder
        torch.set_num_threads(args.threads)
        model = CrossEncoder(str(path), device="cpu", max_length=384,
            local_files_only=True, trust_remote_code=False,
            model_kwargs={"attn_implementation": "eager"})
        loaded = perf_counter()
        queries = [
            "Машинное обучение для прогнозирования спроса в розничной торговле",
            "Machine learning for retail demand forecasting",
        ]
        positive = "Machine learning models forecast retail product demand using sales history and seasonality."
        negative = "Machine learning models classify skin lesions from medical photographs for cancer diagnosis."
        pairs = [(query, document) for query in queries for document in (positive, negative)]
        with torch.inference_mode():
            raw = model.predict(pairs, batch_size=2, show_progress_bar=False,
                convert_to_numpy=True, activation_fn=torch.nn.Identity())
        scores = raw.reshape(-1).tolist()
        if len(scores) != len(pairs) or not all(math.isfinite(float(s)) for s in scores):
            raise ValueError("invalid_model_scores")
    except Exception as exc:
        emit(dict(status="load_or_inference_failed", error_type=type(exc).__name__,
            error=str(exc)[:500], local_directory=str(path),
            download_attempted=False, paid_calls=0, device="cpu"), args.output)
        return 2

    payload = dict(status="inference_completed", local_directory=str(path),
        torch_version=torch.__version__, device="cpu", download_attempted=False,
        paid_calls=0, load_seconds=round(loaded-started, 3),
        inference_seconds=round(perf_counter()-loaded, 3),
        config_sha256=hashlib.sha256((path/"config.json").read_bytes()).hexdigest(),
        synthetic_pairs=True,
        note="This verifies local loading and contrastive pairs, not end-to-end search quality.",
        comparisons=[dict(query=query, positive_score=float(scores[2*i]),
            negative_score=float(scores[2*i+1]), expected_order=scores[2*i] > scores[2*i+1])
            for i,query in enumerate(queries)],
        weight_files=[dict(name=p.name, bytes=p.stat().st_size) for p in weights])
    if args.hash_weights:
        for record, weight in zip(payload["weight_files"], weights):
            digest = hashlib.sha256()
            with weight.open("rb") as stream:
                for chunk in iter(lambda:stream.read(8*1024*1024), b""):
                    digest.update(chunk)
            record["sha256"] = digest.hexdigest()
    emit(payload, args.output)
    return 0 if all(item["expected_order"] for item in payload["comparisons"]) else 3


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path,
        default=Path("runtime/models/mmarco-mMiniLMv2-L12-H384-v1"))
    parser.add_argument("--threads", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hash-weights", action="store_true")
    raise SystemExit(run(parser.parse_args()))
