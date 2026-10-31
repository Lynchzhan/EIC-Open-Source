from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).parents[2] / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from eic.vector.comparison import compare_recall
from eic.vector.qdrant_reader import VectorHit


def hits(values: list[dict]) -> list[VectorHit]:
    return [VectorHit(str(value["id"]), float(value.get("score", 0)), value.get("payload", {})) for value in values]


input_path = Path(__import__("sys").argv[1])
output_path = Path(__import__("sys").argv[2])
cases = json.loads(input_path.read_text(encoding="utf-8"))
output = []
for case in cases:
    result = compare_recall(hits(case["pgvector_results"]), hits(case["qdrant_results"]))
    output.append({"query": case["query"], "overlap_rate": result.jaccard, "pgvector_only": result.pgvector_only, "qdrant_only": result.qdrant_only})
output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
