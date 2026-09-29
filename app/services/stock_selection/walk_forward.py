from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Generic, Iterable, Sequence, TypeVar

from app.services.stock_selection.schemas import LabeledSample


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class LeakageViolation:
    sample_id: str
    prediction_date: date
    reason: str


@dataclass(frozen=True, slots=True)
class LeakageAudit:
    prediction_date: date
    sample_count: int
    violation_count: int
    violations: tuple[LeakageViolation, ...]

    @property
    def passed(self) -> bool:
        return self.violation_count == 0


class MaturityQueue(Generic[T]):
    """Release samples only after their labels were observable.

    The comparison is intentionally strict. A label that becomes known at the
    close of T cannot be used to create another T-close prediction.
    """

    def __init__(
        self,
        items: Iterable[T],
        *,
        available_date: Callable[[T], date],
    ) -> None:
        self._available_date = available_date
        self._items = sorted(list(items), key=available_date)
        self._cursor = 0

    @property
    def remaining(self) -> int:
        return len(self._items) - self._cursor

    def release_before(self, prediction_date: date) -> list[T]:
        released: list[T] = []
        while self._cursor < len(self._items):
            item = self._items[self._cursor]
            if self._available_date(item) >= prediction_date:
                break
            released.append(item)
            self._cursor += 1
        return released


class PointInTimeTrainingPool(Generic[T]):
    """Accumulate only mature samples that pass purge and embargo rules."""

    def __init__(
        self,
        items: Iterable[T],
        *,
        trading_dates: Sequence[date],
        feature_date: Callable[[T], date],
        label_end_date: Callable[[T], date],
        label_available_date: Callable[[T], date],
        sample_id: Callable[[T], str],
        purge_sessions: int,
        embargo_sessions: int = 0,
        include_item: Callable[[T], bool] | None = None,
    ) -> None:
        if purge_sessions < 0 or embargo_sessions < 0:
            raise ValueError("purge_sessions and embargo_sessions must not be negative")
        dates = list(trading_dates)
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("trading_dates must be unique and ascending")
        self._date_index = {value: index for index, value in enumerate(dates)}
        self._feature_date = feature_date
        self._label_end_date = label_end_date
        self._label_available_date = label_available_date
        self._sample_id = sample_id
        self._include_item = include_item or (lambda item: bool(getattr(item, "tradable", True)))
        self._purge_sessions = purge_sessions
        self._embargo_sessions = embargo_sessions
        self._queue = MaturityQueue(items, available_date=label_available_date)
        self._deferred: list[T] = []
        self._pool: list[T] = []
        self._seen: set[str] = set()

    @property
    def samples(self) -> tuple[T, ...]:
        return tuple(self._pool)

    def _eligible(self, item: T, prediction_date: date) -> bool:
        if self._label_end_date(item) >= prediction_date:
            return False
        if self._label_available_date(item) >= prediction_date:
            return False
        prediction_index = self._date_index.get(prediction_date)
        feature_index = self._date_index.get(self._feature_date(item))
        if prediction_index is None or feature_index is None:
            raise ValueError("sample and prediction dates must exist in trading_dates")
        latest_feature_index = prediction_index - self._purge_sessions - self._embargo_sessions - 1
        return feature_index <= latest_feature_index

    def advance(self, prediction_date: date) -> tuple[T, ...]:
        if prediction_date not in self._date_index:
            raise ValueError("prediction_date must exist in trading_dates")
        self._deferred.extend(self._queue.release_before(prediction_date))
        still_deferred: list[T] = []
        for item in self._deferred:
            identifier = self._sample_id(item)
            if identifier in self._seen:
                continue
            if not self._include_item(item):
                self._seen.add(identifier)
                continue
            if self._eligible(item, prediction_date):
                self._pool.append(item)
                self._seen.add(identifier)
            else:
                still_deferred.append(item)
        self._deferred = still_deferred
        return self.samples


def audit_training_samples(
    samples: Iterable[LabeledSample],
    *,
    prediction_date: date,
    trading_dates: Sequence[date],
    purge_sessions: int,
    embargo_sessions: int = 0,
) -> LeakageAudit:
    date_index = {value: index for index, value in enumerate(trading_dates)}
    if prediction_date not in date_index:
        raise ValueError("prediction_date must exist in trading_dates")
    latest_feature_index = date_index[prediction_date] - purge_sessions - embargo_sessions - 1
    violations: list[LeakageViolation] = []
    sample_rows = list(samples)
    for sample in sample_rows:
        reasons: list[str] = []
        if sample.feature_date >= prediction_date:
            reasons.append("feature_not_historical")
        if sample.label_end_date >= prediction_date:
            reasons.append("label_window_not_ended")
        if sample.label_available_date >= prediction_date:
            reasons.append("label_not_available")
        feature_index = date_index.get(sample.feature_date)
        if feature_index is None:
            reasons.append("feature_date_missing_from_calendar")
        elif feature_index > latest_feature_index:
            reasons.append("purge_or_embargo_violation")
        for reason in reasons:
            violations.append(
                LeakageViolation(
                    sample_id=sample.sample_id,
                    prediction_date=prediction_date,
                    reason=reason,
                )
            )
    return LeakageAudit(
        prediction_date=prediction_date,
        sample_count=len(sample_rows),
        violation_count=len(violations),
        violations=tuple(violations),
    )
