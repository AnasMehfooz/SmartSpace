"""models_catalog.json loader (SDD 2.4, 9.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CatalogFile:
    path: str
    url: str
    size_bytes: int
    sha256: str


@dataclass
class CatalogModel:
    id: str
    role: str
    display_name: str
    raw: dict[str, Any]
    files: list[CatalogFile] = field(default_factory=list)

    @property
    def bundled(self) -> bool:
        return bool(self.raw.get("bundled"))

    @property
    def size_bytes(self) -> int:
        return int(self.raw.get("size_bytes") or sum(f.size_bytes for f in self.files))

    @property
    def tiers(self) -> list[int]:
        return list(self.raw.get("tiers") or [0, 1, 2, 3, 4])

    @property
    def min_tier(self) -> int:
        return min(self.tiers)

    @property
    def dim(self) -> int | None:
        return self.raw.get("dim")

    @property
    def file_name(self) -> str | None:
        return self.raw.get("file_name") or (self.files[0].path if self.files else None)

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


class Catalog:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.models: dict[str, CatalogModel] = {}
        for m in data.get("models", []):
            files = [
                CatalogFile(path=f["path"], url=f["url"], size_bytes=int(f["size_bytes"]), sha256=f["sha256"].lower())
                for f in m.get("files", [])
            ]
            self.models[m["id"]] = CatalogModel(
                id=m["id"], role=m["role"], display_name=m["display_name"], raw=m, files=files
            )
        self.tier_ctx_cap = {int(k): int(v) for k, v in data.get("tier_ctx_cap", {}).items()} or {
            0: 4096, 1: 4096, 2: 6144, 3: 8192, 4: 8192,
        }
        self.tier_defaults = {int(k): v for k, v in data.get("tier_defaults", {}).items()}

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, model_id: str) -> CatalogModel | None:
        return self.models.get(model_id)

    def by_role(self, role: str) -> list[CatalogModel]:
        return [m for m in self.models.values() if m.role == role]

    def defaults_for_tier(self, tier: int) -> dict[str, str]:
        d = dict(self.tier_defaults.get(tier) or {})
        if "embedding" not in d:
            emb = self.by_role("embedding")
            if emb:
                d["embedding"] = emb[0].id
        if "llm" not in d:
            llms = sorted(self.by_role("llm"), key=lambda m: m.size_bytes)
            if llms:
                d["llm"] = llms[0].id
        ocr = self.by_role("ocr")
        if ocr:
            d["ocr"] = ocr[0].id
        return d
