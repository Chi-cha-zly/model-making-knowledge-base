"""Internal evidence identity stays separate from public search/citation payloads."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvidenceRef:
    document_id: int
    version: int
    chunks: tuple[tuple[int, str], ...]


@dataclass
class RetrievalResult:
    hits: list[dict]
    requested_strategy: str
    effective_strategy: str
    version: str
    diagnostics: dict = field(default_factory=dict)
    evidence_refs: dict[str, EvidenceRef] = field(default_factory=dict)

    def metadata(self):
        return {
            'requested_strategy': self.requested_strategy,
            'effective_strategy': self.effective_strategy,
            'version': self.version,
            **self.diagnostics,
        }
