"""Bounded real-data development preflight and explicitly authorized execution.

No patient data is embedded in this package.  ``preflight`` is always offline;
``execute`` additionally requires the batch-specific private authorization
artifact.  Neither operation creates a clinician assignment or a clinical
Reference.  See project AGENTS.md for original-text and review boundaries.
"""
from .batch import execute_batch, preflight_batch, replay_batch

__all__ = ['execute_batch', 'preflight_batch', 'replay_batch']
