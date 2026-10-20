"""Small transparent metrics; unjudged material is reported, never hidden."""
import math


def precision_at_k(ranking, grades, k):
    return sum(grades.get(mid) == 2 for mid in ranking[:k]) / k


def ranking_metrics(ranking, grades):
    positives = {mid for mid, grade in grades.items() if grade == 2}
    found = positives.intersection(ranking)
    known_hits = sum(grades.get(mid) == 2 for mid in ranking)
    unknown = sum(mid not in grades for mid in ranking)
    n = len(ranking)
    return dict(p5=precision_at_k(ranking, grades, 5), p10=precision_at_k(ranking, grades, 10),
        returned=n, judged_returned=n - unknown, unjudged_returned=unknown,
        precision_returned_lower=known_hits / n if n else None,
        precision_returned_upper=(known_hits + unknown) / n if n else None,
        recall_known=len(found) / len(positives) if positives else None,
        missed_known=len(positives - set(ranking)), known_relevant=len(positives))


def fit_threshold(scores, labels):
    """F0.5 favors precision; the cutoff itself is fitted on every train pair."""
    if not scores or len(scores) != len(labels) or not any(labels) or not all(math.isfinite(v) for v in scores):
        raise ValueError('Need finite scores and labeled positives.')
    unique = sorted(set(scores))
    candidates = [unique[0] - 1] + [(a + b) / 2 for a, b in zip(unique, unique[1:])] + [unique[-1] + 1]
    best = None
    for threshold in candidates:
        tp = sum(score >= threshold and label for score, label in zip(scores, labels))
        fp = sum(score >= threshold and not label for score, label in zip(scores, labels))
        fn = sum(score < threshold and label for score, label in zip(scores, labels))
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        f05 = 1.25 * precision * recall / (.25 * precision + recall) if precision + recall else 0
        candidate = (f05, precision, recall, threshold)
        if best is None or candidate > best:
            best = candidate
    return best[3]
