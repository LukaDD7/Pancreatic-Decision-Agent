"""Keep oversized tool output out of the conversation, without losing it.

dsh's `spill` family does exactly this and the numbers here justify copying it:
in the frozen review batch, 83.6% of every evidence-pool byte was scaffolding
around 146-character text blocks, and 94% of block sends were repeats. The fix
is not a bigger ceiling; it is not sending the bytes until they are needed.

A spilled result becomes a preview plus a locator plus retrieval guidance. The
full text is written to a private file and can be read back with an offset. If
the write fails, the original content stays visible: degraded recall is
acceptable, silent truncation is not.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

HEAD_CHARS = 600
TAIL_CHARS = 300
PREVIEW_MARKER = "[spilled-result]"


class SpillStore:
    """Store oversized text outside the context and hand back a locator."""

    def __init__(self, directory, *, max_inline_tokens=None, meter=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_inline_tokens = max_inline_tokens
        self.meter = meter
        self.written = []

    def _tokens(self, text):
        if self.meter is not None:
            return self.meter.measure_text(text)
        return len(text) // 2

    def _path_for(self, text):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        return self.directory / (digest + ".txt")

    def _write(self, text):
        path = self._path_for(text)
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        return path

    def apply(self, result):
        """Return ``result`` unchanged, or a preview carrying a locator."""
        if self.max_inline_tokens is None:
            return result
        content = result.content or ""
        if self._tokens(content) <= self.max_inline_tokens:
            return result
        try:
            path = self._write(content)
        except OSError:
            # Keeping the payload visible is better than losing it.
            return result
        self.written.append(dict(tool=result.name, path=str(path),
                                 characters=len(content)))
        preview = (
            "%s output was %d characters and has been stored outside this "
            "conversation.\nlocator: %s\ntotal_characters: %d\n\n"
            "Read it back with read_spilled(path, start, end). Read only the "
            "range you need; the full text is not repeated here.\n\n"
            "--- head ---\n%s\n\n--- tail ---\n%s"
            % (result.name, len(content), path, len(content),
               content[:HEAD_CHARS], content[-TAIL_CHARS:]))
        result.content = preview
        result.metadata = dict(result.metadata or {}, spilled=True,
                               characters=len(content), locator=str(path))
        return result


def read_spilled(path, start=0, end=None, *, allowed_root=None):
    """Read a stored span back. Refuses paths outside the spill directory."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError("SPILLED_PATH_MISSING:" + str(path))
    if allowed_root is not None:
        root = Path(allowed_root).resolve()
        resolved = target.resolve()
        if root != resolved and root not in resolved.parents:
            raise PermissionError("SPILLED_PATH_OUTSIDE_STORE")
    text = target.read_text(encoding="utf-8")
    start = max(0, int(start))
    end = len(text) if end is None else min(len(text), int(end))
    return dict(path=str(target), total_characters=len(text), start=start,
                end=end, text=text[start:end])