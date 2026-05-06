import time
import smtplib
import os
import psycopg2
from flask import Flask, request
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


def extract_product_id(url):
    return url.rstrip("/").split("/")[-1]


# ---------------- EMAIL ---------------- #
def send_email(product_name, old_price, new_price, url):
    msg = EmailMessage()
    msg["Subject"] = f"🌸 Price Dropped: {product_name}"
    msg["From"] = EMAIL
    msg["To"] = EMAIL

    msg.set_content(f"""
Price dropped!

Product: {product_name}
Old Price: {old_price} PLN
New Price: {new_price} PLN

Link: {url}
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

    conn.commit()
    conn.close()


def get_saved_price(product_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_price FROM products WHERE product_id = %s",
        (product_id,)
    )

    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def update_price(product_id, url, price):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (product_id, url, last_price)
        VALUES (%s, %s, %s)
        ON CONFLICT (product_id)
        DO UPDATE SET last_price = EXCLUDED.last_price
    """, (product_id, url, price))

    conn.commit()
    conn.close()


def get_all_products():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT product_id, url FROM products")
    rows = cursor.fetchall()

    conn.close()
    return rows



# ---------------- SCRAPER ---------------- #

def check_price(product_id, url, page):
    try:
        print("\n--- Checking product ---")
        print("URL:", url)

        page.goto(
            url,
            timeout=30000,
            wait_until="domcontentloaded"
        )

        # Small delay helps JS render
        page.wait_for_timeout(3000)

        # -------- PRODUCT NAME -------- #
        name = "Unknown product"

        try:
            page.wait_for_selector("h1", timeout=5000)
            title_el = page.query_selector("h1")

            if title_el:
                name = title_el.inner_text().strip()
        except:
            print("Could not get product name")

        print("NAME:", name)

        # -------- PRICE LOGIC -------- #
        price = None

        # -------- FORMAT 1 (Ceneo standard) -------- #
        whole = page.query_selector(".price-format__whole")
        fraction = page.query_selector(".price-format__fraction")

        if whole:
            w = whole.inner_text().strip()
            f = fraction.inner_text().strip() if fraction else "00"

            if w and w.replace(".", "").isdigit():
                price = float(f"{w}.{f}")
            else:
                print("Invalid price format (format1):", repr(w))

        # -------- FORMAT 2 (value + penny) -------- #
        if price is None:
            value = page.query_selector(".value")
            penny = page.query_selector(".penny")

            if value:
                v = value.inner_text().strip()
                p = penny.inner_text().replace(",", "").strip() if penny else "00"

                if v.isdigit():
                    price = float(f"{v}.{p}")
                else:
                    print("Invalid price format (format2):", repr(v))

        # -------- FORMAT 3 (.price text) -------- #
        if price is None:
            alt = page.query_selector(".price")
            if alt:
                text = alt.inner_text().strip()
                if text:
                    text = text.replace("zł", "").replace(",", ".").replace(" ", "")
                    try:
                        price = float(text)
                    except:
                        print("Invalid price format (format3):", repr(text))

        # -------- FORMAT 4 (regex fallback ANYWHERE) -------- #
        if price is None:
            import re
            body_text = page.inner_text("body")

            match = re.search(r"(\d+[.,]\d+)\s*zł", body_text)
            if match:
                price = float(match.group(1).replace(",", "."))
                print("Fallback regex used")

        # -------- FINAL CHECK -------- #
        if price is None:
            print("❌ Price not found")
            return

        print(f"{product_id} -> {price} PLN")

    except Exception as e:
        print(f"❌ ERROR for {product_id}: {e}")

        # -------- DATABASE -------- #
        old_price = get_saved_price(product_id)

        if old_price is None:
            print("First run, saving price.")

        elif price < old_price * 0.99:
            print("🚨 PRICE DROPPED!")
            send_email(product_id, old_price, price, url)

        update_price(product_id, url, price)

    except Exception as e:
        print(f"❌ ERROR for {product_id}: {e}")
           
 

# ---------------- FLASK ROUTES ---------------- #
@app.route("/")
def index():
    return "Price tracker is running ✅"


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

        for product_id, url in products:
            check_price(product_id, url, page)
            time.sleep(3)  # important: slow down

        browser.close()

    return "Scraper finished."


@app.route("/add")
def add_from_url():
    url = request.args.get("url")

    if not url:
        return "Use /add?url=PRODUCT_URL"

    clean = clean_url(url)
    product_id = extract_product_id(clean)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (product_id, url, last_price)
        VALUES (%s, %s, %s)
        ON CONFLICT (product_id) DO NOTHING
    """, (product_id, clean, None))

    conn.commit()
    conn.close()

    return f"Added: {product_id}"


# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    init_db()

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

                for product_id, url in products:
                    check_price(product_id, url, page)
                    time.sleep(3)

                browser.close()

    # Otherwise → run Flask (Render)
    else:
        app.run(host="0.0.0.0", port=10000)
