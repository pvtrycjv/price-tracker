import time
import smtplib
import os
import psycopg2
from flask import Flask, request, redirect
from email.message import EmailMessage
from playwright.sync_api import sync_playwright
import re


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
    import html
    safe_product_name = html.escape(product_name)

    delete_url = (
        "https://price-tracker-rqi7.onrender.com/delete?url="
        + quote(url, safe="")
    )

    # -------- STATS -------- #
    stats = get_price_stats(url)

    if stats and stats["min"] is not None:
        min_p = stats["min"] if stats["min"] is not None else "-"
        max_p = stats["max"] if stats["max"] is not None else "-"
        avg_p = stats["avg"] if stats["avg"] is not None else "-"

        
        stats_text = (
            f"Min: {stats['min']} PLN\n"
            f"Max: {stats['max']} PLN\n"
            f"Avg: {stats['avg']} PLN"
        )
    else:
        stats_text = "No history yet"

    # -------- ALL PRODUCTS -------- #
    products = get_all_tracked_products()

    product_summary_html = ""
    product_summary_text = ""

    for name, purl, price in products:
        safe_name = html.escape(name if name else "Unknown")

        from urllib.parse import quote

        stop_tracking_url = (
            "https://price-tracker-rqi7.onrender.com/delete?url="
            + quote(purl, safe="")
        )

        product_summary_html += (
            f'<li>'
            f'<a href="{purl}">{safe_name}</a> → {price} PLN '
            f'<a href="{stop_tracking_url}" '
            f'style="color:#d9534f;text-decoration:none;font-weight:bold;" '
            f'title="Stop tracking this product">[x]</a>'
            f'</li>'
        )


        product_summary_text += (
            f"- {name if name else 'Unknown'} → {purl} → {price} PLN\n"
            f"  Stop tracking: {stop_tracking_url}\n"
        )



    # -------- EMAIL -------- #
    msg = EmailMessage()
    msg["Subject"] = f"🌸 Price Dropped: {product_name}"
    msg["From"] = EMAIL
    msg["To"] = EMAIL

    # plain text version
    msg.set_content(f"""
Price dropped!

Product: {product_name}
Old Price: {old_price} PLN
New Price: {new_price} PLN

--- Stats ---
{stats_text}

--- All tracked products ---
{product_summary_text}

Link:
{url}

Stop tracking:
{delete_url}
""")

    # HTML version
    msg.add_alternative(f"""
    <html>

    <body>

    <h2>🌸 Price Dropped!</h2>

    <p>
        <b>{safe_product_name}</b><br>
        Old price: {old_price} PLN<br>
        New price: {new_price} PLN
    </p>

    <h3>📈 Stats</h3>
    <p style="white-space:pre-line;">{stats_text}</p>

    <h3>🛒 All tracked products</h3>
    <ul>
        {product_summary_html}
    </ul>

    <p>
        <a href="{url}"
           style="display:inline-block;padding:10px 18px;background:#eddbdd;color:black;text-decoration:none;border-radius:6px;">
           🔗 Open link
        </a>

        &nbsp;

        <a href="https://price-tracker-rqi7.onrender.com/"
           style="display:inline-block;padding:10px 18px;background:#eddbdd;color:black;text-decoration:none;border-radius:6px;">
           ➕ Add product
        </a>
    </p>

    <p>
        <a href="{delete_url}"
           style="display:inline-block;padding:10px 18px;background:#eddbdd;color:black;text-decoration:none;border-radius:6px;">
           🗑 Stop tracking
        </a>
    </p>

    </body>
    </html>
    """, subtype="html")

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
        name TEXT,
        url TEXT UNIQUE,
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


def update_price(url, name, price):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO products (name, url, last_price)
        VALUES (%s, %s, %s)
        ON CONFLICT (url)
        DO UPDATE SET
            name = EXCLUDED.name,
            last_price = EXCLUDED.last_price
""", (name, url, price))

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

    cursor.execute("SELECT name, url, last_price FROM products")
    rows = cursor.fetchall()

    conn.close()
    return rows



# ---------------- SCRAPER ---------------- #

def check_price(url, page):
    try:
        import re
        import json

        print("\n--- Checking product ---")
        print("URL:", url)

        page.goto(
            url,
            timeout=30000,
            wait_until="domcontentloaded"
        )

        page.wait_for_timeout(3000)

        # =====================================================
        # HELPER: CLEAN AND CONVERT PRICE TO FLOAT
        # =====================================================

        def parse_price(text):
            if not text:
                return None

            text = str(text).strip()

            # Remove currency and unnecessary characters
            text = (
                text.replace("zł", "")
                    .replace("PLN", "")
                    .replace("pln", "")
                    .replace("\xa0", "")
                    .replace(" ", "")
                    .replace(",", ".")
            )

            # Keep only numbers and decimal points
            text = re.sub(r"[^\d.]", "", text)

            try:
                return float(text)
            except ValueError:
                return None

        # =====================================================
        # PRODUCT NAME
        # =====================================================

        name = "Unknown product"

        # 1. Open Graph title
        try:
            og_title = page.query_selector(
                'meta[property="og:title"]'
            )

            if og_title:
                content = og_title.get_attribute("content")

                if content and content.strip():
                    name = content.strip()
                    print("NAME FROM OG:TITLE:", name)

        except Exception as e:
            print("OG title error:", e)

        # 2. H1
        if name == "Unknown product":
            try:
                h1 = page.query_selector("h1")

                if h1:
                    h1_text = h1.inner_text().strip()

                    if h1_text:
                        name = h1_text
                        print("NAME FROM H1:", name)

            except Exception as e:
                print("H1 error:", e)

        # 3. Meta title
        if name == "Unknown product":
            try:
                meta_title = page.query_selector(
                    'meta[name="title"]'
                )

                if meta_title:
                    content = meta_title.get_attribute("content")

                    if content:
                        name = content.strip()
                        print("NAME FROM META TITLE:", name)

            except Exception as e:
                print("Meta title error:", e)

        # 4. JSON-LD product name
        if name == "Unknown product":
            try:
                scripts = page.query_selector_all(
                    'script[type="application/ld+json"]'
                )

                for script in scripts:
                    content = script.inner_text()

                    if not content:
                        continue

                    data = json.loads(content)

                    def find_product_name(obj):
                        if isinstance(obj, dict):

                            if (
                                obj.get("@type") == "Product"
                                and obj.get("name")
                            ):
                                return obj["name"]

                            for value in obj.values():
                                result = find_product_name(value)

                                if result:
                                    return result

                        elif isinstance(obj, list):

                            for item in obj:
                                result = find_product_name(item)

                                if result:
                                    return result

                        return None

                    json_name = find_product_name(data)

                    if json_name:
                        name = json_name.strip()
                        print("NAME FROM JSON-LD:", name)
                        break

            except Exception as e:
                print("JSON-LD name error:", e)

        print("FINAL NAME:", name)

        # =====================================================
        # PRICE EXTRACTION
        # =====================================================

        price = None

        # =====================================================
        # 1. VISIBLE PRICE SELECTORS
        # =====================================================

        price_selectors = [
            '[data-testid="price"]',
            '[data-testid="product-price"]',
            '[data-testid="price-current"]',
            '[itemprop="price"]',
            '.price',
            '.product-price',
            '.current-price',
            '.sales-price',
            '.special-price'
        ]

        print("\n--- PRICE SEARCH ---")

        for selector in price_selectors:

            try:
                elements = page.query_selector_all(selector)

                for element in elements:

                    if not element.is_visible():
                        continue

                    text = element.inner_text().strip()

                    if not text:
                        continue

                    candidate = parse_price(text)

                    if candidate is not None and candidate > 0:

                        print(
                            "PRICE CANDIDATE:",
                            selector,
                            "->",
                            text,
                            "->",
                            candidate
                        )

                        # Avoid accepting tiny values like
                        # "5" from a price fragment or quantity
                        if candidate >= 1:

                            price = candidate

                            print(
                                "PRICE FROM VISIBLE SELECTOR:",
                                selector,
                                price
                            )

                            break

                if price is not None:
                    break

            except Exception as e:
                print("Selector error:", selector, e)

        # =====================================================
        # 2. JSON-LD PRODUCT PRICE
        # =====================================================

        if price is None:

            try:

                scripts = page.query_selector_all(
                    'script[type="application/ld+json"]'
                )

                for script in scripts:

                    content = script.inner_text()

                    if not content:
                        continue

                    data = json.loads(content)

                    def find_product_price(obj):

                        if isinstance(obj, dict):

                            obj_type = obj.get("@type")

                            if (
                                obj_type == "Product"
                                or (
                                    isinstance(obj_type, list)
                                    and "Product" in obj_type
                                )
                            ):

                                offers = obj.get("offers")

                                if isinstance(offers, dict):

                                    value = offers.get("price")

                                    if value:
                                        return value

                                    price_spec = offers.get(
                                        "priceSpecification"
                                    )

                                    if isinstance(price_spec, dict):
                                        return price_spec.get("price")

                                elif isinstance(offers, list):

                                    for offer in offers:

                                        if isinstance(offer, dict):
                                            value = offer.get("price")

                                            if value:
                                                return value

                            for value in obj.values():

                                result = find_product_price(value)

                                if result is not None:
                                    return result

                        elif isinstance(obj, list):

                            for item in obj:

                                result = find_product_price(item)

                                if result is not None:
                                    return result

                        return None

                    json_price = find_product_price(data)

                    if json_price is not None:

                        candidate = parse_price(json_price)

                        if candidate is not None:

                            price = candidate

                            print(
                                "PRICE FROM JSON-LD:",
                                price
                            )

                            break

            except Exception as e:
                print("JSON-LD price error:", e)

        # =====================================================
        # 3. META PRICE
        # =====================================================

        if price is None:

            meta_selectors = [
                'meta[property="product:price:amount"]',
                'meta[property="og:price:amount"]',
                'meta[name="twitter:data1"]'
            ]

            for selector in meta_selectors:

                try:

                    meta = page.query_selector(selector)

                    if meta:

                        content = meta.get_attribute("content")

                        candidate = parse_price(content)

                        if candidate is not None:

                            price = candidate

                            print(
                                "PRICE FROM META:",
                                selector,
                                price
                            )

                            break

                except Exception as e:
                    print("Meta price error:", e)

        # =====================================================
        # 4. PAGE TEXT FALLBACK
        # =====================================================

        if price is None:

            try:

                body_text = page.locator("body").inner_text()

                # Find prices followed by PLN or zł
                matches = re.findall(
                    r"(\d[\d\s\xa0]*[.,]\d{2})\s*(?:zł|PLN)",
                    body_text,
                    re.IGNORECASE
                )

                print("BODY PRICE MATCHES:", matches)

                if matches:

                    # Usually the first price is the main product
                    candidate = parse_price(matches[0])

                    if candidate is not None:

                        price = candidate

                        print(
                            "PRICE FROM BODY TEXT:",
                            price
                        )

            except Exception as e:
                print("Body price error:", e)

        # =====================================================
        # 5. FINAL CHECK
        # =====================================================

        if price is None:

            print("❌ PRICE NOT FOUND")
            return

        print("\n==============================")
        print("FINAL PRODUCT:", name)
        print("FINAL PRICE:", price, "PLN")
        print("==============================\n")

        # =====================================================
        # DATABASE + EMAIL
        # =====================================================

        old_price = get_saved_price(url)

        if old_price is None:

            print("First run, saving price.")

            update_price(url, name, price)

        elif price < old_price * 0.99:

            print("🚨 PRICE DROPPED!")

            update_price(url, name, price)

            send_email(
                name,
                old_price,
                price,
                url
            )

        else:

            update_price(url, name, price)

        save_price_history(url, price)

    except Exception as e:

        print(f"❌ ERROR for {url}: {e}")
                 
            
            
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
        "DELETE FROM price_history WHERE product_id = %s",
        (url,)
    )

    conn.commit()
    conn.close()

    return redirect("/")


# ---------------- MAIN ---------------- #

# ---------------- PRICE HISTORY ---------------- #

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


# ---------------- PRICE STATISTICS ---------------- #

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


# ---------------- CLEANUP OLD HISTORY ---------------- #

def cleanup_old_history():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM price_history
        WHERE checked_at < NOW() - INTERVAL '60 days'
    """)

    conn.commit()
    conn.close()



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
