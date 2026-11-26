"""Unchanged AI_OS ridge calibration math and quality gate.
Source: skills/literature-intelligence/trainer.py
SHA256: b22b4816a28166f6e105d539423f4e0f7c93d2e192adcb47686a1a067fa9c21b
"""
from __future__ import annotations
import math
from typing import Any

TRAINABLE=("important","useful","neutral","irrelevant")
LABEL_VALUE={"important":3.0,"useful":2.0,"neutral":1.0,"irrelevant":0.0}
POSITIVE={"important","useful"}
RELEVANCE_CLASSES=("HIGH","MEDIUM","LOW","IRRELEVANT")
CLASS_THRESHOLDS={"high":2.5,"medium":1.5,"low":0.5}
FEATURE_NAMES=("research_question_match","research_phase_match","transfer_value","method_match","application_match","attention_memory","keyword_evidence")
MIN_NEW_FEEDBACK=20
RIDGE=1.0

def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    size = len(vector)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular calibration matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * other for value, other in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(size)]


def fit(vectors: list[list[float]], targets: list[float]) -> tuple[float, list[float]]:
    rows = [[1.0, *vector] for vector in vectors]
    size = len(rows[0])
    matrix = [[sum(row[i] * row[j] for row in rows) for j in range(size)] for i in range(size)]
    for index in range(1, size):
        matrix[index][index] += RIDGE
    rhs = [sum(row[index] * target for row, target in zip(rows, targets)) for index in range(size)]
    coefficients = solve(matrix, rhs)
    return coefficients[0], coefficients[1:]


def predict(vector: list[float], intercept: float, weights: list[float]) -> float:
    return intercept + sum(value * weight for value, weight in zip(vector, weights))


def baseline_score(vector: list[float]) -> float:
    return sum(value * weight for value, weight in zip(vector[:6], (64, 32, 16, 8, 4, 2))) + vector[6]


def score_to_attention(score: float, thresholds: dict[str, float] | None = None) -> str:
    """Map ordinal relevance to a stable four-class prediction."""
    limits = thresholds or CLASS_THRESHOLDS
    if score >= float(limits["high"]):
        return "HIGH"
    if score >= float(limits["medium"]):
        return "MEDIUM"
    if score >= float(limits["low"]):
        return "LOW"
    return "IRRELEVANT"


def ndcg(scores: list[float], labels: list[str]) -> float:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    ideal = sorted(range(len(labels)), key=lambda index: (-LABEL_VALUE[labels[index]], index))
    def gain(indices: list[int]) -> float:
        return sum((2 ** LABEL_VALUE[labels[index]] - 1) / math.log2(rank + 2) for rank, index in enumerate(indices))
    denominator = gain(ideal)
    return gain(order) / denominator if denominator else 0.0


def ranking_metrics(scores: list[float], labels: list[str]) -> dict[str, float | int]:
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    top_k = max(1, math.ceil(len(labels) * 0.25))
    top = order[:top_k]
    positives = {index for index, label in enumerate(labels) if label in POSITIVE}
    irrelevant = {index for index, label in enumerate(labels) if label == "irrelevant"}
    recall = len(positives & set(top)) / len(positives) if positives else 0.0
    return {
        "important_useful_recall": recall,
        "false_negative_signal_rate": 1.0 - recall if positives else 0.0,
        "false_positive_signal_rate": len(irrelevant & set(top)) / top_k,
        "ranking_utility": ndcg(scores, labels),
        "top_ranked_positive_rate": len(positives & set(top)) / top_k,
        "top_k": top_k,
    }


def classification_metrics(predictions: list[str], labels: list[str]) -> dict[str, Any]:
    matrix = {label: {level: 0 for level in RELEVANCE_CLASSES} for label in TRAINABLE}
    for label, prediction in zip(labels, predictions):
        if prediction not in RELEVANCE_CLASSES:
            raise ValueError(f"unknown relevance class: {prediction}")
        matrix[label][prediction] += 1

    def rate(label_set: set[str], accepted: set[str]) -> float:
        indices = [index for index, label in enumerate(labels) if label in label_set]
        return (
            sum(predictions[index] in accepted for index in indices) / len(indices)
            if indices else 0.0
        )

    predicted_value = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0, "IRRELEVANT": 0.0}
    return {
        "confusion_matrix": matrix,
        "important_recall_as_high": rate({"important"}, {"HIGH"}),
        "important_recall_as_high_or_medium": rate({"important"}, {"HIGH", "MEDIUM"}),
        "important_predicted_irrelevant_rate": rate({"important"}, {"IRRELEVANT"}),
        "useful_recall_as_high_or_medium": rate({"useful"}, {"HIGH", "MEDIUM"}),
        "irrelevant_predicted_high_rate": rate({"irrelevant"}, {"HIGH"}),
        "positive_predicted_irrelevant_rate": rate(POSITIVE, {"IRRELEVANT"}),
        "classification_utility": sum(
            1.0 - abs(LABEL_VALUE[label] - predicted_value[prediction]) / 3.0
            for label, prediction in zip(labels, predictions)
        ) / len(labels) if labels else 0.0,
    }


def evaluation_metrics(scores: list[float], predictions: list[str], labels: list[str]) -> dict[str, Any]:
    return {**ranking_metrics(scores, labels), **classification_metrics(predictions, labels)}


def candidate_predictions(vectors: list[list[float]], targets: list[float]) -> list[float]:
    predictions = []
    for held_out in range(len(vectors)):
        train_x = [value for index, value in enumerate(vectors) if index != held_out]
        train_y = [value for index, value in enumerate(targets) if index != held_out]
        intercept, weights = fit(train_x, train_y)
        predictions.append(predict(vectors[held_out], intercept, weights))
    return predictions


def gate(candidate: dict[str, Any], active: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    if candidate["ranking_utility"] <= active["ranking_utility"] + 1e-9:
        reasons.append("ranking_utility_not_improved")
    if candidate["important_useful_recall"] + 0.05 < active["important_useful_recall"]:
        reasons.append("important_useful_recall_regressed")
    if candidate["false_negative_signal_rate"] > active["false_negative_signal_rate"] + 0.05:
        reasons.append("false_negative_rate_regressed")
    if candidate["false_positive_signal_rate"] > active["false_positive_signal_rate"] + 0.05:
        reasons.append("false_positive_rate_regressed")
    if candidate["top_ranked_positive_rate"] + 1e-9 < active["top_ranked_positive_rate"]:
        reasons.append("top_ranked_positive_rate_regressed")
    if candidate["important_recall_as_high"] + 0.05 < active["important_recall_as_high"]:
        reasons.append("important_high_recall_regressed")
    if candidate["important_recall_as_high_or_medium"] + 0.05 < active["important_recall_as_high_or_medium"]:
        reasons.append("important_high_or_medium_recall_regressed")
    if candidate["useful_recall_as_high_or_medium"] + 0.05 < active["useful_recall_as_high_or_medium"]:
        reasons.append("useful_high_or_medium_recall_regressed")
    if candidate["positive_predicted_irrelevant_rate"] > active["positive_predicted_irrelevant_rate"] + 0.05:
        reasons.append("positive_predicted_irrelevant_regressed")
    if candidate["important_predicted_irrelevant_rate"] > active["important_predicted_irrelevant_rate"] + 1e-9:
        reasons.append("important_predicted_irrelevant_regressed")
    if candidate["irrelevant_predicted_high_rate"] > active["irrelevant_predicted_high_rate"] + 0.05:
        reasons.append("irrelevant_predicted_high_regressed")
    return not reasons, reasons
