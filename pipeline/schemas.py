"""
Data classes representing the state flowing through the pipeline.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Boundaries:
    """LLM-provided boundaries for the Related Work section."""
    source: str  # "explicit" or "fallback"
    start_page: int
    end_page: int
    start_snippet: str
    end_snippet: str

    @classmethod
    def from_dict(cls, d: dict) -> Optional["Boundaries"]:
        """Parse from the LLM response dict. Returns None if related_work is null."""
        rw = d.get("related_work")
        if rw is None or not isinstance(rw, dict):
            return None
        return cls(
            source=rw.get("source", "explicit"),
            start_page=int(rw.get("start_page", 0)),
            end_page=int(rw.get("end_page", 0)),
            start_snippet=rw.get("start_snippet", ""),
            end_snippet=rw.get("end_snippet", ""),
        )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "start_snippet": self.start_snippet,
            "end_snippet": self.end_snippet,
        }


@dataclass
class GateResult:
    """Result of the extraction gate validation."""
    status: str  # "success", "partial", "error"
    extracted_text: Optional[str] = None
    start_found: bool = False
    end_found: bool = False
    error_details: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status in ("success", "partial")


@dataclass
class VerificationResult:
    """Result of the verification LLM check."""
    is_correct: bool = False
    is_complete: bool = False
    issues: list[str] = field(default_factory=list)
    suggested_correction: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.is_correct and self.is_complete

    @classmethod
    def from_dict(cls, d: dict) -> "VerificationResult":
        return cls(
            is_correct=d.get("is_correct", False),
            is_complete=d.get("is_complete", False),
            issues=d.get("issues", []),
            suggested_correction=d.get("suggested_correction"),
        )


@dataclass
class ErrorAnalysis:
    """Result of the error analysis LLM diagnosis."""
    diagnosis: str = ""
    correct_section_exists: bool = False
    suggested_start_page: Optional[int] = None
    suggested_end_page: Optional[int] = None
    suggested_start_text: Optional[str] = None
    suggested_end_text: Optional[str] = None
    reasoning: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ErrorAnalysis":
        return cls(
            diagnosis=d.get("diagnosis", ""),
            correct_section_exists=d.get("correct_section_exists", False),
            suggested_start_page=d.get("suggested_start_page"),
            suggested_end_page=d.get("suggested_end_page"),
            suggested_start_text=d.get("suggested_start_text"),
            suggested_end_text=d.get("suggested_end_text"),
            reasoning=d.get("reasoning", ""),
        )


@dataclass
class IterationRecord:
    """Record of a single iteration through the pipeline."""
    attempt: int
    boundaries: Optional[dict] = None
    gate_status: Optional[str] = None
    verification: Optional[dict] = None
    error_analysis: Optional[dict] = None
    correction_context: Optional[str] = None


@dataclass
class PipelineResult:
    """Final result of the full pipeline run."""
    paper: str
    final_status: str  # "accepted", "accepted_with_flag", "failed", "no_related_work"
    total_iterations: int = 0
    boundaries: Optional[Boundaries] = None
    extracted_text: Optional[str] = None
    iterations: list[IterationRecord] = field(default_factory=list)

    def to_log_dict(self) -> dict:
        """Serialize the full pipeline run for pipeline_log.json."""
        return {
            "paper": self.paper,
            "final_status": self.final_status,
            "total_iterations": self.total_iterations,
            "final_boundaries": self.boundaries.to_dict() if self.boundaries else None,
            "iterations": [
                {
                    "attempt": it.attempt,
                    "boundaries": it.boundaries,
                    "gate_status": it.gate_status,
                    "verification": it.verification,
                    "error_analysis": it.error_analysis,
                    "correction_context": it.correction_context,
                }
                for it in self.iterations
            ],
        }
