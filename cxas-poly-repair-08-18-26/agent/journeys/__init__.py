"""One module per user journey, plus the machinery they share.

A journey module declares everything that journey owns, and nothing another journey
owns. WHEN it contributes is decided by the assembly in `app.py`, not in the module --
the engine fires the first task whose condition holds, so order is the contract there.
A fragment says WHAT a journey contributes; the assembly says WHEN.

`common/` holds what no single journey owns: if only one journey reads it, it belongs
in that journey's module instead.
"""
