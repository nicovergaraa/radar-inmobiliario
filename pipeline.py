#!/usr/bin/env python3
"""
RADAR INMOBILIARIO — pipeline diario
Ingesta (API MercadoLibre) → deduplicación → scoring vs mediana UF/m²
→ racionales con Claude → reporte HTML en docs/index.html (GitHub Pages).

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
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "db.json"
SHOWN_PATH = ROOT / "data" / "shown.json"
REPORT_PATH = ROOT / "docs" / "index.html"
CONFIG_PATH = ROOT / "config.json"

MELI_SEARCH = "https://api.mercadolibre.com/sites/MLC/search"
MELI_ITEM_DESC = "https://api.mercadolibre.com/items/{}/description"
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


def get_meli_token():
    tok = os.environ.get("ML_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    cid = os.environ.get("ML_CLIENT_ID", "").strip()
    sec = os.environ.get("ML_CLIENT_SECRET", "").strip()
    if cid and sec:
        r = requests.post("https://api.mercadolibre.com/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": cid, "client_secret": sec}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]
    return ""


def fetch_pages(cfg):
    """Descarga páginas de resultados. Usa token si está definido."""
    headers = {"User-Agent": "radar-inmobiliario/1.0"}
    token = get_meli_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    items, pages = [], cfg.get("paginas", 10)
    for page in range(pages):
        params = {"category": "MLC1459", "limit": 50, "offset": page * 50}
        r = requests.get(MELI_SEARCH, params=params, headers=headers, timeout=30)
        if r.status_code in (401, 403):
            sys.exit(
                "ERROR: la API de MercadoLibre requiere autenticación "
                f"(HTTP {r.status_code}). Crea una app en "
                "https://developers.mercadolibre.cl y agrega como secrets de "
                "GitHub (Settings → Secrets and variables → Actions) un "
                "ML_ACCESS_TOKEN, o bien ML_CLIENT_ID y ML_CLIENT_SECRET "
                "para que el pipeline pida el token solo."
            )
        r.raise_for_status()
        batch = r.json().get("results", [])
        items.extend(batch)
        print(f"Página {page + 1}/{pages}: {len(batch)} avisos")
        if len(batch) < 50:
            break
        time.sleep(0.8)  # ritmo amable con la API
    return items


def parse_item(item, uf_value):
    try:
        attrs = {a["id"]: a.get("value_name") for a in item.get("attributes", [])}
        domain = item.get("domain_id") or ""
        ptype = "otro"
        if "HOUSE" in domain:
            ptype = "casa"
        elif "APARTMENT" in domain:
            ptype = "depto"
        elif "LAND" in domain or "LOT" in domain:
            ptype = "terreno"
        op = "arriendo" if "RENT" in domain else "venta"
        if attrs.get("OPERATION"):
            op = "venta" if "venta" in attrs["OPERATION"].lower() else op

        comuna = (item.get("location") or {}).get("city", {}).get("name")
        m2 = parse_num(attrs.get("TOTAL_AREA")) or parse_num(attrs.get("COVERED_AREA"))
        if not comuna or not m2 or m2 < 10 or not item.get("price"):
            return None

        cur = item.get("currency_id")
        if cur == "CLF":
            price_uf = item["price"]
        elif cur == "CLP":
            price_uf = item["price"] / uf_value
        else:
            return None
        if op == "venta" and price_uf < 100:
            return None

        return {
            "lid": item["id"],
            "title": (item.get("title") or "")[:90],
            "comuna": comuna,
            "ptype": ptype,
            "op": op,
            "m2": m2,
            "dorms": parse_num(attrs.get("BEDROOMS")),
            "baths": parse_num(attrs.get("FULL_BATHROOMS")),
            "priceUF": round(price_uf),
            "url": item.get("permalink"),
            "thumb": (item.get("thumbnail") or "").replace("http://", "https://"),
            "seller": (item.get("seller") or {}).get("id"),
        }
    except Exception:
        return None


# ---------------------------------------------------------------- dedup


def find_match(cand, props):
    cand_tok = tokenize(cand["title"])
    for pid, p in props.items():
        if cand["lid"] in p["ids"]:
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
        same_photo = p.get("thumb") and p["thumb"] == cand.get("thumb")
        same_seller = p.get("seller") and p["seller"] == cand.get("seller")
        sim = jaccard(cand_tok, tokenize(p["title"]))
        pdiff = abs(p["priceUF"] - cand["priceUF"]) / max(p["priceUF"], 1)
        # Señales fuertes: misma foto de portada o mismo vendedor (ya se exigió
        # misma comuna, m² ±5% y dormitorios). La similitud de texto exige
        # umbral alto Y precio cercano: es preferible perder alguna
        # republicación cruzada entre corredoras a fusionar propiedades
        # distintas y contaminar el historial de precios.
        if same_photo or same_seller or (sim >= 0.85 and pdiff <= 0.10):
            return pid
    return None


def ingest(items, props, uf_value, cfg):
    today = date.today().isoformat()
    added = merged = changes = 0
    comunas_filter = {c.strip().lower() for c in cfg.get("comunas", []) if c.strip()}
    for raw in items:
        c = parse_item(raw, uf_value)
        if not c:
            continue
        if comunas_filter and c["comuna"].lower() not in comunas_filter:
            continue
        pid = find_match(c, props)
        if pid:
            p = props[pid]
            if c["lid"] not in p["ids"]:
                p["ids"].append(c["lid"])
                p["repubs"] = p.get("repubs", 0) + 1
                merged += 1
            last = p["priceHist"][-1]
            if abs(last["uf"] - c["priceUF"]) / last["uf"] > 0.01:
                p["priceHist"].append({"d": today, "uf": c["priceUF"]})
                changes += 1
            p.update(priceUF=c["priceUF"], lastSeen=today, url=c["url"] or p["url"])
        else:
            props["p" + c["lid"]] = {
                **c,
                "ids": [c["lid"]],
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
    desc = fetch_description(p["ids"][0])
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


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;600&family=Public+Sans:wght@400;600&display=swap');
:root{--paper:#F4F7F4;--ink:#16262B;--muted:#5C6B66;--line:#D9E1DA;--green:#1E7A52;--deep:#0C4A33;--gsoft:#E4F1E9;--amber:#A66300;--asoft:#F6EDDC;--red:#A63D2F;--rsoft:#F5E6E2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:'Public Sans',sans-serif}
header{background:var(--deep);color:#fff;padding:20px 16px}
.wrap{max-width:680px;margin:0 auto;padding:0 16px}
h1{font-family:'Archivo',sans-serif;font-weight:800;font-size:22px;letter-spacing:-.02em;margin:0}
.sub{font-family:'IBM Plex Mono',monospace;font-size:12px;opacity:.75;margin-top:4px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;gap:12px}.row img{width:72px;height:72px;object-fit:cover;border-radius:8px;background:var(--paper);flex-shrink:0}
.score{font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:20px;color:var(--deep)}
.price{font-family:'IBM Plex Mono',monospace;font-size:14px;float:right}
.title{font-family:'Archivo',sans-serif;font-weight:600;font-size:14px;line-height:1.3;margin-top:2px}
.meta{font-size:12px;color:var(--muted);margin-top:3px}
.band{position:relative;height:8px;border-radius:4px;margin-top:12px;background:linear-gradient(90deg,var(--deep),var(--gsoft) 50%,var(--rsoft))}
.band .mid{position:absolute;left:50%;top:-3px;width:2px;height:14px;background:var(--ink);opacity:.5}
.band .dot{position:absolute;top:-4px;width:16px;height:16px;margin-left:-8px;border-radius:50%;background:var(--green);border:3px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.35)}
.bandlbl{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;font-family:'IBM Plex Mono',monospace}
.comp{font-size:12px;color:var(--muted);margin-top:8px;font-family:'IBM Plex Mono',monospace}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px;margin:6px 6px 0 0}
.b-g{background:var(--gsoft);color:var(--deep)}.b-a{background:var(--asoft);color:var(--amber)}
.rat{background:var(--paper);border-radius:8px;padding:10px;margin-top:10px;font-size:13px;line-height:1.45}
.risk{color:var(--red);font-size:12px;margin-top:4px}
a.btn{display:inline-block;margin-top:10px;padding:8px 14px;border:1px solid var(--deep);border-radius:8px;color:var(--deep);font-weight:600;font-size:13px;text-decoration:none}
footer{font-size:11px;color:var(--muted);text-align:center;padding:24px 16px;line-height:1.5}
"""


def render_report(entries, stats, cfg, shown):
    today_new = sum(1 for e in entries if e["pid"] not in shown or e["p"]["priceUF"] < shown[e["pid"]])
    now = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    cards = []
    for e in entries:
        p, esc = e["p"], html.escape
        pos = 50 - max(-0.4, min(0.4, e["score"])) / 0.4 * 50
        is_new = e["pid"] not in shown or p["priceUF"] < shown[e["pid"]]
        badges = ""
        if is_new:
            badges += '<span class="badge b-g">nueva en el radar</span>'
        if e["drops"]:
            badges += f'<span class="badge b-g">{e["drops"]} baja(s) de precio</span>'
        if p["repubs"]:
            badges += f'<span class="badge b-a">republicada ×{p["repubs"]}</span>'
        if e["days"] > 120:
            badges += f'<span class="badge b-a">{e["days"]} días en mercado</span>'
        rat_html = ""
        if p.get("rationale"):
            risks = "".join(f'<div class="risk">⚠ {esc(r)}</div>' for r in p["rationale"].get("riesgos", []))
            rat_html = f'<div class="rat">{esc(p["rationale"]["racional"])}{risks}</div>'
        img = f'<img src="{esc(p["thumb"])}" alt="">' if p.get("thumb") else ""
        cards.append(f"""
<div class="card">
 <div class="row">{img}
  <div style="min-width:0;flex:1">
   <span class="price">UF {p['priceUF']:,}</span><span class="score">−{e['score']:.0%}</span>
   <div class="title">{esc(p['title'])}</div>
   <div class="meta">{esc(p['comuna'])} · {p['ptype']} · {p['m2']:.0f} m²{f" · {p['dorms']:.0f}D" if p.get('dorms') else ""}</div>
  </div>
 </div>
 <div class="band"><div class="mid"></div><div class="dot" style="left:{pos:.0f}%"></div></div>
 <div class="bandlbl"><span>−40% (barata)</span><span>mediana zona</span><span>+40%</span></div>
 <div class="comp">{e['ufm2']:.1f} UF/m² vs {e['med']:.1f} mediana ({esc(e['med_scope'])}, n={e['med_n']})</div>
 <div>{badges}</div>{rat_html}
 <a class="btn" href="{esc(p['url'] or '#')}" target="_blank" rel="noreferrer">Ver aviso</a>
</div>""")
    body = "\n".join(cards) or '<div class="card">Sin oportunidades sobre el umbral hoy. La base sigue acumulando historial: mañana habrá más comparables.</div>'
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Radar Inmobiliario — {date.today().isoformat()}</title>
<style>{CSS}</style></head><body>
<header><div class="wrap">
 <h1>RADAR INMOBILIARIO</h1>
 <div class="sub">{date.today().isoformat()} · {stats['n_props']:,} propiedades · {stats['listings']:,} avisos · {today_new} nuevas hoy · actualizado {now}</div>
</div></header>
<div class="wrap">{body}</div>
<footer>Score contra precios de lista, no de venta. Verifica siempre en terreno, títulos y recepción final antes de decidir.</footer>
</body></html>""", encoding="utf-8")
    print(f"Reporte generado: {REPORT_PATH} ({len(entries)} oportunidades, {today_new} nuevas)")


# ---------------------------------------------------------------- main


def main():
    cfg = load_json(CONFIG_PATH, {})
    db = load_json(DB_PATH, {"props": {}})
    shown = load_json(SHOWN_PATH, {})
    props = db["props"]

    uf = get_uf(cfg)
    items = fetch_pages(cfg)
    ingest(items, props, uf, cfg)

    entries = score_all(props, cfg)
    top = [e for e in entries if e["score"] >= cfg.get("min_score", 0.15)][: cfg.get("top_n", 50)]
    add_rationales(top, props, cfg)
    top = [e for e in score_all(props, cfg) if e["score"] >= cfg.get("min_score", 0.15)][: cfg.get("top_n", 50)]

    stats = {"n_props": len(props), "listings": sum(len(p["ids"]) for p in props.values())}
    render_report(top, stats, cfg, shown)

    for e in top:  # registrar como mostradas (al precio actual)
        shown[e["pid"]] = min(shown.get(e["pid"], 10**9), e["p"]["priceUF"])
    save_json(DB_PATH, {"props": props, "updated": datetime.now(timezone.utc).isoformat()})
    save_json(SHOWN_PATH, shown)
    print("Pipeline OK")


if __name__ == "__main__":
    main()
