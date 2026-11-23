"""In-memory conversation sessions.

We keep the conversation ourselves (not the provider's stored conversations): provider-neutral,
testable, and we can trim it. Old tool outputs are trimmed from what is sent to the model;
the full outputs are kept separately so the validator can still trace numbers from earlier turns.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field

TTL_SECONDS = 2 * 60 * 60
KEEP_FULL_TURNS = 3
TRIMMED = '{"trimmed": "older tool output removed to save context; call the tool again if needed"}'


@dataclass
class Session:
    id: str
    items: list[dict] = field(default_factory=list)          # conversation sent to the model
    turn_starts: list[int] = field(default_factory=list)     # index in items where each turn starts
    tool_outputs: list[tuple[str, dict]] = field(default_factory=list)  # (tool, full output), for validation
    last_used: float = field(default_factory=time.time)

    def start_turn(self) -> None:
        self.turn_starts.append(len(self.items))
        self.last_used = time.time()

    def trim(self, keep_turns: int = KEEP_FULL_TURNS) -> None:
        """Replace tool outputs older than the last `keep_turns` turns (calls stay paired with outputs)."""
        if len(self.turn_starts) <= keep_turns:
            return
        cutoff = self.turn_starts[-keep_turns]
        for item in self.items[:cutoff]:
            if item.get("type") == "function_call_output":
                item["output"] = TRIMMED


class SessionStore:
    def __init__(self, ttl: int = TTL_SECONDS):
        self.ttl = ttl
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None) -> Session:
        with self._lock:
            self._expire()
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            s = Session(id=session_id or uuid.uuid4().hex[:12])
            self._sessions[s.id] = s
            return s

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _expire(self) -> None:
        now = time.time()
        for sid in [k for k, s in self._sessions.items() if now - s.last_used > self.ttl]:
            del self._sessions[sid]
