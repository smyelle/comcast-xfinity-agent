# pylint: disable=missing-function-docstring,missing-class-docstring,missing-module-docstring,invalid-name,undefined-variable,line-too-long,broad-exception-caught

"""PolySynth callback: mark the Video conversation as ACTIVE (sticky ownership).

agent_action: this comment satisfies the lint rule.

The Video specialist is a child of the repair orchestrator and does NOT retain
control across turns -- every turn re-enters at the orchestrator, which must
re-decide whether to hand back to Video. A bare follow-up like "yes" (answering
a Video offer such as a TV-box restart) carries no video keyword, so the
orchestrator would otherwise steal the turn and use its own INTERNET gateway
restart. Setting {video_flow_active} deterministically here lets the
orchestrator's VIDEO CONVERSATION OWNERSHIP rule keep such follow-ups inside the
Video agent. Cleared by the Video instruction on hand-back / close.
"""

# pylint: disable=undefined-variable

from typing import Optional


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
  try:
    callback_context.state["video_flow_active"] = "true"
  except Exception as e:
    print(f"[video_before_agent_callback] could not set video_flow_active: {e}")
  return None

