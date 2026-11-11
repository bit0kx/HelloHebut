"""Reproducible CPU embedding comparison, isolated from every live Chroma collection.

Preparation (no evaluation): --prepare downloads pinned official safetensors;
--freeze reads the active publication into tmp/embedding_corpus_manifest.json.
Evaluation: --corpus <frozen-json> --repeats 3. Optional packages belong in a
separate experiment venv: torch==2.6.0+cpu (official CPU index),
transformers==4.46.3, safetensors==0.7.0, tokenizers==0.20.3. This tokenizer
version matches Chroma 0.5.23 so the baseline tokenizer remains unchanged.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
TMP = ROOT / "experiments/v2/tmp"
MODEL_ID = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
MODEL_DIR = TMP / "embedding_models" / "bge-small-zh-v1.5" / REVISION
MODEL_FILES = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json",
               "special_tokens_map.json", "vocab.txt")
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
DATASETS = ("retrieval_reference.json", "retrieval_holdout.json")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_model():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = MODEL_DIR / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    files = {}
    for filename in MODEL_FILES:
        target = MODEL_DIR / filename
        old = manifest.get("files", {}).get(filename, {})
        if not target.exists() or digest(target) != old.get("sha256"):
            url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{filename}"
            part = target.with_suffix(target.suffix + ".part")
            print(f"Downloading {filename}", file=sys.stderr, flush=True)
            with urllib.request.urlopen(url, timeout=60) as source, part.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            part.replace(target)
        files[filename] = {"sha256": digest(target), "bytes": target.stat().st_size}
        # Preserve completed downloads on a subsequent network failure.
        write_json(manifest_file, {"model": MODEL_ID, "revision": REVISION, "files": files,
                                  "source": f"https://huggingface.co/{MODEL_ID}/tree/{REVISION}"})
    return {"prepared": True, "model": MODEL_ID, "revision": REVISION, "files": files}


def freeze_corpus():
    from core.runtime import CURRENT_RUN, RunContext
    from tooling.mcp_backend import PublicDataBackend
    backend = PublicDataBackend()
    token = CURRENT_RUN.set(RunContext(timeout_s=240))
    try:
        kb = backend._kb()
        snapshot = kb._snapshot()
        corpus = kb._active_get(include=["documents", "metadatas"])
        content = json.dumps(corpus, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(content.encode()).hexdigest()
        path = TMP / f"embedding_corpus_{sha[:12]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        manifest = {"corpus_file": path.name, "sha256": sha, "generation": snapshot["generation"],
                    "chunks": len(corpus["ids"]), "collection": os.getenv("V2_KNOWLEDGE_COLLECTION", "hello_hebut_v2")}
        write_json(TMP / "embedding_corpus_manifest.json", manifest)
        return manifest
    finally:
        CURRENT_RUN.reset(token)
        backend.close()


class BGEEncoder:
    def __init__(self):
        import torch
        from transformers import AutoModel, AutoTokenizer
        manifest = json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8"))
        for filename in MODEL_FILES:
            if digest(MODEL_DIR / filename) != manifest["files"][filename]["sha256"]:
                raise ValueError("model_file_hash_mismatch")
        torch.set_num_threads(4)
        torch.manual_seed(0)
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
        self.model = AutoModel.from_pretrained(MODEL_DIR, local_files_only=True, use_safetensors=True)
        self.model.cpu().eval()

    def encode(self, texts, *, query=False):
        import numpy as np
        rows = []
        for start in range(0, len(texts), 8):
            batch = texts[start:start + 8]
            if query:
                batch = [QUERY_INSTRUCTION + text for text in batch]
            inputs = self.tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
            with self.torch.inference_mode():
                vectors = self.model(**inputs).last_hidden_state[:, 0]
                vectors = self.torch.nn.functional.normalize(vectors, p=2, dim=1)
            rows.append(vectors.numpy())
        return np.concatenate(rows).astype("float32")


class MiniLMEncoder:
    def __init__(self):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        self.model = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])

    def encode(self, texts, *, query=False):
        import numpy as np
        return np.asarray(self.model(texts), dtype="float32")


def run(corpus_path, repeats):
    import numpy as np
    from retrieval_ablation import matches, score, targets
    from tooling.knowledge_base import KnowledgeBase
    if repeats < 1 or repeats > 20:
        raise ValueError("repeats must be between 1 and 20")
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    corpus_hash = digest(corpus_path)
    documents = corpus["documents"]
    source_hits = [KnowledgeBase._result_item(i, d, m)
                   for i, d, m in zip(corpus["ids"], documents, corpus["metadatas"])]
    datasets = [json.loads((ROOT / "experiments/v2/datasets" / name).read_text(encoding="utf-8")) for name in DATASETS]
    for dataset in datasets:
        for case in dataset["cases"]:
            for target in targets(case):
                if not any(matches(hit, target) for hit in source_hits):
                    raise ValueError("unmatched_reference:" + case["id"])
    report = {"completed": True, "corpus_sha256": corpus_hash, "chunks": len(documents),
              "script_sha256": digest(Path(__file__)),
              "scoring_source_sha256": digest(Path(__file__).with_name("retrieval_ablation.py")),
              "dataset_sha256": {name: digest(ROOT / "experiments/v2/datasets" / name) for name in DATASETS},
              "python": platform.python_version(), "platform": platform.platform(), "repeats": repeats,
              "versions": {p: importlib.metadata.version(p) for p in ("torch", "transformers", "safetensors", "onnxruntime", "chromadb", "numpy")},
              "models": {}, "benchmarks": [],
              "limitations": "Small source-derived development sets, not an independent human-reviewed holdout. "
                  "Pure vector retrieval with identical frozen documents/year filters/top-8; no reranker or hybrid tuning. "
                  "No relevance threshold is calibrated; zero results only follow an empty year-filtered corpus. "
                  "CPU timings are local component measurements, not production end-to-end latency. BGE-M3 was not run; live collections unchanged."}
    for name, factory in (("minilm_onnx_384", MiniLMEncoder), ("bge_small_zh_512", BGEEncoder)):
        started = time.monotonic()
        encoder = factory()
        matrix = encoder.encode(documents)
        matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        index_path = TMP / "embedding_indexes" / corpus_hash[:12] / (name + ".npy")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(index_path, matrix, allow_pickle=False)
        model_info = {"dimensions": matrix.shape[1], "index_bytes": index_path.stat().st_size,
                      "index_sha256": digest(index_path), "build_s": round(time.monotonic() - started, 3),
                      "device": "cpu", "max_tokens": 256 if name.startswith("minilm") else 512}
        if name.startswith("bge"):
            model_info.update(model=MODEL_ID, revision=REVISION, pooling="CLS + L2", query_instruction=QUERY_INSTRUCTION,
                              artifact_manifest=json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8")))
        else:
            artifact_dir = Path(encoder.model.DOWNLOAD_PATH) / encoder.model.EXTRACTED_FOLDER_NAME
            model_info.update(model="all-MiniLM-L6-v2", pooling="mean + L2", query_instruction="",
                              onnx_sha256=digest(artifact_dir / "model.onnx"),
                              tokenizer_sha256=digest(artifact_dir / "tokenizer.json"),
                              providers=["CPUExecutionProvider"])
        report["models"][name] = model_info
        encoder.encode(["模型预热"], query=True)
        for dataset in datasets:
            rows, elapsed = [], []
            for case in dataset["cases"]:
                eligible = [i for i, m in enumerate(corpus["metadatas"])
                            if case.get("year") is None or m.get("effective_year") == case["year"]]
                trials, rankings = [], []
                for _ in range(repeats):
                    started = time.monotonic()
                    query = encoder.encode([case["query"]], query=True)[0]
                    query /= max(float(np.linalg.norm(query)), 1e-12)
                    # Stable tie order across models and repetitions.
                    similarity = matrix[eligible] @ query if eligible else np.asarray([])
                    order = np.argsort(-similarity, kind="stable")[:8]
                    indexes = [eligible[i] for i in order]
                    trials.append((time.monotonic() - started) * 1000)
                    rankings.append(indexes)
                result = score(case, [source_hits[i] for i in rankings[0]])
                result.update(latency_ms=[round(t, 3) for t in trials], stable_across_repeats=all(x == rankings[0] for x in rankings))
                rows.append(result)
                elapsed.extend(trials)
            positive = [row for row in rows if "recall_at_8" in row]
            negative = [row for row in rows if "empty_as_expected" in row]
            metrics = {key: round(statistics.mean(row[key] for row in positive), 6)
                       for key in ("recall_at_8", "ndcg_at_8", "mrr_at_8")}
            metrics.update(missing_year_empty=sum(r["empty_as_expected"] for r in negative), missing_year=len(negative),
                           query_ms_p50=round(float(np.percentile(elapsed, 50)), 3),
                           query_ms_p95=round(float(np.percentile(elapsed, 95)), 3))
            report["benchmarks"].append({"model": name, "benchmark_id": dataset["benchmark_id"], "metrics": metrics, "rows": rows})
        del encoder
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.prepare:
        print(json.dumps(prepare_model()))
    elif args.freeze:
        print(json.dumps(freeze_corpus()))
    else:
        corpus_path = args.corpus
        if corpus_path is None:
            manifest = json.loads((TMP / "embedding_corpus_manifest.json").read_text(encoding="utf-8"))
            corpus_path = TMP / manifest["corpus_file"]
            if digest(corpus_path) != manifest["sha256"]:
                raise ValueError("corpus_hash_mismatch")
        report_path = ROOT / "experiments/v2/reports/stage6_embedding_ablation.json"
        try:
            result = run(corpus_path, args.repeats)
        except Exception as exc:
            write_json(report_path, {"completed": False, "error_type": type(exc).__name__, "error": str(exc),
                                    "model": MODEL_ID, "revision": REVISION,
                                    "note": "Evaluation did not complete; no benchmark results are claimed."})
            raise
        write_json(report_path, result)
        print(json.dumps({"completed": True, "models": list(result["models"]),
                          "benchmarks": [{k: b[k] for k in ("model", "benchmark_id", "metrics")} for b in result["benchmarks"]]}))
