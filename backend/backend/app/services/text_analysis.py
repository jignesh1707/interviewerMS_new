from app.core.logging import get_logger

logger = get_logger(__name__)

FILLER_WORDS = {
    "um", "uh", "erm", "ah", "like", "basically", "actually", "literally",
    "honestly", "so", "right", "you know", "i mean", "kind of", "sort of",
    "stuff", "things", "whatever", "anyway",
}

HEDGE_WORDS = {
    "maybe", "perhaps", "probably", "possibly", "i guess", "i think",
    "not sure", "might", "somewhat", "hopefully",
}

STAR_MARKERS = {
    "situation": {"situation", "context", "at the time", "we had", "the problem was", "project", "team was", "deadline"},
    "task": {"task", "goal", "objective", "responsible", "my role", "i was asked", "needed to", "had to"},
    "action": {"i built", "i implemented", "i led", "i designed", "i created", "i decided", "i wrote", "i proposed", "i migrated", "i automated", "we implemented", "approach", "steps"},
    "result": {"as a result", "resulting in", "improved", "reduced", "increased", "delivered", "impact", "saved", "achieved", "percent", "%", "we shipped", "which led"},
}

ACTION_VERBS = {
    "built", "implemented", "designed", "led", "created", "migrated", "automated",
    "optimized", "launched", "delivered", "reduced", "increased", "improved",
    "mentored", "architected", "debugged", "deployed", "refactored", "drove",
}

POSITIVE_SIGNALS = {
    "user impact", "customer", "metric", "data", "test", "monitor", "scalable",
    "collaborated", "ownership", "trade-off", "tradeoff", "root cause", "postmortem",
}

NEGATIVE_SIGNALS = {"just", "only", "i don't know", "no idea", "never", "can't"}


def _tokenize(text: str) -> list[str]:
    cleaned = "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in text)
    return [token for token in cleaned.split() if token]


def _count_phrases(text: str, phrases: set[str]) -> dict[str, int]:
    lowered = text.lower()
    counts: dict[str, int] = {}
    for phrase in phrases:
        if " " in phrase:
            found = lowered.count(phrase)
        else:
            found = sum(1 for token in _tokenize(lowered) if token == phrase)
        if found:
            counts[phrase] = found
    return counts


def analyze_transcript(text: str, duration_seconds: float | None = None) -> dict:
    words = _tokenize(text)
    word_count = len(words)
    sentences = [segment.strip() for segment in text.replace("\n", " ").split(".") if segment.strip()]
    sentence_count = max(1, len(sentences))
    avg_sentence_length = round(word_count / sentence_count, 1)

    filler_counts = _count_phrases(text, FILLER_WORDS)
    hedge_counts = _count_phrases(text, HEDGE_WORDS)
    filler_total = sum(filler_counts.values())

    star_hits = {stage: sorted(_count_phrases(text, markers).keys()) for stage, markers in STAR_MARKERS.items()}
    star_coverage = round(sum(1 for hits in star_hits.values() if hits) / len(star_hits), 2)

    verbs_used = sorted({verb for verb in ACTION_VERBS if verb in words})
    positive_hits = sorted(_count_phrases(text, POSITIVE_SIGNALS).keys())
    negative_hits = sorted(_count_phrases(text, NEGATIVE_SIGNALS).keys())

    unique_ratio = round(len(set(words)) / word_count, 3) if word_count else 0.0
    words_per_minute = None
    if duration_seconds and duration_seconds > 1:
        words_per_minute = round(word_count / (duration_seconds / 60.0), 1)

    filler_ratio = round(filler_total / word_count, 4) if word_count else 0.0

    return {
        "word_count": word_count,
        "sentence_count": len(sentences),
        "avg_sentence_length": avg_sentence_length,
        "unique_word_ratio": unique_ratio,
        "duration_seconds": round(duration_seconds, 2) if duration_seconds else None,
        "words_per_minute": words_per_minute,
        "filler_counts": filler_counts,
        "filler_total": filler_total,
        "filler_ratio": filler_ratio,
        "hedge_counts": hedge_counts,
        "hedge_total": sum(hedge_counts.values()),
        "star_coverage": star_coverage,
        "star_hits": star_hits,
        "action_verbs": verbs_used,
        "positive_signals": positive_hits,
        "negative_signals": negative_hits,
    }


def heuristic_score(metrics: dict) -> dict:
    word_count = metrics.get("word_count", 0)
    filler_ratio = metrics.get("filler_ratio", 0.0)
    star_coverage = metrics.get("star_coverage", 0.0)
    verbs = metrics.get("action_verbs", [])
    positives = metrics.get("positive_signals", [])
    negatives = metrics.get("negative_signals", [])
    wpm = metrics.get("words_per_minute")

    if word_count < 25:
        depth = 30.0
    elif word_count < 60:
        depth = 55.0
    elif word_count <= 320:
        depth = 90.0
    elif word_count <= 500:
        depth = 78.0
    else:
        depth = 65.0

    structure = 35.0 + star_coverage * 60.0
    if metrics.get("avg_sentence_length", 0) > 35:
        structure -= 10.0
    structure = max(0.0, min(100.0, structure))

    delivery = 100.0 - filler_ratio * 900.0
    if wpm is not None:
        if wpm < 90:
            delivery -= 12.0
        elif wpm > 190:
            delivery -= 12.0
    delivery = max(0.0, min(100.0, delivery))

    impact = 45.0 + min(len(positives), 4) * 10.0 + min(len(verbs), 6) * 4.0 - len(negatives) * 6.0
    impact = max(0.0, min(100.0, impact))

    overall = round(depth * 0.3 + structure * 0.3 + delivery * 0.2 + impact * 0.2, 1)
    return {
        "clarity": round(delivery, 1),
        "structure": round(structure, 1),
        "depth": round(depth, 1),
        "impact": round(impact, 1),
        "overall": overall,
    }


def aggregate_heuristic_scores(per_answer: list[dict]) -> dict:
    if not per_answer:
        return {"clarity": 0.0, "structure": 0.0, "depth": 0.0, "impact": 0.0, "overall": 0.0}
    keys = ("clarity", "structure", "depth", "impact", "overall")
    totals = {key: 0.0 for key in keys}
    for item in per_answer:
        for key in keys:
            totals[key] += float(item.get(key, 0.0))
    count = len(per_answer)
    return {key: round(value / count, 1) for key, value in totals.items()}
