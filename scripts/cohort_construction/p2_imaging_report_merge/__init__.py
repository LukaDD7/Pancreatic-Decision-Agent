"""Correct frozen P2 imaging sources from the hospital imaging-report archive.

The two modules are deliberately separate:

* :mod:`~.archive` reads the untrusted export and recovers each cell's clinical
  role from content, refusing to proceed when its evidence disagrees.
* :mod:`~.merge` rewrites the frozen source bundles into a new private directory
  with the recovered cells and the three named clocks.

:mod:`~.run_merge` is the command-line entry point.
"""