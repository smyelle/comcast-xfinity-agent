"""The `before_model` half of progressive fan-out, against the real callback source.

The engine decides WHAT the turn is; the callback is the only thing that can read CES
state, drop a `pending` placeholder before it reaches intake, and set `partial` on the
response. None of that is reachable from the offline engine harness, so it is driven here
with the CES globals stubbed — the same trick `test_async_tools.py` uses.

Stubs are not a runtime. These pin the callback's own logic (which payloads it ingests,
which flag it sets); whether CES actually runs the dispatched parts concurrently, and
whether a fresh pass sees a background tool's state write, are the two hops no offline
test can reach.
"""

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

_CES_GLOBALS = ("CallbackContext", "LlmRequest", "Content", "Tool", "ces_internal")


class _Part:
  """A CES `Part`, reduced to the two constructors a fan-out narration uses."""

  def __init__(self, kind, **data):
    self.kind = kind
    self.__dict__.update(data)

  @classmethod
  def from_text(cls, text=""):
    return cls("text", text=text)

  @classmethod
  def from_function_call(cls, name="", args=None):
    return cls("call", name=name, args=args or {})

  @classmethod
  def from_json(cls, payload=""):
    return cls("json", payload=payload)


class _LlmResponse:
  def __init__(self, parts):
    self.parts = parts
    self.partial = None

  @classmethod
  def from_parts(cls, parts):
    return cls(parts)


class _Config:
  def __init__(self):
    self.system_instruction = "base"
    self.hidden = []

  def hide_tool(self, name):
    self.hidden.append(name)


class _Request:
  def __init__(self, contents=None):
    self.config = _Config()
    self.contents = contents if contents is not None else []


class _Ctx:
  def __init__(self, state=None):
    self.state = dict(state or {})


def _load():
  path = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
      "src/flows/engine/framework/callbacks/before_model.py")
  spec = importlib.util.spec_from_file_location("_bm_fanout", path)
  mod = importlib.util.module_from_spec(spec)
  for name in _CES_GLOBALS:
    setattr(mod, name, type(name, (), {}))
  mod.Part = _Part
  mod.LlmResponse = _LlmResponse
  mod.tools = type("tools", (), {})
  spec.loader.exec_module(mod)
  return mod


BM = _load()


# ── The placeholder ──────────────────────────────────────────────────────────


def test_the_platform_placeholder_is_recognized_in_both_shapes():
  """`after_tool` unwraps a top-level `result` key, so by the time a payload reaches
  the callback it can be either shape."""
  assert BM._is_pending({"result": "pending"})
  assert BM._is_pending("pending")
  assert BM._is_pending("PENDING ")


def test_a_real_result_is_not_a_placeholder():
  """The cost of a false positive is a result silently thrown away."""
  assert not BM._is_pending({"success": True, "stock": "2"})
  assert not BM._is_pending({"result": "pending review"})
  assert not BM._is_pending(None)


# ── Reading the publications ─────────────────────────────────────────────────


_FAN = {
    "group": "diagnostics",
    "legs": ["inventory", "shipping"],
    "tools": {"inventory": "diagnostics_inventory_leg",
              "shipping": "diagnostics_shipping_leg"},
    "done_legs": [],
}


def test_a_published_leg_is_read_out_of_its_own_state_key():
  ctx = _Ctx({"diagnostics_inventory": json.dumps({"success": True, "stock": "2"})})
  got = BM._fanout_publications(ctx, {"_fanout": dict(_FAN)})
  assert got == [("inventory", "diagnostics_inventory_leg",
                  {"success": True, "stock": "2"})]


def test_a_leg_that_has_not_published_is_not_reported():
  """The whole point of the separate keys: an absent one is simply still running."""
  ctx = _Ctx({"diagnostics_inventory": json.dumps({"success": True})})
  assert [leg for leg, _t, _p in
          BM._fanout_publications(ctx, {"_fanout": dict(_FAN)})] == ["inventory"]


def test_a_leg_already_ingested_is_not_read_again():
  """Otherwise its `then_say` is spoken once per pass for the rest of the group."""
  ctx = _Ctx({"diagnostics_inventory": json.dumps({"success": True})})
  fan = dict(_FAN, done_legs=["inventory"])
  assert BM._fanout_publications(ctx, {"_fanout": fan}) == []


def test_every_outstanding_leg_is_checked_not_only_the_one_the_watcher_named():
  """The watcher's job is to make the pass HAPPEN. Two legs landing inside one window
  are both picked up, and a watcher that under-reports costs nothing."""
  ctx = _Ctx({"diagnostics_inventory": json.dumps({"success": True}),
              "diagnostics_shipping": json.dumps({"success": True})})
  assert [leg for leg, _t, _p in
          BM._fanout_publications(ctx, {"_fanout": dict(_FAN)})] == [
              "inventory", "shipping"]


def test_an_unparseable_publication_is_kept_rather_than_dropped():
  """A leg that wrote something the framework cannot read still REPORTED. Dropping it
  would leave the group outstanding on a leg that has already finished."""
  ctx = _Ctx({"diagnostics_inventory": "not json"})
  _leg, _tool, payload = BM._fanout_publications(ctx, {"_fanout": dict(_FAN)})[0]
  assert payload == {"raw": "not json"}


def test_no_group_in_flight_reads_no_state():
  assert BM._fanout_publications(_Ctx({"diagnostics_inventory": "{}"}), {}) == []


# ── The narration response ───────────────────────────────────────────────────


def _apply(action, state=None, contents=None):
  ctx = _Ctx(state)
  # A preempt that dispatches a tool is honoured with EMPTY contents (it is a silent
  # engine-driven fire); one that only speaks needs a user turn to react to.
  req = _Request(contents=[] if contents is None else contents)
  resp = BM._apply_directive(ctx, req, {}, action, "test")
  return ctx, req, resp


def test_a_fanout_narration_is_marked_partial():
  """`partial` is what speaks the line WITHOUT ending the turn. A full preempt hands
  the floor back and abandons the legs still in flight."""
  _ctx, _req, resp = _apply({
      "preempt": True, "partial": True, "message": "The line test is back.",
      "function_call": {"name": "diagnostics_watch", "args": {"seen": "line"}},
      "hide_tools": [],
  })
  assert resp.partial is True
  assert [p.kind for p in resp.parts] == ["text", "call"]
  assert resp.parts[1].name == "diagnostics_watch"
  assert resp.parts[1].args == {"seen": "line"}


def test_an_ordinary_preempt_is_not_partial():
  """Back-compat: the flag is opt-in, so every turn that predates this is unchanged."""
  _ctx, _req, resp = _apply(
      {"preempt": True, "message": "Anything else?", "hide_tools": []},
      contents=[object()])
  assert resp.partial is None


def test_a_bare_re_dispatch_speaks_nothing():
  """An empty watch window costs a pass, not a sentence: the response carries the
  call and no text at all."""
  _ctx, _req, resp = _apply({
      "preempt": True, "message": "",
      "function_call": {"name": "diagnostics_watch", "args": {"seen": ""}},
      "hide_tools": [],
  })
  assert [p.kind for p in resp.parts] == ["call"]


def test_the_dispatch_turn_clears_the_previous_runs_publications():
  """State outlives the group; a re-fire reading the last run's value would narrate a
  stale finding before the backend had been asked."""
  ctx, _req, _resp = _apply(
      {"preempt": True, "message": "one moment",
       "function_calls": [{"name": "diagnostics_inventory_leg", "args": {}}],
       "state_writes": {"pop": ["diagnostics_inventory"]},
       "hide_tools": []},
      state={"diagnostics_inventory": "stale"})
  assert "diagnostics_inventory" not in ctx.state
