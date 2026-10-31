import os, re, csv, io, json, base64, math
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
import seaborn as sns

app = Flask(__name__)
ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(ROOT, "data", "history.csv")
os.makedirs(os.path.dirname(FILE), exist_ok=True)
STORES = ["Amazon", "Flipkart", "Google Books", "Apple Books"]
FIELDS = ["checked_at", "query", "source", "title", "status",
          "original_price", "original_currency", "converted", "base", "url"]


def fetch(url, params=None):
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def book(title, price, currency, url, **extra):
    return dict(title=title, original_price=number(price),
                original_currency=currency, url=url, **extra)


def similarity(query, title):
    clean = lambda s: " ".join(re.findall(r"\w+", s.lower()))
    return round(100 * max(
        SequenceMatcher(None, clean(query), clean(t)).ratio()
        for t in [title, title.split(":")[0]]))


def structured_products(data, url):
    result = []
    if isinstance(data, list):
        for item in data:
            result.extend(structured_products(item, url))
    elif isinstance(data, dict):
        types = data.get("@type", [])
        if "Product" in ([types] if isinstance(types, str) else types):
            offers = data.get("offers") or []
            for offer in offers if isinstance(offers, list) else [offers]:
                if not isinstance(offer, dict) or not data.get("name"):
                    continue
                blocked = any(s in str(offer.get("availability", ""))
                              for s in ["OutOfStock", "SoldOut", "Discontinued"])
                result.append(book(
                    data["name"], None if blocked else offer.get("price"),
                    offer.get("priceCurrency"),
                    urljoin(url, offer.get("url") or data.get("url") or "")))
        for child in data.values():
            if isinstance(child, (dict, list)):
                result.extend(structured_products(child, url))
    return result


def products(source, query):
    result = []
    if source in ["Amazon", "Flipkart"]:
        amazon = source == "Amazon"
        url = "https://www.amazon.in/s" if amazon else "https://www.flipkart.com/search"
        r = fetch(url, {"k": query, "i": "stripbooks"} if amazon else {"q": query})
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()
        if any(s in text for s in ["robot check", "verify you are human",
                                   "enter the characters you see below"]):
            raise ValueError("Website returned a verification page.")

        if amazon:
            for card in soup.select('[data-component-type="s-search-result"]'):
                title = card.select_one("h2")
                link = card.select_one('a[href*="/dp/"]')
                price = card.select_one(".a-price:not(.a-text-price) .a-offscreen")
                if title and link:
                    match = re.search(r"₹\s*([\d,]+(?:\.\d{1,2})?)",
                                      price.get_text() if price else "")
                    result.append(book(
                        title.get_text(" ", strip=True),
                        match.group(1).replace(",", "") if match else None,
                        "INR", urljoin(r.url, link["href"]), country="IN"))

        if not result:
            for tag in soup.select('script[type="application/ld+json"]'):
                try:
                    result.extend(structured_products(json.loads(tag.get_text()), r.url))
                except (ValueError, TypeError):
                    continue
        if not result:
            raise ValueError("No readable products. Page may require JavaScript.")

    elif source == "Google Books":
        params = {"q": "intitle:" + query, "maxResults": 20}
        if os.getenv("GOOGLE_BOOKS_API_KEY"):
            params["key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
        data = fetch("https://www.googleapis.com/books/v1/volumes", params).json()
        for item in data.get("items", []):
            info, sale = item.get("volumeInfo") or {}, item.get("saleInfo") or {}
            price = sale.get("retailPrice") or sale.get("listPrice") or {}
            if info.get("title"):
                result.append(book(
                    info["title"], price.get("amount"), price.get("currencyCode"),
                    sale.get("buyLink") or info.get("infoLink", ""),
                    free=sale.get("saleability") == "FREE",
                    authors=info.get("authors", []), country=sale.get("country")))
    else:
        country = os.getenv("BOOKS_COUNTRY", "US")
        data = fetch("https://itunes.apple.com/search",
                     dict(term=query, country=country, media="ebook",
                          entity="ebook", limit=20)).json()
        result = [
            book(i["trackName"], i.get("price"), i.get("currency"),
                 i.get("trackViewUrl", ""), authors=[i.get("artistName", "")],
                 country=country)
            for i in data.get("results", []) if i.get("trackName")]
    return result


def compare(source, query, base, rates):
    result = {"source": source}
    try:
        items = products(source, query)
        if not items:
            return dict(result, status="not_found", error="No matching book.")
        chosen = max(items, key=lambda b: similarity(query, b["title"]))
        score = similarity(query, chosen["title"])
        if score < 85:
            return dict(result, status="not_found", error="No close title match.")
        result.update(chosen, match=score)
        if chosen.get("free"):
            return dict(result, status="ok", original_price=0, converted=0)
        price, currency = chosen["original_price"], chosen["original_currency"]
        if price is None or not currency:
            return dict(result, status="unavailable", error="No selling price.")
        if not re.fullmatch(r"[A-Z]{3}", str(currency)):
            raise ValueError("Invalid price currency.")
        if currency != base:
            pair = (currency, base)
            if pair not in rates:
                data = fetch(f"https://api.frankfurter.dev/v2/rate/{currency}/{base}").json()
                rates[pair] = number(data.get("rate"))
            if not rates[pair]:
                raise ValueError("Exchange rate unavailable.")
            price *= rates[pair]
        return dict(result, status="ok", converted=round(price, 2))
    except requests.RequestException:
        return dict(result, status="error",
                    error="Request failed, timed out, or was blocked/rate-limited.")
    except ValueError as error:
        return dict(result, status="error", error=str(error))
    except Exception:
        return dict(result, status="error", error="Unexpected website response.")


def chart(results, base):
    valid = [r for r in results if r["status"] == "ok"]
    fig = Figure(figsize=(7, 3.5), layout="constrained")
    ax = fig.subplots()
    if valid:
        sns.barplot(x=[r["converted"] for r in valid],
                    y=[r["source"] for r in valid], orient="h",
                    color="#8270e3", errorbar=None, ax=ax)
        ax.bar_label(ax.containers[0], fmt="%.2f", padding=4)
        ax.set_xlim(0, max(1, max(r["converted"] for r in valid)) * 1.3)
        ax.set_xlabel(f"Price ({base})")
        sns.despine(ax=ax)
    else:
        ax.text(.5, .5, "No prices available", ha="center", va="center")
        ax.set_axis_off()
    image = io.BytesIO()
    fig.savefig(image, format="png", dpi=120)
    return "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()


def safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


@app.get("/")
def home():
    path = os.path.join(ROOT, "index.html")
    return send_file(path) if os.path.isfile(path) else ("Add index.html beside app.py.", 404)


@app.get("/api/search")
def search():
    queries = list(dict.fromkeys(q.strip() for q in request.args.getlist("q") if q.strip()))
    base = request.args.get("base", "INR").upper()
    stores = list(dict.fromkeys(s.strip() for s in request.args.get(
        "sources", "Amazon,Flipkart").split(",") if s.strip()))
    if not queries or len(queries) > 6 or any(len(q) > 250 for q in queries):
        return jsonify(error="Enter 1–6 titles, up to 250 characters each."), 400
    if base not in {"INR", "USD", "GBP", "EUR"} or not stores or any(s not in STORES for s in stores):
        return jsonify(error="Invalid currency or website."), 400

    books, rates = [], {}
    for query in queries:
        results = [compare(s, query, base, rates) for s in stores]
        valid = [r for r in results if r["status"] == "ok"]
        best = min(valid, key=lambda r: r["converted"], default=None)
        books.append(dict(
            query=query, results=results, chart=chart(results, base),
            cheapest=best["source"] if best else None,
            savings=round(max(r["converted"] for r in valid) -
                          best["converted"], 2) if best else 0))

    now, saved = datetime.now().astimezone().isoformat(), True
    try:
        new = not os.path.exists(FILE) or os.path.getsize(FILE) == 0
        with open(FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                writer.writeheader()
            for b in books:
                for r in b["results"]:
                    row = {k: r.get(k) for k in FIELDS}
                    row.update(query=b["query"], base=base, checked_at=now)
                    writer.writerow({k: safe(v) for k, v in row.items()})
    except OSError:
        saved = False
    return jsonify(books=books, base=base, checked_at=now, history_saved=saved)


@app.get("/api/export.csv")
def export_csv():
    if os.path.exists(FILE):
        return send_file(FILE, as_attachment=True, download_name="book-radar-history.csv")
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(FIELDS)
    return send_file(io.BytesIO(buffer.getvalue().encode()), mimetype="text/csv",
                     as_attachment=True, download_name="book-radar-history.csv")


@app.get("/api/history")
def history():
    records = {}
    query, base = request.args.get("q", "").casefold(), request.args.get("base", "INR").upper()
    if base not in {"INR", "USD", "GBP", "EUR"}:
        return jsonify(error="Invalid currency."), 400
    if os.path.exists(FILE):
        with open(FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["status"] == "ok" and row["base"] == base:
                    if not query or row["query"].casefold() == query:
                        records.setdefault(row["source"], []).append(dict(
                            t=row["checked_at"], price=float(row["converted"]), query=row["query"]))
    return jsonify(records)


if __name__ == "__main__":
    app.run(port=5000, debug=False, threaded=False)