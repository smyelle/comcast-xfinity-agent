"""Interactive chat session with turn tracking and trace integration.

Originally vendored from cxas-scrapi's `feat/cxas-chat` branch so that the driver
depends only on PUBLIC ``cxas_scrapi.core.sessions`` + ``core.traces`` rather than
a fork-only chat layer.

Client construction goes through an injectable ``client_factory`` (default
:mod:`flows.live.clients`) so an embedding host can supply its own factory — for
example one that applies a per-request CES endpoint override.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import clients as _default_clients
from . import _import_lock

if TYPE_CHECKING:  # type-only: clients are constructed via the client factory
    from cxas_scrapi.core.traces import Traces

logger = logging.getLogger(__name__)

__all__ = ["ChatSession", "TurnRecord", "SessionEndedError"]


class SessionEndedError(Exception):
    """Raised when trying to send to an ended session."""

    pass


class TurnRecord:
    """Immutable record of a single conversation turn."""

    def __init__(self, turn_index: int, user_text: str, response: dict[str, Any]):
        self.turn_index = turn_index
        self.user_text = user_text
        self.agent_text: str = response.get("agent_text", "")
        self.tool_calls: list[dict] = response.get("tool_calls", [])
        self.tool_responses: list[dict] = response.get("tool_responses", [])
        self.agent_transfer: Any = response.get("agent_transfer")
        self.session_ended: bool = response.get("session_ended", False)
        self.payloads: list[dict] = response.get("payloads", [])
        self.raw_response: dict[str, Any] = response


class ChatSession:
    """Manages a live conversation with turn history and state tracking.

    Usage:
        session = ChatSession(app_name="projects/.../apps/...")
        result = session.send("Hello")
        result = session.send("I'd like a table for 4")
        trace = session.get_trace()  # fetch trace for this session
        session.close()
    """

    def __init__(
        self,
        app_name: str,
        channel: str | None = None,
        deployment_id: str | None = None,
        historical_contexts: list[dict] | str | None = None,
        turn_count: int | None = None,
        session_id: str | None = None,
        initial_turn_count: int = 0,
        initial_variable_state: dict[str, Any] | None = None,
        capture_si: bool = False,
        client_factory: Any = None,
        **session_kwargs: Any,
    ):
        self._app_name = app_name
        self._channel = channel
        self._capture_si = capture_si
        self._deployment_id = deployment_id
        self._historical_contexts = historical_contexts
        self._turn_count = turn_count
        self._initial_turn_count = initial_turn_count
        self._session_kwargs = session_kwargs
        self._clients = client_factory or _default_clients

        self._sessions = self._clients.make_sessions(
            app_name=app_name,
            deployment_id=deployment_id,
            **session_kwargs,
        )
        self._session_id = (
            session_id
            if session_id is not None
            else self._sessions.create_session_id()
        )
        self._turns: list[TurnRecord] = []
        self._variable_state: dict[str, Any] = (
            dict(initial_variable_state) if initial_variable_state else {}
        )
        self._closed = False
        # Lazily-built, cached Traces client (trace/bug reads). Built once so we
        # don't reconstruct + re-auth per turn, and its construction is serialized
        # against any concurrent cxas warm-up (circular-import race).
        self._traces: Any = None

    @property
    def session_id(self) -> str:
        """The unique session ID for this conversation."""
        return self._session_id

    @property
    def turns(self) -> list[TurnRecord]:
        """All turns in this conversation so far."""
        return list(self._turns)

    @property
    def is_ended(self) -> bool:
        """Whether the session has ended (via agent or explicit close)."""
        if self._closed:
            return True
        if self._turns and self._turns[-1].session_ended:
            return True
        return False

    @property
    def current_turn_index(self) -> int:
        """The index that will be assigned to the next turn."""
        return self._initial_turn_count + len(self._turns)

    def _dispatch(self, kwargs: dict[str, Any], label: str) -> TurnRecord:
        """Run one turn, fold its variable updates into state, record it.

        The shared tail of every send path. Splitting it out keeps the three
        callers to the part that actually differs — the request they build.
        """
        turn_index = self.current_turn_index
        raw_response = self._sessions.run(**kwargs)
        structured = self._sessions.get_structured_response(raw_response)

        for var_dict in structured.get("variable_updates", []):
            if isinstance(var_dict, dict):
                self._variable_state.update(var_dict)

        turn = TurnRecord(
            turn_index=turn_index,
            user_text=label,
            response=structured,
        )
        self._turns.append(turn)
        return turn

    def send_input(
        self,
        *,
        text: str | None = None,
        dtmf: str | None = None,
        event: str | None = None,
        event_vars: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
        tool_responses: list[dict[str, Any]] | None = None,
        modality: Any = None,
        use_tool_fakes: bool | None = None,
        label: str | None = None,
    ) -> TurnRecord:
        """Send any single input the platform accepts, and record the turn.

        The general form behind `send` and `send_event`. It carries none of their
        first-turn behavior — what you pass is what is sent — because its callers
        (a one-shot CLI turn, a scripted step) are explicit about the whole request
        and a hidden first-turn injection would be a surprise there.
        """
        if self.is_ended:
            raise SessionEndedError(f"Session {self._session_id} has already ended.")

        kwargs: dict[str, Any] = {"session_id": self._session_id}
        for key, value in (
            ("text", text), ("dtmf", dtmf), ("event", event),
            ("event_vars", event_vars), ("variables", variables),
            ("tool_responses", tool_responses), ("modality", modality),
            ("use_tool_fakes", use_tool_fakes),
        ):
            if value is not None:
                kwargs[key] = value

        if label is None:
            label = text if text is not None else (
                f"[dtmf: {dtmf}]" if dtmf is not None else
                f"[event: {event}]" if event is not None else "[input]")
        return self._dispatch(kwargs, label)

    def send(self, text: str) -> TurnRecord:
        """Send a message and return the turn record.

        Raises SessionEndedError if session has already ended.
        If channel was set, injects {"event_data": {"channel": channel}} as
        variables on the first turn only.
        """
        from cxas_scrapi.core.sessions import Modality

        if self.is_ended:
            raise SessionEndedError(f"Session {self._session_id} has already ended.")

        turn_index = self.current_turn_index
        variables: dict[str, Any] = {}
        if turn_index == 0 and self._variable_state:
            # Seed the server with the caller-provided initial variable state on
            # the first turn (e.g. a pinned current_date for deterministic
            # relative-date resolution in tests). Without this the variables are
            # only tracked locally and never reach the session.
            variables.update(self._variable_state)
        if self._channel and turn_index == 0:
            variables["event_data"] = {"channel": self._channel}
        if self._capture_si:
            # Sent every turn so SI capture works even mid-session (the server
            # records the per-pass system instruction into sm._si_trace).
            variables["capture_si"] = True

        kwargs: dict[str, Any] = {
            "session_id": self._session_id,
            "text": text,
            "modality": Modality.TEXT,
        }
        if variables:
            kwargs["variables"] = variables
        if self._historical_contexts is not None and turn_index == 0:
            kwargs["historical_contexts"] = self._historical_contexts
        if self._turn_count is not None and turn_index == 0:
            kwargs["turn_count"] = self._turn_count

        return self._dispatch(kwargs, text)

    def send_event(
        self,
        event_name: str,
        event_vars: dict[str, Any] | None = None,
    ) -> TurnRecord:
        """Fire a CES event and return the turn record."""
        if self.is_ended:
            raise SessionEndedError(f"Session {self._session_id} has already ended.")

        return self._dispatch(
            {"session_id": self._session_id,
             "event": event_name,
             "event_vars": event_vars},
            f"[event: {event_name}]",
        )

    def get_state(self) -> dict[str, Any]:
        """Extract current variable/slot state from the turn history.

        Returns dict with keys:
        - "active_agent": str | None (from last agent_transfer or initial)
        - "slot_machine": dict (from accumulated variable updates)
        - "filled_slots": dict (from sm.filled)
        - "session_ended": bool
        - "turn_count": int
        - "pending_transfer": str | None
        """
        state: dict[str, Any] = {
            "active_agent": None,
            "slot_machine": {},
            "filled_slots": {},
            "session_ended": False,
            "turn_count": len(self._turns),
            "pending_transfer": None,
        }

        for turn in self._turns:
            if turn.agent_transfer:
                target = turn.agent_transfer
                if hasattr(target, "display_name"):
                    state["active_agent"] = target.display_name
                elif isinstance(target, dict):
                    state["active_agent"] = target.get(
                        "display_name", target.get("target_agent")
                    )
                else:
                    state["active_agent"] = str(target)
                state["pending_transfer"] = state["active_agent"]

            if turn.session_ended:
                state["session_ended"] = True

        sm = self.get_slot_machine()
        if sm:
            state["slot_machine"] = sm
            filled = sm.get("filled", {})
            if isinstance(filled, dict):
                state["filled_slots"] = filled

        return state

    def _traces_client(self) -> "Traces":
        """Build (once) + cache the Traces client; serialize its construction.

        cxas client construction can trigger lazy submodule imports that race a
        concurrent warm-up (`cannot import name 'Sessions'` circular import), so
        build under the shared import lock. Cached so per-turn trace reads don't
        pay the auth + stub construction cost again.
        """
        if self._traces is None:
            with _import_lock.LOCK:
                self._traces = self._clients.make_traces(
                    app_name=self._app_name, **self._session_kwargs
                )
        return self._traces

    def get_trace(self, fmt: str = "json") -> str:
        """Fetch the full trace report for this session's conversation.

        The conversation_id is the session_id.
        """
        return self._traces_client().get_report(self._session_id, fmt=fmt)

    def get_normalized_trace(self) -> dict[str, Any]:
        """Fetch the normalized trace dict for this session (cached client)."""
        return self._traces_client().get_normalized(self._session_id)

    def get_slot_machine(self) -> dict[str, Any]:
        """Get slot_machine state from accumulated session variables.

        Checks both 'sm' and 'slot_machine' keys since agents use either name for
        the slot machine variable.
        """
        for key in ("sm", "slot_machine"):
            val = self._variable_state.get(key)
            if isinstance(val, dict) and val:
                return val
        return {}

    def get_flow_context(self) -> dict[str, Any]:
        """Get multi-flow context from top-level session variables.

        Returns dict with:
        - active_config_id: currently active flow config
        - agent_config_map: {agent_name: config_id} for all flows
        - active_sm_key: which variable key holds the active sm
        """
        agent_map = self._variable_state.get("agent_config_map", "")
        if isinstance(agent_map, str) and agent_map:
            try:
                import json

                agent_map = json.loads(agent_map)
            except (ValueError, TypeError):
                agent_map = {}

        return {
            "active_config_id": self._variable_state.get("_active_config_id"),
            "agent_config_map": agent_map if isinstance(agent_map, dict) else {},
            "active_sm_key": self._variable_state.get("_active_sm_key"),
        }

    def export_turns_summary(self) -> list[dict[str, Any]]:
        """Export turns as a list of dicts for scripting/comparison.

        Each dict: {"turn": int, "user": str, "agent": str,
                     "tool_calls": [...], "transfer": str|None}
        """
        summaries = []
        for turn in self._turns:
            transfer = None
            if turn.agent_transfer:
                if hasattr(turn.agent_transfer, "display_name"):
                    transfer = turn.agent_transfer.display_name
                elif isinstance(turn.agent_transfer, dict):
                    transfer = turn.agent_transfer.get(
                        "display_name",
                        turn.agent_transfer.get("target_agent"),
                    )
                else:
                    transfer = str(turn.agent_transfer)

            summaries.append(
                {
                    "turn": turn.turn_index,
                    "user": turn.user_text,
                    "agent": turn.agent_text,
                    "tool_calls": turn.tool_calls,
                    "transfer": transfer,
                }
            )
        return summaries

    def close(self) -> None:
        """Mark session as closed. Idempotent."""
        self._closed = True
