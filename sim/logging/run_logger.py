"""Per-run logger.

Streams `turns.jsonl`, `events.jsonl`, `verdicts.jsonl` to disk during a run;
finalizes with `world_final.json`, `run.json`, `index.json`, `SCHEMA.md`, and
`transcript.md`. Long payloads (doc bodies, meeting transcripts) are hoisted
to `artifacts/<sha256>.txt` and referenced by hash in the JSONL.

Append-only: a crashed run is still scoreable at the `slice_safe` tier.
Determinism: any identical payload across runs lands at the same artifact hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from sim.agent.driver import TurnRecord
from sim.scenario.schema import ScenarioYaml
from sim.store import World


ARTIFACT_THRESHOLD_BYTES = 4096


class RunLogger:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.run_dir / "artifacts"
        self.artifacts_dir.mkdir(exist_ok=True)

        self._turns_f = (self.run_dir / "turns.jsonl").open("a")
        self._events_f = (self.run_dir / "events.jsonl").open("a")
        self._verdicts_f = (self.run_dir / "verdicts.jsonl").open("a")

        self._transcript_lines: list[str] = []
        self._tool_counts: dict[str, int] = defaultdict(int)
        self._error_count: int = 0
        self._turn_count: int = 0
        self._verdict_count: int = 0
        self._closed: bool = False

    # ------------------------------------------------------------------
    # World event subscription
    # ------------------------------------------------------------------

    def wire(self, world: World) -> None:
        """Subscribe to every world event we want in events.jsonl."""

        def _generic_handler_factory(event_name: str):
            def handler(name: str, payload: dict[str, Any]) -> None:
                self.log_world_event(name, payload)
            return handler

        # We subscribe per known event kind so the JSONL has clean names.
        for kind in (
            "person_added", "channel_added", "message_inserted",
            "email_thread_created", "email_inserted",
            "task_created", "task_status_changed", "task_assigned",
            "task_work_logged", "task_dependency_added", "task_commented",
            "doc_created", "doc_edited", "doc_commented",
            "calendar_event_created", "calendar_rsvp",
            "meeting_transcript_added",
            "notification_created", "notification_seen",
        ):
            world.subscribe(kind, _generic_handler_factory(kind))

    # ------------------------------------------------------------------
    # Streaming writes
    # ------------------------------------------------------------------

    def log_turn(self, record: TurnRecord) -> None:
        notifications = [n.model_dump() for n in record.result.notifications]
        result_payload = self._hoist(record.result.result)
        line = {
            "turn": record.turn,
            "sim_time_before": record.sim_time_before,
            "sim_time_after": record.sim_time_after,
            "tool": record.call.tool,
            "args": record.call.args,
            "ok": record.result.ok,
            "cost_minutes": record.result.cost_minutes,
            "error": record.result.error,
            "notifications_count": len(notifications),
            "result": result_payload,
        }
        self._turns_f.write(json.dumps(line, default=str) + "\n")
        self._turns_f.flush()

        self._transcript_lines.append(self._render_turn_md(record))
        self._tool_counts[record.call.tool] += 1
        self._turn_count += 1
        if not record.result.ok:
            self._error_count += 1

    def log_world_event(self, kind: str, payload: dict[str, Any]) -> None:
        line = {"kind": kind, "payload": payload}
        self._events_f.write(json.dumps(line, default=str) + "\n")
        self._events_f.flush()

    def log_verdict(self, verdict: dict[str, Any]) -> None:
        self._verdicts_f.write(json.dumps(verdict, default=str) + "\n")
        self._verdicts_f.flush()
        self._verdict_count += 1

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def finalize(
        self,
        world: World,
        config: ScenarioYaml,
        scenario_path: Path | None = None,
        *,
        status: str = "completed",
        final_sim_time: int | None = None,
    ) -> None:
        """Write end-of-run artifacts.

        `final_sim_time`, if provided, is the scheduler's clock at termination.
        Pass it explicitly when the clock advanced past the last logged turn
        (e.g., the driver bumps sim_time to end_sim_time on graceful idle-out).
        Without it, we fall back to the last turn's `sim_time_after` from
        `turns.jsonl` — which under-reports when the driver advances post-turn
        and causes the eval's `requires_full_run` tier gate to misfire.
        """
        if self._closed:
            return
        if final_sim_time is None:
            final_sim_time = _read_sim_time_from_jsonl(self.run_dir / "turns.jsonl")
        (self.run_dir / "world_final.json").write_text(world.to_json())
        (self.run_dir / "run.json").write_text(json.dumps({
            "scenario_id": config.id,
            "seed": config.seed,
            "end_sim_time": config.end_sim_time,
            "agent_id": config.agent_id,
            "status": status,
            "final_sim_time": final_sim_time,
            "completed_at": dt.datetime.now(dt.UTC).isoformat(),
        }, indent=2))
        (self.run_dir / "index.json").write_text(json.dumps({
            "turn_count": self._turn_count,
            "error_count": self._error_count,
            "verdict_count": self._verdict_count,
            "tool_counts": dict(self._tool_counts),
            "final_sim_time": final_sim_time,
        }, indent=2))
        (self.run_dir / "SCHEMA.md").write_text(_render_schema_doc())
        (self.run_dir / "transcript.md").write_text("\n\n".join(self._transcript_lines))
        if scenario_path:
            scenario_copy = self.run_dir / "scenario"
            if scenario_copy.exists():
                shutil.rmtree(scenario_copy)
            shutil.copytree(scenario_path, scenario_copy)
        self._turns_f.close()
        self._events_f.close()
        self._verdicts_f.close()
        self._closed = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _hoist(self, payload: Any) -> Any:
        s = json.dumps(payload, default=str)
        if len(s) <= ARTIFACT_THRESHOLD_BYTES:
            return payload
        digest = hashlib.sha256(s.encode("utf-8")).hexdigest()
        path = self.artifacts_dir / f"{digest}.json"
        if not path.exists():
            path.write_text(s)
        return {"_artifact_ref": digest, "_size_bytes": len(s)}

    def _render_turn_md(self, record: TurnRecord) -> str:
        lines: list[str] = []
        lines.append(f"## Turn {record.turn} — sim_time {record.sim_time_before} → {record.sim_time_after}")
        lines.append(f"**Tool:** `{record.call.tool}`")
        args_repr = json.dumps(record.call.args, default=str)
        if len(args_repr) <= 200:
            lines.append(f"Args: `{args_repr}`")
        else:
            lines.append(f"Args: `{args_repr[:200]}...`")
        lines.append(f"Cost: {record.result.cost_minutes} sim-min · "
                     f"OK: {record.result.ok} · "
                     f"Notifications: {len(record.result.notifications)}")
        if record.result.error:
            lines.append(f"Error: {record.result.error}")
        return "\n".join(lines)


def _read_sim_time_from_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    last = 0
    with path.open("r") as f:
        for line in f:
            try:
                obj = json.loads(line)
                last = obj.get("sim_time_after", last)
            except json.JSONDecodeError:
                continue
    return last


def _render_schema_doc() -> str:
    """Auto-generate a schema reference from pydantic models so it never drifts."""
    from sim.agent.briefing import Briefing
    from sim.store.entities import (
        CalendarEvent, Channel, Doc, DocVersion, Email, EmailThread,
        MeetingTranscript, Message, Notification, Person, Task,
    )
    from sim.tools.base import ToolCall, ToolResult

    lines = ["# Schema reference",
             "",
             "_Auto-generated from pydantic models. Re-run finalize() to refresh._",
             ""]
    for model in (
        Person, Channel, Message, EmailThread, Email, Task, Doc, DocVersion,
        CalendarEvent, MeetingTranscript, Notification, ToolCall, ToolResult,
        Briefing,
    ):
        lines.append(f"## {model.__name__}")
        lines.append("")
        for field_name, field_info in model.model_fields.items():
            annotation = field_info.annotation
            default_repr = repr(field_info.default) if field_info.default is not None else ""
            lines.append(f"- `{field_name}`: `{annotation}` {default_repr}")
        lines.append("")
    return "\n".join(lines)
