"""Minimal agent harness for P2 checkpoint/episode reconstruction.

Not a workflow of fixed request shapes. One append-only session log, one agent
loop with tool calls, on-demand skills, spill for oversized tool output, and
automatic context condensation instead of a hand-tuned character ceiling.

Frozen batches, their caches and their receipts are never read or written here.
"""
__all__ = ["VERSION"]

VERSION = "p2-checkpoint-harness-0.1.0"