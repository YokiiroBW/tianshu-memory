"""Narrow capabilities assembled by the project application for domain modules."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceLookup:
    document: Callable
    current: Callable


@dataclass(frozen=True, slots=True)
class DirectoryWriter:
    ensure_project: Callable
    store_version: Callable
    bump: Callable


@dataclass(frozen=True, slots=True)
class ContinuationReader:
    reference: Callable
    query: Callable
    seal: Callable
