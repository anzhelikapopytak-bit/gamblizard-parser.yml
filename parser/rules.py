from __future__ import annotations

import html as html_lib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Detection, SiteConfig, UrlItem


BAD_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf",
    ".zip", ".css", ".js", ".woff", ".woff2", ".ttf", ".ico",
)

BAD_PARTS_COMMON = (
    "/tag/", "/author/", "/feed", "/wp-json/", "#",
)

ES_GENERIC_BRAND_SLUGS = {
    "", "list", "lista", "casinos", "casino", "mejores", "top",
    "nuevo", "nuevos", "pagos", "sets", "tragaperras", "juegos",
    "bonos", "bono", "resenas", "resena", "online",
}

FR_GENERIC_BRAND_SLUGS = {
    "", "list", "liste", "casinos", "casino", "meilleurs", "top",
    "nouveau", "nouveaux", "paiements", "jeux", "bonus", "avis",
    "machines-a-sous", "methodes-de-paiement", "depot-minimum",
}


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def clean_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def path_of(url: str) -> str:
    try:
        return unquote(urlparse(url).path or "/")
    except Exception:
        return urlparse(url).path or "/"


def tail_slug(url: str) -> str:
    parts = [p for p in path_of(url).split("/") if p]
    return parts[-1] if parts else ""


def title_case(value: str) -> str:
    small = {"de", "del", "la", "las", "los", "y", "et", "des", "du", "le", "les", "en"}
    words = []
    for index, word in enumerate(clean_text(value).split()):
        if index and word.lower() in small:
            words.append(word.lower())
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def parse_patterns(raw: str) -> list[str]:
    raw = (raw or "").replace("\\|", "|")
    return [
        part.strip().strip("\"'").lower()
        for part in re.split(r"[|,\n\r\t]+", raw)
        if part.strip()
    ]


def matches_pattern_list(url: str, patterns_raw: str, source_sitemap: str = "") -> bool:
    u = (url or "").lower()
    source = (source_sitemap or "").lower()
    path = path_of(url).rstrip("/").lower()
    tail = tail_slug(url).lower()

    for pattern in parse_patterns(patterns_raw):
        if pattern.startswith("source-sitemap:"):
            marker = pattern.split(":", 1)[1].strip()
            if marker and marker in source:
                return True
            continue
        if pattern.startswith("footprint:"):
            continue
        if pattern.startswith("prefix:"):
            marker = pattern.split(":", 1)[1].strip()
            if marker and tail.startswith(marker):
                return True
            continue
        if pattern.startswith("suffix:"):
            marker = pattern.split(":", 1)[1].strip()
            if marker and path.endswith(marker):
                return True
            continue
        if pattern.startswith("regex:"):
            try:
                if re.search(pattern.split(":", 1)[1], url, re.I):
                    return True
            except re.error:
                pass
            continue
        if pattern and pattern in u:
            return True
    return False


def is_non_content_url(url: str, geo: str) -> bool:
    u = (url or "").lower()
    bad_parts = list(BAD_PARTS_COMMON)
    if geo == "ES":
        bad_parts += [
            "/category/", "/categoria/", "/privacy", "/terms", "/cookies",
            "/contacto", "/sobre-nosotros", "/about",
        ]
    else:
        bad_parts += [
            "/category/", "/categorie/", "/privacy", "/terms", "/cookies",
            "/contact", "/a-propos", "/about",
        ]
    if any(part in u for part in bad_parts):
        return True
    return any(u.endswith(ext) for ext in BAD_EXTENSIONS)


def clean_brand_slug_es(slug: str) -> str:
    s = strip_accents(html_lib.unescape(slug or "")).lower()
    s = re.sub(r"\.html?$", "", s)
    s = re.sub(r"[_+]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    tails = [
        r"-casino-resena-\d+$", r"-casino-resena$", r"-es-resena-\d+$",
        r"-es-resena$", r"-resena-\d+$", r"-resena$", r"-bonos?$",
        r"-casino$",
    ]
    for pattern in tails:
        s = re.sub(pattern, "", s, flags=re.I)
    s = re.sub(r"^casino-", "", s, flags=re.I)
    s = re.sub(r"casino$", "", s, flags=re.I)
    s = s.replace("-", " ")
    s = re.sub(
        r"\b(casino|bono|bonos|resena|resenas|online|espana|juego|apuestas)\b",
        " ", s, flags=re.I,
    )
    return clean_text(s)


def clean_brand_slug_fr(slug: str) -> str:
    s = strip_accents(html_lib.unescape(slug or "")).lower()
    s = re.sub(r"\.html?$", "", s)
    s = re.sub(r"[_+]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    tails = [
        r"-casino-en-ligne$", r"-casino-review$", r"-casino-avis$",
        r"-avis$", r"-review$", r"-bonus$", r"-code-promo$", r"-casino$",
    ]
    for pattern in tails:
        s = re.sub(pattern, "", s, flags=re.I)
    s = re.sub(r"casino$", "", s, flags=re.I)
    s = s.replace("-", " ")
    s = re.sub(
        r"\b(casino|bonus|avis|revue|review|en ligne|canada|france|francais|quebec)\b",
        " ", s, flags=re.I,
    )
    return clean_text(s)


def brand_key(value: str, geo: str) -> str:
    v = strip_accents(value or "").lower().replace("&", "and")
    v = re.sub(r"[_\s-]+", "", v)
    if not v.startswith("casino"):
        v = re.sub(r"casino$", "", v)
    if geo == "ES":
        v = re.sub(r"(?:bonos?|resenas?|reviews?|online|espana)$", "", v)
    else:
        v = re.sub(r"(?:bonus|avis|revue|review|online|canada|france|francais)$", "", v)
    return re.sub(r"[^a-z0-9]", "", v)


def extract_brand_from_url(url: str, config: SiteConfig, geo: str) -> str:
    host = host_of(config.site or url)
    parts = [p for p in path_of(url).split("/") if p]
    slug = tail_slug(url)

    if geo == "ES":
        for marker in (
            ("legalbet.es", "casinos"),
            ("casino.org", "resenas"),
            ("tribuna.com", "resenas-de-casinos"),
        ):
            if host == marker[0] and marker[1] in parts:
                idx = parts.index(marker[1])
                if idx + 1 < len(parts):
                    slug = parts[idx + 1]
                    break
        cleaned = clean_brand_slug_es(slug)
    else:
        for marker in (("gambling.com", "casinos-en-ligne"), ("casinocanada.com", "casinos")):
            if host == marker[0] and marker[1] in parts:
                idx = parts.index(marker[1])
                if idx + 1 < len(parts):
                    slug = parts[idx + 1]
                    break
        cleaned = clean_brand_slug_fr(slug)
    return title_case(cleaned)


def category_key_from_url(url: str, geo: str) -> str:
    parts = [p for p in path_of(url).split("/") if p]
    if not parts:
        return ""
    last = parts[-1]

    if geo == "ES":
        if re.fullmatch(r"casino|casinos|bonos?|tragaperras|pagos|sets|juegos|juegos-casinos-gratis", last, re.I) and len(parts) >= 2:
            last = "-".join(parts[-2:])
        value = strip_accents(unquote(last)).lower()
        replacements = [
            (r"mejores-casinos-online", "onlinecasino"),
            (r"casinos?", ""),
            (r"bonos?", "bonus"),
            (r"tragaperras", "slots"),
            (r"juegos-casinos-gratis", "freegames"),
            (r"juegos", "games"),
            (r"metodos-de-pago", "payment"),
            (r"pagos", "payment"),
            (r"sin-deposito", "nodeposit"),
            (r"tiradas-gratis|giros-gratis", "freespins"),
            (r"espana|espanol|online", ""),
        ]
        weak = {"casino", "bonus", "resena", "review", "online"}
    else:
        if re.fullmatch(r"casino|casinos|casinos-en-ligne|bonus|paiements|methodes-de-paiement|machines-a-sous|jeux|depot-minimum", last, re.I) and len(parts) >= 2:
            last = "-".join(parts[-2:])
        value = strip_accents(unquote(last)).lower()
        replacements = [
            (r"casinos-en-ligne|casino-en-ligne", "onlinecasino"),
            (r"casinos?", ""),
            (r"bonus-sans-depot|sans-depot", "nodeposit"),
            (r"tours-gratuits", "freespins"),
            (r"machines-a-sous", "slots"),
            (r"methodes-de-paiement|paiements", "payment"),
            (r"depot-minimum", "minimumdeposit"),
            (r"jeux", "games"),
            (r"france|canada|quebec|francais|online|enligne", ""),
        ]
        weak = {"casino", "bonus", "avis", "review", "online"}

    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    if len(value) < 3 or value in weak:
        return ""
    return value


def commercial_key_from_h1(h1: str, geo: str) -> str:
    s = strip_accents(clean_text(h1)).lower()
    if not s:
        return ""
    s = s.replace("€", " euro ").replace("$", " dollar ")

    if geo == "ES":
        phrases = [
            (r"\bnuevos?\s+casinos?\b|\bcasinos?\s+nuevos?\b", " newcasino "),
            (r"\bcasinos?\s+(?:online|en\s+linea)\b", " onlinecasino "),
            (r"\b(?:bonos?\s+)?sin\s+deposito\b|\bno\s+deposit\b", " nodeposit "),
            (r"\b(?:tiradas?|giros?)\s+gratis\b|\bfree\s*spins?\b", " freespins "),
            (r"\btragaperras\b|\bslots?\b", " slots "),
            (r"\bmetodos?\s+de\s+pago\b|\bpagos?\b", " payment "),
            (r"\bretiros?\s+rapidos?\b", " fastpayout "),
            (r"\bretiro\b", " payout "),
            (r"\bdeposito\s+minimo\b", " minimumdeposit "),
            (r"\bbono\s+de\s+bienvenida\b", " welcomebonus "),
            (r"\bsin\s+licencia\b", " nolicense "),
            (r"\bsin\s+(?:verificacion|kyc)\b", " noverification "),
            (r"\bcasino\s+en\s+vivo\b", " livecasino "),
            (r"\bjuegos\s+de\s+casino\b", " games "),
            (r"\bbonos?\b", " bonus "),
        ]
        stopwords = r"\b(mejores?|top|nuevo|nuevos|nueva|nuevas|online|casino|casinos|sitio|sitios|espana|espanol|resena|resenas|guia|guias|lista|para|en|los|las|de|del|un|una|y|con|jugadores|202[4-9])\b"
        weak = {"casino", "casinos", "bonus", "resena", "review", "guia", "games"}
    else:
        phrases = [
            (r"\bnouveaux?\s+casinos?\b|\bcasinos?\s+nouveaux?\b", " newcasino "),
            (r"\bcasinos?\s+en\s+ligne\b", " onlinecasino "),
            (r"\bbonus\s+sans\s+depot\b|\bsans\s+depot\b|\bno\s+deposit\b", " nodeposit "),
            (r"\btours?\s+gratuits?\b|\bfree\s*spins?\b", " freespins "),
            (r"\bmachines?\s+a\s+sous\b", " slots "),
            (r"\bmethodes?\s+de\s+paiement\b|\bmoyens?\s+de\s+paiement\b|\bpaiements?\b", " payment "),
            (r"\bretraits?\s+rapides?\b", " fastpayout "),
            (r"\bretrait\b", " payout "),
            (r"\bdepot\s+minimum\b|\bfaible\s+depot\b", " minimumdeposit "),
            (r"\bbonus\s+de\s+bienvenue\b", " welcomebonus "),
            (r"\bcasino\s+en\s+direct\b", " livecasino "),
            (r"\bjeux\s+de\s+casino\b", " games "),
        ]
        stopwords = r"\b(meilleurs?|top|nouveau|nouveaux|nouvelle|nouvelles|online|casino|casinos|site|sites|canada|quebec|france|francais|avis|revue|review|guide|guides|liste|pour|en|les|des|de|du|un|une|et|avec|joueurs|202[4-9])\b"
        weak = {"casino", "casinos", "bonus", "avis", "review", "guide", "games"}

    for pattern, replacement in phrases:
        s = re.sub(pattern, replacement, s)
    s = re.sub(stopwords, " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    if len(s) < 4 or s in weak:
        return ""
    return s


def extract_h1(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    return clean_text(h1.get_text(" ", strip=True)) if h1 else ""


def class_text(soup: BeautifulSoup, marker: str) -> str:
    marker = marker.lower()
    node = soup.find(class_=lambda value: value and marker in " ".join(value if isinstance(value, list) else [value]).lower())
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def extract_generic_bonus(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True))
    patterns = [
        r"(?:bonus|bono|offre)[^.!?]{0,120}(?:\d+[\s.,]?%|\d+[\s.,]?(?:€|\$|C\$)|\d+\s+(?:giros|tiradas|tours|free\s*spins))[^.!?]{0,100}",
        r"\d+[\s.,]?%[^.!?]{0,100}(?:bonus|bono|offre)",
        r"\d+\s+(?:giros|tiradas|tours|free\s*spins)[^.!?]{0,100}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return clean_text(match.group(0))[:300]
    return ""


def clean_bonus(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"^(?:bonus|bono|offre)\s*[:\-]?\s*", "", value, flags=re.I)
    return value[:500]


def extract_bonus(html: str, config: SiteConfig, geo: str) -> str:
    fp = (config.bonus_footprint or "").lower()
    if not fp or fp == "skip" or not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    if geo == "ES":
        if "legalbet" in fp:
            candidates += [class_text(soup, "bk-header__promo"), class_text(soup, "bk-header-promo")]
        if "casasdeapuestas" in fp or "bonus-data" in fp:
            candidates += [class_text(soup, "main-bonus-text"), class_text(soup, "bonus-data")]
        if "casinoguru" in fp or "bonus-name-1" in fp:
            candidates += [class_text(soup, "bonus-name-1")]
        if "webapuestas" in fp or "contenido-bono" in fp:
            candidates += [class_text(soup, "contenido-bono"), class_text(soup, "font-semibold"), class_text(soup, "font-bold")]
    else:
        if "jeux" in fp or "brand-banner-bonus" in fp:
            candidates += [class_text(soup, "brand-banner-bonus-text")]
        if "gamblingcomfr" in fp or "data-offer" in fp:
            node = soup.find(attrs={"data-offer": True})
            if node:
                candidates.append(clean_text(node.get_text(" ", strip=True)))
        if "casinobonusca" in fp or "bonus-offer-text" in fp:
            candidates += [class_text(soup, "bonus-offer-text"), class_text(soup, "main-title")]
        if "lescasinoenligne" in fp or "bonus-title" in fp:
            candidates += [class_text(soup, "bonus__title")]
        if "casinocanada" in fp or "cs-casino-head-bonus" in fp:
            candidates += [class_text(soup, "cs-casino-head__bonus_text")]

    candidates.append(extract_generic_bonus(html))
    for candidate in candidates:
        cleaned = clean_bonus(candidate)
        if cleaned:
            return cleaned
    return ""


def extract_ref_link(html: str, base_url: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    hrefs = []
    for node in soup.find_all("a", href=True):
        href = urljoin(base_url, node.get("href", "").strip())
        if href:
            hrefs.append(href)
    markers = ("/go/", "/visit/", "/play/", "/out/", "/redirect", "ref=", "aff", "click", "track")
    for href in hrefs:
        if any(marker in href.lower() for marker in markers):
            return href
    return ""


def html_has(html: str, pattern: str) -> bool:
    return bool(html and re.search(pattern, html, re.I | re.S))


def _our_brand_by_path(item: UrlItem, geo: str) -> bool:
    path = path_of(item.url).strip("/")
    parts = path.split("/") if path else []
    source = (item.source_sitemap or "").lower()
    if any(marker in source for marker in ("casino-sitemap", "casinos-sitemap", "review-sitemap", "reviews-sitemap")):
        return True
    marker = "casinos"
    if marker in parts:
        idx = parts.index(marker)
        if idx + 1 == len(parts) - 1:
            slug = parts[-1].lower()
            generic = ES_GENERIC_BRAND_SLUGS if geo == "ES" else FR_GENERIC_BRAND_SLUGS
            return slug not in generic
    return False


def prefilter_item(item: UrlItem, config: SiteConfig, geo: str) -> bool:
    if is_non_content_url(item.url, geo):
        return False
    if config.include_regex:
        try:
            if not re.search(config.include_regex, item.url, re.I):
                return False
        except re.error:
            pass
    if config.exclude_regex:
        try:
            if re.search(config.exclude_regex, item.url, re.I):
                return False
        except re.error:
            pass

    host = host_of(config.site)
    u = item.url.lower()
    source = item.source_sitemap.lower()
    if config.is_our:
        return (
            matches_pattern_list(item.url, config.brand_patterns, item.source_sitemap)
            or matches_pattern_list(item.url, config.category_patterns, item.source_sitemap)
        )

    if geo == "ES":
        if host == "legalbet.es":
            return "/casinos/" in u or "/sets/" in u
        if host == "casasdeapuestas.com":
            return True
        if host == "es.casino.guru":
            return bool(re.search(r"-resena(?:-\d+)?/?$", path_of(item.url), re.I) or "-casino-resena" in u or "-bono" in u or "/mejores-casinos-online/" in u)
        if host == "casino.org":
            return "/es-es/" in u and any(part in u for part in ("/resenas/", "/bonos/", "/pagos/", "/tragaperras/", "/es-es/"))
        if host == "webapuestas.com":
            return any(part in u for part in ("/casinos/", "/bonos/", "/juegos-casinos-gratis/")) or len([p for p in path_of(item.url).split("/") if p]) <= 2
        if host == "tribuna.com":
            return "/es/casino/" in u or "es-casino" in source or "es-casas-de-apuestas" in source
    else:
        if host == "jeux.ca":
            return "page-sitemap" in source or path_of(item.url).rstrip("/").endswith("-casino")
        if host == "gambling.com":
            return "/ca/fr/" in u and any(part in u for part in ("/casinos-en-ligne/", "/bonus/", "/machines-a-sous/"))
        if host == "casinobonusca.com":
            return "/fr/" in u
        if host == "lescasinoenligne.ca":
            return True
        if host == "casinocanada.com":
            return "/fr/" in u

    return matches_pattern_list(item.url, config.brand_patterns, item.source_sitemap) or matches_pattern_list(item.url, config.category_patterns, item.source_sitemap)


def classify_item(item: UrlItem, config: SiteConfig, geo: str, html: str = "") -> Detection:
    url = item.url
    u = url.lower()
    source = item.source_sitemap.lower()
    host = host_of(config.site or url)
    h1 = extract_h1(html)
    h1_key = commercial_key_from_h1(h1, geo)
    url_key = category_key_from_url(url, geo)

    if is_non_content_url(url, geo):
        return Detection("OTHER", reason="non-content")

    if config.is_our:
        if _our_brand_by_path(item, geo) or matches_pattern_list(url, config.brand_patterns, source) and not _is_generic_brand_url(url, geo):
            brand = extract_brand_from_url(url, config, geo)
            key = brand_key(brand, geo)
            if brand and key:
                return Detection("BRAND", brand=brand, brand_key=key, html=html, reason="our brand")
        if h1_key:
            return Detection("CATEGORY", category_key=h1_key, url_key=url_key, h1=h1, h1_key=h1_key, html=html, reason="our h1")
        if matches_pattern_list(url, config.category_patterns, source) and url_key:
            return Detection("CATEGORY", category_key=url_key, url_key=url_key, h1=h1, html=html, reason="our url")
        return Detection("OTHER", h1=h1, html=html, reason="our no commercial key")

    if geo == "ES":
        detection = _classify_es(item, config, html, h1, h1_key, url_key)
    else:
        detection = _classify_fr(item, config, html, h1, h1_key, url_key)

    if detection.page_type == "BRAND" and not detection.brand:
        detection.brand = extract_brand_from_url(url, config, geo)
        detection.brand_key = brand_key(detection.brand, geo)
    if detection.page_type == "CATEGORY" and not detection.category_key:
        detection.category_key = h1_key or url_key
    detection.html = html
    return detection


def _is_generic_brand_url(url: str, geo: str) -> bool:
    slug = strip_accents(clean_brand_slug_es(tail_slug(url)) if geo == "ES" else clean_brand_slug_fr(tail_slug(url))).lower().replace(" ", "-")
    return slug in (ES_GENERIC_BRAND_SLUGS if geo == "ES" else FR_GENERIC_BRAND_SLUGS)


def _category_detection(h1: str, h1_key: str, url_key: str, clear_url: bool, reason: str) -> Detection:
    if h1_key:
        return Detection("CATEGORY", category_key=h1_key, url_key=url_key, h1=h1, h1_key=h1_key, reason=reason + " h1")
    if clear_url and url_key:
        return Detection("CATEGORY", category_key=url_key, url_key=url_key, h1=h1, reason=reason + " url")
    return Detection("OTHER", h1=h1, reason=reason + " weak")


def _classify_es(item: UrlItem, config: SiteConfig, html: str, h1: str, h1_key: str, url_key: str) -> Detection:
    url = item.url
    u = url.lower()
    host = host_of(config.site or url)
    path = path_of(url)

    if host == "legalbet.es":
        if "/casinos/" in u and not _is_generic_brand_url(url, "ES"):
            return Detection("BRAND", reason="legalbet casinos")
        if "/sets/" in u:
            return _category_detection(h1, h1_key, url_key, True, "legalbet sets")

    if host == "casasdeapuestas.com":
        if html_has(html, r"bonus-data\s*[\"'][^>]*data-type=") or html_has(html, r"class=[\"'][^\"']*bonus-data"):
            return Detection("BRAND", reason="casas bonus footprint")
        if any(part in u for part in ("/bonos/", "/casinos/", "/tragaperras/")):
            return _category_detection(h1, h1_key, url_key, True, "casas folder")

    if host == "es.casino.guru":
        if re.search(r"-resena(?:-\d+)?/?$", path, re.I) or "-casino-resena" in u:
            return Detection("BRAND", reason="casino guru review")
        if "/mejores-casinos-online/" in u or re.search(r"-bonos?/?$", path, re.I):
            return _category_detection(h1, h1_key, url_key, True, "casino guru category")

    if host == "casino.org":
        if "/es-es/" not in u:
            return Detection("OTHER", reason="casino.org other locale")
        if "/es-es/resenas/" in u and not _is_generic_brand_url(url, "ES"):
            return Detection("BRAND", reason="casino.org review")
        if any(part in u for part in ("/es-es/bonos/", "/es-es/pagos/", "/es-es/tragaperras/")):
            return _category_detection(h1, h1_key, url_key, True, "casino.org category")
        return _category_detection(h1, h1_key, url_key, False, "casino.org broad")

    if host == "webapuestas.com":
        if html_has(html, r"contenido-bono") or tail_slug(url).lower().startswith("casino-"):
            return Detection("BRAND", reason="webapuestas bonus footprint")
        if any(part in u for part in ("/casinos/", "/bonos/", "/juegos-casinos-gratis/")):
            return _category_detection(h1, h1_key, url_key, True, "webapuestas category")

    if host == "tribuna.com":
        if "/es/casino/resenas-de-casinos/" in u and not _is_generic_brand_url(url, "ES"):
            return Detection("BRAND", reason="tribuna review")
        if "/es/casino/" in u:
            return _category_detection(h1, h1_key, url_key, True, "tribuna casino")

    if matches_pattern_list(url, config.brand_patterns, item.source_sitemap) and not _is_generic_brand_url(url, "ES"):
        return Detection("BRAND", reason="generic brand pattern")
    if matches_pattern_list(url, config.category_patterns, item.source_sitemap):
        return _category_detection(h1, h1_key, url_key, True, "generic category pattern")
    return Detection("OTHER", h1=h1, reason="no ES rule")


def _classify_fr(item: UrlItem, config: SiteConfig, html: str, h1: str, h1_key: str, url_key: str) -> Detection:
    url = item.url
    u = url.lower()
    host = host_of(config.site or url)
    path = path_of(url)

    if host == "jeux.ca":
        if re.search(r"-casino/?$", path, re.I):
            return Detection("BRAND", reason="jeux suffix")
        if html_has(html, r"brand-banner-bonus-text"):
            return Detection("BRAND", reason="jeux bonus footprint")
        if html_has(html, r"bw-author-byline__user--title") or "page-sitemap" in item.source_sitemap.lower():
            return _category_detection(h1, h1_key, url_key, False, "jeux page")

    if host == "gambling.com":
        if "/ca/fr/" not in u:
            return Detection("OTHER", reason="gambling other locale")
        if "/ca/fr/casinos-en-ligne/" in u:
            if html_has(html, r"data-product-type=[\"']Casino[\"']") and html_has(html, r"data-offer="):
                brand = _extract_gambling_brand(html) or extract_brand_from_url(url, config, "FR")
                return Detection("BRAND", brand=brand, brand_key=brand_key(brand, "FR"), reason="gambling offer")
            return _category_detection(h1, h1_key, url_key, False, "gambling review/category")
        if "/ca/fr/bonus/" in u or "/ca/fr/machines-a-sous/" in u:
            return _category_detection(h1, h1_key, url_key, True, "gambling category")

    if host == "casinobonusca.com":
        if html_has(html, r"bonus-offer-text\s+main-title"):
            return Detection("BRAND", reason="casinobonusca footprint")
        if re.search(r"-casino/?$", path, re.I):
            return Detection("BRAND", reason="casinobonusca suffix")
        if html_has(html, r"tag-casino-expert"):
            return _category_detection(h1, h1_key, url_key, False, "casinobonusca expert")
        if "/bonus-sans-depot/" in u or "/tours-gratuits/" in u:
            return _category_detection(h1, h1_key, url_key, True, "casinobonusca category")
        return _category_detection(h1, h1_key, url_key, False, "casinobonusca broad")

    if host == "lescasinoenligne.ca":
        if re.search(r"-casino\.html$", path, re.I):
            return Detection("BRAND", reason="lescasino suffix")
        if html_has(html, r"bonus__title"):
            return Detection("BRAND", reason="lescasino bonus footprint")
        return _category_detection(h1, h1_key, url_key, False, "lescasino category")

    if host == "casinocanada.com":
        if "/fr/" not in u:
            return Detection("OTHER", reason="casinocanada other locale")
        if "/fr/casinos/" in u:
            if html_has(html, r"cs-casino-head__bonus_text"):
                return Detection("BRAND", reason="casinocanada bonus footprint")
            return _category_detection(h1, h1_key, url_key, False, "casinocanada casinos category")
        if any(part in u for part in ("/fr/paiements/", "/fr/bonus-de-casinos/", "/fr/depot-minimum/", "/fr/jeux/")):
            return _category_detection(h1, h1_key, url_key, True, "casinocanada category")

    if matches_pattern_list(url, config.brand_patterns, item.source_sitemap) and not _is_generic_brand_url(url, "FR"):
        return Detection("BRAND", reason="generic brand pattern")
    if matches_pattern_list(url, config.category_patterns, item.source_sitemap):
        return _category_detection(h1, h1_key, url_key, True, "generic category pattern")
    return Detection("OTHER", h1=h1, reason="no FR rule")


def _extract_gambling_brand(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(attrs={"data-product-type": re.compile(r"casino", re.I)})
    if node:
        for attr in ("data-product-name", "data-brand", "aria-label", "title"):
            value = node.get(attr)
            if value:
                return title_case(clean_text(value))
        heading = node.find(["h1", "h2", "h3"])
        if heading:
            text = clean_text(heading.get_text(" ", strip=True))
            text = re.sub(r"\bcasino\b.*$", "", text, flags=re.I)
            if text:
                return title_case(text)
    return ""
