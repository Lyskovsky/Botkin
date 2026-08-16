import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))

from database.models import FunnelEvent


def test_funnel_counts(test_db):
    db = test_db
    for uid, ev in [
        (1, "onboarding_started"),
        (1, "persona_selected"),
        (1, "quiz_completed"),
        (1, "goal_computed"),
        (1, "first_food_logged"),
        (2, "onboarding_started"),
        (2, "persona_selected"),
    ]:
        db.add(FunnelEvent(user_id=uid, event=ev, meta={}))
    db.commit()
    from webhook.admin import _funnel_counts

    counts = _funnel_counts(db, days=3650)
    assert counts["onboarding_started"] == 2
    assert counts["first_food_logged"] == 1


def test_funnel_counts_distinct_users(test_db):
    db = test_db
    # один и тот же юзер логирует onboarding_started дважды → считаем как 1 уникального
    db.add(FunnelEvent(user_id=7, event="onboarding_started", meta={}))
    db.add(FunnelEvent(user_id=7, event="onboarding_started", meta={}))
    db.commit()
    from webhook.admin import _funnel_counts

    counts = _funnel_counts(db, days=3650)
    assert counts["onboarding_started"] == 1
