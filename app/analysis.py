"""Language analysis and the context-budget guard (leg 02, leg 04 step 5).

Counts prompt tokens and trims prompts that would overflow a model's safe
``num_ctx``. ``num_ctx`` injection stops the default 32k cliff, and the budget
guard is the backstop for a prompt that overflows even the safe ceiling.

Why the self-verification pass was removed: docs/context-budget-per-model.md.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .obs import InstrumentedAction, emit_instrumented_action, llm_truncation_avoided_total

# Per-message overhead approximating the chat template's role/delimiter tokens.
# Tokenizer-approximation rationale: docs/backend-catalog.md.
_PER_MESSAGE_OVERHEAD = 4

# Safe fraction of num_ctx a prompt may fill before the guard trims.
# The remaining 10% is headroom for the response and template framing.
_SAFE_FRACTION = 0.9


class PromptPairingError(ValueError):
    """A prompt violates the dialect's ``assistant(tool_calls)`` / ``tool`` contract.

    The OpenAI dialect rejects a ``tool`` message that does not answer a
    preceding ``tool_calls``, and the rejection arrives as an opaque upstream
    400 several seconds and several retries later (issue #113). The budget
    guard raises this instead so the caller learns which message is unpaired,
    locally, before anything is dispatched.
    """


@lru_cache(maxsize=1)
def _encoder():
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(value: str | list[dict[str, Any]]) -> int:
    """Count tokens in a bare string or a full chat-message list.

    A message list is summed with the same per-message overhead the budget
    guard reserves, so a caller can ask "how big is this prompt" (the leg-04
    step-5 ``count_tokens(messages)`` surface) without reaching for the
    internal helper. A bare string is encoded directly.
    """
    if isinstance(value, list):
        return count_message_tokens(value)
    if not value:
        return 0
    return len(_encoder().encode(value))


def _message_text(message: dict[str, Any]) -> str:
    """Flatten a message's content, tolerating string or OpenAI content-part lists."""
    content = message.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = " ".join(parts)
    else:
        text = ""
    # Tool calls carried on the message also cost tokens.
    for call in message.get("tool_calls", []) or []:
        text += " " + str(call)
    return text


def count_message_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += count_tokens(_message_text(m)) + _PER_MESSAGE_OVERHEAD
    return total


def detect_context_truncation(
    prompt_tokens_sent: int,
    prompt_eval_count: int,
    target_ctx: int,
    tolerance: float = 0.15,
) -> bool:
    """Return whether the backend delivered a shorter context than asked for.

    The flagship ``num_ctx`` injection stops ollama's default 32k cliff, but a
    backend serving ``OLLAMA_NUM_PARALLEL > 1`` loads ``num_ctx`` as the model's
    *total* window and divides it across the parallel slots, so a single request's
    usable window is ``num_ctx / NUM_PARALLEL`` - the injected number silently
    halved (or worse) (issue #33). This is the exact silent-truncation failure the
    proxy exists to kill, pushed one layer down, so it must be surfaced loud.

    The signature is a ``prompt_eval_count`` that came back materially below *both*:

    * ``target_ctx`` - the per-request window the proxy asked each slot to deliver
      (the derived ``num_ctx``), and
    * ``prompt_tokens_sent`` - the proxy's own token count of the prompt it sent.

    The first says the delivered window fell short of the ask; the second says the
    prompt had more to show than the backend processed (so the shortfall is real
    truncation, not just a small prompt that fit). ``tolerance`` is the slack that
    absorbs tokenizer drift between the proxy's tiktoken estimate and ollama's real
    count, so a prompt that merely filled its window is never mistaken for a clip -
    the NUM_PARALLEL division produces a ~50% shortfall, far past any drift.

    Returns ``False`` when there is no signal to judge (``prompt_eval_count`` of 0,
    which openai-dialect backends and some paths report, or a non-positive
    ``target_ctx``).
    """
    if prompt_eval_count <= 0 or target_ctx <= 0:
        return False
    slack = 1.0 - max(0.0, min(tolerance, 1.0))
    delivered_below_ask = prompt_eval_count < target_ctx * slack
    prompt_wanted_more = prompt_eval_count < prompt_tokens_sent * slack
    return delivered_below_ask and prompt_wanted_more


def _tool_call_ids(message: dict[str, Any]) -> set[str]:
    """The call ids an assistant message is waiting on replies for."""
    ids: set[str] = set()
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.add(call_id)
    return ids


def group_tool_call_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split ``messages`` into the smallest units the trimmer may drop whole.

    An ``assistant`` message carrying ``tool_calls`` and the ``tool`` messages
    answering those calls are one indivisible unit. Dropping half of one leaves
    a ``tool`` message with nothing to answer, which the OpenAI dialect rejects
    outright - the defect in issue #113, where a tool-heavy round crossed the
    budget and every trimmed request died on a backend 400. Every other message
    is its own group, so grouping changes nothing for a prompt without tools.
    """
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        index += 1
        if message.get("role") == "assistant" and message.get("tool_calls"):
            pending = _tool_call_ids(message)
            while index < len(messages) and messages[index].get("role") == "tool":
                reply = messages[index]
                call_id = reply.get("tool_call_id")
                if pending and isinstance(call_id, str) and call_id and call_id not in pending:
                    break  # answers an earlier turn's call, so it is not ours.
                group.append(reply)
                pending.discard(call_id if isinstance(call_id, str) else "")
                index += 1
        groups.append(group)
    return groups


def unpaired_tool_message(messages: list[dict[str, Any]]) -> str:
    """Return why ``messages`` breaks tool-call pairing, or ``""`` when it holds.

    Mirrors the upstream rule verbatim: a ``tool`` message must answer a
    preceding message with ``tool_calls``.
    """
    called: set[str] = set()
    run_open = False
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            called |= _tool_call_ids(message)
            run_open = True
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                if call_id not in called:
                    return f"message {index} answers tool call {call_id!r}, which was never made"
            elif not run_open:
                return f"message {index} is a tool reply with no preceding tool call"
            continue
        run_open = False
    return ""


def apply_context_budget(
    logical_model: str,
    messages: list[dict[str, Any]],
    num_ctx: int,
    headroom: int,
    bound_by: str = "",
) -> tuple[list[dict[str, Any]], int, bool]:
    """Trim oldest non-system turns until the prompt fits ``num_ctx - headroom``.

    Returns ``(messages, token_count, trimmed)``. System messages are always
    kept (they hold the task framing whose loss caused the leg-01 garbage). The
    most recent turn is kept even if it alone is over budget - we never drop the
    live question. Increments ``llm_truncation_avoided_total`` when it trims.

    Trimming drops whole ``assistant(tool_calls)`` + ``tool`` groups, never a
    partial one, and the trimmed list is checked for pairing before it is
    returned. A list that cannot be made valid raises
    :class:`PromptPairingError` rather than travelling to the backend as an
    opaque 400 (issue #113).

    A ``num_ctx`` of 0 means the route has no locally-derived bound - a hosted
    provider owns its own window and is the authority on it (issue #115) - so
    nothing is trimmed. ``bound_by`` names which bound produced ``num_ctx`` and
    rides the trim event, because a trim log that does not say what bound the
    request cannot distinguish a VRAM limit from a cost ceiling.
    """
    total = count_message_tokens(messages)
    if num_ctx <= 0:
        return messages, total, False
    budget = max(num_ctx - headroom, 1)
    if total <= budget:
        return messages, total, False

    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    system_tokens = count_message_tokens(system)
    # Keep the newest turns; drop from the oldest end of `rest` until we fit.
    kept_rev: list[list[dict[str, Any]]] = []
    running = system_tokens
    for group in reversed(group_tool_call_turns(rest)):
        cost = count_message_tokens(group)
        if running + cost > budget and kept_rev:
            break  # would overflow and we already kept the live turn - stop.
        running += cost
        kept_rev.append(group)

    trimmed_rest = [message for group in reversed(kept_rev) for message in group]
    # Reassemble preserving original ordering: systems first (their relative
    # order preserved), then the surviving non-system tail in order.
    result = system + trimmed_rest
    new_total = count_message_tokens(result)

    dropped = len(messages) - len(result)
    if dropped <= 0:
        # One live turn over budget cannot be trimmed, and the model will cap
        # it, so this is not an avoided truncation - do not claim one.
        return result, new_total, False

    # Group-wise dropping cannot orphan a reply, so a failure here means the
    # caller's own prompt was already unpaired. Say which message, locally.
    if reason := unpaired_tool_message(result):
        raise PromptPairingError(f"{logical_model}: {reason}")

    emit_instrumented_action(
        InstrumentedAction(
            log_event="request.prompt_trimmed",
            metric=lambda: llm_truncation_avoided_total.labels(logical_model=logical_model).inc(),
            span_event="request.prompt_trimmed",
            fields={
                "logical_model": logical_model,
                "original_token_count": total,
                "final_token_count": new_total,
                "budget_tokens": budget,
                "target_num_ctx": num_ctx,
                "headroom_tokens": headroom,
                "dropped_message_count": dropped,
                **({"budget_bound_by": bound_by} if bound_by else {}),
            },
        )
    )
    return result, new_total, True


def fit_to_budget(
    messages: list[dict[str, Any]],
    num_ctx: int,
    logical_model: str = "",
) -> list[dict[str, Any]]:
    """Return ``messages`` trimmed to fit ``_SAFE_FRACTION`` of ``num_ctx``.

    The leg-04 step-5 public surface: an over-budget prompt never reaches the
    model. It reserves 10% of ``num_ctx`` as headroom, keeps every ``system``
    message and the latest user turn, drops the oldest non-system turns until it
    fits, and increments ``llm_truncation_avoided_total`` when it trims. Thin
    wrapper over :func:`apply_context_budget` that returns just the message
    list; callers wanting the token count or trim flag use that directly.
    """
    headroom = num_ctx - int(num_ctx * _SAFE_FRACTION)
    fitted, _tokens, _trimmed = apply_context_budget(logical_model, messages, num_ctx, headroom)
    return fitted
