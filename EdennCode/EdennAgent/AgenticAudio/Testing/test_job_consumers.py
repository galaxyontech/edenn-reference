"""Every job this product enqueues must have something that runs it.

This went wrong three times, the same way each time: a tool enqueues a task, a
worker to run it is written or is not, and no deployable ROLE ever runs it — so
on the shared fleet the job sits in the queue forever while the API cheerfully
reports it queued. Narration hit it first. Restyling hit it and was only found
by an audit. Sound effects were the worst of the three — no worker class and no
role at all — and are now wired, which is why the known-gap list below is empty.

Nothing about the code looks wrong when this happens: the tool is correct, the
queue is correct, the worker (when it exists) is correct. The defect lives in
the space between them, which is exactly the kind nobody notices until a
customer waits forever.

The standalone deployment hides all of it, because it completes jobs in-process
— which is why this is a fleet concern and why it stayed invisible.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Task types the agent puts on the queue, read off the code that enqueues them.
MEDIA = Path("EdennCode/EdennAgent/AgenticAudio/tools/media.py")

#: Task types with no consumer anywhere on the fleet, and why that is known.
#: This list may only ever SHRINK. A new entry means shipping a feature that
#: silently never runs; removing one means somebody wired it.
KNOWN_UNCONSUMED: dict[str, str] = {}


def _enqueued_task_types() -> set[str]:
    return set(re.findall(r'task_type="([a-z_]+)"', MEDIA.read_text()))


def _enqueued_queue_names() -> dict[str, str]:
    """Task type -> the queue name its enqueue actually uses.

    Read off the source rather than derived, because the two disagree for at
    least one task type and a derived name is the mistake this file exists to
    catch.
    """
    text = MEDIA.read_text()
    found: dict[str, str] = {}
    for match in re.finditer(
        r'namespaced_queue_name\(\s*"([a-z0-9-]+)".*?task_type="([a-z_]+)"',
        text,
        re.DOTALL,
    ):
        found[match.group(2)] = match.group(1)
    return found


def _worker_main() -> str:
    """The entrypoint's CODE, comments stripped.

    The gate below is a substring check, and a comment that names a queue —
    including the one explaining why the SFX queue name differs from its task
    type — would satisfy it with the actual consumer deleted. Line-level
    stripping is enough here: the file keeps no '#' inside string literals.
    """
    source = Path("EdennCode/Deployment/async_pipeline_v2/worker_main.py").read_text()
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def test_the_enqueued_task_types_are_the_ones_we_think_they_are() -> None:
    """If this list changes, the coverage question below has to be re-asked."""
    assert _enqueued_task_types() == {
        "video_music_monolith", "audio_creative_edit", "voiceover", "video_sfx",
    }


def test_every_enqueued_task_type_has_a_consumer_or_is_a_known_gap() -> None:
    """The gate. A new orphan fails here rather than in a customer's queue."""
    from EdennCode.Deployment.async_pipeline_v2.worker_main import (
        supported_worker_roles,
    )

    source = _worker_main()
    roles = supported_worker_roles()
    assert roles, "no deployable roles at all"

    orphans = set()
    for task_type in _enqueued_task_types():
        # A consumer exists when the entrypoint names the queue the ENQUEUE
        # writes to. Deriving that name from the task type is a guess, and for
        # sound effects the guess is wrong — the task type is "video_sfx" and
        # the queue is "sfx-pipeline". A role listening on the derived name
        # would satisfy a derived check and idle forever next to a full queue,
        # so the names are read off the enqueue itself.
        queue = _enqueued_queue_names().get(task_type)
        assert queue, f"no queue name found for {task_type} in {MEDIA}"
        if queue not in source:
            orphans.add(task_type)

    unexpected = orphans - set(KNOWN_UNCONSUMED)
    assert not unexpected, (
        "these job types are enqueued and nothing on the fleet runs them: "
        f"{sorted(unexpected)} — wire a role in worker_main.py, the way "
        "narration and creative-edit are wired"
    )

    fixed = set(KNOWN_UNCONSUMED) - orphans
    assert not fixed, (
        f"{sorted(fixed)} now has a consumer — remove it from KNOWN_UNCONSUMED "
        "so the next orphan cannot hide behind it"
    )


def test_restyling_is_no_longer_one_of_the_orphans() -> None:
    """It was, until this change: a worker class existed and no role ran it."""
    from EdennCode.Deployment.async_pipeline_v2.worker_main import (
        CREATIVE_EDIT_ROLES,
        supported_worker_roles,
    )

    assert CREATIVE_EDIT_ROLES <= supported_worker_roles()
    assert "audio-creative-edit-pipeline" in _worker_main()


def test_the_known_gap_is_documented_rather_than_merely_tolerated() -> None:
    for task_type, reason in KNOWN_UNCONSUMED.items():
        assert len(reason) > 40, f"{task_type} is excused without an explanation"
