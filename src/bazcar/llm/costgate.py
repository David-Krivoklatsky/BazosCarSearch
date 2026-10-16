"""Cost estimate and the paid-model confirmation gate for bulk evaluations.

Confirmation is required only for re-evaluation: a paid batch that overwrites
cars which already have a score. Scoring cars with no score yet is never
prompted (the monthly budget guard still applies).

* free model, or no existing scores in the batch -> queued immediately;
* paid model AND existing scores would be overwritten -> parked
  (``confirmed = false``) and a Telegram message with the cost prediction and
  [Rescore] / [Cancel] buttons is sent. Nothing is paid until confirmed.

Estimates are deliberately conservative ranges, not promises: input tokens come
from the real prompt length plus an average listing, output from a typical JSON
reply. Import-light (no bs4) so the Vercel functions can use it.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from bazcar.llm.provider import resolve_prompt

logger = logging.getLogger(__name__)

AVG_LISTING_CHARS = 900       # title + specs + description preview (live catalog average)
OUT_TOKENS_PER_CAR = 180      # JSON reply with why/risk/condition in Slovak
CHARS_PER_TOKEN = 3.2         # Slovak text tokenizes denser than English
SECONDS_PER_EVAL = 20         # rough per-car latency, matches the bot's estimate


def model_price(models: list[dict], model_id: str | None) -> tuple[float, float] | None:
    """(USD per 1M input, USD per 1M output) for ``model_id``; None when unknown."""
    for model in models:
        if model.get("id") == model_id:
            p_in, p_out = model.get("in_per_m"), model.get("out_per_m")
            if p_in is None or p_out is None:
                return None
            return float(p_in), float(p_out)
    return None


def estimate_cost(
    cars: int,
    *,
    in_per_m: float,
    out_per_m: float,
    prompt_name: str = "standard",
    criteria: str | None = None,
) -> dict[str, Any]:
    """Predicted tokens, USD (with low/high range) and minutes for ``cars`` evaluations."""
    per_car_in = (len(resolve_prompt(prompt_name)) + len(criteria or "") + AVG_LISTING_CHARS) / CHARS_PER_TOKEN
    tokens_in = cars * per_car_in
    tokens_out = cars * OUT_TOKENS_PER_CAR
    usd = (tokens_in * in_per_m + tokens_out * out_per_m) / 1_000_000
    return {
        "cars": cars,
        "tokens_in": round(tokens_in),
        "tokens_out": round(tokens_out),
        "usd": round(usd, 4),
        "usd_low": round(usd * 0.6, 4),
        "usd_high": round(usd * 1.8, 4),
        "minutes": max(1, round(cars * SECONDS_PER_EVAL / 60)),
    }


def confirmation_text(
    *,
    model: str,
    cars: int,
    scope_label: str,
    estimate: dict[str, Any] | None,
    existing: int = 0,
    spent_usd: float | None = None,
    budget_usd: float | None = None,
) -> str:
    """HTML message for the Telegram confirmation (escaped)."""
    esc = html.escape
    lines = [
        "💸 <b>Paid model — please confirm</b>",
        f"Model: <code>{esc(model)}</code>",
        f"Cars to score: <b>{cars}</b> ({esc(scope_label)}) — "
        f"<b>{existing}</b> already scored (re-evaluated), {cars - existing} new",
    ]
    if estimate is None:
        lines.append("Estimated cost: <i>unknown (price not available)</i>")
    else:
        lines.append(
            f"Estimated cost: <b>≈ ${estimate['usd']:.2f}</b> "
            f"(range ${estimate['usd_low']:.2f}–${estimate['usd_high']:.2f})"
        )
        lines.append(f"Expected time: ~{estimate['minutes']} min")
    if spent_usd is not None and budget_usd is not None:
        lines.append(f"AI spend this month: ${spent_usd:.2f} of ${budget_usd:.2f}")
    lines.append("Cars that were not scored yet are scored without this prompt.")
    return "\n".join(lines)


async def enqueue_evaluation(
    repo,
    *,
    notifier,
    chat_id: int | None,
    profile_key: str,
    criteria: str | None,
    model: str,
    prompt_name: str,
    fallback_models: list[str] | None,
    max_listings: int,
    days: int,
    ad_ids: list[int] | None,
    force: bool,
    cars: int,
    existing: int = 0,
    prices: list[dict],
    scope_label: str,
    spent_usd: float | None = None,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    """Queue (or park for confirmation) a bulk evaluation. Never raises on pricing gaps.

    ``cars`` = everything the batch scores; ``existing`` = how many of them already
    have a score that would be overwritten. Only ``existing > 0`` on a paid model
    asks the user.
    """
    if cars <= 0:
        return {"status": "nothing", "cars": 0}
    price = model_price(prices, model)
    if price is None:
        # Unknown price: estimate unavailable, treated as paid when re-scoring.
        estimate, paid = None, True
    else:
        estimate = estimate_cost(
            cars,
            in_per_m=price[0],
            out_per_m=price[1],
            prompt_name=prompt_name,
            criteria=criteria,
        )
        paid = price[0] + price[1] > 0
    confirmed = not (paid and existing > 0)
    task_id = await repo.create_eval_task(
        profile_key,
        criteria,
        model,
        max_listings,
        days,
        prompt_name=prompt_name,
        fallback_models=fallback_models,
        ad_ids=ad_ids,
        chat_id=chat_id,
        force=force,
        confirmed=confirmed,
        estimate_usd=estimate["usd"] if estimate else None,
    )
    result: dict[str, Any] = {
        "status": "queued" if confirmed else "awaiting",
        "task_id": task_id,
        "cars": cars,
        "existing": existing,
        "estimate": estimate,
    }
    if not confirmed and notifier is not None and chat_id is not None:
        text = confirmation_text(
            model=model,
            cars=cars,
            scope_label=scope_label,
            estimate=estimate,
            existing=existing,
            spent_usd=spent_usd,
            budget_usd=budget_usd,
        )
        markup = {
            "inline_keyboard": [[
                {"text": f"✅ Rescore {cars} cars", "callback_data": f"ev:ok:{task_id}"},
                {"text": "✖ Cancel", "callback_data": f"ev:no:{task_id}"},
            ]]
        }
        try:
            await notifier.send_message(text, chat_id=chat_id, reply_markup=markup)
        except Exception:
            logger.warning("cost confirmation message failed", exc_info=True)
    return result


def notifier_for(token: str | None, chat_id: int | None):
    """A Telegram notifier for confirmation prompts (None when not configured)."""
    if not token or chat_id is None:
        return None
    from bazcar.notify import TelegramNotifier

    return TelegramNotifier(token, str(chat_id))


async def queue_evaluation(
    repo,
    *,
    token: str | None,
    chat_id: int | None,
    criteria: str | None,
    model: str,
    prompt_name: str,
    fallback_models: list[str] | None,
    max_listings: int,
    days: int,
    ad_ids: list[int] | None = None,
    force: bool = False,
    prices: list[dict] | None = None,
    scope_label: str,
    spent_usd: float | None = None,
    budget_usd: float | None = None,
) -> dict[str, Any]:
    """Plan a bulk evaluation and queue it through the cost gate.

    * ``ad_ids`` + ``force``: re-score exactly these cars with ``model``.
    * ``ad_ids`` without force: only the ids that have no score under this spec.
    * window (no ids): the recent cars that have no score under this spec.
    Dedupes against open tasks of the same kind. Returns the gate result with
    ``status`` in {"queued", "awaiting", "nothing", "already"}.
    """
    from bazcar.core.profiles import profile_key

    p_key = profile_key(criteria, prompt=prompt_name)
    ids = [int(a) for a in (ad_ids or []) if int(a) > 0]
    pending = await repo.pending_eval_tasks()
    for task in pending:
        if (
            task["profile_key"] == p_key
            and bool(task.get("ad_ids")) == bool(ids)
            and bool(task.get("force")) == bool(force)
        ):
            return {"status": "already", "cars": 0}
    existing = 0
    if ids:
        cached = await repo.fetch_evaluations(p_key, ids)
        if force:
            # explicit re-score: every listed car is scored, the scored ones are overwritten
            cars = len(ids)
            existing = sum(1 for i in ids if i in cached)
        else:
            cars = sum(1 for i in ids if i not in cached)
    else:
        cars, _ = await repo.count_missing_evaluations(p_key, days=days, limit=max_listings)
    notifier = notifier_for(token, chat_id)
    try:
        return await enqueue_evaluation(
            repo,
            notifier=notifier,
            chat_id=chat_id,
            profile_key=p_key,
            criteria=criteria,
            model=model,
            prompt_name=prompt_name,
            fallback_models=fallback_models,
            max_listings=max_listings,
            days=days,
            ad_ids=ids or None,
            force=force,
            cars=cars,
            existing=existing,
            prices=prices or [],
            scope_label=scope_label,
            spent_usd=spent_usd,
            budget_usd=budget_usd,
        )
    finally:
        if notifier is not None:
            await notifier.close()


async def month_spend(repo) -> float:
    """Trailing-30-day evaluation spend in USD (0.0 when unknown)."""
    try:
        cost = await repo.eval_cost(None, 30)
        return float((cost.get("total") or {}).get("cost_usd") or 0)
    except Exception:
        return 0.0
