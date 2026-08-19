"""A callback that CALLS a module-level helper instead of re-declaring it.

Only a callback's own source is rendered into the deployed file, so a helper beside it
used to be undefined in the sandbox. Authors worked around that by copying the helper
into the body -- and writing it out again in the second callback that needed it, where
the copies drift.

Referenced helpers are now carried, dependency-first, the same way a referenced pydantic
model already was. Run this to see what gets emitted:

    python packages/flows/examples/inline_callback_helpers.py
"""

from flows.authoring import tools as _tools

SIZE_CUES = {"ALL": ["everything", "all of them"], "ONE": ["just one", "only my"]}


def _size_of(text: str) -> str:
  """Shared by both callbacks; each used to need its own copy."""
  for value, cues in SIZE_CUES.items():
    if any(cue in text for cue in cues):
      return value
  return ""


def before_model_callback(callback_context, llm_request) -> None:
  """Reads the helper and the map beside it, both at module scope."""
  callback_context.state["size"] = _size_of("just one device")


if __name__ == "__main__":
  source = _tools.render_callable(before_model_callback)
  # Dependency-first: the map is defined above the helper that reads it, and the helper
  # above the callback that calls it. The sandbox imports nothing, so order is the only
  # thing keeping the emitted file from raising on its first call.
  assert source.index("SIZE_CUES = {") < source.index("def _size_of(")
  assert source.index("def _size_of(") < source.index("def before_model_callback(")
  print(source)
