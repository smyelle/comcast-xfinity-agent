"""A slot's ``description=`` reaches the model-facing tool description.

A generated setter otherwise ships the SDK default ``"Record the value for <name>."`` as
its ``pythonFunction.description`` -- the string the model reads when deciding whether/how
to call the tool -- which tells the model nothing. ``intent_slot`` / ``passive_slot`` now
take ``description=``, emitted onto that field; a slot without one keeps the default.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_slot_description.py
"""

from __future__ import annotations

import json
import os

import flows

_DESC = "Record whether the caller's issue is about billing or a technical fault."


def _emit(tmp_path) -> str:
  f = flows.Flow("triage", root_agent="Triage_Agent")
  f.add(
      flows.intent_slot(
          "topic", {"billing": ["bill", "charge"], "tech": ["broken", "down"]},
          description=_DESC),
      # No description -> keeps the SDK default.
      flows.passive_slot("noted", kind="intent", option_cues={"yes": ["ok", "sure"]}),
      flows.announce("done", ["all set"], requires=["topic"], end=True),
  )
  out = str(tmp_path / "app")
  flows.build_app(flows.App(root_flow=f, app_display_name="Triage"), out)
  return out


def _tool_description(out: str, name: str) -> str:
  with open(os.path.join(out, "tools", name, f"{name}.json")) as fh:
    return json.load(fh)["pythonFunction"]["description"]


def test_description_reaches_the_setter_json(tmp_path):
  out = _emit(tmp_path)
  assert _tool_description(out, "set_topic") == _DESC


def test_absent_description_keeps_the_sdk_default(tmp_path):
  out = _emit(tmp_path)
  assert _tool_description(out, "set_noted") == "Record the value for set_noted."
