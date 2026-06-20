import time
import smtplib
import os
import psycopg2
from flask import Flask, request, redirect
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

# ---------------- APP ---------------- #
app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
EMAIL = os.environ.get("EMAIL")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


# ---------------- DB CONNECTION ---------------- #

def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

# ---------------- HELPERS ---------------- #
def clean_url(url):
    return url.split("?")[0]


# ---------------- EMAIL ---------------- #
 
 
def send_email(product_name, old_price, new_price, url):
    from urllib.parse import quote

    delete_url = (
        "https://price-tracker-rqi7.onrender.com/delete?url="
        + quote(url, safe="")
    )

    # -------- STATS -------- #
    stats = get_price_stats(url)

    if stats and stats["min"] is not None:
        stats_text = (
            f"Min: {stats['min']} PLN\n"
            f"Max: {stats['max']} PLN\n"
            f"Avg: {stats['avg']} PLN"
        )
    else:
        stats_text = "No history yet"

    # -------- ALL PRODUCTS -------- #
    products = get_all_tracked_products()

    product_summary = "\n".join(
        [f"- {purl} → {price} PLN" for purl, price in products]
    )

    msg = EmailMessage()
    msg["Subject"] = f"🌸 Price Dropped: {product_name}"
    msg["From"] = EMAIL
    msg["To"] = EMAIL

    msg.set_content(f"""
Price dropped!

Product: {product_name}
Old Price: {old_price} PLN
New Price: {new_price} PLN


[📈] Stats
{stats_text}

[🛒] All tracked products
{product_summary}

[🔗] Link:
{url}

[🗑] Stop tracking:
{delete_url}
""")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL, APP_PASSWORD)
        server.send_message(msg)

    print("📩 Email sent!")


# ---------------- DATABASE ---------------- #
def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        product_id TEXT UNIQUE,
        url TEXT,
        last_price FLOAT
    )
    """)

    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id SERIAL PRIMARY KEY,
        product_id TEXT,
        price FLOAT,
        checked_at TIMESTAMP DEFAULT NOW()
    )
    """)

    conn.commit()
    conn.close()


def get_saved_price(url):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_price FROM products WHERE url = %s",
        (url,)
    )

    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def update_price(url, price):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (url, last_price)
        VALUES (%s, %s)
        ON CONFLICT (url)
        DO UPDATE SET last_price = EXCLUDED.last_price
    """, (url, price))

    conn.commit()
    conn.close()


def get_all_products():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT url, last_price FROM products")
    rows = cursor.fetchall()

    conn.close()
    return rows

def get_all_tracked_products():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT url, last_price FROM products")
    rows = cursor.fetchall()

    conn.close()
    return rows


# ---------------- SCRAPER ---------------- #

def check_price(url, page):
    try:
        print("\n--- Checking product ---")
        print("URL:", url)

        page.goto(
            url,
            timeout=30000,
            wait_until="domcontentloaded"
        )

        page.wait_for_timeout(3000)

        # -------- PRODUCT NAME -------- #
        name = "Unknown product"

        try:
            page.wait_for_selector("h1", timeout=5000)
            title_el = page.query_selector("h1")

            if title_el:
                name = title_el.inner_text().strip()

        except Exception as e:
            print("Could not get product name:", e)

        print("NAME:", name)

        # =====================================================
        # PRICE LOGIC (RESTORED SIMPLE VERSION)
        # =====================================================

        price = None

        # -------- FORMAT 1 (CENEO SIMPLE) -------- #
        whole = page.query_selector(".price-format__whole")
        fraction = page.query_selector(".price-format__fraction")

        if whole:
            w = whole.inner_text().strip()
            f = fraction.inner_text().strip() if fraction else "00"

            if w and w.replace(".", "").isdigit():
                price = float(f"{w}.{f}")

        # -------- FORMAT 2 -------- #
        if price is None:
            value = page.query_selector(".value")
            penny = page.query_selector(".penny")

            if value:
                v = value.inner_text().strip()
                p = penny.inner_text().replace(",", "").strip() if penny else "00"

                if v.isdigit():
                    price = float(f"{v}.{p}")

        # -------- FORMAT 3 -------- #
        if price is None:
            alt = page.query_selector(".price")

            if alt:
                text = alt.inner_text().strip()

                if text:
                    text = (
                        text.replace("zł", "")
                            .replace(",", ".")
                            .replace(" ", "")
                    )

                    try:
                        price = float(text)
                    except:
                        print("Invalid format3")

        # -------- FORMAT 4 (fallback) -------- #
        if price is None:
            import re

            body_text = page.inner_text("body")

            match = re.search(r"(\d+[.,]\d+)\s*zł", body_text)

            if match:
                price = float(match.group(1).replace(",", "."))

                print("Fallback regex used")

        # =====================================================
        # FINAL CHECK
        # =====================================================

        if price is None:
            print("❌ Price not found")
            return

        print(f"{url} -> {price} PLN")

        # =====================================================
        # DATABASE + EMAIL
        # =====================================================

        old_price = get_saved_price(url)

        if old_price is None:
            print("First run, saving price.")

        elif price < old_price * 0.99:
            print("🚨 PRICE DROPPED!")
            send_email(name, old_price, price, url)

        update_price(url, price)
        save_price_history(url, price)

    except Exception as e:
        print(f"❌ ERROR for {url}: {e}")
        
 
def save_price_history(url, price):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT price
        FROM price_history
        WHERE product_id = %s
        ORDER BY checked_at DESC
        LIMIT 1
    """, (url,))

    last = cursor.fetchone()

    if last is None or abs(last[0] - price) > 0.01:
        cursor.execute("""
            INSERT INTO price_history (product_id, price)
            VALUES (%s, %s)
        """, (url, price))

    conn.commit()
    conn.close()



def get_price_stats(product_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            MIN(price),
            MAX(price),
            AVG(price)
        FROM price_history
        WHERE product_id = %s
    """, (product_id,))

    stats = cursor.fetchone()
    conn.close()

    return {
        "min": stats[0],
        "max": stats[1],
        "avg": round(stats[2], 2) if stats[2] else None
    }
    
def cleanup_old_history():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM price_history
        WHERE checked_at < NOW() - INTERVAL '60 days'
    """)

    conn.commit()
    conn.close()

# ---------------- FLASK ROUTES ---------------- #
@app.route("/")
def index():
    return """
    <h1>🌸 Price Tracker</h1>

    <form action="/add" method="get">
        <input
            type="text"
            name="url"
            placeholder="Paste product URL here..."
            style="width:500px;height:35px;"
            required
        >

        <button style="height:40px;" type="submit">
            Start tracking
        </button>
    </form>
    """


@app.route("/run")
def run_scraper():
    products = get_all_products()

    if not products:
        return "No products in database."

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            locale="pl-PL"
        )

        page = context.new_page()

        for url, last_price in products:
            check_price(url, page)
            time.sleep(3)  

        browser.close()

    return "Scraper finished."


@app.route("/add")
def add_from_url():
    url = request.args.get("url")

    if not url:
        return "Use /add?url=PRODUCT_URL"

    clean = clean_url(url)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (url, last_price)
        VALUES (%s, %s)
        ON CONFLICT (url) DO NOTHING
    """, (clean, None))

    conn.commit()
    conn.close()

    return f"Added: {clean}"


@app.route("/delete")
def delete_product():
    url = request.args.get("url")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM products WHERE url = %s",
        (url,)
    )

    cursor.execute(
        "DELETE FROM price_history WHERE url = %s",
        (url,)
    )

    conn.commit()
    conn.close()

    return redirect("/")


# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    init_db()
    cleanup_old_history()

    # If running in GitHub Actions → run scraper
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("Running in GitHub Actions...")

        products = get_all_products()

        if not products:
            print("No products in DB.")
        else:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    locale="pl-PL"
                )

                page = context.new_page()
                page.set_default_timeout(30000)

                for url, last_price in products:
                    check_price(url, page)
                    time.sleep(3)

                browser.close()

    # Otherwise → run Flask (Render)
    else:
        app.run(host="0.0.0.0", port=10000)
