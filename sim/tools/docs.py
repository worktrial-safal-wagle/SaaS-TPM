"""`docs.*` tool operations — versioned company docs.

Each edit creates a new `DocVersion`. Read returns the latest version
unless a specific `version` arg is supplied. ACLs (`acl_view`, `acl_edit`)
are enforced via `tools.acl`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sim.scheduler import Scheduler
from sim.store import Doc, DocComment, DocVersion, World
from sim.tools.acl import doc_edit_allowed, doc_view_allowed
from sim.tools.base import ToolOp
from sim.tools.costs import COST_DOC_COMMENT, COST_DOC_LIST, COST_DOC_READ, cost_doc_create, cost_doc_edit
from sim.tools.registry import ToolError
from sim.tools.suggest import suggest_id


# ---------------------------------------------------------------------------
# docs.list
# ---------------------------------------------------------------------------


class DocsListArgs(BaseModel):
    pass


def docs_list(world: World, scheduler: Scheduler, args: DocsListArgs, caller_id: str) -> dict[str, Any]:
    out = []
    for doc in sorted(world.docs.values(), key=lambda d: d.id):
        if not doc_view_allowed(world, doc.id, caller_id):
            continue
        out.append({
            "id": doc.id, "title": doc.title, "created_by": doc.created_by,
            "latest_version": doc.versions[-1].version if doc.versions else 0,
        })
    return {"docs": out}


# ---------------------------------------------------------------------------
# docs.read
# ---------------------------------------------------------------------------


class DocsReadArgs(BaseModel):
    doc_id: str
    version: int | None = None


def docs_read(world: World, scheduler: Scheduler, args: DocsReadArgs, caller_id: str) -> dict[str, Any]:
    if args.doc_id not in world.docs:
        raise ToolError(suggest_id("doc", args.doc_id, world.docs.keys()))
    if not doc_view_allowed(world, args.doc_id, caller_id):
        raise ToolError(f"not authorized to view doc: {args.doc_id}")
    doc = world.docs[args.doc_id]
    if not doc.versions:
        raise ToolError(f"doc has no versions: {args.doc_id}")
    if args.version is None:
        version = doc.versions[-1]
    else:
        match = [v for v in doc.versions if v.version == args.version]
        if not match:
            raise ToolError(f"unknown version: {args.version}")
        version = match[0]
    return {
        "id": doc.id, "title": doc.title,
        "version": version.version, "author_id": version.author_id,
        "sim_time": version.sim_time, "body": version.body,
        "latest_version": doc.versions[-1].version,
        "comments": [c.model_dump() for c in doc.comments],
    }


# ---------------------------------------------------------------------------
# docs.create
# ---------------------------------------------------------------------------


class DocsCreateArgs(BaseModel):
    doc_id: str
    title: str
    body: str
    acl_view: list[str] | None = None
    acl_edit: list[str] | None = None


def docs_create(world: World, scheduler: Scheduler, args: DocsCreateArgs, caller_id: str) -> dict[str, Any]:
    if args.doc_id in world.docs:
        raise ToolError(f"doc already exists: {args.doc_id}")
    doc = Doc(
        id=args.doc_id, title=args.title, created_by=caller_id,
        acl_view=args.acl_view, acl_edit=args.acl_edit,
    )
    world.add_doc(doc)
    world.add_doc_version(args.doc_id, DocVersion(
        version=1, author_id=caller_id, body=args.body, sim_time=scheduler.sim_time,
    ))
    return doc.model_dump()


# ---------------------------------------------------------------------------
# docs.edit
# ---------------------------------------------------------------------------


class DocsEditArgs(BaseModel):
    doc_id: str
    body: str


def docs_edit(world: World, scheduler: Scheduler, args: DocsEditArgs, caller_id: str) -> dict[str, Any]:
    if args.doc_id not in world.docs:
        raise ToolError(suggest_id("doc", args.doc_id, world.docs.keys()))
    if not doc_edit_allowed(world, args.doc_id, caller_id):
        raise ToolError(f"not authorized to edit doc: {args.doc_id}")
    doc = world.docs[args.doc_id]
    next_version = (doc.versions[-1].version + 1) if doc.versions else 1
    world.add_doc_version(args.doc_id, DocVersion(
        version=next_version, author_id=caller_id, body=args.body, sim_time=scheduler.sim_time,
    ))
    return {"doc_id": args.doc_id, "version": next_version}


# ---------------------------------------------------------------------------
# docs.comment
# ---------------------------------------------------------------------------


class DocsCommentArgs(BaseModel):
    doc_id: str
    body: str
    anchor: str | None = None


def docs_comment(world: World, scheduler: Scheduler, args: DocsCommentArgs, caller_id: str) -> dict[str, Any]:
    if args.doc_id not in world.docs:
        raise ToolError(suggest_id("doc", args.doc_id, world.docs.keys()))
    if not doc_view_allowed(world, args.doc_id, caller_id):
        raise ToolError(f"not authorized to view doc: {args.doc_id}")
    if not args.body.strip():
        raise ToolError("comment body cannot be empty")
    doc = world.docs[args.doc_id]
    comment = DocComment(
        author_id=caller_id, body=args.body,
        sim_time=scheduler.sim_time, anchor=args.anchor,
    )
    doc.comments.append(comment)
    world._emit("doc_commented", {"doc_id": args.doc_id, "author_id": caller_id, "sim_time": scheduler.sim_time})
    return doc.model_dump()


def docs_ops() -> list[ToolOp]:
    return [
        ToolOp("docs.list", DocsListArgs, COST_DOC_LIST, docs_list,
               description="List company docs visible to you."),
        ToolOp("docs.read", DocsReadArgs, COST_DOC_READ, docs_read,
               description="Read a doc by id; optionally request a specific historical `version`. Latest by default."),
        ToolOp("docs.create", DocsCreateArgs, cost_doc_create, docs_create,
               description="Create a new doc and seed v1 with `body`. Optionally restrict `acl_view`/`acl_edit`."),
        ToolOp("docs.edit", DocsEditArgs, cost_doc_edit, docs_edit,
               description="Replace the doc body — creates a new version. Cost scales with body length."),
        ToolOp("docs.comment", DocsCommentArgs, COST_DOC_COMMENT, docs_comment,
               description="Add a comment to a doc. Useful for capturing decisions inline."),
    ]
