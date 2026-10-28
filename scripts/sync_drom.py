#!/usr/bin/env python3
"""Синхронизация каталога GOLDEN ROAD с витриной дилера на Дроме.

Берёт все объявления со страниц https://auto.drom.ru/goldenroad/ и приводит
data/cars.json к ним:
  * цена, пробег, комплектация, «в наличии в Китае / во Владивостоке» — обновляются;
  * машины, которых больше нет на Дроме (продана / снята), — удаляются вместе с фото;
  * новые объявления — добавляются (фото скачивается в img/).

Если Дром не отдал витрину целиком (ошибка, капча, меньше машин, чем пишет сам
Дром), файл НЕ меняется — чтобы из-за сбоя не стереть каталог.

Запуск:  python3 scripts/sync_drom.py            (меняет файлы)
         python3 scripts/sync_drom.py --dry-run  (только показывает изменения)
Только стандартная библиотека Python 3.9+.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARS = os.path.join(ROOT, "data", "cars.json")
IMG_DIR = os.path.join(ROOT, "img")
DEALER = "https://auto.drom.ru/goldenroad/"
MAX_PAGES = 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
VLAT = timezone(timedelta(hours=10))  # Владивосток

FUELS = {"бензин", "дизель", "гибрид", "электро", "газ", "гбо"}
BOXES = {"акпп", "автомат", "вариатор", "робот", "механика", "мкпп"}
DRIVES = {"передний", "задний", "4wd", "полный"}


def fetch(url, binary=False, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept": "text/html,application/xhtml+xml,image/*,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if binary:
                return data
            m = re.search(rb'charset=["\']?([\w-]+)', data[:3000])
            enc = m.group(1).decode() if m else "windows-1251"
            return data.decode(enc, "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def text(s):
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def ftid(block, name):
    """Текст первого элемента с data-ftid="name"."""
    m = re.search(r'data-ftid="%s"[^>]*>(.*?)</(?:div|span|a|h3)>' % re.escape(name), block, re.S)
    return text(m.group(1)) if m else ""


def parse_page(page_html):
    page_html = re.sub(r"<style[^>]*>.*?</style>", "", page_html, flags=re.S)
    parts = page_html.split('data-ftid="bulls-list_bull"')[1:]
    out = []
    for b in parts:
        m = re.search(r'href="(https://auto\.drom\.ru/[^"]+?/(\d+)\.html)', b)
        if not m:
            continue
        url, car_id = m.group(1), m.group(2)
        title = ftid(b, "bull_title")  # «Hyundai Elantra, 2023»
        tm = re.match(r"(.+?),\s*(\d{4})", title)
        if not tm:
            continue
        price_m = re.search(r'data-ftid="bull_price"[^>]*>(.*?)</span>', b, re.S)
        price = int(re.sub(r"\D", "", text(price_m.group(1)))) if price_m else 0
        if not price:
            continue
        items = [text(x).rstrip(",").strip() for x in
                 re.findall(r'data-ftid="bull_description-item"[^>]*>(.*?)</span>', b, re.S)]
        img = ""
        sm = re.search(r'srcSet="([^"]+)"', b)
        if sm:
            for part in sm.group(1).split(","):
                u = part.strip().split(" ")[0]
                if "gen544" in u or not img:
                    img = u
        out.append({
            "id": car_id, "url": url, "name": tm.group(1).strip(), "year": int(tm.group(2)),
            "trim": ftid(b, "bull_subtitle"), "items": items, "price": price,
            "abroad": 'data-ftid="bull_label_abroad"' in b,
            "noMileageRu": "bull_label-without-mileage-in-russia" in b,
            "location": ftid(b, "bull_location"), "img_src": img,
        })
    return out


def crawl():
    first = fetch(DEALER)
    tm = re.search(r"<title>[^<]*?(\d+)\s+авто", first)
    expected = int(tm.group(1)) if tm else None
    cars, seen, page_html, p = [], set(), first, 1
    while True:
        got = parse_page(page_html)
        new = [c for c in got if c["id"] not in seen]
        if not new:
            break
        for c in new:
            seen.add(c["id"])
            cars.append(c)
        if f"goldenroad/page{p + 1}/" not in page_html or p >= MAX_PAGES:
            break
        p += 1
        time.sleep(1.5)
        page_html = fetch(f"{DEALER}page{p}/")
    return cars, expected


def split_items(items):
    r = {"engine": "", "fuel": "", "box": "", "drive": "", "km": ""}
    for it in items:
        lo = it.lower()
        if "км" in lo and re.search(r"\d", lo):
            r["km"] = it
        elif lo in FUELS:
            r["fuel"] = it
        elif lo in BOXES:
            r["box"] = it
        elif lo in DRIVES:
            r["drive"] = it
        elif "л.с." in lo or re.search(r"\d\s*л\b", lo) or "квт" in lo:
            r["engine"] = it
        elif lo == "новый":
            r["km"] = "новый"
    if not r["km"]:
        r["km"] = "новый"
    return r


def main():
    dry = "--dry-run" in sys.argv
    with open(CARS, encoding="utf-8") as f:
        doc = json.load(f)
    old = {c["id"]: c for c in doc["cars"]}

    drom, expected = crawl()
    print(f"Дром: собрано {len(drom)} объявлений (дилер пишет: {expected}); в приложении было {len(old)}")
    # Защита от сбоев: не трогаем каталог, если выдача неполная.
    if not drom:
        sys.exit("ОШИБКА: витрина Дрома пустая (блокировка/капча?) — каталог не изменён")
    if expected and len(drom) < expected * 0.95:
        sys.exit(f"ОШИБКА: собрано {len(drom)} из {expected} — каталог не изменён")
    if len(drom) < len(old) * 0.5:
        sys.exit("ОШИБКА: на Дроме вдвое меньше машин, чем в приложении — похоже на сбой, каталог не изменён")

    # справочники из уже имеющихся машин: марка по slug и класс автовоза по модели
    make_by_slug, cls_by_model = {}, {}
    for c in old.values():
        slug = c["url"].split("/")[4]
        make_by_slug[slug] = c["make"]
        cls_by_model.setdefault((c["make"], c["model"]), Counter())[c.get("cls", 1)] += 1

    result, added, changed = [], [], []
    for d in drom:
        f = split_items(d["items"])
        slug = d["url"].split("/")[4]
        make = make_by_slug.get(slug) or d["name"].split(" ")[0]
        model = d["name"][len(make):].strip() if d["name"].startswith(make) else " ".join(d["name"].split(" ")[1:])
        prev = old.get(d["id"])
        car = dict(prev) if prev else {"id": d["id"]}
        upd = {
            "make": make, "model": model, "year": d["year"], "trim": d["trim"] or car.get("trim", ""),
            "engine": f["engine"] or car.get("engine", ""), "fuel": f["fuel"] or car.get("fuel", ""),
            "box": f["box"] or car.get("box", ""), "drive": f["drive"] or car.get("drive", ""),
            "km": f["km"], "price": d["price"], "abroad": d["abroad"], "noMileageRu": d["noMileageRu"],
            "location": d["location"] or car.get("location", ""), "url": d["url"],
        }
        if prev:
            diff = [k for k, v in upd.items() if prev.get(k) != v]
            if diff:
                note = f"{make} {model} {d['year']} ({d['id']}): " + ", ".join(
                    f"цена {prev.get('price'):,} → {d['price']:,}".replace(",", " ") if k == "price" else k for k in diff)
                changed.append(note)
        else:
            cnt = cls_by_model.get((make, model))
            upd["cls"] = cnt.most_common(1)[0][0] if cnt else 1
            upd["img"] = ""
            added.append(f"{make} {model} {d['year']} — {d['price']:,} ₽ ({d['id']})".replace(",", " "))
        car.update(upd)
        # фото: качаем, если его ещё нет
        img_path = os.path.join(IMG_DIR, f"{d['id']}.jpg")
        if not os.path.exists(img_path) and d["img_src"] and not dry:
            try:
                data = fetch(d["img_src"], binary=True)
                if len(data) > 2000:
                    with open(img_path, "wb") as fh:
                        fh.write(data)
            except Exception as e:  # noqa: BLE001
                print(f"  фото {d['id']} не скачалось: {e}")
        car["img"] = f"img/{d['id']}.jpg" if os.path.exists(img_path) else car.get("img", "")
        # стабильный порядок полей, как в исходном файле
        order = ["id", "make", "model", "year", "trim", "engine", "fuel", "box", "drive", "km", "price",
                 "abroad", "noMileageRu", "location", "url", "img", "cls"]
        result.append({k: car[k] for k in order if k in car} | {k: v for k, v in car.items() if k not in order})

    new_ids = {c["id"] for c in result}
    removed = [c for i, c in old.items() if i not in new_ids]

    print(f"\nИзменено: {len(changed)}")
    for s in changed:
        print("  ~", s)
    print(f"Новые: {len(added)}")
    for s in added:
        print("  +", s)
    print(f"Проданы / сняты с продажи (удаляются): {len(removed)}")
    for c in removed:
        print(f"  - {c['make']} {c['model']} {c['year']} ({c['id']})")

    if not (changed or added or removed or [c["id"] for c in result] != [c["id"] for c in doc["cars"]]):
        print("\nБез изменений.")
        return
    if dry:
        print("\n--dry-run: файлы не изменены")
        return
    for c in removed:
        p = os.path.join(IMG_DIR, f"{c['id']}.jpg")
        if os.path.exists(p):
            os.remove(p)
    doc["cars"] = result
    doc["updated"] = datetime.now(VLAT).strftime("%d.%m.%Y %H:%M")
    with open(CARS, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"\ncars.json обновлён: {len(result)} авто")


if __name__ == "__main__":
    main()
