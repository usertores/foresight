"""Study profile: what the subject is, so prompts, transcription and filters fit it.

Everything subject-specific lives in a small JSON file (see profiles/ at the project root),
picked with "profile" in config.json. Without one the app is subject-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ca": "Catalan", "nl": "Dutch", "pl": "Polish", "ja": "Japanese",
    "zh": "Chinese",
}


@dataclass
class SourceRule:
    """Files whose top folder or name contains any of `match` get this filter label."""

    label: str
    match: list[str]


@dataclass
class Profile:
    # "CFA Level I", "Organic Chemistry"… Empty: a generic study assistant.
    subject: str = ""
    # One or two sentences added to every prompt, e.g. what exam the student prepares.
    context: str = ""
    # Default language of recorded classes (a Whisper code, or "auto").
    language: str = "en"
    # Terms that bias the transcription towards the subject's vocabulary.
    vocabulary: list[str] = field(default_factory=list)
    # Filter labels; first match wins. Markdown notes are "Notes" unless a rule says otherwise.
    sources: list[SourceRule] = field(default_factory=list)
    # Example questions on the empty search screen.
    examples: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None) -> "Profile":
        if not path:
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        rules = [SourceRule(label=r["label"], match=[m.lower() for m in r["match"]])
                 for r in raw.get("sources", [])]
        return cls(
            subject=raw.get("subject", ""),
            context=raw.get("context", ""),
            language=raw.get("language", "en"),
            vocabulary=list(raw.get("vocabulary", [])),
            sources=rules,
            examples=list(raw.get("examples", [])),
        )

    # ------------------------------------------------------------ prompt pieces

    def tutor(self) -> str:
        """'an expert tutor in CFA Level I' (or a generic tutor), plus the context."""
        who = f"You are an expert tutor in {self.subject}." if self.subject \
            else "You are an expert study tutor."
        return f"{who} {self.context}".strip()

    def classes(self) -> str:
        """How to name the recorded classes in prompts: 'CFA Level I classes' / 'classes'."""
        return f"{self.subject} classes" if self.subject else "classes"

    def whisper_prompt(self) -> str:
        """Initial prompt that nudges Whisper towards the subject's terms."""
        terms = ", ".join(self.vocabulary)
        if self.subject and terms:
            return f"{self.subject}: {terms}."
        return f"{terms}." if terms else self.subject

    def source_label(self, top: str, name: str) -> str | None:
        top, name = top.lower(), name.lower()
        for rule in self.sources:
            if any(m in top or m in name for m in rule.match):
                return rule.label
        return None


def language_name(code: str) -> str:
    """'es' -> 'Spanish'; 'auto' (or unknown) -> the transcript's own language."""
    return LANGUAGES.get(code, "the same language as the transcript")
