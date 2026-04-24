import sqlite3
import time
import smtplib
import shutil
import sys
import os
import psycopg2
from urllib.parse import urlparse
from email.message import EmailMessage
from playwright.sync_api import sync_playwright


DATABASE_URL = os.environ.get("DATABASE_URL")

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# ---------------- MAIL ---------------- #
EMAIL = os.environ.get("EMAIL")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

def send_email(product_id, old_price, new_price, url):
    msg = EmailMessage()
    msg["Subject"] = "🚨 Price Dropped!"
    msg["From"] = EMAIL
    msg["To"] = EMAIL

    msg.set_content(f"""
Price dropped!

Product ID: {product_id}
Old Price: {old_price}
New Price: {new_price}

Link: {url}
""")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL, APP_PASSWORD)
        server.send_message(msg)

    print("📩 Email sent!")

# ---------------- DATABASE ---------------- #

DB_NAME = "prices.db"

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT UNIQUE,
        url TEXT,
        last_price REAL
    )
    """)

    conn.commit()
    conn.close()

def get_saved_price(product_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT last_price FROM products WHERE product_id = ?",
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
        VALUES (?, ?, ?)
        ON CONFLICT(product_id)
        DO UPDATE SET last_price = excluded.last_price
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

def check_price(product_id, url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0",
            locale="pl-PL"
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)

            price_el = page.query_selector("span.price")
            if not price_el:
                print("Price not found")
                return

            price_text = price_el.inner_text().strip()
            price_text = price_text.replace("\xa0", "").replace(" ", "").replace("PLN", "").replace(",", ".")

            price = float(price_text)
            print(f"{product_id} -> {price} PLN")

            old_price = get_saved_price(product_id)

            if old_price is None:
                print("First run, saving price.")

            elif price < old_price * 0.99:
                print("🚨 PRICE DROPPED!")
                send_email(product_id, old_price, price, url)

            update_price(product_id, url, price)

        finally:
            browser.close()

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    init_db()

    products = get_all_products()
    if not products:
        print("No products in DB.")
    else:
        for product_id, url in products:
            check_price(product_id, url)
            time.sleep(2)
