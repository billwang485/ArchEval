"""Shared base classes for in-tree evaluators.

Concrete evaluators live one directory up under ``evaluators/<name>/``
and are registered in ``archbench/evaluators/__init__.py``. This package
holds the bits multiple evaluators share (e.g. the offline-surrogate
calibration algorithm, which is sim-agnostic but currently has only
one wiring — ChampSim).
"""
