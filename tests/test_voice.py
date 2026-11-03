"""Voice measurement, drift detection, and style directives."""

from __future__ import annotations

from novelpilot.core.voice import AI_TELL_WORDS, StyleMetrics, detect_style_drift


def test_measures_sentence_length() -> None:
    text = "The salt was white. The road was long. She walked on and did not look back."
    m = StyleMetrics.measure(text)
    assert 4 < m.avg_sentence_words < 8
    assert m.sample_words > 0


def test_measures_em_dash_density() -> None:
    text = "One — two — three — four — five — six — seven — eight."
    m = StyleMetrics.measure(text)
    assert m.em_dash_rate_per_1k > 0


def test_measures_dialogue_ratio() -> None:
    text = '"Hello," she said. "Are you coming?" He did not answer.'
    m = StyleMetrics.measure(text)
    assert m.dialogue_ratio > 0.1


def test_counts_ai_tell_words() -> None:
    text = "She delved into the tapestry of memory, a testament to the realm."
    m = StyleMetrics.measure(text)
    assert m.tell_word_count > 0


def test_empty_text_is_safe() -> None:
    m = StyleMetrics.measure("")
    assert m.sample_words == 0
    assert m.as_directives()


def test_directives_are_numeric_and_actionable() -> None:
    m = StyleMetrics.measure("One. Two words. Three. Four five six seven eight nine ten.")
    directives = " ".join(m.as_directives())
    assert "Average sentence length" in directives
    assert "Em-dash density target" in directives


def test_detects_style_drift_in_sentence_length() -> None:
    target = StyleMetrics(avg_sentence_words=12.0, sample_words=500)
    from novelpilot.core.voice import VoiceProfile

    profile = VoiceProfile(metrics=target)
    long_text = " ".join(["word " * 40] * 5)
    notes = detect_style_drift(long_text, profile)
    assert any("sentence length drifted" in n for n in notes)


def test_detects_ai_tell_vocabulary_in_drift() -> None:
    from novelpilot.core.voice import VoiceProfile

    profile = VoiceProfile(metrics=StyleMetrics(sample_words=500))
    notes = detect_style_drift("She delved into the tapestry of a vast realm.", profile)
    assert any("AI-tell vocabulary" in n for n in notes)


def test_no_drift_notes_for_short_text() -> None:
    from novelpilot.core.voice import VoiceProfile

    profile = VoiceProfile(metrics=StyleMetrics(sample_words=500))
    assert detect_style_drift("Short.", profile) == []


def test_ai_tell_word_list_is_lowercase() -> None:
    assert all(w == w.lower() for w in AI_TELL_WORDS)
