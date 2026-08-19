"""Starter project scaffolding for `flows new`.

Writes a minimal, immediately-runnable project: an `app.py` defining one flow, one
native pydantic `@flows.tool`, and the `App` — so `flows validate app.py` and
`flows emit app.py --out ./app` work out of the box.
"""

from __future__ import annotations

import json
import os
import re

_APP_PY = '''\
"""A starter slot-filling agent authored with `flows`.

Run:  flows validate app.py
      flows emit app.py --out ./app
"""
from pydantic import BaseModel, Field

import flows


# --- a native CXAS tool: one parameter per input slot, pydantic out ----------
class LookupResult(BaseModel):
    status_message: str = Field(description="Human-readable order status")
    success: bool = True


@flows.tool(flow="my_agent")
def lookup_order(order_id: str) -> LookupResult:
    """Look up the status of an order by its id."""
    return LookupResult(status_message="Your order is out for delivery.")


# --- the flow: authored in the Python DSL (or write flows/*.yaml instead) -----
flow = flows.Flow("my_agent", root_agent="My_Agent",
                  bootstrap={"welcome_slot": "welcome"})
flow.add(
    flows.announce("welcome", ["Hi! I can check on an order for you."],
                   shared=True, preempt=True),
    flows.user_slot("order_id", "What's your order id?"),
    flows.result_slot("status_msg", "lookup"),
    flows.announce("status", ["{status_msg}"], requires=["status_msg"],
                   preempt=True),
    flows.announce("goodbye", ["Thanks for calling. Goodbye."],
                   requires=["status_msg"], preempt=True, end=True),
)
flow.task("lookup", "lookup_order", ["order_id"], "status_msg",
          out_key="status_message", condition=flows.has("order_id"))


# --- the app: full app.json settings (framework state vars auto-added) --------
app = flows.App(
    root_flow=flow,
    app_display_name=__AGENT_NAME__,
    model="gemini-3.1-flash-live",
)
'''

_README = """\
# __AGENT_NAME__

A slot-filling CXAS agent authored with [`flows`](https://pypi/flows).

```
flows validate app.py            # compile + real framework validator
flows emit app.py --out ./app    # write a deployable CXAS app dir
pip install "flows[deploy]"      # then: flows deploy (push + evals via cxas-scrapi)
```

- `app.py` — the flow (Python DSL), a native pydantic `@flows.tool`, and the `App`.
- Prefer YAML? Put the flow in `flows/my_agent.yaml` and load it with
  `flows.load_flow(...)`; YAML and the DSL produce the identical config.
"""


def _markdown_heading(name: str) -> str:
  """`name` as a single-line Markdown H1 body.

  A heading is line-terminated, so any newline in the name would silently split
  it (and demote the rest to body text); collapse all whitespace runs to a space.
  """
  return re.sub(r"\s+", " ", name).strip()


def scaffold_project(dest: str, *, name: str = "My Agent") -> str:
  """Create a starter project at `dest`. Returns the created directory path."""
  os.makedirs(dest, exist_ok=True)
  # Reserve the layout the docs describe even though the starter is single-file.
  os.makedirs(os.path.join(dest, "flows"), exist_ok=True)
  os.makedirs(os.path.join(dest, "tools"), exist_ok=True)
  os.makedirs(os.path.join(dest, "evals"), exist_ok=True)
  with open(os.path.join(dest, "app.py"), "w", encoding="utf-8") as f:
    # `json.dumps` supplies the quotes AND the escaping: a name carrying a quote
    # or a newline must not be able to break the generated module's syntax.
    # `ensure_ascii=False` keeps an accented name readable in the utf-8 file.
    f.write(_APP_PY.replace("__AGENT_NAME__", json.dumps(name, ensure_ascii=False)))
  with open(os.path.join(dest, "README.md"), "w", encoding="utf-8") as f:
    f.write(_README.replace("__AGENT_NAME__", _markdown_heading(name)))
  return os.path.abspath(dest)
