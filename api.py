import os
import json
import hmac
import hashlib
from datetime import date, datetime
from urllib.parse import unquote, parse_qsl

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL")
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")


def get_db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def verify_telegram_auth(init_data: str) -> dict:
    """Verify Telegram WebApp initData"""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        hash_str = parsed.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new("WebAppData".encode(), BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, hash_str):
            raise HTTPException(status_code=401, detail="Invalid auth")
        user_data = json.loads(unquote(parsed.get("user", "{}")))
        return user_data
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Auth failed")


def get_user_id(x_init_data: str = None):
    if not x_init_data:
        raise HTTPException(status_code=401, detail="No auth")
    user = verify_telegram_auth(x_init_data)
    return user["id"]


# ── ROUTES ──────────────────────────────────────────────

@app.get("/api/me")
def get_me(x_init_data: str = Header(None)):
    user_id = get_user_id(x_init_data)
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id = %s", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(user)


@app.get("/api/today")
def get_today(x_init_data: str = Header(None)):
    user_id = get_user_id(x_init_data)
    today = date.today()
    with get_db() as conn:
        meals = conn.execute(
            "SELECT * FROM food_log WHERE user_id=%s AND DATE(logged_at)=%s ORDER BY logged_at",
            (user_id, today)
        ).fetchall()
        water = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as w FROM water_log WHERE user_id=%s AND DATE(logged_at)=%s",
            (user_id, today)
        ).fetchone()
        user = conn.execute("SELECT * FROM users WHERE user_id=%s", (user_id,)).fetchone()
    return {
        "meals": [dict(m) for m in meals],
        "water": int(water["w"]),
        "user": dict(user) if user else None,
    }


@app.get("/api/history")
def get_history(x_init_data: str = Header(None)):
    user_id = get_user_id(x_init_data)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DATE(logged_at) as day, SUM(calories) as cal,
                   SUM(protein) as prot, SUM(fat) as fat, SUM(carbs) as carbs
            FROM food_log WHERE user_id=%s
            GROUP BY day ORDER BY day DESC LIMIT 7
        """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


class MealIn(BaseModel):
    meal_name: str
    portion: int
    calories: int
    protein: float
    fat: float
    carbs: float


@app.post("/api/meals")
def add_meal(meal: MealIn, x_init_data: str = Header(None)):
    user_id = get_user_id(x_init_data)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO food_log (user_id, meal_name, portion, calories, protein, fat, carbs) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (user_id, meal.meal_name, meal.portion, meal.calories, meal.protein, meal.fat, meal.carbs)
        )
    return {"ok": True}


class WaterIn(BaseModel):
    amount: int


@app.post("/api/water")
def add_water(water: WaterIn, x_init_data: str = Header(None)):
    user_id = get_user_id(x_init_data)
    with get_db() as conn:
        conn.execute("INSERT INTO water_log (user_id, amount) VALUES (%s,%s)", (user_id, water.amount))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}
