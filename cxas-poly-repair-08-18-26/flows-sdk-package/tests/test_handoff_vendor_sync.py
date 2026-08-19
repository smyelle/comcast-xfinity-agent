"""Drift gate: the hand-off vendor registry exists TWICE and must stay in step.

`flows.handoff(flows.ujet(...))` knows which vendor shapes exist and what each one
must carry. So does the framework validator, in `validate_dag_config` — and it has
to know INDEPENDENTLY, because a CES tool is a standalone sandbox file that cannot
import the authoring package. So the table is written out twice:

    flows/authoring/handoff.py                     HANDOFF_VENDORS
    framework/tools/validate_dag_config/...py      _HANDOFF_VENDORS

The duplication is not removable here. The obvious fix — a marker key on the wire
so a hand-off payload announces itself instead of being recognized structurally —
was rejected on purpose: those bytes are a live integration contract and changing
them changes what a contact-center platform receives. Both files say "kept in step
by hand", which is exactly the state `test_framework_runtime_sync.py` was written
to end for the byte-synced framework copies.

This is that gate for the registries. It does not remove the duplication; it makes
divergence impossible to miss — a vendor added on one side only, or a required
field added on one side only, fails here and names what to change.

Run: PYTHONPATH=packages/flows/src pytest packages/flows/tests/test_handoff_vendor_sync.py
"""

from __future__ import annotations

import pytest

from flows.authoring import handoff as authoring
from flows.engine import loader as fb


FIX = ("keep flows/authoring/handoff.py:HANDOFF_VENDORS and validate_dag_config's "
       "_HANDOFF_VENDORS in step — a vendor is only shape-checked where it appears "
       "in BOTH")


@pytest.fixture(scope="module")
def validator():
  """The framework validator as the runtime loads it (importlib, off disk)."""
  return fb.load_validator()


def test_the_two_registries_list_the_same_vendors(validator):
  ours = set(authoring.HANDOFF_VENDORS)
  theirs = set(validator._HANDOFF_VENDORS)  # noqa: SLF001 — the point of the gate

  assert ours == theirs, (
      f"hand-off vendor registries disagree: authoring-only={sorted(ours - theirs)}, "
      f"validator-only={sorted(theirs - ours)} — {FIX}")


def test_each_vendor_requires_the_same_fields_on_both_sides(validator):
  """A field required on one side only is worse than one required on neither.

  The authoring builder would refuse a payload the validator happily accepts (or,
  the other way round, the SDK would emit a payload the validator then errors on
  in CI) — and either way the two disagree about what reaches the platform.
  """
  ours = {k: tuple(v) for k, v in authoring.HANDOFF_VENDORS.items()}
  theirs = {k: tuple(required)
            for k, (_label, required) in validator._HANDOFF_VENDORS.items()}  # noqa: SLF001

  assert ours == theirs, f"required hand-off fields disagree — {FIX}"


def test_the_shared_handoff_constants_agree(validator):
  """The scalars that ride alongside the table and are duplicated with it."""
  assert authoring.UJET_KEY == validator._UJET_KEY  # noqa: SLF001
  assert authoring.DIALOGFLOW_KEY == validator._DIALOGFLOW_KEY  # noqa: SLF001
  assert authoring.CXAS_KEY == validator._CXAS_KEY  # noqa: SLF001
  assert (authoring.UJET_ESCALATION_ACTION
          == validator._UJET_ESCALATION_ACTION)  # noqa: SLF001
  # The `end_session` reason — every containment report reads this string, so the
  # two sides disagreeing means the validator warns about correct configs.
  assert authoring.HANDOFF_REASON == validator._HANDOFF_REASON  # noqa: SLF001


def test_every_registered_vendor_is_recognized_by_the_validator(validator):
  """Behaviour, not just text: what the builders emit must classify as a hand-off.

  Two identical tables that the validator's `_handoff_shape` does not actually
  consult would pass the comparisons above and still leave a vendor unchecked.
  """
  built = {
      authoring.UJET_KEY: authoring.ujet(menu_id="90"),
      authoring.DIALOGFLOW_KEY: authoring.dialogflow_cx(
          project="p", location="us", agent_id="a"),
      authoring.CXAS_KEY: authoring.cxas(
          project="p", location="us", app_id="a"),
  }
  assert set(built) == set(authoring.HANDOFF_VENDORS), (
      "a vendor in the registry has no builder exercised here (or vice versa) — "
      "add it, or the gate silently stops covering it")

  for key, payload in built.items():
    shape = validator._handoff_shape(payload.data)  # noqa: SLF001
    assert shape is not None, (
        f"the validator does not recognize a {key} payload the SDK emits, so an "
        f"unpaired one would ship unflagged — {FIX}")
    _label, _is_escalation, missing = shape
    assert not missing, (
        f"the SDK's own {key} payload is missing fields the validator requires: "
        f"{missing} — {FIX}")


def test_a_registry_required_field_the_builder_cannot_fill_is_fatal(monkeypatch):
  """The authoring table is load-bearing, not decoration.

  If it were only a copy kept for the gate to read, a field added to it would
  change nothing and the gate would go green on a builder that never emits the
  field. `ujet()` reads the table, so the mismatch surfaces at authoring time.
  """
  monkeypatch.setitem(authoring.HANDOFF_VENDORS, authoring.UJET_KEY,
                      ("menu_id", "action", "escalation_reason", "queue_id"))
  with pytest.raises(ValueError, match="queue_id"):
    authoring.ujet(menu_id="90")
  # ...and passing it satisfies the table.
  assert authoring.ujet(menu_id="90", extra={"queue_id": "7"}).data["ujet"]["queue_id"] == "7"
