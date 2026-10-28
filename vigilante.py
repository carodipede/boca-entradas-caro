#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vigilante de Abono Solidario de Boca Juniors.

Qué hace:
  - Descarga páginas PÚBLICAS que vos configurás en fuentes.json.
  - Busca textos y enlaces que mencionen Abono Solidario / venta de entradas.
  - Si aparece algo NUEVO (que no vio antes), te avisa por Telegram.

Qué NO hace (a propósito):
  - No inicia sesión en Boca Socios, no guarda contraseñas.
  - No compra, no resuelve CAPTCHA, no salta colas virtuales.
  - No puede saber si hay ubicaciones disponibles: eso solo se ve adentro de Boca Socios.

Uso:
  python vigilante.py --modo normal        (lo que corre cada 15 minutos)
  python vigilante.py --modo prueba        (manda un mensaje de prueba a Telegram)
  python vigilante.py --modo diagnostico   (te cuenta qué "ve" en cada página)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
RUTA_CONFIG = BASE / "fuentes.json"
RUTA_ESTADO = BASE / "estado.json"

USER_AGENT = "BocaAbonoVigilante/1.0 (aviso personal de una socia; una consulta cada 15 minutos)"
TIMEOUT = 20
MAX_BYTES = 3_000_000
MAX_VISTOS = 600
MAX_ITEMS_POR_ALERTA = 5
FALLAS_PARA_AVISAR = 3
PAUSA_ENTRE_FUENTES = 3

CLAVES_POR_DEFECTO = [
    "abono solidario",
    "venta de entradas",
    "entradas",
    "localidades",
    "reserva de ubicaciones",
]

AVISO_DISPONIBILIDAD = (
    "⚠️ Esto es un cambio en el texto de una página pública. NO verifiqué si hay "
    "ubicaciones disponibles: eso solo se ve dentro de Boca Socios (el vigilante "
    "no inicia sesión). Un anuncio de venta no significa que queden lugares. "
    "Entrá y comprá vos manualmente."
)

MESES = (
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    "septiembre|setiembre|octubre|noviembre|diciembre"
)
RE_FECHA_NUM = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
RE_FECHA_TXT = re.compile(r"\b\d{1,2} de (?:%s)\b" % MESES, re.IGNORECASE)
RE_HORA = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:hs|horas|h)\b", re.IGNORECASE)
RE_PARTIDO = re.compile(
    r"Boca(?: Juniors)?\s*(?:(?i:vs\.?|contra|ante)|-)\s*"
    r"([A-ZÁÉÍÓÚÑ][\wáéíóúñ.'-]*(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ.'-]*){0,2})"
)
RE_AGOTADO = re.compile(r"agotad|sold out|sin disponibilidad", re.IGNORECASE)


# ----------------------------------------------------------------- utilidades
def ahora_ar():
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3)))


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


def limpiar(texto):
    return re.sub(r"\s+", " ", texto).strip()


def huella(texto):
    return hashlib.sha1(texto.encode("utf-8")).hexdigest()[:16]


def cargar_json(ruta, por_defecto):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return por_defecto
    except json.JSONDecodeError as e:
        print(f"ERROR: {ruta.name} no es un JSON válido ({e}).")
        raise SystemExit(1)


def guardar_estado(estado):
    nuevo = json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    try:
        actual = RUTA_ESTADO.read_text(encoding="utf-8")
    except FileNotFoundError:
        actual = None
    if actual != nuevo:
        RUTA_ESTADO.write_text(nuevo, encoding="utf-8")


# ------------------------------------------------------------------- Telegram
def enviar_telegram(texto):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Faltan los secretos TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": texto[:4000]},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        # Solo el nombre del error: el mensaje completo podría contener el token.
        print(f"No se pudo contactar a Telegram ({type(e).__name__}).")
        return False
    if r.status_code != 200:
        print(f"Telegram respondió HTTP {r.status_code}: {r.text[:200]}")
        return False
    return True


# ----------------------------------------------------------- robots.txt y red
_cache_robots = {}


def permitido_por_robots(url):
    """True = permitido, False = prohibido, None = no se pudo verificar."""
    p = urlparse(url)
    origen = f"{p.scheme}://{p.netloc}"
    if origen not in _cache_robots:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(
                origen + "/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            _cache_robots[origen] = None
            return None
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
        elif r.status_code in (401, 403):
            rp.disallow_all = True
        elif 400 <= r.status_code < 500:
            rp.allow_all = True
        else:
            _cache_robots[origen] = None
            return None
        _cache_robots[origen] = rp
    rp = _cache_robots[origen]
    if rp is None:
        return None
    return rp.can_fetch(USER_AGENT, url)


def consultar(url, est, condicional=True):
    permitido = permitido_por_robots(url)
    if permitido is False:
        return {"tipo": "robots"}
    if permitido is None:
        return {"tipo": "error", "detalle": "no pude leer robots.txt"}

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-AR,es;q=0.9",
    }
    if condicional:
        if est.get("etag"):
            headers["If-None-Match"] = est["etag"]
        if est.get("last_modified"):
            headers["If-Modified-Since"] = est["last_modified"]

    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT, stream=True)
        with r:
            if r.status_code == 304:
                return {"tipo": "sin_cambios"}
            if r.status_code != 200:
                return {"tipo": "error", "detalle": f"HTTP {r.status_code}"}
            ctype = r.headers.get("Content-Type", "").lower()
            if not any(t in ctype for t in ("html", "xml", "text")):
                return {"tipo": "error", "detalle": f"tipo de contenido {ctype or 'desconocido'}"}
            datos = r.raw.read(MAX_BYTES + 1, decode_content=True)
            return {
                "tipo": "ok",
                "contenido": datos[:MAX_BYTES],
                "etag": r.headers.get("ETag"),
                "last_modified": r.headers.get("Last-Modified"),
                "url_final": r.url,
            }
    except Exception as e:
        return {"tipo": "error", "detalle": type(e).__name__}


# ----------------------------------------------------------------- extracción
def extraer_items(contenido, url_base, claves):
    """Devuelve ({huella: {texto, enlace}}, cantidad_de_caracteres_de_texto)."""
    soup = BeautifulSoup(contenido, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "nav", "footer"]):
        tag.decompose()

    claves_n = [normalizar(c) for c in claves]

    def relevante(t):
        n = normalizar(t)
        return any(c in n for c in claves_n)

    items = {}
    textos_de_enlaces = set()

    for a in soup.find_all("a", href=True):
        texto = limpiar(a.get_text(" "))
        if 8 <= len(texto) <= 300 and relevante(texto):
            enlace = urljoin(url_base, a["href"])
            if enlace.startswith("http"):
                items.setdefault(
                    huella(normalizar(texto) + "|" + enlace),
                    {"texto": texto, "enlace": enlace},
                )
                textos_de_enlaces.add(normalizar(texto))

    texto_pagina = soup.get_text("\n")
    for linea in texto_pagina.split("\n"):
        linea = limpiar(linea)
        if 20 <= len(linea) <= 500 and relevante(linea):
            if normalizar(linea) in textos_de_enlaces:
                continue
            items.setdefault(
                huella(normalizar(linea) + "|"),
                {"texto": linea, "enlace": url_base},
            )

    return items, len(limpiar(texto_pagina))


def detectar_partido(texto):
    m = RE_PARTIDO.search(texto)
    return f"Boca vs {m.group(1).strip()}" if m else None


def detectar_fechas(texto):
    hallazgos = []
    for rx in (RE_FECHA_TXT, RE_FECHA_NUM, RE_HORA):
        for m in rx.finditer(texto):
            if m.group(0) not in hallazgos:
                hallazgos.append(m.group(0))
    return hallazgos[:6]


# -------------------------------------------------------------------- mensajes
def armar_mensaje(nombre, items):
    lineas = [
        "🔔 BOCA — novedad detectada",
        f"Fuente: {nombre}",
        f"Detectado: {ahora_ar():%d/%m/%Y %H:%M} (hora Argentina)",
        "",
    ]
    for i, item in enumerate(items[:MAX_ITEMS_POR_ALERTA], 1):
        texto = item["texto"]
        etiqueta = (
            "🎯 Menciona Abono Solidario"
            if "abono solidario" in normalizar(texto)
            else "ℹ️ Información de venta / entradas"
        )
        partido = detectar_partido(texto) or "no identificado automáticamente"
        fechas = detectar_fechas(texto)
        lineas.append(f"{i}) {etiqueta}")
        lineas.append(f"Partido: {partido}")
        lineas.append("Fechas/horarios en el texto: " + (", ".join(fechas) if fechas else "no encontré"))
        if RE_AGOTADO.search(texto):
            lineas.append("Ojo: el texto menciona agotamiento / sin disponibilidad.")
        lineas.append(f"Texto: “{texto[:350]}”")
        lineas.append(f"Enlace: {item['enlace']}")
        lineas.append("")
    if len(items) > MAX_ITEMS_POR_ALERTA:
        lineas.append(f"… y {len(items) - MAX_ITEMS_POR_ALERTA} novedades más en la misma página.")
        lineas.append("")
    lineas.append(AVISO_DISPONIBILIDAD)
    return "\n".join(lineas)


def ordenar_items(pares):
    """Primero los que mencionan Abono Solidario."""
    return sorted(pares, key=lambda it: 0 if "abono solidario" in normalizar(it["texto"]) else 1)


# ------------------------------------------------------------------ lógica
def registrar_falla(est, nombre, url, detalle):
    est["fallas"] = est.get("fallas", 0) + 1
    print(f"[{nombre}] falla {est['fallas']}: {detalle}")
    if est["fallas"] >= FALLAS_PARA_AVISAR and not est.get("falla_avisada"):
        ok = enviar_telegram(
            f"⚠️ El vigilante no pudo leer “{nombre}” {est['fallas']} veces seguidas "
            f"({detalle}).\n{url}\nNo es que no haya novedades: simplemente no pude "
            "verificar esta página. Te aviso una sola vez; te escribo de nuevo cuando se recupere."
        )
        if ok:
            est["falla_avisada"] = True


def procesar_fuente(fuente, estado, claves):
    url = fuente["url"]
    nombre = fuente.get("nombre") or url
    est = estado["fuentes"].setdefault(url, {})

    res = consultar(url, est)

    if res["tipo"] == "sin_cambios":
        est["fallas"] = 0
        print(f"[{nombre}] sin cambios (HTTP 304).")
        return

    if res["tipo"] == "robots":
        print(f"[{nombre}] robots.txt no permite consultar esta URL.")
        if not est.get("robots_avisado"):
            if enviar_telegram(
                f"🚫 El robots.txt del sitio no permite consultar “{nombre}”.\n{url}\n"
                "No la voy a consultar. Probá con otra página pública del mismo sitio."
            ):
                est["robots_avisado"] = True
        return

    if res["tipo"] == "error":
        registrar_falla(est, nombre, url, res["detalle"])
        return

    # --- respuesta OK
    if est.get("falla_avisada"):
        enviar_telegram(f"✅ El vigilante volvió a poder leer “{nombre}”.")
    est["fallas"] = 0
    est["falla_avisada"] = False
    est["robots_avisado"] = False

    items, largo = extraer_items(res["contenido"], res.get("url_final", url), claves)
    vistos = est.setdefault("vistos", {})
    ahora = ahora_ar().isoformat(timespec="seconds")

    if not est.get("inicializada"):
        for k in items:
            vistos[k] = ahora
        est["inicializada"] = True
        aviso = (
            f"👀 Vigilante activo en “{nombre}”.\nGuardé {len(items)} elementos que ya estaban "
            "publicados (no te aviso por ellos; solo por lo nuevo)."
        )
        if len(items) == 0:
            aviso += (
                "\n⚠️ No encontré ningún texto con las palabras clave. Puede que la página se arme "
                "con JavaScript o que hoy no mencione el tema. Corré el modo diagnóstico."
            )
        enviar_telegram(aviso)
        print(f"[{nombre}] línea base guardada: {len(items)} elementos.")
    else:
        nuevos = [(k, it) for k, it in items.items() if k not in vistos]
        print(f"[{nombre}] {len(items)} elementos, {len(nuevos)} nuevos.")
        if nuevos:
            lista = ordenar_items([it for _, it in nuevos])
            if enviar_telegram(armar_mensaje(nombre, lista)):
                for k, _ in nuevos:
                    vistos[k] = ahora
            # Si Telegram falló, NO se marcan como vistos: se reintenta en la próxima corrida.

    if len(vistos) > MAX_VISTOS:
        est["vistos"] = dict(list(vistos.items())[-MAX_VISTOS:])
    if res.get("etag"):
        est["etag"] = res["etag"]
    else:
        est.pop("etag", None)
    if res.get("last_modified"):
        est["last_modified"] = res["last_modified"]
    else:
        est.pop("last_modified", None)


def diagnosticar(fuentes, claves):
    partes = ["🔧 Diagnóstico del vigilante\n"]
    for f in fuentes:
        url, nombre = f["url"], f.get("nombre") or f["url"]
        partes.append(f"• {nombre}\n{url}")
        res = consultar(url, {}, condicional=False)
        if res["tipo"] == "robots":
            partes.append("  robots.txt: NO permite consultarla.\n")
        elif res["tipo"] == "error":
            partes.append(f"  Error: {res['detalle']}\n")
        else:
            items, largo = extraer_items(res["contenido"], res.get("url_final", url), claves)
            partes.append("  robots.txt: permitido. HTTP 200.")
            partes.append(f"  Texto visible: {largo} caracteres. Elementos con palabras clave: {len(items)}.")
            if largo < 500:
                partes.append("  ⚠️ Casi no hay texto: probablemente la página se arma con JavaScript y este vigilante no la puede leer.")
            elif not items:
                partes.append("  ⚠️ Hay texto pero ninguna palabra clave. Revisá la URL o fuentes.json.")
            for it in list(items.values())[:5]:
                partes.append(f"  - {it['texto'][:120]}")
            partes.append("")
        time.sleep(PAUSA_ENTRE_FUENTES)
    texto = "\n".join(partes)
    print(texto)
    enviar_telegram(texto)


def fuentes_validas(cfg):
    validas = []
    for f in cfg.get("fuentes", []):
        url = str(f.get("url", "")).strip()
        if url.startswith(("http://", "https://")) and "PEGAR" not in url.upper():
            validas.append({"nombre": f.get("nombre", url), "url": url})
    return validas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["normal", "prueba", "diagnostico"], default="normal")
    args = ap.parse_args()

    if args.modo == "prueba":
        ok = enviar_telegram(
            "✅ Prueba del vigilante de Abono Solidario: Telegram está bien configurado.\n"
            f"{ahora_ar():%d/%m/%Y %H:%M} (hora Argentina)"
        )
        print("Mensaje de prueba enviado." if ok else "No se pudo enviar el mensaje de prueba.")
        return 0 if ok else 1

    cfg = cargar_json(RUTA_CONFIG, {})
    claves = cfg.get("palabras_clave") or CLAVES_POR_DEFECTO
    fuentes = fuentes_validas(cfg)
    estado = cargar_json(RUTA_ESTADO, {})
    estado.setdefault("fuentes", {})
    estado.setdefault("avisos", {})

    if not fuentes:
        print("No hay URLs configuradas en fuentes.json.")
        if args.modo == "diagnostico":
            enviar_telegram("🔧 Diagnóstico: fuentes.json no tiene ninguna URL cargada todavía.")
        elif not estado["avisos"].get("sin_fuentes"):
            if enviar_telegram(
                "ℹ️ El vigilante está instalado pero fuentes.json todavía no tiene URLs. "
                "Pegá la(s) URL(s) oficiales para que empiece a vigilar."
            ):
                estado["avisos"]["sin_fuentes"] = True
        guardar_estado(estado)
        return 0
    estado["avisos"]["sin_fuentes"] = False

    if args.modo == "diagnostico":
        diagnosticar(fuentes, claves)
        return 0

    for i, f in enumerate(fuentes):
        if i:
            time.sleep(PAUSA_ENTRE_FUENTES)
        procesar_fuente(f, estado, claves)

    guardar_estado(estado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
