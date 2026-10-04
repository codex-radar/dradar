"""Local text selection only. Catalog adaptation belongs to the v2 client."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping

FIELDS = ("benchmark", "model", "total", "concurrency")

@dataclass(frozen=True)
class Catalog:
    models_by_benchmark: Mapping[str, tuple[str, ...]]
    max_concurrency: int | None
    max_total: int | None
    efforts: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if self.max_concurrency is not None and (type(self.max_concurrency) is not int or self.max_concurrency < 1):
            raise ValueError("invalid concurrency limit")
        if self.max_total is not None and (type(self.max_total) is not int or self.max_total < 1):
            raise ValueError("invalid total limit")

    @classmethod
    def from_bootstrap(cls, value: dict):
        from ..harness_policy import current_catalog
        value=current_catalog(value)
        models, efforts = {}, {}
        for b in value["benchmarks"]:
            benchmark = b["benchmark"]
            models[benchmark] = tuple(dict.fromkeys(m["model"] for m in b["models"]))
            for m in b["models"]:
                key = (benchmark, m["model"])
                efforts[key] = tuple(dict.fromkeys((*efforts.get(key, ()), m["effort"])))
        return cls(models, value["limits"]["max_concurrency"], value["limits"]["max_total_count"], efforts)

@dataclass(frozen=True)
class Selection:
    benchmark: str
    model: str
    total: int
    concurrency: int
    effort: str | None = None

def questions(values: Mapping[str, object], catalog: Catalog) -> list[dict]:
    """Return only missing choices; malformed supplied choices fail closed.

    This local view is not a server wire contract or an authorization grant.
    Do not guess defaults, silently clamp quantities, or ask again for a
    valid explicit value supplied by the user or the webpage.
    """
    extra = set(values) - (set(FIELDS) | {"effort"})
    if extra:
        raise ValueError("unknown selection field")
    benchmark = values.get("benchmark")
    model = values.get("model")
    if benchmark is not None and (not isinstance(benchmark, str) or benchmark not in catalog.models_by_benchmark):
        raise ValueError("benchmark unavailable")
    if model is not None:
        if not isinstance(model, str):
            raise ValueError("model must be text")
        allowed = (catalog.models_by_benchmark[benchmark] if benchmark is not None
                   else tuple(m for ms in catalog.models_by_benchmark.values() for m in ms))
        if model not in allowed:
            raise ValueError("model unavailable for benchmark")
    for field, limit in (("total", catalog.max_total), ("concurrency", catalog.max_concurrency)):
        value = values.get(field)
        if value is not None and (type(value) is not int or (value < 1 or (limit is not None and value > limit))):
            raise ValueError(f"invalid {field}")
    effort = values.get("effort")
    available_efforts = catalog.efforts.get((benchmark, model), ()) if isinstance(benchmark, str) and isinstance(model, str) else ()
    if effort is not None and (not isinstance(effort, str) or (available_efforts and effort not in available_efforts)):
        raise ValueError("effort unavailable for model")
    labels = {"benchmark": "选择题库", "model": "选择 Codex 模型", "total": "总共运行多少题", "concurrency": "同时运行多少题"}
    result = []
    for field in FIELDS:
        if values.get(field) is not None:
            continue
        q = {"field": field, "question": labels[field]}
        if field == "benchmark":
            q["options"] = list(catalog.models_by_benchmark)
        elif field == "model" and benchmark is not None:
            q["options"] = list(catalog.models_by_benchmark[benchmark])
        elif field in ("total", "concurrency"):
            q["minimum"] = 1
            q["maximum"] = catalog.max_total if field == "total" else catalog.max_concurrency
        result.append(q)
    if effort is None and len(available_efforts) > 1:
        result.append({"field": "effort", "question": "这个模型提供多个运行档位，选择哪一个", "options": list(available_efforts)})
    return result

def resolve(values: Mapping[str, object], catalog: Catalog) -> Selection:
    if questions(values, catalog):
        raise ValueError("selection incomplete")
    selected = dict(values)
    efforts = catalog.efforts.get((values["benchmark"], values["model"]), ())
    if selected.get("effort") is None and len(efforts) == 1:
        selected["effort"] = efforts[0]
    return Selection(**selected)
