#!/usr/bin/env python3
"""
RADAR INMOBILIARIO — pipeline diario
Inventario exhaustivo deduplicado de casas en venta (Portal Inmobiliario)
→ novedades diarias, cambios de precio y favoritas del usuario
→ reporte HTML en docs/index.html (GitHub Pages).

Corre automáticamente vía GitHub Actions (ver .github/workflows/daily.yml).
"""

import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path

import imagehash
import requests
from bs4 import BeautifulSoup
from PIL import Image

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "db.json"
REPORT_PATH = ROOT / "docs" / "index.html"
CONFIG_PATH = ROOT / "config.json"

MELI_ITEM_DESC = "https://api.mercadolibre.com/items/{}/description"
PI_BASE = "https://www.portalinmobiliario.com"
PI_SEARCHES = [("casa", "/venta/casa/"), ("depto", "/venta/departamento/")]
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------- utilidades


def load_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return fallback


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def tokenize(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    # los números (calle, m², UF) distinguen títulos genéricos de corredoras
    return {w for w in re.split(r"[^a-z0-9ñ]+", s) if len(w) > 2 or w.isdigit()}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def parse_num(v):
    if v is None:
        return None
    m = re.search(r"[\d.,]+", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def days_since(iso):
    try:
        return max(0, (date.today() - date.fromisoformat(iso)).days)
    except Exception:
        return 0


# ---------------------------------------------------------------- ingesta


def get_uf(cfg):
    try:
        r = requests.get("https://mindicador.cl/api/uf", timeout=20)
        v = r.json()["serie"][0]["valor"]
        if v > 10000:
            print(f"UF del día: {v:,.0f}")
            return v
    except Exception as e:
        print(f"No pude obtener la UF ({e}); uso valor manual de config.json")
    return cfg.get("uf_manual", 39500)


# --------------------------------------------- scraping Portal Inmobiliario


def _read_balanced_json(text, start):
    """Devuelve el objeto JSON {...} que empieza en text[start], balanceando
    llaves y respetando strings."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _embedded_json_blobs(page):
    """JSON embebido en <script>: estado precargado primero, luego JSON-LD."""
    blobs = []
    for m in re.finditer(
        r"(?:__PRELOADED_STATE__|__NEXT_DATA__|__INITIAL_STATE__)\s*=\s*\{", page
    ):
        raw = _read_balanced_json(page, m.end() - 1)
        if raw:
            try:
                blobs.append(json.loads(raw))
            except ValueError:
                pass
    for m in re.finditer(
        r'<script[^>]+type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
        page,
        re.S,
    ):
        try:
            blobs.append(json.loads(m.group(1).strip()))
        except ValueError:
            pass
    return blobs


def _looks_like_listing(d):
    has_title = isinstance(d.get("title"), str) or isinstance(d.get("name"), str)
    has_price = (
        isinstance(d.get("price"), (int, float, dict))
        or isinstance(d.get("prices"), dict)
        or isinstance(d.get("offers"), dict)
    )
    has_link = any(
        isinstance(d.get(k), str) and "/" in d[k] for k in ("permalink", "url")
    ) or isinstance(d.get("id"), str)
    return has_title and has_price and has_link


def _walk_listings(node, found, depth=0):
    if depth > 25:
        return
    if isinstance(node, dict):
        if _looks_like_listing(node):
            found.append(node)
        else:
            for v in node.values():
                _walk_listings(v, found, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_listings(v, found, depth + 1)


def _texts_of(node, out, depth=0):
    """Todos los strings dentro de un nodo JSON (para buscar m², dorms, etc.)."""
    if depth > 8 or len(out) > 400:
        return
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _texts_of(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _texts_of(v, out, depth + 1)


RX_M2 = re.compile(r"(\d[\d.,]*)\s*m²(?:\s*totales)?", re.I)
RX_M2_TOT = re.compile(r"(\d[\d.,]*)\s*m²\s*totales", re.I)
RX_DORMS = re.compile(r"(\d+)\s*dormitorio", re.I)
RX_BATHS = re.compile(r"(\d+)\s*baño", re.I)
RX_MLC = re.compile(r"(MLC-?\d+)")


def _attr_lookup(d, ids):
    for a in d.get("attributes") or []:
        if isinstance(a, dict) and a.get("id") in ids:
            return a.get("value_name") or a.get("value") or a.get("value_id")
    return None


def _norm_currency(c):
    c = (c or "").strip().upper()
    if c in ("CLF", "UF"):
        return "UF"
    if c in ("CLP", "$", "CLP$", "PESO", "PESOS"):
        return "CLP"
    return None


def _location_info(d, texts):
    """(comuna, sector, texto de ubicación). El sector es el barrio, si viene
    estructurado (location.neighborhood) o como parte del texto de ubicación."""
    comuna = sector = loc_text = None
    loc = d.get("location")
    if isinstance(loc, dict):
        city = loc.get("city")
        if isinstance(city, dict) and city.get("name"):
            comuna = city["name"]
        nb = loc.get("neighborhood")
        if isinstance(nb, dict) and nb.get("name"):
            sector = nb["name"]
    addr = d.get("address")
    if isinstance(addr, dict) and not comuna:
        comuna = addr.get("addressLocality")
    # texto tipo "Camino X 123, Los Trapenses, Lo Barnechea, Metropolitana"
    for t in texts:
        if "," in t and not RX_M2.search(t) and len(t) < 120:
            parts = [p.strip() for p in t.split(",") if p.strip()]
            if len(parts) >= 2:
                loc_text = t
                if not comuna:
                    comuna = parts[-2]
                if not sector and len(parts) >= 3:
                    sector = parts[-3]
                break
    return comuna, sector, loc_text


def _listing_from_json(d, ptype):
    title = d.get("title") or d.get("name") or ""
    if isinstance(title, dict):
        title = title.get("text") or ""

    price = cur = None
    pr = d.get("price")
    if isinstance(pr, (int, float)):
        price, cur = pr, d.get("currency_id") or d.get("currency")
    elif isinstance(pr, dict):
        price = parse_num(pr.get("amount") if pr.get("amount") is not None else pr.get("value"))
        cur = pr.get("currency_id") or pr.get("currency") or pr.get("currency_symbol")
    if price is None and isinstance(d.get("prices"), dict):
        for p in d["prices"].get("prices") or []:
            if isinstance(p, dict) and p.get("amount") is not None:
                price, cur = parse_num(p["amount"]), p.get("currency_id")
                break
    if price is None and isinstance(d.get("offers"), dict):
        price = parse_num(d["offers"].get("price"))
        cur = d["offers"].get("priceCurrency")

    url = d.get("permalink") or d.get("url") or ""
    lid = d.get("id") if isinstance(d.get("id"), str) else None
    if not lid and url:
        m = RX_MLC.search(url)
        lid = m.group(1).replace("-", "") if m else None

    photos = []

    def _add_photo(u):
        if isinstance(u, dict):
            u = u.get("url") or u.get("src") or u.get("contentUrl")
        if isinstance(u, str) and u.startswith("http") and u not in photos:
            photos.append(u)

    # galería primero (imágenes grandes), thumbnail después
    pics = d.get("pictures")
    if isinstance(pics, list):
        for pc in pics:
            _add_photo(pc)
    img = d.get("image")
    if isinstance(img, list):
        for u in img:
            _add_photo(u)
    else:
        _add_photo(img)
    _add_photo(d.get("thumbnail"))
    photos = photos[:4]
    thumb = d.get("thumbnail")
    if isinstance(thumb, dict):
        thumb = thumb.get("url") or thumb.get("contentUrl")
    if not isinstance(thumb, str) or not thumb:
        thumb = photos[0] if photos else ""

    texts = []
    _texts_of(d, texts)
    joined = " | ".join(texts)

    m2 = parse_num(_attr_lookup(d, ("TOTAL_AREA", "COVERED_AREA")))
    if not m2:
        m = RX_M2_TOT.search(joined) or RX_M2.search(joined)
        m2 = parse_num(m.group(1)) if m else None
    dorms = parse_num(_attr_lookup(d, ("BEDROOMS",)))
    if dorms is None:
        m = RX_DORMS.search(joined)
        dorms = float(m.group(1)) if m else None
    baths = parse_num(_attr_lookup(d, ("FULL_BATHROOMS", "BATHROOMS")))
    if baths is None:
        m = RX_BATHS.search(joined)
        baths = float(m.group(1)) if m else None

    if not (lid or url) or not title or price is None:
        return None
    comuna, sector, loc_text = _location_info(d, texts)
    return {
        "lid": lid or url,
        "title": str(title),
        "price": price,
        "currency": _norm_currency(cur),
        "m2": m2,
        "dorms": dorms,
        "baths": baths,
        "comuna": comuna,
        "sector": sector,
        "loc": loc_text,
        "url": url or None,
        "thumb": thumb or "",
        "photos": photos,
        "seller": (d.get("seller") or {}).get("id")
        if isinstance(d.get("seller"), dict)
        else None,
        "ptype": ptype,
        "op": "venta",
    }


def _listings_from_html(page, ptype):
    """Fallback: parseo por clases CSS del buscador (ui-search / poly-card)."""
    soup = BeautifulSoup(page, "html.parser")
    cards = soup.select("div.poly-card") or soup.select(
        "li.ui-search-layout__item, div.ui-search-result__wrapper"
    )
    out = []
    for card in cards:
        a = card.select_one(
            "a.poly-component__title, h2.ui-search-item__title a, "
            "a.ui-search-link, a.ui-search-result__content, h3 a, a[href]"
        )
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        url = a.get("href") or ""
        frac = card.select_one(".andes-money-amount__fraction")
        sym = card.select_one(".andes-money-amount__currency-symbol")
        price = parse_num(frac.get_text(strip=True)) if frac else None
        cur = _norm_currency(sym.get_text(strip=True) if sym else None)
        text = card.get_text(" | ", strip=True)
        m = RX_M2_TOT.search(text) or RX_M2.search(text)
        m2 = parse_num(m.group(1)) if m else None
        md = RX_DORMS.search(text)
        mb = RX_BATHS.search(text)
        loc = card.select_one(
            ".poly-component__location, .ui-search-item__location"
        )
        comuna = sector = loc_text = None
        if loc:
            loc_text = loc.get_text(strip=True)
            parts = [p.strip() for p in loc_text.split(",") if p.strip()]
            if parts:
                comuna = parts[-2] if len(parts) >= 2 else parts[-1]
                if len(parts) >= 3:
                    sector = parts[-3]
        img = card.select_one("img")
        thumb = (img.get("data-src") or img.get("src") or "") if img else ""
        photos = [thumb] if thumb.startswith("http") else []
        mid = RX_MLC.search(url)
        out.append(
            {
                "lid": mid.group(1).replace("-", "") if mid else (url or title),
                "title": title,
                "price": price,
                "currency": cur,
                "m2": m2,
                "dorms": float(md.group(1)) if md else None,
                "baths": float(mb.group(1)) if mb else None,
                "comuna": comuna,
                "sector": sector,
                "loc": loc_text,
                "url": url or None,
                "thumb": thumb,
                "photos": photos,
                "seller": None,
                "ptype": ptype,
                "op": "venta",
            }
        )
    return out


def parse_search_page(page, ptype):
    """Extrae avisos: primero JSON embebido (más estable), luego CSS."""
    found = []
    for blob in _embedded_json_blobs(page):
        _walk_listings(blob, found)
    items, seen = [], set()
    for d in found:
        it = _listing_from_json(d, ptype)
        if it and it["lid"] not in seen:
            seen.add(it["lid"])
            items.append(it)
    strategy = "json"
    if not items:
        items = _listings_from_html(page, ptype)
        strategy = "css"
    return items, strategy


CHALLENGE_MARKERS = ("captcha", "cf-challenge", "challenge-form", "px-captcha",
                     "validarte", "are you a human")


def _fold(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", _fold(s)).strip("-")


def _ptype_from_path(path):
    if "departamento" in path or "depto" in path:
        return "depto"
    if "casa" in path:
        return "casa"
    return "otro"


def _path_variants(path):
    """La ruta configurada y variantes menos específicas del slug final,
    quitando palabras del inicio (los-trapenses-lo-barnechea-metropolitana →
    lo-barnechea-metropolitana), por si el slug del sector no existe como
    listado propio y hay que filtrar después."""
    variants = [path]
    base, _, slug = path.rstrip("/").rpartition("/")
    parts = slug.split("-")
    for i in range(1, min(len(parts) - 1, 4)):
        variants.append(f"{base}/{'-'.join(parts[i:])}")
    return variants


def _sector_match(it, sector_folded):
    hay = _fold(" ".join(
        str(it.get(k) or "") for k in ("sector", "comuna", "loc", "title")))
    return sector_folded in hay


def _get_search_html(session, url):
    """(status, html). status 0 = error de red, -1 = challenge/captcha."""
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"ADVERTENCIA: error de red en {url} ({e})")
        return 0, ""
    low = r.text[:20000].lower()
    if any(k in low for k in CHALLENGE_MARKERS):
        print(f"ADVERTENCIA: challenge/captcha detectado en {url}")
        return -1, ""
    return r.status_code, r.text


MAX_EXHAUSTIVE_PAGES = 80  # tope de seguridad del modo exhaustivo


def fetch_pages(cfg):
    """Scrapea resultados públicos de Portal Inmobiliario. Con
    cfg["search_paths"] usa esas rutas en vez de las genéricas, probando
    variantes menos específicas si la configurada no entrega avisos. Con
    cfg["exhaustivo"] pagina hasta agotar el listado (página vacía o solo
    avisos repetidos); si no, respeta cfg["paginas"]."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-CL,es;q=0.9",
        }
    )
    custom = [p if p.startswith("/") else "/" + p
              for p in (cfg.get("search_paths") or [])]
    searches = [(_ptype_from_path(p), p) for p in custom] or PI_SEARCHES
    exhaustive = bool(cfg.get("exhaustivo"))
    per_search = max(1, max(1, cfg.get("paginas", 10)) // len(searches))
    sector = _fold((cfg.get("sector_filtro") or "").strip())
    sector_slug = _slugify(cfg.get("sector_filtro") or "")

    items, seen = [], set()
    for ptype, path in searches:
        # descarga de prueba: encontrar una ruta que entregue avisos
        working = first = None
        for cand in (_path_variants(path) if custom else [path]):
            status, page = _get_search_html(session, PI_BASE + cand)
            if status in (0, -1):
                print("Corto el scraping con lo acumulado")
                return items
            batch, strategy = parse_search_page(page, ptype) if status == 200 else ([], "-")
            if batch:
                working, first = cand, (batch, strategy)
                print(f"Ruta OK: {cand} ({len(batch)} avisos en la primera página)")
                break
            print(f"Ruta {cand}: HTTP {status} / 0 avisos; pruebo variante")
            time.sleep(2.5)
        if not working:
            print(f"ADVERTENCIA: ninguna variante de {path} entregó avisos; "
                  "salto este listado")
            continue
        # sanidad: el sitio puede aceptar un slug desconocido y devolver
        # resultados genéricos en vez de 404
        zona = _fold(cfg.get("zona_label") or "")
        if custom and zona:
            hits = sum(
                1 for b in first[0]
                if zona in _fold(" ".join(
                    str(b.get(k) or "") for k in ("comuna", "sector", "loc", "title")))
            )
            if hits < len(first[0]) / 2:
                print(f"ADVERTENCIA: solo {hits}/{len(first[0])} avisos de la "
                      f"primera página mencionan '{cfg['zona_label']}'; la ruta "
                      "podría estar devolviendo resultados genéricos")
        # si la ruta ya es específica del sector, no hay que filtrar después
        specific = bool(sector_slug) and sector_slug in working

        offset = page_n = 0
        while True:
            if page_n == 0:
                batch, strategy = first
            else:
                time.sleep(2.5)  # ritmo respetuoso con el sitio
                url = PI_BASE + working + f"_Desde_{offset + 1}"
                status, page = _get_search_html(session, url)
                if status in (0, -1):
                    print("Corto el scraping con lo acumulado")
                    return items
                if status != 200:
                    print(f"ADVERTENCIA: HTTP {status} en {url}; "
                          "corto con lo acumulado")
                    return items
                batch, strategy = parse_search_page(page, ptype)
            if not batch:
                print(f"[{ptype}] página {page_n + 1} sin avisos; fin del listado")
                break
            fresh = [b for b in batch if b["lid"] not in seen]
            if exhaustive and not fresh:
                print(f"[{ptype}] página {page_n + 1} solo repite avisos ya "
                      "vistos; fin del listado")
                break
            seen.update(b["lid"] for b in fresh)
            kept = fresh
            note = ""
            if sector and not specific:
                kept = [b for b in fresh if _sector_match(b, sector)]
                note = f", {len(kept)} tras filtro de sector"
            items.extend(kept)
            print(f"[{ptype}] página {page_n + 1} vía {strategy}: "
                  f"{len(batch)} avisos{note} (acumulado {len(items)})")
            offset += len(batch)
            page_n += 1
            if exhaustive:
                if page_n >= MAX_EXHAUSTIVE_PAGES:
                    print("ADVERTENCIA: alcancé el tope de seguridad de "
                          f"{MAX_EXHAUSTIVE_PAGES} páginas; corto este listado")
                    break
            elif page_n >= per_search:
                break
    return items


def parse_item(item, uf_value):
    """Valida un aviso scrapeado y lo deja en el formato interno (precio UF)."""
    try:
        comuna = item.get("comuna")
        m2 = item.get("m2")
        if not comuna or not m2 or m2 < 10 or not item.get("price"):
            return None

        cur = item.get("currency")
        if cur == "UF":
            price_uf = item["price"]
        elif cur == "CLP":
            price_uf = item["price"] / uf_value
        else:
            return None
        op = item.get("op", "venta")
        if op == "venta" and price_uf < 100:
            return None

        return {
            "lid": str(item["lid"]),
            "title": (item.get("title") or "")[:90],
            "comuna": comuna,
            "sector": item.get("sector"),
            "ptype": item.get("ptype", "otro"),
            "op": op,
            "m2": m2,
            "dorms": item.get("dorms"),
            "baths": item.get("baths"),
            "priceUF": round(price_uf),
            "url": item.get("url"),
            "thumb": (item.get("thumb") or "").replace("http://", "https://"),
            "seller": item.get("seller"),
        }
    except Exception:
        return None


# ------------------------------------------------------- hashes de fotos


PHASH_MAX_DIST = 6   # distancia de Hamming máxima para considerar igual
PHASH_MAX_KEEP = 16  # tope de hashes acumulados por propiedad


def hash_new_photos(items, hashed):
    """Descarga y hashea (pHash) las fotos de los avisos sin entrada en el
    cache {lid: [hash]}. Solo procesa lo scrapeado en esta corrida (scope
    vigente); un lid ya hasheado nunca se vuelve a descargar."""
    todo, seen_here = [], set()
    for it in items:
        lid = str(it.get("lid"))
        if lid not in hashed and lid not in seen_here:
            seen_here.add(lid)
            todo.append(it)
    if not todo:
        return
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})
    n_imgs = n_fail = 0
    for it in todo:
        hashes = []
        for u in (it.get("photos") or [])[:4]:
            try:
                r = session.get(u, timeout=10)
                if r.ok:
                    hashes.append(str(imagehash.phash(Image.open(BytesIO(r.content)))))
                    n_imgs += 1
                else:
                    n_fail += 1
            except Exception:
                n_fail += 1
            time.sleep(0.2)
        hashed[str(it["lid"])] = hashes
    print(f"Fotos: {len(todo)} avisos nuevos, {n_imgs} imágenes hasheadas"
          + (f", {n_fail} fallidas" if n_fail else ""))


def _phash_dist(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def photos_match(h1, h2):
    """True/False si hay señal de fotos en ambos lados, None si falta en
    alguno. Coinciden con ≥2 pares a distancia ≤6 (1 par basta si alguno
    tiene una sola foto)."""
    if not h1 or not h2:
        return None
    close = sum(1 for a in h1 for b in h2 if _phash_dist(a, b) <= PHASH_MAX_DIST)
    need = 1 if min(len(h1), len(h2)) == 1 else 2
    return close >= need


def merge_phashes(p, new_hashes):
    if not new_hashes:
        return
    cur = p.get("phashes") or []
    p["phashes"] = (cur + [h for h in new_hashes if h not in cur])[:PHASH_MAX_KEEP]


# ---------------------------------------------------------------- dedup


def migrate_db(props):
    """Migración: ids: [lid] + url único → listings: {lid: url}, sin perder
    historial. Asigna la url conocida al lid que aparezca en ella."""
    migrated = 0
    for p in props.values():
        if "listings" in p:
            continue
        ids = p.pop("ids", [p.get("lid")])
        url = p.get("url")
        url_lid = None
        if url:
            m = RX_MLC.search(url)
            if m:
                url_lid = m.group(1).replace("-", "")
        p["listings"] = {}
        for i, lid in enumerate(ids):
            if lid == url_lid or (url_lid not in ids and i == 0):
                p["listings"][lid] = url
            else:
                p["listings"][lid] = None
        migrated += 1
    if migrated:
        print(f"Base migrada a listings por aviso: {migrated} propiedades")


def find_match(cand, props, counters=None):
    """Busca la propiedad a la que pertenece el aviso. Tras el gate de
    comuna+tipo+operación+m²±5%+dormitorios, la señal principal es la
    coincidencia de fotos por pHash; si ambos lados tienen fotos y NO
    coinciden, no se fusiona por ninguna señal débil (vendedor/texto):
    las corredoras tienen muchas casas parecidas."""
    cn = counters if counters is not None else {}
    cand_tok = tokenize(cand["title"])
    for pid, p in props.items():
        if cand["lid"] in p["listings"]:
            return pid
        if (
            p["comuna"] != cand["comuna"]
            or p["ptype"] != cand["ptype"]
            or p["op"] != cand["op"]
        ):
            continue
        if abs(p["m2"] - cand["m2"]) / p["m2"] > 0.05:
            continue
        if (p.get("dorms") or 0) != (cand.get("dorms") or 0):
            continue
        pm = photos_match(cand.get("phashes"), p.get("phashes"))
        if pm:
            cn["foto"] = cn.get("foto", 0) + 1
            return pid
        if p.get("thumb") and p["thumb"] == cand.get("thumb"):
            cn["url_foto"] = cn.get("url_foto", 0) + 1
            return pid
        if pm is False:
            # ambos con fotos y distintas: son casas diferentes
            continue
        sim = jaccard(cand_tok, tokenize(p["title"]))
        pdiff = abs(p["priceUF"] - cand["priceUF"]) / max(p["priceUF"], 1)
        if (
            p.get("seller")
            and p["seller"] == cand.get("seller")
            and sim >= 0.7
            and pdiff <= 0.10
        ):
            cn["vendedor"] = cn.get("vendedor", 0) + 1
            return pid
        if sim >= 0.85 and pdiff <= 0.10:
            cn["texto"] = cn.get("texto", 0) + 1
            return pid
    return None


def split_merged(props):
    """Reparación única: separa cada propiedad con múltiples avisos en una
    propiedad por aviso (conservando firstSeen y el historial de precios
    actual, que no es separable por aviso). La próxima ingesta re-fusiona
    con las reglas nuevas, ya con fotos."""
    split = 0
    for pid in list(props):
        p = props[pid]
        lids = list(p["listings"])
        if len(lids) <= 1:
            continue
        del props[pid]
        split += 1
        for lid in lids:
            npid = "p" + str(lid)
            if npid in props:
                continue
            q = json.loads(json.dumps(p))  # copia profunda
            q["lid"] = lid
            q["listings"] = {lid: p["listings"][lid]}
            q["url"] = p["listings"][lid] or p.get("url")
            q["repubs"] = 0
            q["phashes"] = []  # se rellena desde el cache por lid
            props[npid] = q
    if split:
        print(f"Reparación: {split} propiedades con avisos múltiples "
              "separadas en una por aviso")
    return split


def _absorb(p, q):
    """Funde q dentro de p (p. ej. el resto de una separación cuyo aviso
    re-fusionó con otra propiedad): conserva el firstSeen más antiguo,
    y une listings y hashes."""
    for lid, u in q["listings"].items():
        if u or lid not in p["listings"]:
            p["listings"][lid] = u or p["listings"].get(lid)
    if q.get("firstSeen", "9999") < p.get("firstSeen", "9999"):
        p["firstSeen"] = q["firstSeen"]
    merge_phashes(p, q.get("phashes") or [])


def refill_phashes(props, hashed):
    """Vuelca el cache {lid: [hash]} a las propiedades que no tienen hashes."""
    for p in props.values():
        if not p.get("phashes"):
            for lid in p["listings"]:
                merge_phashes(p, hashed.get(str(lid), []))


def ingest(items, props, uf_value, cfg, hashed=None):
    today = date.today().isoformat()
    added = merged = changes = 0
    hashed = hashed or {}
    counters = {}
    # marca de scope: el reporte solo muestra propiedades vistas por la
    # configuración vigente; lo demás queda en la base como historial
    scope = cfg.get("zona_label") or ""
    comunas_filter = {c.strip().lower() for c in cfg.get("comunas", []) if c.strip()}
    for raw in items:
        c = parse_item(raw, uf_value)
        if not c:
            continue
        if comunas_filter and c["comuna"].lower() not in comunas_filter:
            continue
        c["phashes"] = hashed.get(c["lid"], [])
        pid = find_match(c, props, counters)
        if pid:
            p = props[pid]
            if c["lid"] not in p["listings"]:
                p["listings"][c["lid"]] = c["url"]
                merged += 1
                # si otra propiedad ya tenía este aviso (resto de una
                # separación), absorberla para no dejar duplicados
                for opid in [
                    k for k, q in props.items()
                    if k != pid and c["lid"] in q["listings"]
                ]:
                    _absorb(p, props.pop(opid))
                p["repubs"] = len(p["listings"]) - 1
            elif c["url"]:
                p["listings"][c["lid"]] = c["url"]
            last = p["priceHist"][-1]
            if abs(last["uf"] - c["priceUF"]) / last["uf"] > 0.01:
                p["priceHist"].append({"d": today, "uf": c["priceUF"]})
                changes += 1
            if c.get("sector") and not p.get("sector"):
                p["sector"] = c["sector"]
            merge_phashes(p, c["phashes"])
            p.update(priceUF=c["priceUF"], lastSeen=today, scope=scope,
                     url=c["url"] or p["url"])
        else:
            props["p" + c["lid"]] = {
                **c,
                "listings": {c["lid"]: c["url"]},
                "scope": scope,
                "firstSeen": today,
                "lastSeen": today,
                "priceHist": [{"d": today, "uf": c["priceUF"]}],
                "repubs": 0,
                "rationale": None,
            }
            added += 1
    # recorte por tamaño
    max_props = cfg.get("max_props", 8000)
    if len(props) > max_props:
        for pid in sorted(props, key=lambda k: props[k]["lastSeen"])[: len(props) - max_props]:
            del props[pid]
    print(f"Ingesta: +{added} propiedades, {merged} republicaciones, {changes} cambios de precio")
    if merged:
        print("Fusiones por señal: "
              f"{counters.get('foto', 0)} fotos (pHash), "
              f"{counters.get('url_foto', 0)} url de foto, "
              f"{counters.get('vendedor', 0)} vendedor+texto, "
              f"{counters.get('texto', 0)} solo texto")
    return added, merged, changes


# ---------------------------------------------------------------- scoring


def score_all(props, cfg):
    min_comp = cfg.get("min_comparables", 6)
    zone, glob = {}, {}
    for p in props.values():
        v = p["priceUF"] / p["m2"]
        if v <= 0:
            continue
        zone.setdefault((p["comuna"], p["ptype"], p["op"]), []).append(v)
        glob.setdefault((p["ptype"], p["op"]), []).append(v)

    out = []
    for pid, p in props.items():
        if p["op"] != cfg.get("operacion", "venta"):
            continue
        zs = zone.get((p["comuna"], p["ptype"], p["op"]), [])
        if len(zs) >= min_comp:
            med, n, scope = statistics.median(zs), len(zs), p["comuna"]
        else:
            gs = glob.get((p["ptype"], p["op"]), [])
            if len(gs) < min_comp:
                continue
            med, n, scope = statistics.median(gs), len(gs), "todas las zonas"
        ufm2 = p["priceUF"] / p["m2"]
        drops = sum(
            1
            for i in range(1, len(p["priceHist"]))
            if p["priceHist"][i]["uf"] < p["priceHist"][i - 1]["uf"]
        )
        out.append(
            {
                "pid": pid, "p": p,
                "score": (med - ufm2) / med,
                "ufm2": ufm2, "med": med, "med_n": n, "med_scope": scope,
                "drops": drops, "days": days_since(p["firstSeen"]),
            }
        )
    out.sort(key=lambda e: -e["score"])
    return out


def zone_criteria(props, cfg):
    """Estadísticas del conjunto ingerido para 'Criterios de la zona'.
    Las medianas se calculan sobre lo ingerido, que con sector_filtro o
    search_paths específicas ya es solo el sector."""
    op = cfg.get("operacion", "venta")
    sel = [p for p in props.values() if p["op"] == op and p.get("m2")]
    if len(sel) < 2:
        return None
    ufm2 = sorted(p["priceUF"] / p["m2"] for p in sel)
    q = statistics.quantiles(ufm2, n=4)
    name = (cfg.get("sector_filtro")
            or ", ".join(cfg.get("comunas") or [])
            or "todas las zonas")
    return {
        "sector": name,
        "n": len(sel),
        "med_ufm2": statistics.median(ufm2),
        "p25": q[0],
        "p75": q[2],
        "med_m2": statistics.median(p["m2"] for p in sel),
        "med_uf": statistics.median(p["priceUF"] for p in sel),
        "all_casas": all(p["ptype"] == "casa" for p in sel),
    }


# ---------------------------------------------------------------- racionales


def fetch_description(lid):
    try:
        r = requests.get(MELI_ITEM_DESC.format(lid), timeout=20)
        if r.ok:
            return (r.json().get("plain_text") or "")[:1500]
    except Exception:
        pass
    return ""


def claude_rationale(entry, api_key):
    p = entry["p"]
    desc = fetch_description(next(iter(p["listings"]), None))
    prompt = f"""Eres un analista inmobiliario chileno experto en detectar oportunidades y riesgos.
Responde SOLO con un objeto JSON válido, sin markdown, con esta forma:
{{"racional": "2-3 frases explicando por qué está barata y qué la hace atractiva", "riesgos": ["riesgo 1", "riesgo 2"]}}
En "riesgos" incluye señales de la descripción (sin recepción final, ocupada, sucesión, litigio, derechos de llave, remate) o riesgos propios de un precio tan bajo. Si no hay señales, indica el riesgo genérico de verificar en terreno y títulos.

Datos:
- {p['title']} ({p['ptype']}, {p['op']}) en {p['comuna']}
- Precio: UF {p['priceUF']:,} | {p['m2']:.0f} m² | {p.get('dorms') or '?'} dorm, {p.get('baths') or '?'} baños
- UF/m²: {entry['ufm2']:.1f} vs mediana de {entry['med_scope']}: {entry['med']:.1f} → {entry['score']:.0%} bajo la referencia
- Días en mercado (real, tras deduplicar): {entry['days']} | Bajas de precio: {entry['drops']} | Republicaciones: {p['repubs']}
- Descripción: {desc or '(no disponible)'}"""
    r = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text")
    return json.loads(re.sub(r"```json|```", "", text).strip())


def template_rationale(entry):
    p = entry["p"]
    parts = [
        f"{entry['score']:.0%} bajo la mediana de {p['ptype']}s en {entry['med_scope']} "
        f"({entry['ufm2']:.1f} vs {entry['med']:.1f} UF/m², n={entry['med_n']})."
    ]
    if entry["drops"]:
        parts.append(f"Ha bajado de precio {entry['drops']} vez/veces: vendedor posiblemente motivado.")
    if entry["days"] > 120:
        parts.append(f"Lleva {entry['days']} días en mercado, lo que da espacio para negociar.")
    return {
        "racional": " ".join(parts),
        "riesgos": ["Verificar en terreno, títulos y recepción final antes de decidir."],
    }


def add_rationales(entries, props, cfg):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    top_k = cfg.get("racionales_top", 15)
    for entry in entries[:top_k]:
        p = entry["p"]
        # regenerar solo si no existe o si el precio cambió desde el último racional
        if p.get("rationale") and p["rationale"].get("_uf") == p["priceUF"]:
            continue
        try:
            rat = claude_rationale(entry, api_key) if api_key else template_rationale(entry)
        except Exception as e:
            print(f"Racional falló para {p['ids'][0]}: {e}")
            rat = template_rationale(entry)
        rat["_uf"] = p["priceUF"]
        props[entry["pid"]]["rationale"] = rat
        if api_key:
            time.sleep(0.5)
    if not api_key:
        print("Nota: sin ANTHROPIC_API_KEY los racionales son plantilla básica. "
              "Agrega el secret para racionales con lectura de descripción y red flags.")


# ---------------------------------------------------------------- reporte

INACTIVE_DAYS = 7  # sin aparecer en la ingesta → inactiva (vendida/retirada)

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;600&family=Public+Sans:wght@400;600&display=swap');
:root{--paper:#F4F7F4;--ink:#16262B;--muted:#5C6B66;--line:#D9E1DA;--green:#1E7A52;--deep:#0C4A33;--gsoft:#E4F1E9;--amber:#A66300;--asoft:#F6EDDC;--red:#A63D2F;--rsoft:#F5E6E2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:'Public Sans',sans-serif}
header{background:var(--deep);color:#fff;padding:20px 16px}
.wrap{max-width:680px;margin:0 auto;padding:0 16px}
h1{font-family:'Archivo',sans-serif;font-weight:800;font-size:22px;letter-spacing:-.02em;margin:0}
.sub{font-family:'IBM Plex Mono',monospace;font-size:12px;opacity:.75;margin-top:4px;line-height:1.6}
h2.sec{font-family:'Archivo',sans-serif;font-weight:700;font-size:17px;margin:28px 0 4px}
h3.grp{font-family:'Archivo',sans-serif;font-weight:700;font-size:14px;color:var(--deep);margin:20px 0 2px}
.card{position:relative;background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;gap:12px}.row img{width:72px;height:72px;object-fit:cover;border-radius:8px;background:var(--paper);flex-shrink:0}
.price{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:16px;color:var(--deep)}
.title{font-family:'Archivo',sans-serif;font-weight:600;font-size:14px;line-height:1.3;margin-top:2px;padding-right:44px}
.meta{font-size:12px;color:var(--muted);margin-top:3px;font-family:'IBM Plex Mono',monospace}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;margin:6px 6px 0 0}
.b-g{background:var(--gsoft);color:var(--deep)}.b-a{background:var(--asoft);color:var(--amber)}.b-r{background:var(--rsoft);color:var(--red)}
.pchg{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;margin-top:8px}
.pchg.down{color:var(--green)}.pchg.up{color:var(--red)}
a.btn{display:inline-block;margin:10px 8px 0 0;padding:10px 14px;min-height:40px;min-width:40px;border:1px solid var(--deep);border-radius:8px;color:var(--deep);font-weight:600;font-size:13px;text-decoration:none}
button.btn{margin:10px 8px 0 0;padding:10px 14px;min-height:40px;min-width:40px;border:1px solid var(--deep);border-radius:8px;background:#fff;color:var(--deep);font-weight:600;font-size:13px;cursor:pointer;font-family:'Public Sans',sans-serif}
.fav-btn{position:absolute;top:8px;right:8px;width:44px;height:44px;border:none;background:none;font-size:24px;line-height:1;color:var(--amber);cursor:pointer;padding:0}
.note{background:var(--asoft);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px;margin:12px 0;line-height:1.4}
.empty{font-size:13px;color:var(--muted);margin:8px 0 4px}
footer{font-size:11px;color:var(--muted);text-align:center;padding:24px 16px;line-height:1.5}
"""


def prop_public(pid, p, active):
    """Datos por propiedad para el JSON embebido (favoritas en JS)."""
    return {
        "t": p["title"],
        "s": p.get("sector") or p.get("comuna") or "",
        "uf": p["priceUF"],
        "m2": round(p["m2"]),
        "ufm2": round(p["priceUF"] / p["m2"], 1),
        "d": p.get("dorms"),
        "b": p.get("baths"),
        "days": days_since(p["firstSeen"]),
        "links": [u for u in p["listings"].values() if u],
        "rep": p.get("repubs", 0),
        "act": active,
        "img": (p.get("thumb") or ""),
    }


def card_html(pid, p, extra_html="", extra_badges=""):
    esc = html.escape
    links = [u for u in p["listings"].values() if u]
    if len(links) > 1:
        btns = " ".join(
            f'<a class="btn" href="{esc(u)}" target="_blank" rel="noreferrer">Aviso {i + 1}</a>'
            for i, u in enumerate(links)
        )
    else:
        one = links[0] if links else (p.get("url") or "#")
        btns = f'<a class="btn" href="{esc(one)}" target="_blank" rel="noreferrer">Ver aviso</a>'
    badges = extra_badges
    if p.get("repubs"):
        badges += f'<span class="badge b-a">republicada ×{p["repubs"]}</span>'
    img = f'<img src="{esc(p["thumb"])}" alt="" loading="lazy">' if p.get("thumb") else ""
    sector = p.get("sector") or p.get("comuna") or "—"
    db_txt = ""
    if p.get("dorms"):
        db_txt += f' · {p["dorms"]:.0f}D'
    if p.get("baths"):
        db_txt += f'/{p["baths"]:.0f}B'
    days = days_since(p["firstSeen"])
    return f"""
<div class="card prop">
 <button class="fav-btn" data-pid="{esc(pid)}" aria-label="marcar favorita">☆</button>
 <div class="row">{img}
  <div style="min-width:0;flex:1">
   <span class="price">UF {p["priceUF"]:,}</span>
   <div class="title">{esc(p["title"])}</div>
   <div class="meta">{esc(sector)} · {p["m2"]:.0f} m² · {p["priceUF"] / p["m2"]:.1f} UF/m²{db_txt} · {days} día{"s" if days != 1 else ""}</div>
  </div>
 </div>{extra_html}
 <div>{badges}</div>
 <div class="links">{btns}</div>
</div>"""


FAVS_JS = """
(function(){
var KEY='radar_favs';
var DATA=JSON.parse(document.getElementById('inv-data').textContent);
function getFavs(){try{var v=JSON.parse(localStorage.getItem(KEY));return Array.isArray(v)?v:[]}catch(e){return[]}}
function setFavs(f){localStorage.setItem(KEY,JSON.stringify(f))}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function cardHTML(pid,p){
 var links=p.links.length>1
   ?p.links.map(function(u,i){return '<a class="btn" href="'+esc(u)+'" target="_blank" rel="noreferrer">Aviso '+(i+1)+'</a>'}).join(' ')
   :'<a class="btn" href="'+esc(p.links[0]||'#')+'" target="_blank" rel="noreferrer">Ver aviso</a>';
 var badges='';
 if(!p.act)badges+='<span class="badge b-r">ya no publicada</span>';
 if(p.rep)badges+='<span class="badge b-a">republicada ×'+p.rep+'</span>';
 var db=p.d?(' · '+p.d+'D'+(p.b?'/'+p.b+'B':'')):'';
 var img=p.img?'<img src="'+esc(p.img)+'" alt="" loading="lazy">':'';
 var price=(p.act?'UF ':'último precio UF ')+p.uf.toLocaleString('es-CL');
 return '<div class="card prop"><button class="fav-btn" data-pid="'+esc(pid)+'">★</button>'+
  '<div class="row">'+img+'<div style="min-width:0;flex:1">'+
  '<span class="price">'+price+'</span>'+
  '<div class="title">'+esc(p.t)+'</div>'+
  '<div class="meta">'+esc(p.s)+' · '+p.m2+' m² · '+p.ufm2+' UF/m²'+db+' · '+p.days+' días</div>'+
  '</div></div><div>'+badges+'</div><div class="links">'+links+'</div></div>';
}
function renderFavs(){
 var favs=getFavs(),box=document.getElementById('favs-list');
 var known=favs.filter(function(pid){return DATA[pid]});
 if(!known.length){box.innerHTML='<div class="empty">Toca ☆ en una tarjeta para guardarla aquí. Se recuerdan en este navegador.</div>';}
 else{box.innerHTML=known.map(function(pid){return cardHTML(pid,DATA[pid])}).join('');}
 document.getElementById('fav-export').style.display=known.length?'':'none';
 syncStars();
}
function syncStars(){
 var favs=getFavs();
 document.querySelectorAll('.fav-btn').forEach(function(b){
  b.textContent=favs.indexOf(b.dataset.pid)>=0?'★':'☆';
 });
}
function toggleFav(pid){
 var favs=getFavs(),i=favs.indexOf(pid);
 if(i>=0)favs.splice(i,1);else favs.push(pid);
 setFavs(favs);renderFavs();
}
function exportFavs(btn){
 var favs=getFavs().filter(function(pid){return DATA[pid]});
 var text=favs.map(function(pid){var p=DATA[pid];
  return p.t+' — '+p.s+' — UF '+p.uf.toLocaleString('es-CL')+' — '+p.m2+' m²'+(p.act?'':' — ya no publicada')+'\\n'+p.links.join('\\n');
 }).join('\\n\\n');
 function done(){btn.textContent='Copiado ✓';setTimeout(function(){btn.textContent='Exportar'},1500)}
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,function(){fallback()})}
 else fallback();
 function fallback(){var ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');done()}catch(e){}document.body.removeChild(ta)}
}
document.addEventListener('click',function(e){
 var b=e.target.closest('.fav-btn');
 if(b){toggleFav(b.dataset.pid);return}
 var x=e.target.closest('#fav-export');
 if(x)exportFavs(x);
});
renderFavs();
})();
"""


def render_report(props, cfg):
    esc = html.escape
    today = date.today().isoformat()
    zona = cfg.get("zona_label", "la comuna")
    scope = cfg.get("zona_label") or ""
    active = {
        pid: p
        for pid, p in props.items()
        if p.get("ptype") == "casa"
        and p.get("scope", "") == scope
        and days_since(p["lastSeen"]) <= INACTIVE_DAYS
    }

    # --- encabezado
    ufm2s = sorted(p["priceUF"] / p["m2"] for p in active.values()) or [0]
    prices = sorted(p["priceUF"] for p in active.values()) or [0]
    med_ufm2 = statistics.median(ufm2s)
    med_uf = statistics.median(prices)
    now = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")

    # --- nuevas hoy (tras dedup: firstSeen de hoy, no republicaciones)
    new_today = {pid: p for pid, p in active.items() if p["firstSeen"] == today}
    first_day = bool(active) and len(new_today) == len(active)
    note = (
        '<div class="note">Primer día del inventario: toda la base es nueva, '
        "por eso todas las propiedades aparecen como “Nuevas hoy”. Desde "
        "mañana esta sección solo mostrará ingresos reales.</div>"
        if first_day
        else ""
    )
    if first_day:
        new_cards = ""
    else:
        new_cards = "".join(
            card_html(pid, p, extra_badges='<span class="badge b-g">nueva hoy</span>')
            for pid, p in sorted(new_today.items(), key=lambda kv: kv[1]["priceUF"])
        ) or '<div class="empty">Sin propiedades nuevas hoy.</div>'

    # --- cambios de precio de hoy
    chg_cards = []
    for pid, p in active.items():
        hist = p["priceHist"]
        if len(hist) >= 2 and hist[-1]["d"] == today:
            prev, new = hist[-2]["uf"], hist[-1]["uf"]
            pct = (new - prev) / prev * 100
            cls, sign = ("down", "−") if new < prev else ("up", "+")
            chg = (
                f'<div class="pchg {cls}">UF {prev:,} → UF {new:,} '
                f"({sign}{abs(pct):.1f}%)</div>"
            )
            chg_cards.append((pct, card_html(pid, p, extra_html=chg)))
    chg_cards.sort(key=lambda t: t[0])
    chg_html = "".join(c for _, c in chg_cards) or '<div class="empty">Sin cambios de precio hoy.</div>'

    # --- inventario completo: por sector y luego precio
    inv_parts = []
    by_sector = {}
    for pid, p in active.items():
        by_sector.setdefault(p.get("sector") or "Otros sectores", []).append((pid, p))
    for sector in sorted(by_sector, key=lambda s: (s == "Otros sectores", s)):
        group = sorted(by_sector[sector], key=lambda kv: kv[1]["priceUF"])
        inv_parts.append(f'<h3 class="grp">{esc(sector)} ({len(group)})</h3>')
        inv_parts.extend(card_html(pid, p) for pid, p in group)
    inv_html = "".join(inv_parts) or '<div class="empty">Inventario vacío.</div>'

    # --- JSON embebido (activas + inactivas del scope, para favoritas)
    inv_data = {
        pid: prop_public(pid, p, pid in active)
        for pid, p in props.items()
        if p.get("ptype") == "casa" and p.get("scope", "") == scope
    }
    inv_json = json.dumps(inv_data, ensure_ascii=False).replace("</", "<\\/")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Radar {esc(zona)} — {today}</title>
<style>{CSS}</style></head><body>
<header><div class="wrap">
 <h1>RADAR {esc(zona.upper())}</h1>
 <div class="sub">{today} · {len(active):,} casas activas · mediana {med_ufm2:.1f} UF/m² · precio mediano UF {med_uf:,.0f} · actualizado {now}</div>
</div></header>
<div class="wrap">
{note}
<h2 class="sec">Mis seleccionadas</h2>
<div id="favs-list"></div>
<button id="fav-export" class="btn" style="display:none">Exportar</button>
<h2 class="sec">Nuevas hoy ({len(new_today)})</h2>
{new_cards}
<h2 class="sec">Cambios de precio ({len(chg_cards)})</h2>
{chg_html}
<h2 class="sec">Inventario completo ({len(active)})</h2>
{inv_html}
</div>
<footer>Inventario deduplicado de avisos públicos: los precios son de lista, no de venta. Propiedades sin aparecer por {INACTIVE_DAYS} días se retiran del inventario (vendidas o despublicadas). Verifica siempre en terreno y títulos antes de decidir.</footer>
<script type="application/json" id="inv-data">{inv_json}</script>
<script>{FAVS_JS}</script>
</body></html>""", encoding="utf-8")
    inactive_n = len(inv_data) - len(active)
    print(f"Reporte generado: {REPORT_PATH} ({len(active)} activas, "
          f"{len(new_today)} nuevas hoy, {len(chg_cards)} cambios de precio, "
          f"{inactive_n} inactivas conservadas)")


# ---------------------------------------------------------------- main


def main():
    cfg = load_json(CONFIG_PATH, {})
    db = load_json(DB_PATH, {"props": {}})
    props = db["props"]
    hashed = db.get("hashed", {})  # cache pHash por listing id
    migrate_db(props)
    split_merged(props)

    uf = get_uf(cfg)
    items = fetch_pages(cfg)
    hash_new_photos(items, hashed)
    refill_phashes(props, hashed)
    ingest(items, props, uf, cfg, hashed)

    render_report(props, cfg)
    save_json(DB_PATH, {
        "props": props,
        "hashed": hashed,
        "updated": datetime.now(timezone.utc).isoformat(),
    })
    print("Pipeline OK")


if __name__ == "__main__":
    main()
