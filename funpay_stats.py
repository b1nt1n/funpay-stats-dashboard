#!/usr/bin/env python3
"""
FunPay profile dashboard.

Examples:
  python funpay_stats.py --profile 7451912 --open
  python funpay_stats.py --profile 7451912 --golden-key YOUR_GOLDEN_KEY --open
  python funpay_stats.py --sales-html sales.html --purchases-html purchases.html --open
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys
import textwrap
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://funpay.com"
ORDER_ENDPOINTS = {
    "sales": f"{BASE_URL}/orders/trade",
    "purchases": f"{BASE_URL}/orders/",
}
PERIODS = {
    "day": {"label": "День", "long_label": "За день", "days": 1},
    "week": {"label": "Неделя", "long_label": "За неделю", "days": 7},
    "month": {"label": "Месяц", "long_label": "За месяц", "days": 31},
    "year": {"label": "Год", "long_label": "За год", "days": 365},
    "all": {"label": "Все время", "long_label": "За все время", "days": None},
}
DIRECTIONS = {
    "sales": {
        "label": "Продажи",
        "money_label": "Всего заработал",
        "orders_label": "Всего продаж",
        "unique_label": "Уникальных покупателей",
        "active_label": "Самый активный покупатель",
        "best_label": "Самая дорогая продажа",
        "counterparty_label": "покупателей",
        "empty_note": "Продажи не найдены. Проверь golden_key или передай сохраненный HTML через --sales-html.",
    },
    "purchases": {
        "label": "Покупки",
        "money_label": "Всего потратил",
        "orders_label": "Всего покупок",
        "unique_label": "Уникальных продавцов",
        "active_label": "Самый активный продавец",
        "best_label": "Самая дорогая покупка",
        "counterparty_label": "продавцов",
        "empty_note": "Покупки доступны только в авторизованном режиме или через --purchases-html.",
    },
}
MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
STATUS_LABELS = {
    "closed": "Закрыто",
    "paid": "В ожидании",
    "refunded": "Возвратов",
    "unknown": "Неизвестно",
}


@dataclass(slots=True)
class Order:
    id: str
    description: str
    price: float
    counterparty_username: str
    counterparty_id: str
    status: str
    date: datetime | None
    category: str
    direction: str


@dataclass(slots=True)
class Offer:
    category: str
    description: str
    price: float


@dataclass(slots=True)
class Review:
    detail: str
    price: float
    category: str
    text: str


@dataclass(slots=True)
class Profile:
    user_id: str | None
    username: str
    rating: str
    reviews_count: int
    registered: str
    avatar_url: str | None
    offers: list[Offer]
    reviews: list[Review]
    review_continue: str | None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_money(value: str | None) -> float:
    if not value:
        return 0.0
    value = value.replace("\xa0", " ").replace(",", ".")
    match = re.search(r"(-?\d+(?:[ .]\d{3})*(?:\.\d+)?)", value)
    if not match:
        return 0.0
    number = match.group(1).replace(" ", "")
    try:
        return float(number)
    except ValueError:
        return 0.0


def money(value: float, currency: str = "₽") -> str:
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value)):,}".replace(",", " ") + f" {currency}"
    return f"{value:,.2f}".replace(",", " ") + f" {currency}"


def normalize_profile_arg(profile: str | None) -> str | None:
    if not profile:
        return None
    profile = profile.strip()
    if profile.isdigit():
        return f"{BASE_URL}/users/{profile}/"
    if profile.startswith(("http://", "https://")):
        return profile
    raise SystemExit("Профиль нужно передать ссылкой FunPay или числовым ID.")


def extract_user_id(profile_url: str | None) -> str | None:
    if not profile_url:
        return None
    match = re.search(r"/users/(\d+)/?", profile_url)
    return match.group(1) if match else None


def absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    return url


def request_session(cookie: str | None, golden_key: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
        }
    )
    if cookie:
        session.headers["Cookie"] = cookie
    elif golden_key:
        session.cookies.set("golden_key", golden_key, domain="funpay.com")
    return session


def fetch_text(session: requests.Session, url: str, *, data: dict[str, str] | None = None) -> str:
    response = session.post(url, data=data, timeout=25) if data is not None else session.get(url, timeout=25)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def parse_app_data(soup: BeautifulSoup) -> dict:
    body = soup.find("body")
    if not body or not body.get("data-app-data"):
        return {}
    try:
        return json.loads(body["data-app-data"])
    except json.JSONDecodeError:
        return {}


def parse_continue_token(soup: BeautifulSoup) -> str | None:
    node = soup.find("input", {"type": "hidden", "name": "continue"})
    return node.get("value") if node else None


def parse_reviews_from_soup(soup: BeautifulSoup) -> list[Review]:
    reviews: list[Review] = []
    for item in soup.select(".review-container"):
        detail_node = item.select_one(".review-item-detail")
        text_node = item.select_one(".review-item-text")
        detail = clean_text(detail_node.get_text(" ", strip=True) if detail_node else "")
        text = clean_text(text_node.get_text(" ", strip=True) if text_node else "")
        category = detail.split(",")[0].strip() if detail else "Отзывы"
        reviews.append(Review(detail=detail, price=parse_money(detail), category=category, text=text))
    return reviews


def parse_profile(html_text: str, profile_url: str | None = None) -> Profile:
    soup = BeautifulSoup(html_text, "html.parser")
    user_id = extract_user_id(profile_url)

    h1 = soup.select_one(".profile h1")
    username = "FunPay"
    if h1:
        name_span = h1.select_one(".mr4")
        username = clean_text(name_span.get_text(" ", strip=True) if name_span else h1.get_text(" ", strip=True))
    elif title := soup.find("title"):
        username = clean_text(title.get_text()).split("/")[0].replace("Пользователь", "").strip() or "FunPay"

    rating = "—"
    if rating_node := soup.select_one(".profile-header-col-rating .rating-value"):
        rating = clean_text(rating_node.get_text(" ", strip=True))

    reviews_count = 0
    if count_node := soup.select_one(".rating-full-count"):
        match = re.search(r"\d+", count_node.get_text(" ", strip=True))
        reviews_count = int(match.group()) if match else 0
    if not reviews_count:
        match = re.search(r"(\d+)\s+отзыв", soup.get_text(" ", strip=True))
        reviews_count = int(match.group(1)) if match else 0

    registered = "—"
    for item in soup.select(".profile-header-cols .param-item"):
        label = clean_text(item.find("h5").get_text(" ", strip=True) if item.find("h5") else "")
        if "Дата регистрации" in label or "Registration date" in label:
            div = item.find("div")
            registered = clean_text(div.get_text(" ", strip=True) if div else "")
            break

    avatar_url = None
    avatar = soup.select_one(".avatar-photo")
    if avatar and avatar.get("style"):
        match = re.search(r"url\((.*?)\)", avatar["style"])
        avatar_url = match.group(1).strip("'\"") if match else None
    if not avatar_url and (img := soup.select_one(".chat-header .media-left img")):
        avatar_url = img.get("src")

    offers: list[Offer] = []
    for offer_block in soup.select(".profile-data-container .offer"):
        category_node = offer_block.select_one(".offer-list-title h3")
        if not category_node:
            continue
        category = clean_text(category_node.get_text(" ", strip=True))
        for row in offer_block.select("a.tc-item"):
            desc_node = row.select_one(".tc-desc-text")
            price_node = row.select_one(".tc-price")
            offers.append(
                Offer(
                    category=category,
                    description=clean_text(desc_node.get_text(" ", strip=True) if desc_node else ""),
                    price=parse_money(price_node.get_text(" ", strip=True) if price_node else ""),
                )
            )

    return Profile(
        user_id=user_id,
        username=username,
        rating=rating,
        reviews_count=reviews_count,
        registered=registered,
        avatar_url=absolute_url(avatar_url),
        offers=offers,
        reviews=parse_reviews_from_soup(soup),
        review_continue=parse_continue_token(soup),
    )


def fetch_more_public_reviews(session: requests.Session, profile: Profile, max_pages: int) -> None:
    if not profile.user_id or not profile.review_continue or max_pages <= 0:
        return

    seen = {(review.detail, review.text) for review in profile.reviews}
    continue_id = profile.review_continue

    for page in range(max_pages):
        html_text = fetch_text(
            session,
            f"{BASE_URL}/users/reviews",
            data={"user_id": profile.user_id, "continue": continue_id, "filter": ""},
        )
        soup = BeautifulSoup(html_text, "html.parser")
        new_reviews = []
        for review in parse_reviews_from_soup(soup):
            key = (review.detail, review.text)
            if key not in seen:
                new_reviews.append(review)
                seen.add(key)

        profile.reviews.extend(new_reviews)
        continue_id = parse_continue_token(soup)
        profile.review_continue = continue_id
        if not continue_id or not new_reviews or (profile.reviews_count and len(profile.reviews) >= profile.reviews_count):
            break
        print(f"Загружено страниц публичных отзывов: {page + 1}; отзывов: {len(profile.reviews)}")


def parse_order_date(value: str) -> datetime | None:
    value = clean_text(value).lower()
    if not value:
        return None
    now = datetime.now()

    try:
        if value.startswith("сегодня"):
            hour, minute = value.split(", ")[1].split(":")
            return datetime(now.year, now.month, now.day, int(hour), int(minute))
        if value.startswith("вчера"):
            hour, minute = value.split(", ")[1].split(":")
            day = now - timedelta(days=1)
            return datetime(day.year, day.month, day.day, int(hour), int(minute))

        date_part, time_part = value.split(", ")
        parts = date_part.split()
        hour, minute = time_part.split(":")
        if len(parts) == 2:
            day, month_name = parts
            year = now.year
        elif len(parts) == 3:
            day, month_name, year = parts
        else:
            return None
        month = MONTHS_RU.get(month_name)
        if not month:
            return None
        return datetime(int(year), month, int(day), int(hour), int(minute))
    except (ValueError, IndexError):
        return None


def detect_status(row) -> str:
    classes = row.get("class") or []
    if "warning" in classes:
        return "refunded"
    if "info" in classes:
        return "paid"
    return "closed"


def parse_counterparty(row) -> tuple[str, str]:
    node = (
        row.select_one(".media-user-name span[data-href]")
        or row.select_one(".media-user-name a[href]")
        or row.select_one(".media-user-name span")
        or row.select_one(".media-user-name")
    )
    if not node:
        return "—", ""

    username = clean_text(node.get_text(" ", strip=True)) or "—"
    href = node.get("data-href") or node.get("href") or ""
    match = re.search(r"/users/(\d+)/?", href)
    return username, (match.group(1) if match else "")


def parse_orders(html_text: str, direction: str) -> tuple[list[Order], str | None]:
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.select_one(".content-account-login, .content-account.content-account-login"):
        raise SystemExit("FunPay вернул страницу входа. Проверь cookie/golden_key.")

    orders: list[Order] = []
    for row in soup.select("a.tc-item"):
        order_node = row.select_one(".tc-order")
        if not order_node:
            continue

        order_id = clean_text(order_node.get_text(" ", strip=True)).lstrip("#")
        desc_node = row.select_one(".order-desc div") or row.select_one(".order-desc")
        price_node = row.select_one(".tc-price")
        date_node = row.select_one(".tc-date-time")
        category_node = row.select_one(".text-muted")
        username, user_id = parse_counterparty(row)

        orders.append(
            Order(
                id=order_id,
                description=clean_text(desc_node.get_text(" ", strip=True) if desc_node else ""),
                price=parse_money(price_node.get_text(" ", strip=True) if price_node else ""),
                counterparty_username=username,
                counterparty_id=user_id,
                status=detect_status(row),
                date=parse_order_date(date_node.get_text(" ", strip=True) if date_node else ""),
                category=clean_text(category_node.get_text(" ", strip=True) if category_node else "Без категории"),
                direction=direction,
            )
        )

    return orders, parse_continue_token(soup)


def fetch_order_list(session: requests.Session, direction: str, max_pages: int) -> list[Order]:
    url = ORDER_ENDPOINTS[direction]
    all_orders: list[Order] = []
    seen_ids: set[str] = set()
    continue_id: str | None = None
    title = DIRECTIONS[direction]["label"].lower()
    print(f"Загружаю {title}: {url}")

    for page in range(max_pages):
        html_text = fetch_text(session, url, data={"continue": continue_id} if continue_id else None)
        orders, next_id = parse_orders(html_text, direction)
        for order in orders:
            key = f"{direction}:{order.id}"
            if order.id and key not in seen_ids:
                all_orders.append(order)
                seen_ids.add(key)
        if not next_id or next_id == continue_id:
            break
        continue_id = next_id
        print(f"Загружено страниц {title}: {page + 1}; заказов: {len(all_orders)}")
    return all_orders


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    days = PERIODS[period]["days"]
    return None if days is None else now - timedelta(days=int(days))


def filter_orders(orders: Iterable[Order], period: str) -> list[Order]:
    start = period_start(period)
    if not start:
        return list(orders)
    return [order for order in orders if order.date is None or order.date >= start]


def most_common(counter: Counter[str], default: str = "—") -> str:
    return counter.most_common(1)[0][0] if counter else default


def median_or_zero(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def serial_chart(items: Iterable[tuple[str, float]]) -> list[dict[str, float | str]]:
    return [{"label": label, "value": round(value, 2)} for label, value in items]


def empty_stats(direction: str, currency: str, note: str | None = None) -> dict:
    return {
        "direction": direction,
        "mode": "empty",
        "total_value": 0.0,
        "total_orders": 0,
        "average_check": 0.0,
        "median_check": 0.0,
        "closed": 0,
        "pending": 0,
        "refunded": 0,
        "unique_counterparties": 0,
        "active_counterparty": "—",
        "active_counterparty_count": 0,
        "best_value": 0.0,
        "best_title": "—",
        "popular_product": "—",
        "popular_category": "—",
        "refund_rate": 0.0,
        "closed_value": 0.0,
        "pending_value": 0.0,
        "refunded_value": 0.0,
        "category_chart": [],
        "status_chart": [],
        "monthly_chart": [],
        "currency": currency,
        "note": note or DIRECTIONS[direction]["empty_note"],
    }


def build_stats_from_orders(orders: list[Order], currency: str, direction: str) -> dict:
    paid_orders = [order for order in orders if order.status != "refunded"]
    closed = [order for order in orders if order.status == "closed"]
    pending = [order for order in orders if order.status == "paid"]
    refunded = [order for order in orders if order.status == "refunded"]
    prices = [order.price for order in paid_orders if order.price > 0]

    counterparty_counter = Counter(
        order.counterparty_username for order in paid_orders if order.counterparty_username and order.counterparty_username != "—"
    )
    product_counter = Counter(order.description for order in paid_orders if order.description)
    category_counter = Counter(order.category for order in paid_orders if order.category)
    value_by_category: defaultdict[str, float] = defaultdict(float)
    value_by_status: defaultdict[str, float] = defaultdict(float)
    monthly: defaultdict[str, float] = defaultdict(float)

    for order in orders:
        value_by_status[STATUS_LABELS.get(order.status, "Другое")] += order.price
    for order in paid_orders:
        value_by_category[order.category] += order.price
        if order.date:
            monthly[order.date.strftime("%Y-%m")] += order.price

    best_order = max(paid_orders, key=lambda item: item.price, default=None)
    active = counterparty_counter.most_common(1)[0] if counterparty_counter else ("—", 0)
    total_orders = len(orders)

    return {
        "direction": direction,
        "mode": "orders",
        "total_value": round(sum(order.price for order in closed), 2),
        "total_orders": total_orders,
        "average_check": round((sum(prices) / len(prices)) if prices else 0.0, 2),
        "median_check": round(median_or_zero(prices), 2),
        "closed": len(closed),
        "pending": len(pending),
        "refunded": len(refunded),
        "unique_counterparties": len(
            set(order.counterparty_id or order.counterparty_username for order in paid_orders if order.counterparty_id or order.counterparty_username)
        ),
        "active_counterparty": active[0],
        "active_counterparty_count": active[1],
        "best_value": round(best_order.price if best_order else 0.0, 2),
        "best_title": best_order.description if best_order else "—",
        "popular_product": most_common(product_counter),
        "popular_category": most_common(category_counter),
        "refund_rate": round((len(refunded) / total_orders * 100) if total_orders else 0.0, 1),
        "closed_value": round(sum(order.price for order in closed), 2),
        "pending_value": round(sum(order.price for order in pending), 2),
        "refunded_value": round(sum(order.price for order in refunded), 2),
        "category_chart": serial_chart(sorted(value_by_category.items(), key=lambda item: item[1], reverse=True)[:8]),
        "status_chart": serial_chart(sorted(value_by_status.items(), key=lambda item: item[1], reverse=True)),
        "monthly_chart": serial_chart(sorted(monthly.items())[-12:]),
        "currency": currency,
        "note": "Точные данные из авторизованного списка заказов FunPay.",
    }


def build_stats_from_public(profile: Profile, currency: str) -> dict:
    review_prices = [review.price for review in profile.reviews if review.price > 0]
    offer_prices = [offer.price for offer in profile.offers if offer.price > 0]
    category_counter = Counter(review.category for review in profile.reviews if review.category)
    offer_category_counter = Counter(offer.category for offer in profile.offers if offer.category)
    product_counter = Counter(offer.description for offer in profile.offers if offer.description)
    value_by_category: defaultdict[str, float] = defaultdict(float)
    for review in profile.reviews:
        value_by_category[review.category] += review.price

    max_review = max(profile.reviews, key=lambda item: item.price, default=None)
    max_offer = max(profile.offers, key=lambda item: item.price, default=None)
    total_value = sum(review_prices)

    return {
        "direction": "sales",
        "mode": "public",
        "total_value": round(total_value, 2),
        "total_orders": profile.reviews_count or len(profile.reviews),
        "average_check": round((sum(review_prices) / len(review_prices)) if review_prices else 0.0, 2),
        "median_check": round(median_or_zero(review_prices), 2),
        "closed": len(profile.reviews),
        "pending": 0,
        "refunded": 0,
        "unique_counterparties": 0,
        "active_counterparty": "Недоступно публично",
        "active_counterparty_count": 0,
        "best_value": round((max_review.price if max_review and max_review.price else max(offer_prices, default=0.0)), 2),
        "best_title": max_review.detail if max_review and max_review.price else (max_offer.description if max_offer else "—"),
        "popular_product": most_common(product_counter),
        "popular_category": most_common(category_counter or offer_category_counter),
        "refund_rate": 0.0,
        "closed_value": round(total_value, 2),
        "pending_value": 0.0,
        "refunded_value": 0.0,
        "category_chart": serial_chart(sorted(value_by_category.items(), key=lambda item: item[1], reverse=True)[:8]),
        "status_chart": serial_chart([("Отзывы", total_value)] if total_value else []),
        "monthly_chart": [],
        "currency": currency,
        "note": "Публичный режим: продажи оценены по отзывам и офферам. Покупатели, возвраты и точные периоды доступны только через golden_key.",
    }


def build_dashboard_data(
    profile: Profile | None,
    sales: list[Order],
    purchases: list[Order],
    currency: str,
    default_period: str,
) -> dict:
    stats = {"sales": {}, "purchases": {}}
    for period in PERIODS:
        period_sales = filter_orders(sales, period)
        period_purchases = filter_orders(purchases, period)
        if period_sales:
            stats["sales"][period] = build_stats_from_orders(period_sales, currency, "sales")
        elif profile:
            stats["sales"][period] = build_stats_from_public(profile, currency)
        else:
            stats["sales"][period] = empty_stats("sales", currency)

        if period_purchases:
            stats["purchases"][period] = build_stats_from_orders(period_purchases, currency, "purchases")
        else:
            stats["purchases"][period] = empty_stats("purchases", currency)

    return {
        "defaultPeriod": default_period,
        "currency": currency,
        "generatedAt": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "profile": {
            "username": profile.username if profile else "FunPay",
            "rating": profile.rating if profile else "—",
            "registered": profile.registered if profile else "—",
            "avatarUrl": profile.avatar_url if profile else None,
        },
        "periods": {key: {"label": value["label"], "longLabel": value["long_label"]} for key, value in PERIODS.items()},
        "directions": DIRECTIONS,
        "stats": stats,
    }


def render_dashboard(data: dict, output: Path) -> Path:
    profile = data["profile"]
    username = profile["username"]
    avatar = profile.get("avatarUrl")
    avatar_html = (
        f'<img src="{html.escape(avatar)}" alt="" />'
        if avatar
        else f'<div class="avatar-fallback">{html.escape(username[:1].upper())}</div>'
    )
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html_doc = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Статистика FunPay - {html.escape(username)}</title>
<style>
:root {{
    color-scheme: dark;
    --bg: #111312;
    --panel: rgba(38, 38, 38, .78);
    --panel-strong: rgba(58, 58, 58, .82);
    --line: rgba(255, 255, 255, .12);
    --text: #f6f4ef;
    --muted: #b9b6ad;
    --green: #44d37d;
    --yellow: #ffd23f;
    --red: #ff544a;
    --blue: #64b5ff;
    --shadow: 0 24px 80px rgba(0, 0, 0, .42);
}}
* {{ box-sizing: border-box; }}
html, body {{ min-height: 100%; }}
body {{
    margin: 0;
    font-family: Inter, "Segoe UI", Arial, sans-serif;
    color: var(--text);
    background:
        radial-gradient(circle at 18% 12%, rgba(68, 211, 125, .14), transparent 28rem),
        radial-gradient(circle at 82% 22%, rgba(100, 181, 255, .15), transparent 24rem),
        linear-gradient(135deg, #171816, #242322 48%, #151515);
}}
button {{ font: inherit; }}
.page {{
    min-height: 100vh;
    padding: 28px;
    display: grid;
    place-items: center;
}}
.shell {{
    width: min(1460px, 100%);
    border: 1px solid var(--line);
    border-radius: 22px;
    background: linear-gradient(145deg, rgba(38, 38, 38, .84), rgba(24, 24, 24, .88));
    box-shadow: var(--shadow);
    overflow: hidden;
    backdrop-filter: blur(18px);
}}
.top {{
    display: flex;
    justify-content: space-between;
    gap: 20px;
    align-items: center;
    padding: 34px 34px 24px;
    border-bottom: 1px solid var(--line);
}}
.identity {{
    display: flex;
    align-items: center;
    gap: 18px;
    min-width: 0;
}}
.avatar {{
    width: 66px;
    height: 66px;
    border-radius: 18px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.18);
    background: #2b2d2b;
    flex: 0 0 auto;
}}
.avatar img, .avatar-fallback {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: grid;
    place-items: center;
    font-size: 30px;
    font-weight: 800;
}}
h1 {{
    margin: 0;
    font-size: clamp(28px, 3vw, 44px);
    line-height: 1;
    letter-spacing: 0;
}}
.subline {{
    margin-top: 8px;
    color: var(--muted);
    display: flex;
    flex-wrap: wrap;
    gap: 10px 16px;
    font-size: 15px;
}}
.actions {{
    display: grid;
    gap: 12px;
    justify-items: end;
}}
.pill {{
    border: 1px solid var(--line);
    background: rgba(255,255,255,.08);
    color: var(--text);
    border-radius: 12px;
    padding: 12px 16px;
    font-weight: 700;
    white-space: nowrap;
}}
.content {{
    padding: 26px 34px 34px;
}}
.toolbar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 24px;
}}
.segmented {{
    display: flex;
    gap: 6px;
    padding: 6px;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: rgba(255,255,255,.06);
}}
.segmented button {{
    border: 0;
    color: var(--muted);
    background: transparent;
    border-radius: 10px;
    padding: 10px 14px;
    font-weight: 800;
    cursor: pointer;
    white-space: nowrap;
}}
.segmented button.active {{
    color: var(--text);
    background: rgba(255,255,255,.14);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.12);
}}
.grid {{
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 20px;
}}
.stat-card {{
    min-height: 126px;
    display: flex;
    align-items: center;
    gap: 22px;
    padding: 24px;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: var(--panel);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
}}
.stat-card.wide {{
    grid-column: span 2;
    background: linear-gradient(110deg, rgba(57, 89, 92, .72), rgba(54, 76, 56, .68));
}}
.stat-card.green {{ border-left: 5px solid var(--green); }}
.stat-card.yellow {{ border-left: 5px solid var(--yellow); }}
.stat-card.red {{ border-left: 5px solid var(--red); }}
.icon {{
    width: 54px;
    height: 54px;
    display: grid;
    place-items: center;
    font-size: 34px;
    flex: 0 0 auto;
}}
.label {{
    color: var(--muted);
    font-size: 16px;
    font-weight: 750;
    line-height: 1.35;
}}
.value {{
    margin-top: 8px;
    font-size: clamp(28px, 3vw, 44px);
    font-weight: 850;
    line-height: 1;
    letter-spacing: 0;
}}
.details {{
    margin-top: 26px;
    display: grid;
    grid-template-columns: minmax(0, 1.05fr) minmax(360px, .95fr);
    gap: 24px;
    border-top: 1px solid var(--line);
    padding-top: 26px;
}}
.panel {{
    border: 1px solid var(--line);
    border-radius: 14px;
    background: rgba(35, 35, 35, .7);
    padding: 24px;
}}
.panel h2 {{
    margin: 0 0 18px;
    font-size: 22px;
    letter-spacing: 0;
}}
.facts {{
    display: grid;
    gap: 16px;
    font-size: 17px;
}}
.fact {{
    display: grid;
    grid-template-columns: 28px minmax(160px, auto) minmax(0, 1fr);
    gap: 10px;
    align-items: baseline;
}}
.fact span:nth-child(2) {{ color: var(--muted); font-weight: 750; }}
.fact strong {{
    min-width: 0;
    overflow-wrap: anywhere;
}}
.bar-list {{
    display: grid;
    gap: 12px;
}}
.bar-row {{
    display: grid;
    grid-template-columns: minmax(120px, 1fr) minmax(160px, 2fr) auto;
    gap: 14px;
    align-items: center;
    font-size: 14px;
}}
.bar-label {{
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.bar-track {{
    height: 12px;
    border-radius: 999px;
    background: rgba(255,255,255,.09);
    overflow: hidden;
}}
.bar-track span {{
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--green), var(--blue));
}}
.bar-value {{
    font-weight: 800;
    white-space: nowrap;
}}
.charts {{
    display: grid;
    gap: 18px;
}}
.empty {{
    color: var(--muted);
    padding: 10px 0;
}}
.note {{
    margin-top: 22px;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.5;
}}
@media (max-width: 980px) {{
    .page {{ padding: 14px; }}
    .top, .content {{ padding-left: 18px; padding-right: 18px; }}
    .top {{ align-items: flex-start; flex-direction: column; }}
    .actions {{ justify-items: start; }}
    .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .details {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
    .segmented {{ width: 100%; overflow-x: auto; }}
    .grid {{ grid-template-columns: 1fr; }}
    .stat-card.wide {{ grid-column: auto; }}
    .stat-card {{ min-height: 108px; padding: 20px; gap: 16px; }}
    .value {{ font-size: 30px; }}
    .bar-row {{ grid-template-columns: 1fr; gap: 7px; }}
    .identity {{ align-items: flex-start; }}
    .fact {{ grid-template-columns: 28px 1fr; }}
    .fact strong {{ grid-column: 2; }}
}}
</style>
</head>
<body>
<main class="page">
    <section class="shell">
        <header class="top">
            <div class="identity">
                <div class="avatar">{avatar_html}</div>
                <div>
                    <h1 id="page-title">Статистика FunPay</h1>
                    <div class="subline">
                        <span>{html.escape(username)}</span>
                        <span>Рейтинг: {html.escape(profile["rating"])}</span>
                        <span>Регистрация: {html.escape(profile["registered"])}</span>
                    </div>
                </div>
            </div>
            <div class="actions">
                <div class="pill">Обновлено: {html.escape(data["generatedAt"])}</div>
                <div class="pill" id="period-pill">За год</div>
            </div>
        </header>

        <div class="content">
            <div class="toolbar">
                <div class="segmented" id="direction-controls" aria-label="Тип статистики"></div>
                <div class="segmented" id="period-controls" aria-label="Период статистики"></div>
            </div>

            <section class="grid" id="stats-grid" aria-label="Основная статистика"></section>

            <section class="details">
                <div class="panel">
                    <h2>Итоги</h2>
                    <div class="facts" id="facts"></div>
                    <div class="note" id="source-note"></div>
                </div>

                <div class="charts">
                    <div class="panel">
                        <h2>Категории</h2>
                        <div class="bar-list" id="category-chart"></div>
                    </div>
                    <div class="panel">
                        <h2>Статусы</h2>
                        <div class="bar-list" id="status-chart"></div>
                    </div>
                    <div class="panel">
                        <h2>Динамика</h2>
                        <div class="bar-list" id="monthly-chart"></div>
                    </div>
                </div>
            </section>
        </div>
    </section>
</main>

<script>
const DASHBOARD_DATA = {payload};
const state = {{ direction: "sales", period: DASHBOARD_DATA.defaultPeriod || "year" }};

function formatMoney(value) {{
    const number = Number(value || 0);
    const body = number.toLocaleString("ru-RU", {{ minimumFractionDigits: Number.isInteger(number) ? 0 : 2, maximumFractionDigits: 2 }});
    return `${{body}} ${{DASHBOARD_DATA.currency}}`;
}}

function formatPercent(value) {{
    return `${{Number(value || 0).toLocaleString("ru-RU", {{ maximumFractionDigits: 1 }})}}%`;
}}

function el(tag, className, text) {{
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}}

function renderControls() {{
    const directionControls = document.getElementById("direction-controls");
    const periodControls = document.getElementById("period-controls");
    directionControls.innerHTML = "";
    periodControls.innerHTML = "";

    Object.entries(DASHBOARD_DATA.directions).forEach(([key, config]) => {{
        const button = el("button", key === state.direction ? "active" : "", config.label);
        button.type = "button";
        button.addEventListener("click", () => {{
            state.direction = key;
            render();
        }});
        directionControls.appendChild(button);
    }});

    Object.entries(DASHBOARD_DATA.periods).forEach(([key, config]) => {{
        const button = el("button", key === state.period ? "active" : "", config.label);
        button.type = "button";
        button.addEventListener("click", () => {{
            state.period = key;
            render();
        }});
        periodControls.appendChild(button);
    }});
}}

function statCard(icon, label, value, options = {{}}) {{
    const card = el("article", `stat-card ${{options.wide ? "wide" : ""}} ${{options.accent || ""}}`.trim());
    card.appendChild(el("div", "icon", icon));
    const body = el("div");
    body.appendChild(el("div", "label", label));
    body.appendChild(el("div", "value", value));
    card.appendChild(body);
    return card;
}}

function fact(icon, label, value) {{
    const row = el("div", "fact");
    row.appendChild(el("span", "", icon));
    row.appendChild(el("span", "", `${{label}}:`));
    row.appendChild(el("strong", "", value || "—"));
    return row;
}}

function renderBars(container, items, moneyValues = true) {{
    container.innerHTML = "";
    if (!items || !items.length) {{
        container.appendChild(el("div", "empty", "Недостаточно данных для графика"));
        return;
    }}
    const max = Math.max(...items.map(item => Number(item.value || 0)), 1);
    items.forEach(item => {{
        const value = Number(item.value || 0);
        const row = el("div", "bar-row");
        const label = el("div", "bar-label", item.label || "—");
        label.title = item.label || "—";
        const track = el("div", "bar-track");
        const bar = document.createElement("span");
        bar.style.width = `${{Math.max(5, value / max * 100)}}%`;
        track.appendChild(bar);
        row.appendChild(label);
        row.appendChild(track);
        row.appendChild(el("div", "bar-value", moneyValues ? formatMoney(value) : String(Math.round(value))));
        container.appendChild(row);
    }});
}}

function render() {{
    const stats = DASHBOARD_DATA.stats[state.direction][state.period];
    const direction = DASHBOARD_DATA.directions[state.direction];
    const period = DASHBOARD_DATA.periods[state.period];

    document.getElementById("page-title").textContent = `Статистика FunPay: ${{direction.label.toLowerCase()}}`;
    document.getElementById("period-pill").textContent = period.longLabel;

    const grid = document.getElementById("stats-grid");
    grid.innerHTML = "";
    grid.appendChild(statCard(state.direction === "sales" ? "💰" : "💳", direction.money_label, formatMoney(stats.total_value), {{ wide: true }}));
    grid.appendChild(statCard("📦", direction.orders_label, String(stats.total_orders || 0)));
    grid.appendChild(statCard("📈", "Средний чек", formatMoney(stats.average_check)));
    grid.appendChild(statCard("✅", "Закрыто", String(stats.closed || 0), {{ accent: "green" }}));
    grid.appendChild(statCard("⏳", "В ожидании", String(stats.pending || 0), {{ accent: "yellow" }}));
    grid.appendChild(statCard("↩", "Возвратов", String(stats.refunded || 0), {{ accent: "red" }}));
    grid.appendChild(statCard("👥", direction.unique_label, stats.unique_counterparties ? String(stats.unique_counterparties) : "—"));

    const facts = document.getElementById("facts");
    facts.innerHTML = "";
    const activeSuffix = stats.active_counterparty_count ? ` (${{stats.active_counterparty_count}})` : "";
    facts.appendChild(fact("🏆", direction.active_label, `${{stats.active_counterparty || "—"}}${{activeSuffix}}`));
    facts.appendChild(fact("💎", direction.best_label, `${{formatMoney(stats.best_value)}} · ${{stats.best_title || "—"}}`));
    facts.appendChild(fact("🔥", "Самый популярный товар", stats.popular_product));
    facts.appendChild(fact("🎮", "Самая популярная категория", stats.popular_category));
    facts.appendChild(fact("📊", "Медианный чек", formatMoney(stats.median_check)));
    facts.appendChild(fact("🧾", "Доля возвратов", formatPercent(stats.refund_rate)));
    facts.appendChild(fact("🟢", "Сумма закрытых", formatMoney(stats.closed_value)));
    facts.appendChild(fact("🟡", "Сумма в ожидании", formatMoney(stats.pending_value)));
    facts.appendChild(fact("🔴", "Сумма возвратов", formatMoney(stats.refunded_value)));
    document.getElementById("source-note").textContent = stats.note || "";

    renderBars(document.getElementById("category-chart"), stats.category_chart, true);
    renderBars(document.getElementById("status-chart"), stats.status_chart, true);
    renderBars(document.getElementById("monthly-chart"), stats.monthly_chart, true);
    renderControls();
}}

render();
</script>
</body>
</html>
"""
    output.write_text(html_doc, encoding="utf-8")
    return output


def read_file(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def load_profile(args: argparse.Namespace, session: requests.Session) -> Profile | None:
    profile_url = normalize_profile_arg(args.profile)
    profile_html = read_file(args.profile_html)
    if profile_html:
        return parse_profile(profile_html, profile_url)
    if profile_url:
        print(f"Загружаю публичный профиль: {profile_url}")
        profile = parse_profile(fetch_text(session, profile_url), profile_url)
        fetch_more_public_reviews(session, profile, args.max_review_pages)
        return profile
    return None


def load_orders(args: argparse.Namespace, session: requests.Session, direction: str) -> list[Order]:
    html_path = args.sales_html if direction == "sales" else args.purchases_html
    if direction == "sales" and not html_path:
        html_path = args.orders_html
    html_text = read_file(html_path)
    if html_text:
        orders, _ = parse_orders(html_text, direction)
        return orders
    if args.golden_key or args.cookie or os.getenv("FUNPAY_GOLDEN_KEY"):
        return fetch_order_list(session, direction, args.max_pages)
    return []


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Генерирует красивую HTML-статистику FunPay по продажам и покупкам.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Без авторизации скрипт строит публичную статистику продаж по профилю.
            Для точной статистики продаж и покупок передай cookie golden_key:
              $env:FUNPAY_GOLDEN_KEY="..."
              python funpay_stats.py --profile 7451912 --open
            """
        ),
    )
    parser.add_argument("--profile", help="Ссылка на профиль FunPay или ID пользователя.")
    parser.add_argument("--golden-key", default=os.getenv("FUNPAY_GOLDEN_KEY"), help="Значение cookie golden_key.")
    parser.add_argument("--cookie", help="Полная строка Cookie из браузера, если нужен не только golden_key.")
    parser.add_argument("--sales-html", help="Путь к сохраненной HTML-странице продаж /orders/trade.")
    parser.add_argument("--purchases-html", help="Путь к сохраненной HTML-странице покупок /orders/.")
    parser.add_argument("--orders-html", help="Старый алиас для --sales-html.")
    parser.add_argument("--profile-html", help="Путь к сохраненной HTML-странице профиля.")
    parser.add_argument("--period", choices=list(PERIODS), default="year", help="Период, который будет выбран в HTML по умолчанию.")
    parser.add_argument("--max-pages", type=int, default=30, help="Сколько страниц заказов загрузить максимум для каждого направления.")
    parser.add_argument("--max-review-pages", type=int, default=15, help="Сколько дополнительных страниц публичных отзывов загрузить.")
    parser.add_argument("--currency", default="₽", help="Символ валюты для отображения.")
    parser.add_argument("--output", default="funpay_stats.html", help="Куда сохранить HTML-дашборд.")
    parser.add_argument("--open", action="store_true", help="Открыть HTML после генерации.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    session = request_session(args.cookie, args.golden_key)

    profile = load_profile(args, session)
    sales = load_orders(args, session, "sales")
    purchases = load_orders(args, session, "purchases")

    if not profile and not sales and not purchases:
        raise SystemExit("Нужен хотя бы --profile, --profile-html, --golden-key, --sales-html или --purchases-html.")

    data = build_dashboard_data(profile, sales, purchases, args.currency, args.period)
    output = render_dashboard(data, Path(args.output).resolve())
    print(f"Готово: {output}")
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
