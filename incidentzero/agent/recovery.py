from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from incidentzero.model.errors import TransientModelError

T = TypeVar("T")


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, sleeper: Callable[[float], None] = time.sleep, backoff_base: float = 0.5) -> None:
        self.max_attempts = max_attempts
        self.sleeper = sleeper
        self.backoff_base = backoff_base

    def call_model(self, fn: Callable[[], T]) -> T:
        """Execute a model call with bounded exponential backoff for TransientModelError only.

        Permanent errors and non-model failures fail fast.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                return fn()
            except TransientModelError:
                if attempts >= self.max_attempts:
                    raise
                backoff = self.backoff_base * (2 ** (attempts - 1))
                self.sleeper(backoff)
