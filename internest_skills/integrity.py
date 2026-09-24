"""Server-side analysis of client typing telemetry to detect pasted / machine-generated answers."""
import statistics

MIN_CHARS_FOR_ANALYSIS = 15
MAX_INTERVALS = 2000


def _clean_intervals(raw):
    out = []
    for v in (raw or [])[:MAX_INTERVALS]:
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if 0 <= v <= 10_000:
            out.append(v)
    return out


def analyze_typing(answer_text: str, stats: dict) -> list[str]:
    """Return a list of integrity flags; empty list means the answer looks human-typed."""
    flags = []
    stats = stats if isinstance(stats, dict) else {}
    length = len((answer_text or "").strip())

    if int(stats.get("paste_attempts", 0) or 0) > 0 or int(stats.get("drop_attempts", 0) or 0) > 0:
        flags.append("paste_attempt")

    if length < MIN_CHARS_FOR_ANALYSIS:
        return flags

    if not stats:
        return flags + ["missing_telemetry"]

    keystrokes = int(stats.get("keystrokes", 0) or 0)
    max_insert = int(stats.get("max_single_insert", 0) or 0)
    intervals = _clean_intervals(stats.get("intervals"))

    # Answer is much longer than the number of keys pressed → text was injected.
    if keystrokes < length * 0.6:
        flags.append("bulk_text_injection")

    # One input event added a long chunk (mobile autocorrect adds ≈ one word at most).
    if max_insert > 20:
        flags.append("large_single_insert")

    if len(intervals) >= 20:
        median = statistics.median(intervals)
        stdev = statistics.pstdev(intervals)
        if median < 30:
            flags.append("superhuman_typing_speed")
        if stdev < 10:
            flags.append("robotic_uniform_rhythm")

    return flags
