# Schema reference

_Auto-generated from pydantic models. Re-run finalize() to refresh._

## Person

- `id`: `<class 'str'>` PydanticUndefined
- `display_name`: `<class 'str'>` PydanticUndefined
- `role`: `<class 'str'>` PydanticUndefined
- `team`: `str | None` 
- `manager_id`: `str | None` 
- `is_agent`: `<class 'bool'>` False
- `timezone_offset_minutes`: `<class 'int'>` 0
- `working_hours`: `<class 'sim.store.entities.WorkingHours'>` PydanticUndefined
- `responsiveness`: `<class 'float'>` 1.0
- `persona_notes`: `<class 'str'>` ''
- `knowledge`: `dict[str, str]` PydanticUndefined
- `next_poll_at`: `<class 'int'>` 0
- `busy_until`: `<class 'int'>` 0

## Channel

- `id`: `<class 'str'>` PydanticUndefined
- `name`: `<class 'str'>` PydanticUndefined
- `members`: `list[str]` PydanticUndefined
- `is_private`: `<class 'bool'>` False
- `is_dm`: `<class 'bool'>` False
- `topic`: `<class 'str'>` ''

## Message

- `id`: `<class 'str'>` PydanticUndefined
- `channel_id`: `<class 'str'>` PydanticUndefined
- `sender_id`: `<class 'str'>` PydanticUndefined
- `body`: `<class 'str'>` PydanticUndefined
- `sim_time`: `<class 'int'>` PydanticUndefined
- `thread_root_id`: `str | None` 
- `mentions`: `list[str]` PydanticUndefined
- `read_by`: `list[str]` PydanticUndefined

## EmailThread

- `id`: `<class 'str'>` PydanticUndefined
- `subject`: `<class 'str'>` PydanticUndefined
- `participants`: `list[str]` PydanticUndefined

## Email

- `id`: `<class 'str'>` PydanticUndefined
- `thread_id`: `<class 'str'>` PydanticUndefined
- `sender_id`: `<class 'str'>` PydanticUndefined
- `to`: `list[str]` PydanticUndefined
- `cc`: `list[str]` PydanticUndefined
- `body`: `<class 'str'>` ''
- `sim_time`: `<class 'int'>` PydanticUndefined
- `read_by`: `list[str]` PydanticUndefined

## Task

- `id`: `<class 'str'>` PydanticUndefined
- `project`: `<class 'str'>` PydanticUndefined
- `title`: `<class 'str'>` PydanticUndefined
- `description`: `<class 'str'>` ''
- `status`: `typing.Literal['Backlog', 'Todo', 'In Progress', 'Blocked', 'In Review', 'Done']` 'Backlog'
- `assignee_id`: `str | None` 
- `reporter_id`: `str | None` 
- `priority`: `typing.Literal['P0', 'P1', 'P2', 'P3']` 'P2'
- `estimated_effort_seconds`: `int | None` 
- `remaining_effort_seconds`: `int | None` 
- `deadline_sim_time`: `int | None` 
- `depends_on`: `list[str]` PydanticUndefined
- `comments`: `list[sim.store.entities.TaskComment]` PydanticUndefined

## Doc

- `id`: `<class 'str'>` PydanticUndefined
- `title`: `<class 'str'>` PydanticUndefined
- `created_by`: `<class 'str'>` PydanticUndefined
- `versions`: `list[sim.store.entities.DocVersion]` PydanticUndefined
- `comments`: `list[sim.store.entities.DocComment]` PydanticUndefined
- `acl_view`: `list[str] | None` 
- `acl_edit`: `list[str] | None` 

## DocVersion

- `version`: `<class 'int'>` PydanticUndefined
- `author_id`: `<class 'str'>` PydanticUndefined
- `body`: `<class 'str'>` PydanticUndefined
- `sim_time`: `<class 'int'>` PydanticUndefined

## CalendarEvent

- `id`: `<class 'str'>` PydanticUndefined
- `title`: `<class 'str'>` PydanticUndefined
- `start_sim_time`: `<class 'int'>` PydanticUndefined
- `end_sim_time`: `<class 'int'>` PydanticUndefined
- `organizer_id`: `<class 'str'>` PydanticUndefined
- `attendees`: `list[str]` PydanticUndefined
- `location`: `<class 'str'>` ''
- `agenda`: `<class 'str'>` ''
- `attended_by_agent`: `<class 'bool'>` False

## MeetingTranscript

- `meeting_id`: `<class 'str'>` PydanticUndefined
- `turns`: `list[sim.store.entities.TranscriptTurn]` PydanticUndefined
- `generated_at`: `<class 'int'>` PydanticUndefined
- `synthesized`: `<class 'bool'>` False

## Notification

- `id`: `<class 'str'>` PydanticUndefined
- `recipient_id`: `<class 'str'>` PydanticUndefined
- `kind`: `<class 'str'>` PydanticUndefined
- `payload`: `<class 'dict'>` PydanticUndefined
- `created_at`: `<class 'int'>` PydanticUndefined
- `seen`: `<class 'bool'>` False

## ToolCall

- `tool`: `<class 'str'>` PydanticUndefined
- `args`: `dict[str, typing.Any]` PydanticUndefined
- `continue_in_tick`: `<class 'bool'>` False

## ToolResult

- `ok`: `<class 'bool'>` PydanticUndefined
- `tool`: `<class 'str'>` PydanticUndefined
- `sim_time`: `<class 'int'>` PydanticUndefined
- `cost_minutes`: `<class 'int'>` PydanticUndefined
- `result`: `typing.Any` 
- `notifications`: `list[sim.store.entities.Notification]` PydanticUndefined
- `error`: `str | None` 

## Briefing

- `now_sim_time`: `<class 'int'>` PydanticUndefined
- `now_label`: `<class 'str'>` PydanticUndefined
- `end_sim_time`: `<class 'int'>` PydanticUndefined
- `agent_persona`: `dict[str, typing.Any]` PydanticUndefined
- `unread_notifications_count`: `<class 'int'>` PydanticUndefined
- `unread_chats`: `list[dict[str, typing.Any]]` PydanticUndefined
- `unread_emails`: `list[dict[str, typing.Any]]` PydanticUndefined
- `open_commitments`: `list[sim.agent.briefing.BriefingCommitment]` PydanticUndefined
- `project_board`: `list[sim.agent.briefing.BriefingProjectBoardItem]` PydanticUndefined
- `upcoming_calendar`: `list[dict[str, typing.Any]]` PydanticUndefined
- `recent_actions`: `list[sim.agent.briefing.BriefingRecentAction]` PydanticUndefined
- `sim_time_stalled_for_turns`: `<class 'int'>` 0
- `last_verdict`: `dict[str, typing.Any] | None` 
- `tool_summary`: `list[str]` PydanticUndefined
- `since_sim_time`: `int | None` 
- `notifications_new`: `list[dict[str, typing.Any]]` PydanticUndefined
- `current_busy_state`: `dict[str, typing.Any] | None` 
