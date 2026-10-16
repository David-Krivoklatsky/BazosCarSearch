"""Cost gate: estimate, confirmation message, parked vs immediate queueing."""

from __future__ import annotations

import asyncio

from bazcar.llm.costgate import (
    confirmation_text,
    enqueue_evaluation,
    estimate_cost,
    model_price,
    queue_evaluation,
)

FREE = {"id": "x/free", "in_per_m": 0.0, "out_per_m": 0.0}
PAID = {"id": "x/paid", "in_per_m": 1.0, "out_per_m": 2.0}
UNKNOWN_PRICE = {"id": "x/unknown", "in_per_m": None, "out_per_m": None}


class _Repo:
    def __init__(self, pending=None, cached=None, missing=(0, 0)):
        self.pending = pending or []
        self.cached = cached or {}
        self.missing = missing
        self.created: list[dict] = []

    async def pending_eval_tasks(self):
        return self.pending

    async def create_eval_task(self, profile_key, criteria, model, max_listings, days, **kw):
        self.created.append({"profile_key": profile_key, "model": model, **kw})
        return len(self.created) + 100

    async def fetch_evaluations(self, p_key, ids):
        return {i: object() for i in self.cached if i in ids}

    async def count_missing_evaluations(self, p_key, days, limit):
        return self.missing

    async def eval_cost(self, p_key=None, days=30):
        return {"total": {"cost_usd": 1.25}}


class _Notifier:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_message(self, text, *, chat_id=None, reply_markup=None, silent=False):
        self.sent.append({"text": text, "chat_id": chat_id, "markup": reply_markup})
        return True

    async def close(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- estimates


def test_estimate_scales_linearly_with_cars() -> None:
    one = estimate_cost(1, in_per_m=1.0, out_per_m=2.0)
    ten = estimate_cost(10, in_per_m=1.0, out_per_m=2.0)
    assert ten["tokens_in"] == round(one["tokens_in"] * 10) or abs(ten["tokens_in"] - one["tokens_in"] * 10) <= 10
    assert ten["usd"] > one["usd"] > 0
    assert ten["minutes"] >= one["minutes"]


def test_estimate_range_brackets_point_estimate() -> None:
    est = estimate_cost(50, in_per_m=0.5, out_per_m=1.5)
    assert est["usd_low"] < est["usd"] < est["usd_high"]


def test_free_model_costs_nothing() -> None:
    assert estimate_cost(100, in_per_m=0.0, out_per_m=0.0)["usd"] == 0


def test_model_price_known_and_unknown() -> None:
    assert model_price([PAID], "x/paid") == (1.0, 2.0)
    assert model_price([UNKNOWN_PRICE], "x/unknown") is None  # missing numbers -> unknown
    assert model_price([PAID], "x/missing") is None


def test_confirmation_text_escapes_and_states_cost() -> None:
    est = estimate_cost(12, in_per_m=1.0, out_per_m=2.0)
    text = confirmation_text(model="a/<b>", cars=12, scope_label="last 30 days",
                             estimate=est, spent_usd=3.0, budget_usd=10.0)
    assert "a/&lt;b&gt;" in text and "<b>12</b>" in text
    assert "Estimated cost" in text and "AI spend this month: $3.00 of $10.00" in text
    unknown = confirmation_text(model="m", cars=3, scope_label="s", estimate=None)
    assert "Estimated cost: <i>unknown" in unknown and "≈ $" not in unknown


# ------------------------------------------------------------ enqueue behaviour


def test_free_model_queues_immediately_without_message() -> None:
    repo, notifier = _Repo(), _Notifier()
    result = _run(enqueue_evaluation(
        repo, notifier=notifier, chat_id=7, profile_key="pk", criteria="c", model="x/free",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=None,
        force=False, cars=5, prices=[FREE], scope_label="s",
    ))
    assert result["status"] == "queued"
    assert repo.created[0]["confirmed"] is True
    assert notifier.sent == []


def test_paid_model_parks_task_and_asks_when_rescoring_existing() -> None:
    repo, notifier = _Repo(), _Notifier()
    result = _run(enqueue_evaluation(
        repo, notifier=notifier, chat_id=7, profile_key="pk", criteria="c", model="x/paid",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=[1, 2],
        force=True, cars=2, existing=2, prices=[PAID], scope_label="2 selected cars",
    ))
    assert result["status"] == "awaiting"
    assert repo.created[0]["confirmed"] is False
    assert repo.created[0]["estimate_usd"] > 0
    assert len(notifier.sent) == 1 and notifier.sent[0]["chat_id"] == 7
    buttons = notifier.sent[0]["markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"ev:ok:{result['task_id']}"
    assert buttons[1]["callback_data"] == f"ev:no:{result['task_id']}"


def test_paid_model_never_asks_to_score_new_cars() -> None:
    repo, notifier = _Repo(), _Notifier()
    result = _run(enqueue_evaluation(
        repo, notifier=notifier, chat_id=7, profile_key="pk", criteria="c", model="x/paid",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=None,
        force=False, cars=40, existing=0, prices=[PAID], scope_label="auto backfill",
    ))
    assert result["status"] == "queued" and repo.created[0]["confirmed"] is True
    assert notifier.sent == []  # brand-new cars: no prompt


def test_unknown_price_asks_only_when_overwriting() -> None:
    repo, notifier = _Repo(), _Notifier()
    result = _run(enqueue_evaluation(
        repo, notifier=notifier, chat_id=7, profile_key="pk", criteria=None, model="x/unknown",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=[1],
        force=True, cars=4, existing=2, prices=[UNKNOWN_PRICE], scope_label="s",
    ))
    assert result["status"] == "awaiting" and result["estimate"] is None
    assert "unknown" in notifier.sent[0]["text"]
    quiet = _run(enqueue_evaluation(
        repo, notifier=notifier, chat_id=7, profile_key="pk", criteria=None, model="x/unknown",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=None,
        force=False, cars=4, existing=0, prices=[UNKNOWN_PRICE], scope_label="s",
    ))
    assert quiet["status"] == "queued" and len(notifier.sent) == 1


def test_nothing_to_score_creates_no_task() -> None:
    repo = _Repo()
    result = _run(enqueue_evaluation(
        repo, notifier=None, chat_id=7, profile_key="pk", criteria=None, model="x/paid",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=None,
        force=False, cars=0, prices=[PAID], scope_label="s",
    ))
    assert result == {"status": "nothing", "cars": 0}
    assert repo.created == []


def test_missing_notifier_still_parks_the_task() -> None:
    repo = _Repo()
    result = _run(enqueue_evaluation(
        repo, notifier=None, chat_id=7, profile_key="pk", criteria=None, model="x/paid",
        prompt_name="standard", fallback_models=None, max_listings=5, days=3, ad_ids=[1, 2, 3],
        force=True, cars=3, existing=3, prices=[PAID], scope_label="s",
    ))
    assert result["status"] == "awaiting"  # the Mini App can still confirm it


# ------------------------------------------------------- planning (queue_evaluation)


def _queue(repo, **kw):
    base = dict(
        token=None, chat_id=7, criteria="c", model="x/free", prompt_name="standard",
        fallback_models=None, max_listings=50, days=30, scope_label="s", prices=[FREE],
    )
    base.update(kw)
    return _run(queue_evaluation(repo, **base))


def test_window_counts_only_unscored_cars() -> None:
    repo = _Repo(missing=(7, 40))
    result = _queue(repo)
    assert result["cars"] == 7 and result["status"] == "queued"


def test_explicit_ids_without_force_count_only_unscored() -> None:
    repo = _Repo(cached={1: 1, 2: 1})
    result = _queue(repo, ad_ids=[1, 2, 3, 4], force=False)
    assert result["cars"] == 2  # ids 3 and 4 have no score under this spec


def test_forced_ids_count_all_and_report_existing() -> None:
    repo = _Repo(cached={1: 1, 2: 1})
    result = _queue(repo, ad_ids=[1, 2, 3], force=True)
    assert result["cars"] == 3 and result["existing"] == 2
    assert repo.created[0]["force"] is True and repo.created[0]["ad_ids"] == [1, 2, 3]


def test_duplicate_open_task_is_not_queued_twice() -> None:
    repo = _Repo(pending=[{"profile_key": "spec-x", "ad_ids": None, "force": False}])
    # profile_key of the request is computed from criteria; align it for the test
    from bazcar.core.profiles import profile_key

    repo.pending[0]["profile_key"] = profile_key("c", prompt="standard")
    result = _queue(repo)
    assert result["status"] == "already" and repo.created == []


def test_month_spend_survives_errors() -> None:
    from bazcar.llm.costgate import month_spend

    class _Broken:
        async def eval_cost(self, *a, **k):
            raise RuntimeError("db down")

    assert _run(month_spend(_Broken())) == 0.0
    assert _run(month_spend(_Repo())) == 1.25
