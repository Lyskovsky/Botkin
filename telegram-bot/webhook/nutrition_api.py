"""FastAPI router for the nutrition day editor.

All endpoints require a valid Telegram WebApp initData in `Authorization: tma <initData>`.
User scope is enforced by extracting user_id from verified initData.
"""

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update

from database import SessionLocal
from database.models import NutritionLog
from database.crud import (
    create_nutrition_log,
    find_meal_for_slot,
    get_activity_by_date,
    get_nutrition_logs_by_date,
    get_nutrition_totals_by_date,
    update_nutrition_item_weight,
    update_nutrition_meal_fields,
    delete_nutrition_item,
    delete_nutrition_log,
    get_recent_product_names,
    set_meal_status,
)
from core.health.garmin_data import sync_today_garmin
from webhook.tg_auth import get_tg_user
from webhook.nutrition_slots import SLOTS, slot_center_time, slot_from_meal, slot_label_ru
from webhook.nutrition_goals import compute_goals
from core.food.nutrition import process_meal_description
from core.food.fiber_table import enrich_items_with_fiber, sum_fiber
from helpers.db_save import normalize_item_to_canonical

router = APIRouter()


def _item_to_wire(idx: int, it: dict) -> dict:
    return {
        "idx": idx,
        "name": it.get("product") or it.get("name") or it.get("food") or "",
        "weight": round(float(it.get("weight_g") or it.get("amount") or 0), 1),
        "kcal": round(float(it.get("calories") or 0), 1),
        "p": round(float(it.get("protein") or 0), 1),
        "f": round(float(it.get("fats") or 0), 1),
        "c": round(float(it.get("carbs") or 0), 1),
        "fib": int(round(float(it.get("fiber") or 0))),
    }


def _totals_to_wire(t: dict) -> dict:
    return {
        "kcal": round(float(t.get("calories") or 0), 1),
        "p": round(float(t.get("protein") or 0), 1),
        "f": round(float(t.get("fats") or 0), 1),
        "c": round(float(t.get("carbs") or 0), 1),
        "fib": int(round(float(t.get("fiber") or 0))),
    }


@router.get("/api/day")
async def get_day(
    date: str = Query(..., description="YYYY-MM-DD"),
    tg_user: dict = Depends(get_tg_user),
):
    try:
        for_date = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date {date!r}, use YYYY-MM-DD")

    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    db = SessionLocal()
    try:
        db.expire_all()
        rows = get_nutrition_logs_by_date(db, user_id=user_id, date=for_date)
        meals = []
        total_fiber_enriched = 0.0
        for r in rows:
            slot = slot_from_meal(r.meal_name, r.meal_time)
            # Enrich items with fiber fallback for legacy records that lack it.
            # Mutates a copy of the JSONB list — safe, not persisted to DB here.
            items_enriched = enrich_items_with_fiber([dict(it) for it in (r.items or [])])
            # Prefer enriched fiber over stored totals.fiber (which is often 0 for
            # historical records written before fiber was tracked consistently).
            meal_totals = dict(r.totals or {})
            meal_totals["fiber"] = sum_fiber(items_enriched)
            total_fiber_enriched += meal_totals["fiber"]
            meals.append(
                {
                    "id": r.id,
                    "meal_name": r.meal_name,
                    "meal_time": r.meal_time.strftime("%H:%M") if r.meal_time else None,
                    "slot": slot,
                    "items": [_item_to_wire(i, it) for i, it in enumerate(items_enriched)],
                    "totals": _totals_to_wire(meal_totals),
                    "status": r.status,
                }
            )
        open_plans = sum(1 for r in rows if r.status == "plan")
        # Override daily fiber with enriched sum (keeps kcal/p/f/c from DB).
        totals_raw = get_nutrition_totals_by_date(db, user_id=user_id, date=for_date)
        totals_raw["fiber"] = round(total_fiber_enriched, 1)
        totals_day = _totals_to_wire(totals_raw)
        # Today's actual activity. Compute as (total - bmr) for math consistency
        # — DO NOT trust active_calories field directly (Apple Health may overwrite it).
        # Use MSK (matches db_save.py date logic; server runs UTC by default).
        # ВАЖНО: синк сегодняшнего Garmin идёт ДО compute_goals — чтобы today-boost
        # считал цель по свежему фактическому расходу, а не по стейлу из БД.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        real_today = _dt.now(_tz(_td(hours=3))).date()
        if for_date == real_today:
            # sync_today_garmin pulls fresh Garmin data (15-min cache). Returns active_calories,
            # but for past days we recompute from total-bmr; for today we use this directly
            # since Garmin sync just wrote consistent values.
            activity_today, _ = sync_today_garmin(user_id, for_date)
        else:
            act_row = get_activity_by_date(db, user_id, for_date)
            if act_row and act_row.total_calories and act_row.bmr_calories:
                activity_today = max(0, float(act_row.total_calories) - float(act_row.bmr_calories))
            else:
                activity_today = float(act_row.active_calories or 0) if act_row else None

        goals = compute_goals(user_id=user_id, for_date=for_date)
        goals["activity_today"] = round(activity_today) if activity_today is not None else None
    finally:
        db.close()

    return {
        "date": for_date.isoformat(),
        "meals": meals,
        "open_plans": open_plans,
        "totals_day": totals_day,
        "goals": goals,
    }


class AddItemPayload(BaseModel):
    date: str
    slot: str
    name: str = Field(..., min_length=1, max_length=255)
    weight: float = Field(..., gt=0, le=5000)
    source: str = "manual"


def _scale_to_weight(base: dict, base_w: float, target_w: float) -> dict:
    """Scale macros from `base` (at `base_w` g) to `target_w` g."""
    factor = 1.0 if base_w <= 0 else target_w / base_w
    return {
        "product": base.get("product") or base.get("name") or "",
        "weight_g": round(target_w, 1),
        "calories": round(float(base.get("calories") or 0) * factor, 1),
        "protein": round(float(base.get("protein") or 0) * factor, 1),
        "fats": round(float(base.get("fats") or 0) * factor, 1),
        "carbs": round(float(base.get("carbs") or 0) * factor, 1),
        "fiber": round(float(base.get("fiber") or 0) * factor, 1),
    }


def _recompute_totals(items: list) -> dict:
    out = {"calories": 0.0, "protein": 0.0, "fats": 0.0, "carbs": 0.0, "fiber": 0.0}
    for it in items:
        for k in out:
            out[k] += float(it.get(k) or 0)
    return {k: round(v, 1) for k, v in out.items()}


@router.post("/api/meal/item", status_code=201)
async def add_meal_item(
    payload: AddItemPayload,
    tg_user: dict = Depends(get_tg_user),
):
    if payload.slot not in SLOTS:
        raise HTTPException(status_code=400, detail=f"Invalid slot {payload.slot!r}. Must be one of {SLOTS}.")
    try:
        for_date = date_type.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date {payload.date!r}, use YYYY-MM-DD")

    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    try:
        items_parsed, _totals = process_meal_description(payload.name)
    except Exception:
        items_parsed = [
            {
                "product": payload.name,
                "weight_g": payload.weight,
                "calories": 0,
                "protein": 0,
                "fats": 0,
                "carbs": 0,
                "fiber": 0,
            }
        ]

    if not items_parsed:
        items_parsed = [
            {
                "product": payload.name,
                "weight_g": payload.weight,
                "calories": 0,
                "protein": 0,
                "fats": 0,
                "carbs": 0,
                "fiber": 0,
            }
        ]

    base = items_parsed[0]
    base_w = float(base.get("weight_g") or 0)
    new_item = _scale_to_weight(base, base_w or payload.weight, payload.weight)
    # Override product name to what the user typed (not what LLM echoed)
    new_item["product"] = payload.name
    # Normalize to canonical schema {food, amount, unit, ...} — single source of truth
    new_item = normalize_item_to_canonical(new_item)

    db = SessionLocal()
    try:
        existing = find_meal_for_slot(db, user_id=user_id, for_date=for_date, slot=payload.slot)
        if existing:
            meal_id = existing.id
            items = list(existing.items or [])
            items.append(new_item)
            new_totals = _recompute_totals(items)
            db.execute(
                sa_update(NutritionLog)
                .where(NutritionLog.id == meal_id)
                .values(items=items, totals=new_totals)
                .execution_options(synchronize_session=False)
            )
            db.commit()
            db.expire_all()
            idx = len(items) - 1
        else:
            row = create_nutrition_log(
                db=db,
                user_id=user_id,
                date=for_date,
                meal_time=slot_center_time(payload.slot),
                meal_name=slot_label_ru(payload.slot),
                items=[new_item],
                totals=_recompute_totals([new_item]),
            )
            meal_id = row.id
            idx = 0
    finally:
        db.expire_all()
        db.close()

    return {"meal_id": meal_id, "item": _item_to_wire(idx, new_item)}


class PatchItemPayload(BaseModel):
    meal_id: int
    idx: int = Field(..., ge=0)
    weight: float = Field(..., gt=0, le=5000)


@router.patch("/api/meal/item")
async def patch_meal_item(payload: PatchItemPayload, tg_user: dict = Depends(get_tg_user)):
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")
    db = SessionLocal()
    try:
        db.expire_all()
        try:
            item, totals = update_nutrition_item_weight(
                db=db,
                meal_id=payload.meal_id,
                user_id=user_id,
                idx=payload.idx,
                new_weight=payload.weight,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="meal not found")
        except IndexError:
            raise HTTPException(status_code=400, detail="item index out of range")
    finally:
        db.expire_all()
        db.close()
    return {"item": _item_to_wire(payload.idx, item), "totals": _totals_to_wire(totals)}


class PatchMealPayload(BaseModel):
    meal_id: int
    meal_name: Optional[str] = None
    meal_time: Optional[str] = None  # "HH:MM"
    status: Optional[str] = None


@router.patch("/api/meal")
async def patch_meal(payload: PatchMealPayload, tg_user: dict = Depends(get_tg_user)):
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")
    mt = None
    if payload.meal_time:
        try:
            from datetime import time as _time

            h, m = payload.meal_time.split(":")
            mt = _time(int(h), int(m))
        except Exception:
            raise HTTPException(status_code=400, detail="meal_time must be HH:MM")
    db = SessionLocal()
    try:
        try:
            row = update_nutrition_meal_fields(
                db=db,
                meal_id=payload.meal_id,
                user_id=user_id,
                meal_name=payload.meal_name,
                meal_time=mt,
            )
            if payload.status is not None:
                row = set_meal_status(db=db, meal_id=payload.meal_id, user_id=user_id, status=payload.status)
        except LookupError:
            raise HTTPException(status_code=404, detail="meal not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result = {
            "meal_id": row.id,
            "meal_name": row.meal_name,
            "meal_time": row.meal_time.strftime("%H:%M") if row.meal_time else None,
            "status": row.status,
        }
    finally:
        db.expire_all()
        db.close()
    return result


@router.delete("/api/meal/item")
async def delete_item(
    meal_id: int = Query(...),
    idx: int = Query(..., ge=0),
    tg_user: dict = Depends(get_tg_user),
):
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")
    db = SessionLocal()
    try:
        try:
            removed, new_totals = delete_nutrition_item(
                db=db,
                meal_id=meal_id,
                user_id=user_id,
                idx=idx,
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="meal not found")
        except IndexError:
            raise HTTPException(status_code=400, detail="item index out of range")
    finally:
        db.expire_all()
        db.close()
    return {
        "removed": _item_to_wire(idx, removed),
        "totals": _totals_to_wire(new_totals) if new_totals else None,
    }


@router.delete("/api/meal", status_code=204)
async def delete_meal(
    meal_id: int = Query(...),
    tg_user: dict = Depends(get_tg_user),
):
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")
    db = SessionLocal()
    try:
        # Snapshot before delete — if this meal is a supplement mirror,
        # the paired supplements_log row needs to go too.
        from database.models import NutritionLog
        from core.health.supplements import delete_mirror_supplement_for

        meal = db.query(NutritionLog).filter(NutritionLog.id == meal_id, NutritionLog.user_id == user_id).first()
        snapshot = (meal.date, meal.meal_time, meal.meal_name) if meal else None

        ok = delete_nutrition_log(db=db, log_id=meal_id, user_id=user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="meal not found")
        if snapshot:
            d, t, name = snapshot
            delete_mirror_supplement_for(db, user_id, d, t, name)
    finally:
        db.expire_all()
        db.close()
    return Response(status_code=204)


@router.get("/api/favorites")
async def get_favorites(
    limit: int = Query(15, ge=1, le=50, description="Max number of products to return"),
    tg_user: dict = Depends(get_tg_user),
):
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    db = SessionLocal()
    try:
        db.expire_all()
        favorites = get_recent_product_names(db=db, user_id=user_id, limit=limit)
    finally:
        db.close()

    return favorites
