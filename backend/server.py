"""Sixty-second multiplayer score API for Tart Bakalım."""

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from queue import LifoQueue, Empty, Full
from pathlib import Path
import json
import os
import random
import re
import threading
from uuid import UUID, uuid4

from flask import Flask, jsonify, request
import psycopg
from psycopg.rows import dict_row


app = Flask(__name__)
TARGETS = json.loads((Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))
ORIGIN = "https://tart-bakalim-game.onrender.com"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
schema_lock = threading.Lock()
schema_ready = False
idle_connections = LifoQueue(maxsize=4)


def varied_deck():
    rng = random.SystemRandom()
    groups = {}
    for card_id, card in TARGETS.items():
        groups.setdefault(card["category"], []).append(card_id)
    for cards in groups.values():
        rng.shuffle(cards)
    deck = []
    previous = None
    while groups:
        categories = list(groups)
        rng.shuffle(categories)
        if len(categories) > 1 and categories[0] == previous:
            categories[0], categories[1] = categories[1], categories[0]
        for category in categories:
            deck.append(groups[category].pop())
            if not groups[category]:
                del groups[category]
            previous = category
    return deck


def open_database():
    global schema_ready
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    if not schema_ready:
        with schema_lock:
            if not schema_ready:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS runs (
                            id uuid PRIMARY KEY,
                            name varchar(20) NOT NULL,
                            started_at timestamptz NOT NULL,
                            question_started_at timestamptz NOT NULL,
                            finished_at timestamptz,
                            score integer NOT NULL DEFAULT 0,
                            answered integer NOT NULL DEFAULT 0,
                            close_count integer NOT NULL DEFAULT 0,
                            combo integer NOT NULL DEFAULT 0,
                            risk_used boolean NOT NULL DEFAULT false,
                            awaiting_next boolean NOT NULL DEFAULT false,
                            deck text NOT NULL,
                            deck_index integer NOT NULL DEFAULT 0
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS runs_score_idx ON runs (score DESC, answered DESC) WHERE finished_at IS NOT NULL")
                    cur.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS duration_seconds integer NOT NULL DEFAULT 120")
                conn.commit()
                schema_ready = True
    return conn


@contextmanager
def database():
    try:
        conn = idle_connections.get_nowait()
    except Empty:
        conn = open_database()
    if conn.closed:
        conn = open_database()
    try:
        with conn.transaction():
            yield conn
    finally:
        if not conn.closed:
            try:
                idle_connections.put_nowait(conn)
            except Full:
                conn.close()


def credit_processing(cur, run, received_at, new_question=False):
    """Server/DB waiting does not consume the player's sixty seconds."""
    ready_at = datetime.now(timezone.utc)
    run["started_at"] += max(timedelta(0), ready_at - received_at)
    if new_question:
        run["question_started_at"] = ready_at
    cur.execute("UPDATE runs SET started_at=%s, question_started_at=%s WHERE id=%s",
                (run["started_at"], run["question_started_at"], run["id"]))


def round_clock(run):
    return {"round_ms": round(seconds_left(run, datetime.now(timezone.utc)) * 1000),
            "ends_at": (run["started_at"] + timedelta(seconds=run["duration_seconds"])).isoformat()}


def error(message, status=400):
    return jsonify(error=message), status


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def run_id(data):
    try:
        return UUID(str(data.get("session_id", "")))
    except ValueError:
        return None


def seconds_left(run, now):
    return max(0.0, (run["started_at"] + timedelta(seconds=run["duration_seconds"]) - now).total_seconds())


def expire_runs(cur):
    cur.execute("""
        UPDATE runs SET finished_at = started_at + duration_seconds * interval '1 second'
        WHERE finished_at IS NULL AND started_at + duration_seconds * interval '1 second' <= now()
    """)


def best_query():
    return """
        WITH best AS (
            SELECT DISTINCT ON (lower(name)) name, lower(name) AS key, score, answered, finished_at
            FROM runs WHERE finished_at IS NOT NULL AND answered > 0 AND duration_seconds = 60
            ORDER BY lower(name), score DESC, answered DESC, finished_at ASC
        ), ranked AS (
            SELECT name, key, score, answered,
                   row_number() OVER (ORDER BY score DESC, answered DESC, finished_at ASC) AS rank
            FROM best
        )
    """


def summary(cur, run):
    cur.execute(best_query() + "SELECT rank FROM ranked WHERE key = lower(%s)", (run["name"],))
    row = cur.fetchone()
    return {
        "finished": True,
        "score": run["score"],
        "answered": run["answered"],
        "close_count": run["close_count"],
        "rank": row["rank"] if row else None,
    }


def finish_run(cur, run, now):
    if run["finished_at"] is None:
        cur.execute("UPDATE runs SET finished_at = %s WHERE id = %s", (min(now, run["started_at"] + timedelta(seconds=run["duration_seconds"])), run["id"]))
        run["finished_at"] = now
    return summary(cur, run)


def grade(guess, target, speed_seconds, risk, combo):
    difference = abs(guess - target) / target
    accuracy = 1000 if difference <= .05 else 800 if difference <= .10 else 600 if difference <= .20 else 350 if difference <= .35 else 150 if difference <= .50 else 30
    near = difference <= .10
    next_combo = combo + 1 if near else 0
    speed = round(speed_seconds * 10) if difference <= .20 else 0
    combo_bonus = 100 * (next_combo - 1) if near and next_combo > 1 else 0
    multiplier = (2 if difference <= .20 else 0) if risk else 1
    labels = [(0.05, "Tartı gözü!"), (0.10, "Çok yakın!"), (0.20, "İyi tahmin!"), (0.35, "Biraz uzak kaldın")]
    label = next((text for threshold, text in labels if difference <= threshold), "Sürpriz ağırlık!")
    return {
        "points": multiplier * (accuracy + speed + combo_bonus),
        "error": difference,
        "near": near,
        "nextCombo": next_combo,
        "label": label,
        "accuracy": accuracy,
        "speed": speed,
        "comboBonus": combo_bonus,
        "multiplier": multiplier,
    }


@app.after_request
def cors(response):
    if request.headers.get("Origin") == ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = ORIGIN
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def options(path):
    return "", 204


@app.get("/api/health")
def health():
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return jsonify(ok=True, cards=len(TARGETS))


@app.post("/api/start")
def start():
    name = re.sub(r"\s+", " ", str(body().get("name", "")).strip())
    if not 2 <= len(name) <= 20 or any(ord(c) < 32 for c in name):
        return error("Oyuncu adı 2–20 karakter olmalı.")
    now = datetime.now(timezone.utc)
    deck = varied_deck()
    session_id = uuid4()
    with database() as conn:
        with conn.cursor() as cur:
            now = datetime.now(timezone.utc)
            cur.execute("""
                INSERT INTO runs (id, name, started_at, question_started_at, deck, duration_seconds)
                VALUES (%s, %s, %s, %s, %s, 60)
            """, (session_id, name, now, now, json.dumps(deck)))
    return jsonify(session_id=str(session_id), ends_at=(now + timedelta(seconds=60)).isoformat(), question={"id": deck[0]})


@app.post("/api/guess")
def guess():
    data = body()
    session_id = run_id(data)
    value = data.get("guess")
    risk = data.get("risk", False)
    if session_id is None or type(value) is not int or not 1 <= value <= 9999999 or type(risk) is not bool:
        return error("Geçersiz tahmin.")
    now = datetime.now(timezone.utc)
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id = %s FOR UPDATE", (session_id,))
            run = cur.fetchone()
            if run is None:
                return error("Tur bulunamadı.", 404)
            if run["finished_at"] or seconds_left(run, now) <= 0:
                return jsonify(finish_run(cur, run, now))
            if run["awaiting_next"]:
                return error("Önce sonraki soruya geç.", 409)
            if risk and run["risk_used"]:
                return error("Risk hakkı kullanıldı.", 409)
            card_id = json.loads(run["deck"])[run["deck_index"]]
            speed_seconds = max(0.0, 15 - (now - run["question_started_at"]).total_seconds())
            result = grade(value, TARGETS[card_id]["grams"], speed_seconds, risk, run["combo"])
            score = run["score"] + result["points"]
            answered = run["answered"] + 1
            close_count = run["close_count"] + int(result["near"])
            credit_processing(cur, run, now)
            cur.execute("""
                UPDATE runs SET score=%s, answered=%s, close_count=%s, combo=%s,
                    risk_used=%s, awaiting_next=true WHERE id=%s
            """, (score, answered, close_count, result["nextCombo"], run["risk_used"] or risk, session_id))
    return jsonify(**result, target=TARGETS[card_id]["grams"], score=score, answered=answered,
                   close_count=close_count, risk_available=not (run["risk_used"] or risk),
                   seconds_left=speed_seconds, **round_clock(run))


@app.post("/api/next")
def next_question():
    session_id = run_id(body())
    if session_id is None:
        return error("Geçersiz tur.")
    now = datetime.now(timezone.utc)
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id = %s FOR UPDATE", (session_id,))
            run = cur.fetchone()
            if run is None:
                return error("Tur bulunamadı.", 404)
            if run["finished_at"] or seconds_left(run, now) <= 0:
                return jsonify(finish_run(cur, run, now))
            if not run["awaiting_next"]:
                return error("Önce tahmin yap.", 409)
            deck = json.loads(run["deck"])
            index = run["deck_index"] + 1
            if index >= len(deck):
                return jsonify(finish_run(cur, run, now))
            credit_processing(cur, run, now, new_question=True)
            cur.execute("""
                UPDATE runs SET deck=%s, deck_index=%s, question_started_at=%s,
                    awaiting_next=false WHERE id=%s
            """, (json.dumps(deck), index, run["question_started_at"], session_id))
    return jsonify(question={"id": deck[index]}, **round_clock(run))


@app.post("/api/finish")
def finish():
    session_id = run_id(body())
    if session_id is None:
        return error("Geçersiz tur.")
    now = datetime.now(timezone.utc)
    with database() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE id = %s FOR UPDATE", (session_id,))
            run = cur.fetchone()
            if run is None:
                return error("Tur bulunamadı.", 404)
            result = finish_run(cur, run, now)
    return jsonify(result)


@app.get("/api/leaderboard")
def leaderboard():
    with database() as conn:
        with conn.cursor() as cur:
            expire_runs(cur)
            cur.execute(best_query() + "SELECT name, score, answered, rank FROM ranked ORDER BY rank LIMIT 20")
            players = cur.fetchall()
    return jsonify(players=players)
