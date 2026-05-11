"""Notification dispatcher.

Subscribes to world events that should fan out to per-recipient inboxes
(`message_inserted`, `email_inserted`, `calendar_event_created`, etc.) and
creates `Notification` rows. The dispatcher is wired up once at startup.

Notification IDs are deterministic — `notif.<source_kind>.<source_id>.<recipient>`
so replaying a scenario produces the same notification ids and the agent's
notification cursor lines up across runs.
"""

from __future__ import annotations

from sim.store.entities import Notification
from sim.store.world import World


def wire_notification_dispatcher(world: World) -> None:
    world.subscribe("message_inserted", _on_message_inserted_factory(world))
    world.subscribe("email_inserted", _on_email_inserted_factory(world))
    world.subscribe("calendar_event_created", _on_calendar_event_created_factory(world))


def _on_message_inserted_factory(world: World):
    def handler(event_name: str, payload: dict) -> None:
        msg_id = payload["message_id"]
        channel_id = payload["channel_id"]
        sender_id = payload["sender_id"]
        sim_time = payload["sim_time"]
        mentions = list(payload.get("mentions", []))
        channel = world.get_channel(channel_id)
        if channel is None:
            return

        # Direct DM: notify the other party.
        # Mention in any channel: notify the mentioned person.
        # Public channel non-mention: do NOT notify (avoid notification spam).
        recipients: set[str] = set(mentions)
        if channel.is_dm:
            recipients.update(m for m in channel.members if m != sender_id)
        recipients.discard(sender_id)

        for recipient in sorted(recipients):
            notif_id = f"notif.message.{msg_id}.{recipient}"
            if notif_id in world.notifications:
                continue
            world.add_notification(
                Notification(
                    id=notif_id,
                    recipient_id=recipient,
                    kind="chat_message",
                    payload={
                        "channel_id": channel_id,
                        "message_id": msg_id,
                        "sender_id": sender_id,
                    },
                    created_at=sim_time,
                )
            )

    return handler


def _on_email_inserted_factory(world: World):
    def handler(event_name: str, payload: dict) -> None:
        email_id = payload["email_id"]
        thread_id = payload["thread_id"]
        sender_id = payload["sender_id"]
        sim_time = payload["sim_time"]
        recipients = set(payload.get("to", [])) | set(payload.get("cc", []))
        recipients.discard(sender_id)
        for recipient in sorted(recipients):
            notif_id = f"notif.email.{email_id}.{recipient}"
            if notif_id in world.notifications:
                continue
            world.add_notification(
                Notification(
                    id=notif_id,
                    recipient_id=recipient,
                    kind="email",
                    payload={
                        "thread_id": thread_id,
                        "email_id": email_id,
                        "sender_id": sender_id,
                    },
                    created_at=sim_time,
                )
            )

    return handler


def _on_calendar_event_created_factory(world: World):
    def handler(event_name: str, payload: dict) -> None:
        event_id = payload["event_id"]
        sim_time = payload["start_sim_time"]
        for attendee in sorted(payload.get("attendees", [])):
            notif_id = f"notif.calendar.{event_id}.{attendee}"
            if notif_id in world.notifications:
                continue
            world.add_notification(
                Notification(
                    id=notif_id,
                    recipient_id=attendee,
                    kind="calendar_event",
                    payload={"event_id": event_id, "start_sim_time": sim_time},
                    created_at=sim_time,
                )
            )

    return handler
