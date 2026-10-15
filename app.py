import os
import secrets
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.environ.get("ULY_GRILL_DB", os.path.join(BASE_DIR, "uly_grill.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
ADMIN_PASSWORD = os.environ.get("ULY_GRILL_ADMIN_PASSWORD", "change-this-password")
MAX_VISITS = 10

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        visits INTEGER NOT NULL DEFAULT 0 CHECK(visits >= 0 AND visits <= 10),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS visit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        staff_name TEXT NOT NULL DEFAULT 'admin',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        note TEXT NOT NULL DEFAULT 'Подарок выдан',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()[:70]
    phone = request.form.get("phone", "").strip()[:25]
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(name) < 2 or len(digits) < 10:
        flash("Проверь имя и номер телефона.", "error")
        return redirect(url_for("index"))
    conn = db()
    existing = conn.execute("SELECT card_code FROM customers WHERE phone = ?", (phone,)).fetchone()
    if existing:
        flash("Карта с таким номером уже есть. Введи номер в разделе поиска карты ниже.", "error")
        return redirect(url_for("find_card", phone=phone))
    card_code = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO customers(card_code, name, phone) VALUES(?,?,?)", (card_code, name, phone))
    conn.commit()
    return redirect(url_for("customer_card", card_code=card_code))

@app.route("/find-card", methods=["GET", "POST"])
def find_card():
    phone = request.values.get("phone", "").strip()
    customer = None
    if phone:
        customer = db().execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
        # For this starter app, lookup is by exact phone; don't show a card unless the user knows the phone.
    if request.method == "POST" and not customer:
        flash("Карта с таким номером не найдена. Проверь номер или создай новую карту.", "error")
    return render_template("find_card.html", customer=customer, phone=phone)

@app.route("/card/<card_code>")
def customer_card(card_code):
    customer = db().execute("SELECT * FROM customers WHERE card_code = ?", (card_code,)).fetchone()
    if not customer:
        abort(404)
    visits = db().execute(
        "SELECT created_at, staff_name FROM visit_log WHERE customer_id = ? ORDER BY id DESC LIMIT 10",
        (customer["id"],)
    ).fetchall()
    rewards = db().execute(
        "SELECT created_at, note FROM rewards WHERE customer_id = ? ORDER BY id DESC",
        (customer["id"],)
    ).fetchall()
    return render_template("card.html", customer=customer, visits=visits, rewards=rewards)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, ADMIN_PASSWORD):
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Неверный пароль.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    flash("Вы вышли из панели.", "success")
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = db()
    query = request.args.get("q", "").strip()
    if query:
        customers = conn.execute(
            "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY created_at DESC",
            (f"%{query}%", f"%{query}%")
        ).fetchall()
    else:
        customers = conn.execute("SELECT * FROM customers ORDER BY created_at DESC").fetchall()
    stats = {
        "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "visits": conn.execute("SELECT COUNT(*) FROM visit_log").fetchone()[0],
        "rewards": conn.execute("SELECT COUNT(*) FROM rewards").fetchone()[0],
    }
    return render_template("admin.html", customers=customers, stats=stats, query=query)

@app.route("/admin/visit/<int:customer_id>", methods=["POST"])
@admin_required
def add_visit(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not customer:
        abort(404)
    if customer["visits"] >= MAX_VISITS:
        flash("На карте уже 10 визитов. Сначала выдай подарок.", "error")
        return redirect(url_for("admin_dashboard"))
    conn.execute("UPDATE customers SET visits = visits + 1 WHERE id = ?", (customer_id,))
    conn.execute("INSERT INTO visit_log(customer_id, staff_name) VALUES(?,?)", (customer_id, "admin"))
    conn.commit()
    flash(f"Визит добавлен для {customer['name']}.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/reward/<int:customer_id>", methods=["POST"])
@admin_required
def issue_reward(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not customer:
        abort(404)
    if customer["visits"] < MAX_VISITS:
        flash("Подарок можно выдать после 10 визитов.", "error")
        return redirect(url_for("admin_dashboard"))
    conn.execute("INSERT INTO rewards(customer_id, note) VALUES(?,?)", (customer_id, "Подарок выдан"))
    conn.execute("UPDATE customers SET visits = 0 WHERE id = ?", (customer_id,))
    conn.commit()
    flash(f"Подарок отмечен, карта {customer['name']} начата заново.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/health")
def health():
    return {"status": "ok", "app": "ULY GRILL"}

@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
