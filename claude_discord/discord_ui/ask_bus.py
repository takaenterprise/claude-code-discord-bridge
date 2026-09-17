"""In-process routing bus for AskUserQuestion interactions.

AskView button/select callbacks call ``ask_bus.post_answer()`` to deliver the
user's choice to the coroutine waiting inside ``_collect_ask_answers``.

Using an asyncio.Queue (rather than a Future) means:
- Multiple answers can be posted safely without raising InvalidStateError.
- The waiting side can use ``asyncio.wait_for`` with any timeout it likes.
- The view itself needs no reference to the internal Future/Event; routing is
  fully decoupled.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class AskAnswerBus:
    """Routes button/select interactions to the coroutine awaiting an answer.

    One instance is shared across all active sessions (module-level singleton).
    Each waiting session registers a Queue keyed by thread_id; AskView callbacks
    post the chosen labels into that Queue.
    """

    def __init__(self) -> None:
        # thread_id -> (nonce of the question being awaited, queue)
        self._waiters: dict[int, tuple[str | None, asyncio.Queue[list[str]]]] = {}

    def register(self, thread_id: int, nonce: str | None = None) -> asyncio.Queue[list[str]]:
        """Register a waiter for *thread_id* and return its Queue.

        The caller should await ``queue.get()`` (ideally with a timeout).
        Call :meth:`unregister` when done regardless of success/timeout.

        ``nonce`` identifies the specific question being awaited.  Answers
        posted for any other nonce are dropped (see :meth:`post_answer`).
        ``None`` keeps the pre-nonce behaviour for callers that do not pass one.
        """
        q: asyncio.Queue[list[str]] = asyncio.Queue()
        self._waiters[thread_id] = (nonce, q)
        logger.debug("AskAnswerBus: registered waiter for thread %d (nonce=%s)", thread_id, nonce)
        return q

    def post_answer(self, thread_id: int, answers: list[str], nonce: str | None = None) -> bool:
        """Deliver *answers* to the coroutine waiting for *thread_id*.

        The answer is delivered only when *nonce* matches the nonce the waiter
        registered, so a click on the buttons of an older question cannot be
        filed as the answer to a newer one in the same thread.

        Returns True if a matching waiter was found (live session, same
        question), False otherwise (session gone, or a different question).
        """
        entry = self._waiters.get(thread_id)
        if entry is None:
            logger.debug("AskAnswerBus: no waiter for thread %d (bot restarted?)", thread_id)
            return False
        waiter_nonce, q = entry
        if waiter_nonce != nonce:
            logger.warning(
                "AskAnswerBus: dropped answer for thread %d — nonce mismatch "
                "(answer=%s, awaiting=%s)",
                thread_id,
                nonce,
                waiter_nonce,
            )
            return False
        q.put_nowait(answers)
        logger.debug("AskAnswerBus: delivered %r to thread %d", answers, thread_id)
        return True

    def unregister(self, thread_id: int) -> None:
        """Remove the waiter for *thread_id* (called after answer or timeout)."""
        self._waiters.pop(thread_id, None)
        logger.debug("AskAnswerBus: unregistered waiter for thread %d", thread_id)


# Module-level singleton — import this everywhere.
ask_bus = AskAnswerBus()
