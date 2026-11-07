#!/usr/bin/env python3
"""Синхронизация каталога GOLDEN ROAD с витриной дилера на Дроме.

Берёт все объявления со страниц https://auto.drom.ru/goldenroad/ и приводит
data/cars.json к ним:
  * цена, пробег, комплектация, «в наличии в Китае / во Владивостоке» — обновляются;
  * машины, которых больше нет на Дроме (продана / снята), — удаляются вместе с фото;
  * новые объявления — добавляются (фото скачивается в img/);
  * галерея: до 12 фото каждого объявления сохраняются в gal/<id>/1.jpg … (1000 px, без метаданных).
    За один запуск — не больше GAL_PER_RUN машин, остальные догружаются в следующие запуски.

Если Дром не отдал витрину целиком (ошибка, капча, меньше машин, чем пишет сам
Дром), файл НЕ меняется — чтобы из-за сбоя не стереть каталог.

Запуск:  python3 scripts/sync_drom.py            (меняет файлы)
         python3 scripts/sync_drom.py --dry-run  (только показывает изменения)
Python 3.9+. Для сжатия фото галереи нужен Pillow (pip install pillow); без него фото сохраняются как есть.
"""
import html
import json
import os
import io
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARS = os.path.join(ROOT, "data", "cars.json")
IMG_DIR = os.path.join(ROOT, "img")
GAL_DIR = os.path.join(ROOT, "gal")
MAX_PHOTOS = 12                                         # фото в галерее одной машины
GAL_PER_RUN = int(os.environ.get("GAL_PER_RUN", "40"))  # сколько машин догружать за один запуск
GAL_WIDTH, GAL_QUALITY = 1000, 72
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


def gallery_urls(ad_html):
    """Все большие фото объявления в порядке Дрома (первое — главное)."""
    keys, urls = set(), []
    for m in re.finditer(r'(https://s\d*\.auto\.drom\.ru/photo/v2/([\w-]+)/gen1200\.jpg)', ad_html):
        if m.group(2) not in keys:
            keys.add(m.group(2))
            urls.append(m.group(1))
    return urls


def spread(idx, k):
    """k номеров, равномерно разложенных по списку idx."""
    if k <= 0 or not idx:
        return []
    if len(idx) <= k:
        return list(idx)
    return [idx[round(i * (len(idx) - 1) / (k - 1))] for i in range(k)] if k > 1 else [idx[0]]


def photo_order(n):
    """Порядок, в котором пробуем фото, чтобы набрать MAX_PHOTOS лучших.
    Объявления продавца устроены одинаково: 1 — главное, 2 — промо с QR-кодом,
    3–8 — кузов с разных сторон, дальше — приборы, экран, камера, люк, салон, в конце — багажник.
    Берём главное + 3 ракурса кузова (спереди, сзади-сбоку, сзади), а остальное — равномерно
    из салона и функций, обязательно с последним фото (багажник). Запасные — все остальные по порядку."""
    if n <= MAX_PHOTOS + 1:
        first = list(range(n))
    else:
        ext = [i for i in (0, 3, 5, 6) if i < n]
        rest = list(range(8, n))
        inner = spread(rest[:-1], MAX_PHOTOS - len(ext) - 1) + rest[-1:]
        first = ext + inner
    return first + [i for i in range(n) if i not in first]


def is_promo(im):
    """Промо-картинка с QR-кодом — вертикальная, а фото машины — горизонтальные."""
    return im.height > im.width * 0.9


def shrink(data):
    """Уменьшает фото до GAL_WIDTH и убирает метаданные. Возвращает None для промо-картинки.
    Без Pillow — возвращает как есть."""
    try:
        from PIL import Image
    except ImportError:
        return data
    im = Image.open(io.BytesIO(data))
    if is_promo(im):
        return None
    im = im.convert("RGB")
    if im.width > GAL_WIDTH:
        im = im.resize((GAL_WIDTH, round(im.height * GAL_WIDTH / im.width)), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=GAL_QUALITY, optimize=True, progressive=True)
    return out.getvalue()


def load_gallery(car):
    """Скачивает до MAX_PHOTOS лучших фото машины в gal/<id>/ (кузов, салон, функции — в порядке
    объявления). Возвращает число фото (0 — не получилось)."""
    urls = gallery_urls(fetch(car["url"]))
    if not urls:
        return 0
    tmp = os.path.join(GAL_DIR, car["id"] + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        got = {}  # номер в объявлении → готовое фото
        for i in photo_order(len(urls)):
            if len(got) >= MAX_PHOTOS:
                break
            data = fetch(urls[i], binary=True)
            time.sleep(0.3)
            if len(data) < 2000:
                continue
            small = shrink(data)
            if small:
                got[i] = small
        if not got:
            return 0
        for n, i in enumerate(sorted(got), 1):  # в галерее — в порядке объявления
            with open(os.path.join(tmp, f"{n}.jpg"), "wb") as fh:
                fh.write(got[i])
        dst = os.path.join(GAL_DIR, car["id"])
        shutil.rmtree(dst, ignore_errors=True)
        os.rename(tmp, dst)
        return len(got)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        if car.get("photos") and not os.path.isdir(os.path.join(GAL_DIR, d["id"])):
            car.pop("photos")  # папку удалили руками — догрузим заново
        order = ["id", "make", "model", "year", "trim", "engine", "fuel", "box", "drive", "km", "price",
                 "abroad", "noMileageRu", "location", "url", "img", "photos", "cls"]
        result.append({k: car[k] for k in order if k in car} | {k: v for k, v in car.items() if k not in order})

    new_ids = {c["id"] for c in result}
    removed = [c for i, c in old.items() if i not in new_ids]

    # галерея: догружаем фото машинам, у которых её ещё нет (не больше GAL_PER_RUN за запуск)
    gal_added = []
    if not dry:
        os.makedirs(GAL_DIR, exist_ok=True)
        todo = [c for c in result if not c.get("photos")][:GAL_PER_RUN]
        for c in todo:
            try:
                n = load_gallery(c)
            except Exception as e:  # noqa: BLE001
                print(f"  галерея {c['id']} не скачалась: {e}")
                continue
            if n:
                c["photos"] = n
                gal_added.append(f"{c['make']} {c['model']} ({c['id']}): {n} фото")
            time.sleep(1.5)
        left = sum(1 for c in result if not c.get("photos"))
        print(f"\nГалерея: добавлено {len(gal_added)} машин, осталось без галереи {left}")

    print(f"\nИзменено: {len(changed)}")
    for s in changed:
        print("  ~", s)
    print(f"Новые: {len(added)}")
    for s in added:
        print("  +", s)
    print(f"Проданы / сняты с продажи (удаляются): {len(removed)}")
    for c in removed:
        print(f"  - {c['make']} {c['model']} {c['year']} ({c['id']})")

    if not (changed or added or removed or gal_added or [c["id"] for c in result] != [c["id"] for c in doc["cars"]]):
        print("\nБез изменений.")
        return
    if dry:
        print("\n--dry-run: файлы не изменены")
        return
    for c in removed:
        p = os.path.join(IMG_DIR, f"{c['id']}.jpg")
        if os.path.exists(p):
            os.remove(p)
        if re.match(r"^\d{4,12}$", str(c["id"])):
            shutil.rmtree(os.path.join(GAL_DIR, c["id"]), ignore_errors=True)
    doc["cars"] = result
    doc["updated"] = datetime.now(VLAT).strftime("%d.%m.%Y %H:%M")
    with open(CARS, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"\ncars.json обновлён: {len(result)} авто")


if __name__ == "__main__":
    main()
