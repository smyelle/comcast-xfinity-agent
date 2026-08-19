"""Shared test helpers for the flows package."""

from __future__ import annotations

import pytest


@pytest.fixture()
def dag_config():
  """Load an emitted `<id>_dag` module and return the config it produces.

  Emitted dag source is not a readable Python literal any more — the config travels as a
  compact JSON string that `json.loads` expands at call time, because CES recompiles the
  module on every invocation and a large literal costs milliseconds each time.

  So a test that greps the source for `'flow_types'` or `'tool': 'x_leg'` now fails on
  the QUOTING while the config is perfectly correct. Those tests meant to assert content;
  this gives them the content. Grep the source only when the source itself is the subject.
  """
  def load(source: str, config_id: str) -> dict:
    namespace: dict = {}
    exec(compile(source, f"<dag_config_{config_id}>", "exec"), namespace)  # noqa: S102
    return namespace[f"{config_id}_dag"]()

  return load
