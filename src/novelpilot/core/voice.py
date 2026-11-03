"""Voice: the style fingerprint that keeps prose from sliding into AI-default.

Two ideas do the work here.

**Measure, then instruct.**  Rather than hoping the model writes well, we
measure the target style on a reference sample (or on the model's own first
chapter) and turn the measurements into explicit numeric directives: target
sentence length, em-dash rate, paragraph length, dialogue ratio.  Models
follow numbers far more reliably than they follow "write in the style of...".

**Constrain the tells.**  The characteristic AI-tell is not "AI vocabulary" so
much as *rhythm*: uniformly medium-length sentences, a dash every other
paragraph, three-sentence paragraphs everywhere.  The profile therefore sets
*variation targets* and banned patterns, and the writer prompt states them as
hard rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that make prose read as machine-written.
AI_TELL_WORDS = [
    "delve", "tapestry", "testament", "realm", "bustling", "labyrinth", "nuanced",
    "multifaceted", "poignant", "evocative", "myriad", "quintessential", "unwavering",
    "indelible", "palpable", "crescendo", "whisper", "shimmered", "tremble",
]

EM_DASH = "—"
EN_DASH = "–"


@dataclass(slots=True)
class StyleMetrics:
    """Measurable properties of a prose sample."""

    avg_sentence_words: float = 14.0
    sentence_length_stddev: float = 4.0
    avg_paragraph_sentences: float = 3.0
    em_dash_rate_per_1k: float = 1.0
    dialogue_ratio: float = 0.35
    tell_word_count: int = 0
    sample_words: int = 0

    def as_directives(self) -> list[str]:
        """Turn measurements into prompt directives."""
        d = [
            f"Average sentence length: ~{self.avg_sentence_words:.0f} words, "
            f"but vary it -- include sentences from {max(4, int(self.avg_sentence_words - self.sentence_length_stddev))} "
            f"to {int(self.avg_sentence_words + self.sentence_length_stddev)} words.",
            f"Aim for {self.avg_paragraph_sentences:.1f} sentences per paragraph on average; "
            "deliberately vary paragraph length.",
            f"Em-dash density target: about {self.em_dash_rate_per_1k:.1f} per 1000 words. "
            "Do not overuse dashes as a substitute for punctuation.",
            f"Dialogue should be roughly {self.dialogue_ratio * 100:.0f}% of the text "
            "for conversation-led scenes; vary this per scene.",
            "Vary sentence openings. Do not begin consecutive sentences with the same "
            "word or the same part of speech.",
        ]
        if self.tell_word_count:
            d.append(
                "Avoid the following words entirely: "
                + ", ".join(AI_TELL_WORDS)
                + "."
            )
        return d

    @classmethod
    def measure(cls, text: str) -> "StyleMetrics":
        if not text or not text.strip():
            return cls()
        words = re.findall(r"\b[\w'’-]+\b", text)
        n_words = len(words)
        if n_words == 0:
            return cls()

        # Sentences: split on terminal punctuation, keep non-empty.
        raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
        sent_lengths = [len(re.findall(r"\b[\w'’-]+\b", s)) for s in raw_sentences]
        sent_lengths = [s for s in sent_lengths if s > 0]

        avg = sum(sent_lengths) / len(sent_lengths) if sent_lengths else 0.0
        if len(sent_lengths) > 1:
            mean = avg
            var = sum((s - mean) ** 2 for s in sent_lengths) / (len(sent_lengths) - 1)
            std = var**0.5
        else:
            std = 0.0

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        para_sent_counts = []
        for p in paragraphs:
            c = len([s for s in re.split(r"(?<=[.!?…])\s+", p) if s.strip()])
            para_sent_counts.append(max(c, 1))
        avg_para = sum(para_sent_counts) / len(para_sent_counts) if para_sent_counts else 0.0

        dashes = text.count(EM_DASH) + text.count(EN_DASH)
        dash_rate = dashes / (n_words / 1000) if n_words else 0.0

        dialogue_chars = sum(len(m) for m in re.findall(r'[""][^""]*[""]', text))
        dialogue_ratio = dialogue_chars / len(text) if text else 0.0

        low = text.lower()
        tell_count = sum(low.count(w) for w in AI_TELL_WORDS)

        return cls(
            avg_sentence_words=avg,
            sentence_length_stddev=std,
            avg_paragraph_sentences=avg_para,
            em_dash_rate_per_1k=dash_rate,
            dialogue_ratio=dialogue_ratio,
            tell_word_count=tell_count,
            sample_words=n_words,
        )


@dataclass(slots=True)
class VoiceProfile:
    """Per-book style target plus per-character overrides."""

    metrics: StyleMetrics = field(default_factory=StyleMetrics)
    genre_directives: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    character_overrides: dict[str, list[str]] = field(default_factory=dict)

    def directives_for(self, character_id: str | None = None) -> list[str]:
        out = list(self.genre_directives) + self.metrics.as_directives()
        if character_id and character_id in self.character_overrides:
            out += self.character_overrides[character_id]
        return out

    def banned(self) -> list[str]:
        return list(self.banned_phrases) + list(AI_TELL_WORDS)


def build_voice_profile(
    metrics: StyleMetrics | None = None,
    genre_directives: list[str] | None = None,
    banned_phrases: list[str] | None = None,
) -> VoiceProfile:
    return VoiceProfile(
        metrics=metrics or StyleMetrics(),
        genre_directives=genre_directives or [],
        banned_phrases=banned_phrases or [],
    )


def detect_style_drift(text: str, profile: VoiceProfile) -> list[str]:
    """Compare written text against the target profile; return human-readable drift notes."""
    measured = StyleMetrics.measure(text)
    notes: list[str] = []
    target = profile.metrics

    # The vocabulary check is exact, not statistical -- a banned word is a
    # banned word even in a two-line excerpt, so it runs regardless of length.
    if measured.tell_word_count > 0:
        words = sorted({w for w in AI_TELL_WORDS if w in text.lower()})
        notes.append("AI-tell vocabulary present: " + ", ".join(words))

    # The statistical comparisons need a meaningful sample on both sides.
    if measured.sample_words < 50 or target.sample_words < 50:
        return notes

    if abs(measured.avg_sentence_words - target.avg_sentence_words) > 3:
        notes.append(
            f"Average sentence length drifted: {measured.avg_sentence_words:.1f} words "
            f"(target ~{target.avg_sentence_words:.0f})."
        )
    if measured.em_dash_rate_per_1k > target.em_dash_rate_per_1k * 2.5 + 1:
        notes.append(
            f"Em-dash overuse: {measured.em_dash_rate_per_1k:.1f} per 1000 words "
            f"(target ~{target.em_dash_rate_per_1k:.1f})."
        )
    return notes
