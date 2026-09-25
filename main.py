#!/usr/bin/env python3
"""Automate de veille scientifique DT1.

Collecte les nouveautes (PubMed, ClinicalTrials.gov, flux RSS), les
resume en francais de facon strictement extractive (aucune information
non presente dans l'extrait source n'est ajoutee), puis insere le
resultat en tete de la section "5. Nouveautes et fil d'actualite" du
fichier Markdown cible, sans jamais toucher au reste du document.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import hashlib
import html
import json
import logging
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
import yaml
from dateutil import parser as dateparser

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
except ImportError:  # pragma: no cover - degrade gracefully
    GoogleTranslator = None
    MyMemoryTranslator = None

BASE_DIR = Path(__file__).resolve().parent
DOI_RE = re.compile(r'10\.\d{4,9}/[-._;()/:A-Za-z0-9]+')
NCT_RE = re.compile(r'NCT\d{8}')
SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

# Les flux RSS d'agences/industriels (FDA, EMA, ANSM, Eli Lilly...) publient
# TOUTES leurs actualités, pas seulement celles sur le DT1. Sans ce filtre,
# un communiqué RH de l'ANSM ou une approbation FDA sur une tout autre
# maladie serait inséré à tort (déjà observé en test). PubMed et
# ClinicalTrials.gov n'ont pas besoin de ce filtre : leurs requêtes
# elles-mêmes imposent déjà "type 1 diabetes".
T1D_RELEVANCE_RE = re.compile(
    r"type\s*1\s*diabetes|type-1\s*diabetes|\bt1d\b|diabète\s+de\s+type\s*1|\bdt1\b",
    re.IGNORECASE,
)

logger = logging.getLogger("veille_dt1")


# ---------------------------------------------------------------------------
# Modeles de donnees
# ---------------------------------------------------------------------------

@dataclass
class RawItem:
    """Item brut collecte, avant traduction, dans sa langue source."""

    dedup_key: str
    date: datetime
    country: str
    title_src: str
    summary_src: str
    axis: Optional[str]
    doc_type: str
    maturity: str
    confidence: str
    confidence_note: str
    source_name: str
    source_url: str


@dataclass
class Entry:
    """Item pret a etre inséré dans le Markdown (apres traduction)."""

    date: datetime
    country: str
    title_fr: str
    axis: str
    doc_type: str
    maturity: str
    summary_fr: str
    confidence: str
    confidence_note: str
    source_name: str
    source_url: str


@dataclass
class FicheField:
    """Un champ (`- **Label** : texte`) d'une fiche technologie existante,
    avec la position exacte (indices de lignes, inclusifs) qu'il occupe
    dans le fichier source — y compris ses éventuelles lignes de
    continuation repliées (ancien format multi-lignes)."""

    label: str
    text: str
    line_start: int
    line_end: int


@dataclass
class Fiche:
    """Une fiche technologie existante (### ...) des sections 2 à 4."""

    title_raw: str
    axis: str
    identifiers: list[str]
    fields: dict[str, FicheField]


# ---------------------------------------------------------------------------
# Configuration / dedup store
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_dedup(path: Path) -> dict:
    if not path.exists():
        return {"seen": [], "last_run": None, "known_fiches": []}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_dedup(path: Path, store: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# ---------------------------------------------------------------------------
# Helpers generiques
# ---------------------------------------------------------------------------

def get_path(data: dict, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def extractive_summary(text: str) -> str:
    """Resume purement extractif : integralite de l'extrait source (toutes
    les phrases), sans aucune reformulation, ajout d'information, ni
    troncature — l'utilisateur veut le texte complet dans les fiches, les
    nouvelles entrées et l'email, jamais coupé par "…"."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s]
    return " ".join(sentences)


_last_translate_call = [0.0]
_MIN_TRANSLATE_INTERVAL = 1.0  # espacement prudent, service gratuit sans clé


def _throttle_translate() -> None:
    elapsed = time.time() - _last_translate_call[0]
    if elapsed < _MIN_TRANSLATE_INTERVAL:
        time.sleep(_MIN_TRANSLATE_INTERVAL - elapsed)


_TRANSLATE_CHUNK_MAX_CHARS = 450  # marge de sécurité sous la limite de 500 caractères de MyMemory


def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Découpe `text` en morceaux <= max_chars, en coupant aux frontières de
    phrase (jamais au milieu d'un mot) pour rester sous la limite de requête
    du moteur de traduction le plus restrictif (MyMemory, 500 caractères)."""
    sentences = [s for s in SENTENCE_SPLIT_RE.split(text) if s]
    chunks: list[str] = []
    current = ""
    for s in sentences:
        candidate = f"{current} {s}".strip() if current else s
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(s) <= max_chars:
            current = s
        else:
            # Phrase à elle seule plus longue que max_chars (rare) : découpe
            # brute au mot le plus proche.
            words = s.split(" ")
            piece = ""
            for w in words:
                candidate_piece = f"{piece} {w}".strip() if piece else w
                if len(candidate_piece) <= max_chars:
                    piece = candidate_piece
                else:
                    if piece:
                        chunks.append(piece)
                    piece = w
            if piece:
                current = piece
    if current:
        chunks.append(current)
    return chunks


_TRANSLATE_CALL_TIMEOUT = 8.0  # secondes

# Coupe-circuit : si les moteurs de traduction échouent N fois d'affilée
# (blocage réseau/anti-abus persistant — ex. IP de datacenter GitHub
# Actions bloquée par Google), inutile de retenter le même échec pour
# chacun des dizaines d'items restants : ça borne le temps total d'une
# exécution à quelques dizaines de secondes de détection, plutôt que des
# dizaines de minutes. Remis à zéro dès qu'un appel réussit (reprise
# automatique si le service redevient joignable en cours d'exécution).
_CIRCUIT_BREAKER_THRESHOLD = 5
_consecutive_translate_failures = [0]

# Coupe-circuit par moteur : sur certains réseaux (ex. IP partagée de
# datacenter GitHub Actions), un moteur précis peut échouer de façon
# systématique (quota epuisé, blocage anti-abus) alors que l'autre
# fonctionne parfaitement — retenter le moteur cassé à chaque item gaspille
# plusieurs secondes par item pour rien. Après quelques échecs consécutifs,
# ce moteur est ignoré pour le reste de l'exécution (pas de retry immédiat
# non plus : sur une erreur "too many requests", réessayer une seconde plus
# tard échoue quasi toujours pareil).
_ENGINE_FAILURE_THRESHOLD = 3
_engine_consecutive_failures: dict = {}

# GoogleTranslator/MyMemoryTranslator (deep-translator) n'exposent aucun
# paramètre timeout sur leurs requêtes HTTP internes : un appel qui ne
# répond jamais (fréquent en cas de blocage anti-abus depuis une IP de
# datacenter, ex. GitHub Actions) resterait sinon bloqué indéfiniment et
# gèlerait toute l'exécution. Chaque appel est donc exécuté dans un thread
# à part, avec un délai maximal — le pool est partagé (jamais fermé) pour
# ne jamais attendre un thread bloqué : un timeout abandonne simplement le
# résultat, le thread sous-jacent meurt de lui-même à la fin du script.
_TRANSLATE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="translate")


def _call_with_timeout(fn, text: str, timeout: float):
    future = _TRANSLATE_EXECUTOR.submit(fn, text)
    return future.result(timeout=timeout)


def _translate_chunk(text: str) -> str:
    """Traduit un seul morceau de texte (sous la limite de longueur des
    moteurs) vers le français. Deux moteurs sont essayés dans l'ordre
    (Google puis MyMemory, tous deux via deep-translator, sans clé). Le
    texte n'est jamais réécrit ni résumé ici : c'est une traduction, pas une
    reformulation. En cas d'échec des deux moteurs, le texte source est
    renvoyé, préfixé d'un marqueur explicite, plutôt que d'inventer une
    traduction."""
    engines = []
    if GoogleTranslator is not None:
        engines.append(("Google", lambda t: GoogleTranslator(source="auto", target="fr").translate(t)))
    if MyMemoryTranslator is not None:
        # MyMemory utilise des codes de locale (fr-FR, en-GB...), pas les
        # codes courts de GoogleTranslator.
        engines.append(
            ("MyMemory", lambda t: MyMemoryTranslator(source="en-GB", target="fr-FR").translate(t))
        )

    if not engines:
        return f"[traduction indisponible - dépendance manquante] {text}"

    if _consecutive_translate_failures[0] >= _CIRCUIT_BREAKER_THRESHOLD:
        return f"[traduction automatique indisponible - service injoignable, réessais suspendus pour cette exécution] {text}"

    last_error = None
    attempted_any = False
    for name, engine in engines:
        if _engine_consecutive_failures.get(name, 0) >= _ENGINE_FAILURE_THRESHOLD:
            continue  # ce moteur échoue systématiquement depuis le début du run
        attempted_any = True
        _throttle_translate()
        try:
            result = _call_with_timeout(engine, text, _TRANSLATE_CALL_TIMEOUT)
            _last_translate_call[0] = time.time()
            if result:
                _consecutive_translate_failures[0] = 0
                _engine_consecutive_failures[name] = 0
                return result
        except Exception as exc:  # pragma: no cover - dépend du réseau
            _last_translate_call[0] = time.time()
            last_error = exc
            _engine_consecutive_failures[name] = _engine_consecutive_failures.get(name, 0) + 1
            logger.info("Traduction (%s) : échec (%s)", name, exc)

    if not attempted_any:
        return f"[traduction automatique indisponible - tous les moteurs sont en échec systématique pour cette exécution] {text}"

    _consecutive_translate_failures[0] += 1
    logger.warning(
        "Echec de traduction automatique (tous moteurs), %d échec(s) consécutif(s) : %s",
        _consecutive_translate_failures[0],
        last_error,
    )
    return f"[traduction automatique indisponible] {text}"


def translate_fr(text: str) -> str:
    """Traduit `text` vers le français, en le découpant d'abord en morceaux
    de moins de 500 caractères (voir `_split_into_chunks`) pour rester sous
    la limite du moteur MyMemory, puis en recollant les traductions de
    chaque morceau — nécessaire depuis que les résumés ne sont plus tronqués
    et peuvent dépasser largement cette limite."""
    if not text:
        return text
    if len(text) <= _TRANSLATE_CHUNK_MAX_CHARS:
        return _translate_chunk(text)
    chunks = _split_into_chunks(text, _TRANSLATE_CHUNK_MAX_CHARS)
    return " ".join(_translate_chunk(c) for c in chunks)


def classify_axis(text: str, axis_keywords: dict, hint: Optional[str] = None) -> Optional[str]:
    if hint:
        return hint
    text_l = text.lower()
    scores = {
        axis: sum(1 for kw in kws if kw.lower() in text_l)
        for axis, kws in axis_keywords.items()
    }
    best_axis = max(scores, key=scores.get) if scores else None
    if not best_axis or scores[best_axis] == 0:
        return None
    return best_axis


def classify_maturity(text: str, maturity_keywords: dict) -> str:
    text_l = text.lower()
    # Ordre de priorite : un communique "commercialise" prime sur une
    # mention accessoire d'un essai de phase anterieure.
    for label in ("Commercialisé", "Phase 3", "Phase 2", "Phase 1", "Préclinique"):
        kws = maturity_keywords.get(label, [])
        if any(kw.lower() in text_l for kw in kws):
            return label
    return "non précisé dans la source"


def classify_confidence(
    source_kind: str,
    has_doi: bool,
    has_nct: bool,
    domain: Optional[str],
    official_domains: list,
) -> tuple[str, str]:
    if source_kind == "clinicaltrials":
        return "Élevé", ""
    if source_kind == "pubmed":
        if has_doi:
            return "Élevé", ""
        return "Moyen", "publication indexée PubMed, DOI non récupéré"
    if source_kind == "rss":
        if has_doi or has_nct:
            return "Élevé", "identifiant DOI/NCT cité dans le communiqué"
        if domain and any(d in domain for d in official_domains):
            return "Moyen", "communiqué officiel d'organisme/entreprise, sans DOI/NCT"
        return "Faible", "non confirmé — pas de lien vers une source primaire"
    return "Faible", "non confirmé"


# ---------------------------------------------------------------------------
# Source : PubMed (NCBI E-utilities)
# ---------------------------------------------------------------------------

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _pubmed_sleep(cfg_pubmed: dict) -> None:
    time.sleep(0.11 if cfg_pubmed.get("api_key") else 0.35)


def _parse_pubmed_date(article) -> Optional[datetime]:
    pubdate = article.find(".//Journal/JournalIssue/PubDate")
    if pubdate is None:
        return None
    year = pubdate.findtext("Year")
    month = pubdate.findtext("Month") or "1"
    day = pubdate.findtext("Day") or "1"
    if year:
        try:
            dt = dateparser.parse(f"{year} {month} {day}", default=datetime(1900, 1, 1))
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            try:
                dt = dateparser.parse(year, default=datetime(1900, 1, 1))
                return dt.replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                return None
    medline_date = pubdate.findtext("MedlineDate")
    if medline_date:
        try:
            dt = dateparser.parse(medline_date, fuzzy=True, default=datetime(1900, 1, 1))
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            return None
    return None


# Pays réel de l'institution de recherche, retrouvé dans le texte libre des
# affiliations d'auteurs — PLUTÔT que MedlineJournalInfo/Country, qui indique
# le pays d'immatriculation de la REVUE et s'est révélé trompeur en pratique
# (ex. observé : étude conduite en Chine ou aux Pays-Bas, publiée dans une
# revue immatriculée au Royaume-Uni -> le pays affiché aurait été faux).
# Liste non exhaustive mais couvrant les pays les plus fréquents dans la
# littérature DT1 ; toujours une correspondance de chaîne EXACTE (jamais une
# déduction). "Georgia" (le pays) est délibérément absent : dans ce corpus,
# elle désignerait presque toujours l'État américain (ex. Atlanta, GA), et
# une fausse détection serait pire qu'une absence de détection.
COUNTRY_NAME_VARIANTS: dict[str, str] = {
    "united states of america": "United States", "united states": "United States",
    "usa": "United States", "u.s.a.": "United States", "u.s.a": "United States", "u.s.": "United States",
    "united kingdom": "United Kingdom", "u.k.": "United Kingdom", "uk": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "people's republic of china": "China", "p.r. china": "China", "pr china": "China",
    "mainland china": "China", "china": "China",
    "hong kong": "Hong Kong", "taiwan": "Taiwan",
    "democratic people's republic of korea": "North Korea", "north korea": "North Korea",
    "republic of korea": "South Korea", "south korea": "South Korea", "korea": "South Korea",
    "the netherlands": "Netherlands", "netherlands": "Netherlands", "holland": "Netherlands",
    "russian federation": "Russia", "russia": "Russia",
    "czech republic": "Czechia", "czechia": "Czechia",
    "united arab emirates": "United Arab Emirates", "uae": "United Arab Emirates",
    "germany": "Germany", "france": "France", "italy": "Italy", "spain": "Spain",
    "portugal": "Portugal", "switzerland": "Switzerland", "austria": "Austria",
    "belgium": "Belgium", "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
    "finland": "Finland", "iceland": "Iceland", "ireland": "Ireland", "poland": "Poland",
    "hungary": "Hungary", "romania": "Romania", "bulgaria": "Bulgaria", "croatia": "Croatia",
    "slovenia": "Slovenia", "slovakia": "Slovakia", "serbia": "Serbia", "greece": "Greece",
    "turkey": "Turkey", "estonia": "Estonia", "latvia": "Latvia", "lithuania": "Lithuania",
    "luxembourg": "Luxembourg", "malta": "Malta", "cyprus": "Cyprus", "ukraine": "Ukraine",
    "belarus": "Belarus",
    "canada": "Canada", "mexico": "Mexico", "méxico": "Mexico", "brazil": "Brazil",
    "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia", "peru": "Peru",
    "venezuela": "Venezuela", "uruguay": "Uruguay", "paraguay": "Paraguay", "bolivia": "Bolivia",
    "ecuador": "Ecuador", "costa rica": "Costa Rica", "panama": "Panama", "cuba": "Cuba",
    "dominican republic": "Dominican Republic",
    "australia": "Australia", "new zealand": "New Zealand",
    "japan": "Japan", "india": "India", "pakistan": "Pakistan", "bangladesh": "Bangladesh",
    "sri lanka": "Sri Lanka", "nepal": "Nepal", "thailand": "Thailand", "vietnam": "Vietnam",
    "malaysia": "Malaysia", "indonesia": "Indonesia", "philippines": "Philippines",
    "singapore": "Singapore",
    "israel": "Israel", "saudi arabia": "Saudi Arabia", "iran": "Iran", "iraq": "Iraq",
    "egypt": "Egypt", "jordan": "Jordan", "lebanon": "Lebanon", "qatar": "Qatar",
    "kuwait": "Kuwait", "oman": "Oman", "bahrain": "Bahrain",
    "south africa": "South Africa", "nigeria": "Nigeria", "kenya": "Kenya", "ghana": "Ghana",
    "morocco": "Morocco", "tunisia": "Tunisia", "algeria": "Algeria", "ethiopia": "Ethiopia",
    "uganda": "Uganda", "tanzania": "Tanzania",
}
COUNTRY_MATCH_RE = re.compile(
    r"(?<![a-zA-Z])(" + "|".join(re.escape(k) for k in sorted(COUNTRY_NAME_VARIANTS, key=len, reverse=True)) + r")(?![a-zA-Z])",
    re.IGNORECASE,
)


def extract_country_from_affiliations(article) -> str:
    """Pays de l'institution de recherche, retrouvé par recherche déterministe
    d'un nom de pays connu dans le texte libre des affiliations d'auteurs
    (jamais dans le pays d'immatriculation de la revue). Si plusieurs pays
    distincts apparaissent (équipe multinationale), les trois premiers par
    ordre alphabétique sont retenus (même convention que pour
    ClinicalTrials.gov, cf. `fetch_clinicaltrials`). Aucune correspondance
    trouvée -> "non précisé dans la source", jamais une déduction."""
    affiliations = [
        (node.text or "")
        for node in article.findall(".//AuthorList/Author/AffiliationInfo/Affiliation")
    ]
    text = " ".join(affiliations)
    if not text:
        return "non précisé dans la source"
    found = {COUNTRY_NAME_VARIANTS[m.group(1).lower()] for m in COUNTRY_MATCH_RE.finditer(text)}
    if not found:
        return "non précisé dans la source"
    return ", ".join(sorted(found)[:3])


def _parse_pubmed_article(article, axis: str) -> Optional[RawItem]:
    pmid = article.findtext(".//PMID")
    title = article.findtext(".//Article/ArticleTitle") or ""
    abstract_parts = [
        (node.text or "") for node in article.findall(".//Abstract/AbstractText")
    ]
    abstract = " ".join(p for p in abstract_parts if p).strip()
    date = _parse_pubmed_date(article)
    if date is None or not pmid:
        return None

    doi = None
    for node in article.findall(".//ELocationID"):
        if node.get("EIdType") == "doi":
            doi = node.text
    if not doi:
        for node in article.findall(".//ArticleIdList/ArticleId"):
            if node.get("IdType") == "doi":
                doi = node.text

    country = extract_country_from_affiliations(article)

    dedup_key = f"doi:{doi.lower()}" if doi else f"pmid:{pmid}"
    source_url = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

    confidence, note = classify_confidence("pubmed", bool(doi), False, None, [])

    return RawItem(
        dedup_key=dedup_key,
        date=date,
        country=country,
        title_src=title,
        summary_src=abstract or title,
        axis=axis,
        doc_type="Article",
        maturity="",  # renseignée par l'appelant, une fois les mots-clés de config disponibles
        confidence=confidence,
        confidence_note=note,
        source_name="PubMed",
        source_url=source_url,
    )


def fetch_pubmed(cfg: dict, window_start: datetime, window_end: datetime) -> tuple[list[RawItem], int]:
    pubmed_cfg = cfg.get("pubmed", {})
    maturity_keywords = cfg.get("maturity_keywords", {})
    items: list[RawItem] = []
    common_params = {
        "tool": "veille-dt1",
        "email": pubmed_cfg.get("email", ""),
    }
    if pubmed_cfg.get("api_key"):
        common_params["api_key"] = pubmed_cfg["api_key"]

    for query_cfg in pubmed_cfg.get("queries", []):
        axis = query_cfg["axis"]
        query = query_cfg["query"]
        params = dict(common_params)
        params.update(
            {
                "db": "pubmed",
                "term": query,
                "datetype": "pdat",
                "mindate": window_start.strftime("%Y/%m/%d"),
                "maxdate": window_end.strftime("%Y/%m/%d"),
                "retmax": pubmed_cfg.get("max_results_per_query", 25),
                "retmode": "json",
            }
        )
        try:
            resp = requests.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
            resp.raise_for_status()
            pmids = resp.json().get("esearchresult", {}).get("idlist", [])
        except Exception as exc:
            logger.warning("PubMed esearch a échoué pour l'axe %s : %s", axis, exc)
            continue
        _pubmed_sleep(pubmed_cfg)

        if not pmids:
            continue

        fetch_params = dict(common_params)
        fetch_params.update({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})
        try:
            # POST recommandé par NCBI pour les listes d'ID (évite les 400
            # intermittents observés en GET sur certaines requêtes).
            resp = requests.post(f"{EUTILS_BASE}/efetch.fcgi", data=fetch_params, timeout=30)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            logger.warning("PubMed efetch a échoué pour l'axe %s : %s", axis, exc)
            continue
        _pubmed_sleep(pubmed_cfg)

        for article in root.findall(".//PubmedArticle"):
            item = _parse_pubmed_article(article, axis)
            if item is None:
                continue
            item.maturity = classify_maturity(
                f"{item.title_src} {item.summary_src}", maturity_keywords
            )
            if window_start.date() <= item.date.date() <= window_end.date():
                items.append(item)
    return items, 0


# ---------------------------------------------------------------------------
# Source : ClinicalTrials.gov API v2
# ---------------------------------------------------------------------------

PHASE_LABELS = {
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Commercialisé",
}


def fetch_clinicaltrials(cfg: dict, window_start: datetime, window_end: datetime) -> tuple[list[RawItem], int]:
    ct_cfg = cfg.get("clinicaltrials", {})
    base_url = ct_cfg.get("base_url", "https://clinicaltrials.gov/api/v2/studies")
    condition = ct_cfg.get("condition", "Type 1 Diabetes")
    max_results = ct_cfg.get("max_results", 50)

    date_range = f"AREA[LastUpdatePostDate]RANGE[{window_start:%Y-%m-%d},{window_end:%Y-%m-%d}]"
    params = {
        "query.cond": condition,
        "filter.advanced": date_range,
        "sort": "LastUpdatePostDate:desc",
        "pageSize": min(100, max_results),
    }

    items: list[RawItem] = []
    page_token = None
    fetched = 0
    filtered_out = 0
    while fetched < max_results:
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = requests.get(base_url, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("ClinicalTrials.gov : requête échouée : %s", exc)
            break

        studies = payload.get("studies", [])
        for study in studies:
            ps = study.get("protocolSection", {})
            nct_id = get_path(ps, "identificationModule", "nctId")
            title = get_path(ps, "identificationModule", "briefTitle") or ""
            summary = get_path(ps, "descriptionModule", "briefSummary") or ""
            status = get_path(ps, "statusModule", "overallStatus") or ""
            last_update = get_path(ps, "statusModule", "lastUpdatePostDateStruct", "date")
            phases = get_path(ps, "designModule", "phases", default=[]) or []
            locations = get_path(ps, "contactsLocationsModule", "locations", default=[]) or []
            sponsor = get_path(ps, "sponsorCollaboratorsModule", "leadSponsor", "name") or "non précisé dans la source"
            conditions = get_path(ps, "conditionsModule", "conditions", default=[]) or []

            if not nct_id or not last_update:
                continue

            # query.cond="Type 1 Diabetes" fait aussi remonter des essais sans
            # rapport (ex. un essai de pharmacocinétique en DT2, un essai chez
            # volontaires sains) : on revérifie sur le champ structuré
            # "conditions" (autorité) plutôt que de faire confiance à la
            # recherche côté API, en repli sur le titre/résumé si absent.
            relevance_text = " ".join(conditions) or f"{title} {summary}"
            if not T1D_RELEVANCE_RE.search(relevance_text):
                filtered_out += 1
                continue
            try:
                date = dateparser.parse(last_update).replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
            if not (window_start.date() <= date.date() <= window_end.date()):
                continue

            countries = sorted({loc.get("country") for loc in locations if loc.get("country")})
            country = ", ".join(countries[:3]) if countries else "non précisé dans la source"

            maturity = "non précisé dans la source"
            for phase in phases:
                if phase in PHASE_LABELS:
                    maturity = PHASE_LABELS[phase]
                    break

            confidence, note = classify_confidence("clinicaltrials", True, True, None, [])
            axis = classify_axis(f"{title} {summary}", cfg.get("axis_keywords", {}))

            items.append(
                RawItem(
                    dedup_key=f"nct:{nct_id}",
                    date=date,
                    country=country,
                    title_src=title,
                    summary_src=f"{summary} (Promoteur : {sponsor}. Statut : {status}.)".strip(),
                    axis=axis,
                    doc_type="Essai clinique",
                    maturity=maturity,
                    confidence=confidence,
                    confidence_note=note,
                    source_name=f"ClinicalTrials.gov, {nct_id}",
                    source_url=f"https://clinicaltrials.gov/study/{nct_id}",
                )
            )
            fetched += 1
            if fetched >= max_results:
                break

        page_token = payload.get("nextPageToken")
        if not page_token or not studies:
            break

    if filtered_out:
        logger.info(
            "ClinicalTrials.gov : %d essai(s) hors-sujet (condition ≠ DT1) ignoré(s)", filtered_out
        )
    return items, filtered_out


# ---------------------------------------------------------------------------
# Source : flux RSS / Atom
# ---------------------------------------------------------------------------

def fetch_rss(cfg: dict, window_start: datetime, window_end: datetime) -> tuple[list[RawItem], int]:
    axis_keywords = cfg.get("axis_keywords", {})
    maturity_keywords = cfg.get("maturity_keywords", {})
    official_domains = cfg.get("official_org_domains", [])
    items: list[RawItem] = []

    filtered_out = 0
    for feed_cfg in cfg.get("rss_feeds", []):
        if not feed_cfg.get("enabled", True):
            continue
        url = feed_cfg["url"]
        headers = {"User-Agent": "Mozilla/5.0 (compatible; VeilleDT1Bot/1.0; +mailto:optisun.tracking@gmail.com)"}
        try:
            # Récupération via `requests` (CA bundle certifi) plutôt que le
            # fetch interne de feedparser (urllib + magasin de certificats
            # système Windows), qui peut échouer derrière certains proxys/
            # antivirus locaux interceptant le TLS.
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                raise parsed.bozo_exception or RuntimeError("flux vide/illisible")
        except Exception as exc:
            logger.warning("RSS %s : impossible de lire le flux (%s)", feed_cfg.get("name", url), exc)
            continue

        for entry in parsed.entries:
            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published_struct:
                continue
            date = datetime(*published_struct[:6], tzinfo=timezone.utc)
            if not (window_start.date() <= date.date() <= window_end.date()):
                continue

            title = entry.get("title", "")
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", entry.get("description", "")))
            link = entry.get("link", url)
            full_text = f"{title} {summary}"

            if not T1D_RELEVANCE_RE.search(full_text):
                filtered_out += 1
                continue  # actualité hors-sujet (agence/industriel multi-pathologies)

            doi_match = DOI_RE.search(full_text)
            nct_match = NCT_RE.search(full_text)
            domain = urlsplit(link).netloc

            confidence, note = classify_confidence(
                "rss", bool(doi_match), bool(nct_match), domain, official_domains
            )
            axis = classify_axis(full_text, axis_keywords, hint=feed_cfg.get("axis_hint"))
            maturity = classify_maturity(full_text, maturity_keywords)

            if doi_match:
                dedup_key = f"doi:{doi_match.group(0).lower()}"
            elif nct_match:
                dedup_key = f"nct:{nct_match.group(0)}"
            else:
                dedup_key = f"url:{canonical_url(link)}"

            items.append(
                RawItem(
                    dedup_key=dedup_key,
                    date=date,
                    country=feed_cfg.get("country", "non précisé dans la source"),
                    title_src=title,
                    summary_src=summary,
                    axis=axis,
                    doc_type="Communiqué",
                    maturity=maturity,
                    confidence=confidence,
                    confidence_note=note,
                    source_name=feed_cfg.get("name", domain),
                    source_url=link,
                )
            )
    if filtered_out:
        logger.info("RSS : %d élément(s) hors-sujet (pas de mention DT1) ignoré(s)", filtered_out)
    return items, filtered_out


# ---------------------------------------------------------------------------
# Fiches technologie existantes (sections 2-4) : parsing et correspondance
# ---------------------------------------------------------------------------
#
# Logique à deux niveaux :
#   - si une nouveauté correspond (par identifiant exact, jamais par sens) à
#     une fiche ### déjà suivie, on met à jour SES champs Maturité, Pays,
#     Bénéfices, Inconvénients & risques et Source principale ;
#   - sinon, elle devient une nouvelle entrée en section 5.
# Les deux ne se produisent jamais pour le même élément.

SECTION_AXIS_HEADINGS = {
    "## 2. Axe Biologique et Cellulaire": "Biologique",
    "## 3. Axe Mécanique et Technologique": "Mécanique",
    "## 4. Axe Immunothérapie": "Immunothérapie",
}
FICHE_TITLE_RE = re.compile(r"^###\s+(.+?)\s*$")
FIELD_START_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*\s*:\s*(.*)$")
HEADING_RE = re.compile(r"^#{1,6}\s")
FICHE_TITLE_SEP_RE = re.compile(r"\s(?:---|—)\s")

# Champs qu'une nouveauté correspondant à une fiche est autorisée à modifier.
# "Niveau de confiance" n'en fait délibérément pas partie (cf. consigne :
# c'est un jugement curé manuellement, pas un champ que l'automate réécrit).
FICHE_UPDATABLE_FIELDS = ("Maturité", "Pays", "Bénéfices", "Inconvénients & risques", "Source principale")

# Longueur en-dessous de laquelle un identifiant de fiche (ex: "ATG", "IL-2")
# est jugé trop générique pour être cherché dans le corps du texte : on ne le
# cherche alors que dans le titre de la nouveauté, pour limiter les faux
# positifs (toujours une correspondance EXACTE de chaîne, jamais floue).
SHORT_IDENTIFIER_MAX_LEN = 3


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_identifiers(title_raw: str) -> list[str]:
    """Extrait, de façon déterministe, les identifiants exacts d'une fiche
    à partir de son titre `### ...` (ex : "Zimislecel (VX-880)" à partir de
    "Zimislecel (VX-880) --- îlots dérivés de cellules souches").

    Découpe uniquement sur le séparateur de titre ("---" ou "—") puis sur
    les parenthèses/barres obliques du membre de gauche : aucune analyse de
    sens, seulement de la manipulation de chaîne."""
    left = FICHE_TITLE_SEP_RE.split(title_raw, maxsplit=1)[0].strip()
    identifiers = set()
    if left:
        identifiers.add(_normalize_ws(left).casefold())
    for paren_content in re.findall(r"\(([^)]*)\)", left):
        for alt in re.split(r"[/,]", paren_content):
            alt = _normalize_ws(alt).casefold()
            if alt:
                identifiers.add(alt)
    primary = _normalize_ws(re.split(r"\(", left)[0]).casefold()
    if primary:
        identifiers.add(primary)
    return sorted(identifiers, key=len, reverse=True)


def parse_fiches(markdown_text: str) -> list[Fiche]:
    """Relit les fiches ### des sections 2 à 4 telles qu'elles existent
    actuellement dans le fichier, avec la position exacte de chacun de leurs
    champs (pour permettre une réécriture chirurgicale, champ par champ,
    sans toucher au reste de la fiche ni à sa mise en forme d'origine)."""
    lines = markdown_text.split("\n")
    fiches: list[Fiche] = []
    current_axis = None
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped in SECTION_AXIS_HEADINGS:
            current_axis = SECTION_AXIS_HEADINGS[stripped]
            i += 1
            continue
        if stripped.startswith("## "):
            current_axis = None
            i += 1
            continue
        title_match = FICHE_TITLE_RE.match(lines[i]) if current_axis else None
        if title_match:
            title_raw = title_match.group(1)
            i += 1
            while i < n and lines[i].strip() == "":
                i += 1
            fields: dict[str, FicheField] = {}
            while i < n:
                if lines[i].strip() == "":
                    i += 1
                    break
                if HEADING_RE.match(lines[i].strip()):
                    break
                field_match = FIELD_START_RE.match(lines[i])
                if not field_match:
                    i += 1  # ligne inattendue : ignorée défensivement
                    continue
                label = field_match.group(1).strip()
                text_parts = [field_match.group(2).strip()] if field_match.group(2).strip() else []
                field_start = i
                i += 1
                while i < n and lines[i].strip() != "" and not FIELD_START_RE.match(lines[i]) and not HEADING_RE.match(lines[i].strip()):
                    text_parts.append(lines[i].strip())
                    i += 1
                fields[label] = FicheField(
                    label=label,
                    text=_normalize_ws(" ".join(text_parts)),
                    line_start=field_start,
                    line_end=i - 1,
                )
            fiches.append(
                Fiche(
                    title_raw=title_raw,
                    axis=current_axis,
                    identifiers=extract_identifiers(title_raw),
                    fields=fields,
                )
            )
            continue
        i += 1
    return fiches


def match_item_to_fiche(title_text: str, full_text: str, fiches: list[Fiche]) -> Optional[Fiche]:
    """Cherche une correspondance EXACTE (chaîne, insensible à la casse et
    aux espaces superflus) entre une nouveauté et un identifiant de fiche
    existante. Jamais de correspondance floue sur le sens. En cas
    d'ambiguïté (plusieurs fiches matchées), on refuse de choisir et on
    traite l'élément comme une nouveauté ordinaire plutôt que de risquer une
    mise à jour de la mauvaise fiche."""
    title_norm = _normalize_ws(title_text).casefold()
    full_norm = _normalize_ws(full_text).casefold()
    matched = []
    for fiche in fiches:
        for ident in fiche.identifiers:
            haystack = title_norm if len(ident) <= SHORT_IDENTIFIER_MAX_LEN else full_norm
            pattern = r"(?<![a-z0-9])" + re.escape(ident) + r"(?![a-z0-9])"
            if re.search(pattern, haystack):
                matched.append(fiche)
                break
    unique = {f.title_raw: f for f in matched}
    if len(unique) == 0:
        return None
    if len(unique) > 1:
        logger.warning(
            "Correspondance ambiguë entre plusieurs fiches (%s) — traité comme nouveauté, pas de mise à jour automatique",
            ", ".join(unique),
        )
        return None
    return next(iter(unique.values()))


def route_benefit_risk(text_fr: str, risk_keywords: list[str]) -> tuple[str, str]:
    """Répartit les phrases d'un résumé (déjà en français) entre
    "Bénéfices" et "Inconvénients & risques", par simple présence de
    mots-clés de risque configurés — un tri déterministe par mots-clés, pas
    une classification sémantique. Une phrase sans mot-clé de risque va par
    défaut en "Bénéfices" (le principe éditorial du document existant :
    l'absence de risque signalé n'est pas en soi un bénéfice, mais reflète
    l'avancement/l'usage rapporté)."""
    sentences = [s for s in SENTENCE_SPLIT_RE.split(text_fr) if s]
    kws = [k.casefold() for k in risk_keywords]
    benefit, risk = [], []
    for s in sentences:
        s_l = s.casefold()
        (risk if any(k in s_l for k in kws) else benefit).append(s)
    return " ".join(benefit), " ".join(risk)


# Ordre de progression de la maturité : sert à ne jamais RÉTROGRADER une
# fiche (ex. un article sur un essai de phase 3 pour une NOUVELLE indication
# d'un médicament déjà commercialisé ne doit pas faire repasser sa fiche de
# "Commercialisé" à "Phase 3" — un cas réellement observé en test).
MATURITY_RANK = {
    "Préclinique": 0,
    "Phase 1": 1,
    "Phase 2": 2,
    "Phase 3": 3,
    "Commercialisé": 4,
}


@dataclass
class FicheUpdate:
    fiche: Fiche
    new_maturity: Optional[str] = None
    new_country: Optional[str] = None
    benefit_additions: list[str] = field(default_factory=list)
    risk_additions: list[str] = field(default_factory=list)
    source_additions: list[str] = field(default_factory=list)
    # Détail par item source ayant contribué à cette fiche (une entrée par
    # item, même structure que `new_entries`) — sert uniquement à l'affichage
    # email détaillé, en plus des additions agrégées ci-dessus qui pilotent
    # l'écriture réelle dans le Markdown.
    items: list[dict] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(
            self.new_maturity or self.new_country or self.benefit_additions
            or self.risk_additions or self.source_additions
        )

    def display_country(self) -> str:
        """Pays à faire figurer dans le récapitulatif (email, résumé JSON) :
        celui tout juste renseigné cette exécution, sinon celui déjà présent
        sur la fiche (curé manuellement ou renseigné lors d'une exécution
        précédente) — jamais déduit."""
        if self.new_country:
            return self.new_country
        existing = self.fiche.fields.get("Pays")
        if existing and existing.text.strip():
            return existing.text.strip()
        return "non précisé dans la source"


def build_fiche_updates(
    matched_items: list[tuple[RawItem, Fiche]], risk_keywords: list[str], date_format: str
) -> dict[str, FicheUpdate]:
    """Construit, par fiche, le texte à ajouter à chaque champ autorisé —
    toujours en complétant (jamais en effaçant une information déjà
    validée), et toujours à partir d'un extrait traduit, jamais reformulé."""
    updates: dict[str, FicheUpdate] = {}
    by_fiche: dict[str, list[RawItem]] = {}
    fiche_by_title: dict[str, Fiche] = {}
    for item, fiche in matched_items:
        by_fiche.setdefault(fiche.title_raw, []).append(item)
        fiche_by_title[fiche.title_raw] = fiche

    for title_raw, items in by_fiche.items():
        fiche = fiche_by_title[title_raw]
        upd = FicheUpdate(fiche=fiche)
        for item in sorted(items, key=lambda it: it.date):
            date_str = item.date.strftime(date_format)
            extract = extractive_summary(item.summary_src)
            summary_fr = translate_fr(extract) if extract else ""
            benefit_fr, risk_fr = route_benefit_risk(summary_fr, risk_keywords) if summary_fr else ("", "")

            if item.maturity and item.maturity != "non précisé dans la source":
                current = fiche.fields.get("Maturité")
                current_text = current.text.strip() if current else ""
                current_rank = MATURITY_RANK.get(current_text, -1)
                candidate_rank = MATURITY_RANK.get(item.maturity, -1)
                already_ahead = upd.new_maturity and MATURITY_RANK.get(upd.new_maturity, -1) >= candidate_rank
                if current_text.casefold() != item.maturity.casefold() and candidate_rank > current_rank and not already_ahead:
                    upd.new_maturity = item.maturity

            if item.country and item.country != "non précisé dans la source" and not upd.new_country:
                current_country = fiche.fields.get("Pays")
                current_country_text = current_country.text.strip() if current_country else ""
                if not current_country_text or current_country_text == "non précisé dans la source":
                    upd.new_country = item.country

            if benefit_fr:
                upd.benefit_additions.append(f"[{date_str}] {benefit_fr}")
            if risk_fr:
                upd.risk_additions.append(f"[{date_str}] {risk_fr}")

            source_citation = f"{item.source_name} ; {item.source_url}"
            existing_source = fiche.fields.get("Source principale")
            already_present = existing_source and item.source_url in existing_source.text
            if not already_present:
                upd.source_additions.append(source_citation)

            upd.items.append(
                {
                    "date": date_str,
                    "country": item.country,
                    "axis": item.axis or AXIS_FALLBACK,
                    "doc_type": item.doc_type,
                    "maturity": item.maturity,
                    "confidence": item.confidence,
                    "confidence_note": item.confidence_note,
                    "title_fr": translate_fr(item.title_src),
                    "summary_fr": summary_fr or "non précisé dans la source",
                    "benefit_fr": benefit_fr,
                    "risk_fr": risk_fr,
                    "source_name": item.source_name,
                    "source_url": item.source_url,
                }
            )

        if upd.has_changes():
            updates[title_raw] = upd
    return updates


def apply_fiche_updates(markdown_text: str, updates: dict[str, FicheUpdate]) -> str:
    """Réécrit, pour chaque fiche mise à jour, uniquement les lignes des
    champs concernés — en une seule ligne strictement formatée
    `- **Label** : texte`, sans toucher au titre, à "Niveau de confiance",
    ni à aucune autre fiche."""
    if not updates:
        return markdown_text
    lines = markdown_text.split("\n")
    edits: list[tuple[int, int, str]] = []  # (line_start, line_end, new_line)

    for upd in updates.values():
        fiche = upd.fiche
        if upd.new_maturity and "Maturité" in fiche.fields:
            f = fiche.fields["Maturité"]
            edits.append((f.line_start, f.line_end, f"- **Maturité** : {upd.new_maturity}"))
        if upd.new_country and "Pays" in fiche.fields:
            f = fiche.fields["Pays"]
            edits.append((f.line_start, f.line_end, f"- **Pays** : {upd.new_country}"))
        if upd.benefit_additions and "Bénéfices" in fiche.fields:
            f = fiche.fields["Bénéfices"]
            new_text = " ".join([f.text] + upd.benefit_additions) if f.text else " ".join(upd.benefit_additions)
            edits.append((f.line_start, f.line_end, f"- **Bénéfices** : {new_text}"))
        if upd.risk_additions and "Inconvénients & risques" in fiche.fields:
            f = fiche.fields["Inconvénients & risques"]
            new_text = " ".join([f.text] + upd.risk_additions) if f.text else " ".join(upd.risk_additions)
            edits.append((f.line_start, f.line_end, f"- **Inconvénients & risques** : {new_text}"))
        if upd.source_additions and "Source principale" in fiche.fields:
            f = fiche.fields["Source principale"]
            new_text = " ; ".join([f.text] + upd.source_additions) if f.text else " ; ".join(upd.source_additions)
            edits.append((f.line_start, f.line_end, f"- **Source principale** : {new_text}"))

    for line_start, line_end, new_line in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[line_start : line_end + 1] = [new_line]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatage Markdown
# ---------------------------------------------------------------------------

AXIS_FALLBACK = "Non précisé (axe non déterminé automatiquement)"


def build_entry(raw: RawItem, date_format: str) -> Entry:
    summary_extract = extractive_summary(raw.summary_src)
    title_fr = translate_fr(raw.title_src)
    summary_fr = translate_fr(summary_extract) if summary_extract else "non précisé dans la source"
    return Entry(
        date=raw.date,
        country=raw.country,
        title_fr=title_fr,
        axis=raw.axis or AXIS_FALLBACK,
        doc_type=raw.doc_type,
        maturity=raw.maturity,
        summary_fr=summary_fr,
        confidence=raw.confidence,
        confidence_note=raw.confidence_note,
        source_name=raw.source_name,
        source_url=raw.source_url,
    )


def format_entry_md(entry: Entry, date_format: str) -> str:
    date_str = entry.date.strftime(date_format)
    confidence_line = entry.confidence
    if entry.confidence_note:
        confidence_line += f" ({entry.confidence_note})"
    lines = [
        f"#### [{date_str}] [{entry.country}] — {entry.title_fr}",
        "",
        f"- **Axe** : {entry.axis}",
        f"- **Type** : {entry.doc_type}",
        f"- **Maturité** : {entry.maturity}",
        f"- **Résumé (FR)** : {entry.summary_fr}",
        f"- **Niveau de confiance** : {confidence_line}",
        f"- **Source** : [{entry.source_name}]({entry.source_url})",
    ]
    return "\n".join(lines)


SECTION5_RE = re.compile(r"(## 5\. Nouveautés et fil d'actualité\n)")


def insert_entries(markdown_text: str, entries: list[Entry], date_format: str) -> str:
    if not entries:
        return markdown_text
    match = SECTION5_RE.search(markdown_text)
    if not match:
        raise ValueError(
            "Section '## 5. Nouveautés et fil d'actualité' introuvable : "
            "le fichier n'a pas la structure attendue, insertion annulée."
        )
    insertion_point = match.end()
    block = "\n\n".join(format_entry_md(e, date_format) for e in entries)
    new_text = (
        markdown_text[:insertion_point]
        + "\n"
        + block
        + "\n\n"
        + markdown_text[insertion_point:]
    )
    return new_text


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veille scientifique DT1")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="N'écrit rien, affiche le diff")
    parser.add_argument("--window-start", help="AAAA-MM-JJ, pour un test sur fenêtre fixe")
    parser.add_argument("--window-end", help="AAAA-MM-JJ, pour un test sur fenêtre fixe")
    parser.add_argument("--limit", type=int, help="Nombre max d'entrées (override config)")
    parser.add_argument(
        "--ignore-dedup",
        action="store_true",
        help="Ne pas filtrer les éléments déjà vus (utile pour un test sur fenêtre passée)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Journalisation détaillée (niveau DEBUG) pour le débogage : décisions de "
        "classement, tentatives de correspondance fiche/nouveauté, etc.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Force l'envoi d'un email de notification (même en --dry-run, même sans "
        "changement) pour valider la configuration SMTP.",
    )
    return parser.parse_args(argv)


class WarningCollector(logging.Handler):
    """Capture tous les messages WARNING/ERROR d'une exécution, pour les reprendre
    dans le résumé JSON (et donc dans la notification Windows) sans avoir à
    reparser le fichier de log."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def setup_logging(log_file: Path, debug: bool = False) -> WarningCollector:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Rotation automatique : le journal ne grossit jamais indéfiniment (2 Mo par
    # fichier, 10 fichiers conservés en historique, soit plusieurs mois de recul
    # à raison d'une exécution par semaine).
    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=10, encoding="utf-8")
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    collector = WarningCollector()
    collector.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.addHandler(collector)
    return collector


def write_summary(path: Path, summary: dict) -> None:
    """Écrit un résumé JSON de la dernière exécution (logs/last_run_summary.json) :
    compte-rendu structuré, exploitable aussi bien par un humain qui débogue que
    par le script de notification Windows (run_veille.ps1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)


def _build_email_body(summary: dict) -> str:
    lines = [
        f"Veille scientifique DT1 — {'échec' if summary.get('error') else 'résultat'} de l'exécution",
        f"Fenêtre : {summary.get('window_start')} -> {summary.get('window_end')}",
        "",
    ]
    if summary.get("error"):
        lines.append(f"ERREUR : {summary['error']}")
        lines.append("Voir logs\\veille.log pour le détail complet.")
        return "\n".join(lines)

    fiches = summary.get("fiches_updated", [])
    entries = summary.get("new_entries", [])
    separator = "-" * 60

    def _item_lines(item: dict, title_key: str = "title_fr") -> list[str]:
        out = [f"* [{item.get('date', '?')}] [{item.get('country', 'non précisé dans la source')}] [{item.get('axis', '?')}] {item.get(title_key, '')}"]
        out.append(f"  Type : {item.get('doc_type', '?')}  |  Maturité : {item.get('maturity', '?')}")
        confidence_line = item.get("confidence", "?")
        if item.get("confidence_note"):
            confidence_line += f" ({item['confidence_note']})"
        out.append(f"  Confiance : {confidence_line}")
        if item.get("benefit_fr"):
            out.append(f"  Bénéfice : {item['benefit_fr']}")
        if item.get("risk_fr"):
            out.append(f"  Risque : {item['risk_fr']}")
        out.append(f"  Résumé : {item.get('summary_fr') or item.get('summary', 'non précisé dans la source')}")
        out.append(f"  Source : {item.get('source_name', '?')} — {item.get('source_url', '')}")
        return out

    if fiches:
        lines.append(f"FICHES MISES À JOUR ({len(fiches)})")
        lines.append(separator)
        for f in fiches:
            lines.append(f"### {f['title']} (fiche existante)")
            # Idem version HTML : résumé condensé seulement utile quand
            # plusieurs items alimentent la même fiche (sinon doublon avec
            # la carte de l'unique item juste en dessous).
            if len(f.get("items", [])) > 1:
                lines.append(f"  Pays : {f.get('country', 'non précisé dans la source')}")
                if f.get("new_maturity"):
                    lines.append(f"  Nouvelle maturité retenue : {f['new_maturity']}")
                for s in f.get("source_additions", []):
                    lines.append(f"  Nouvelle source : {s}")
            lines.append("")
            for item in f.get("items", []):
                lines.extend(_item_lines(item))
                lines.append("")
        lines.append("")

    if entries:
        lines.append(f"NOUVELLES ENTRÉES ({len(entries)})")
        lines.append(separator)
        for e in entries:
            lines.extend(_item_lines(e, title_key="title"))
            lines.append("")
        lines.append("")

    if not fiches and not entries:
        lines.append("Aucune nouveauté cette semaine.")
        lines.append("")

    if summary.get("warnings_count"):
        lines.append(f"{summary['warnings_count']} avertissement(s) — voir logs\\veille.log pour le détail.")
        lines.append("")

    lines.append("Document : diabete-type1-veille.md")
    lines.append("Journal détaillé : logs\\veille.log")
    return "\n".join(lines)


_CONFIDENCE_COLORS = {
    "Élevé": ("#e6f4ea", "#1e7e34"),
    "Moyen": ("#fff4e5", "#b25e00"),
    "Faible": ("#fdecea", "#c62828"),
}


def _confidence_badge(confidence: str) -> str:
    bg, fg = _CONFIDENCE_COLORS.get(confidence, ("#eceff1", "#455a64"))
    return (
        f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;'
        f'background:{bg};color:{fg};font-weight:bold;font-size:12px;">{html.escape(confidence or "?")}</span>'
    )


_AXIS_COLORS = {
    "Biologique": ("#f3e5f5", "#6a1b9a"),
    "Mécanique": ("#e3f2fd", "#1565c0"),
    "Immunothérapie": ("#e0f2f1", "#00695c"),
}


def _axis_badge(axis: str) -> str:
    bg, fg = _AXIS_COLORS.get(axis, ("#eceff1", "#455a64"))
    return (
        f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;'
        f'background:{bg};color:{fg};font-weight:bold;font-size:12px;">{html.escape(axis or "?")}</span>'
    )


def _item_card_html(item: dict, title_key: str) -> str:
    """Rendu HTML d'un item (nouvelle entrée, ou item source d'une fiche mise
    à jour) — même gabarit dans les deux cas : titre, date/pays/axe,
    type|maturité|confiance, bénéfice, risque, résumé, source."""
    esc = html.escape
    confidence_note = (
        f' <span style="color:#777;font-size:12px;">({esc(item["confidence_note"])})</span>'
        if item.get("confidence_note")
        else ""
    )
    parts = ['<div style="margin:0 0 16px 0;padding:0 0 10px 0;border-bottom:1px solid #eee;">']
    parts.append(
        '<div style="font-size:13px;color:#555;margin-bottom:2px;">'
        f'<b>{esc(item.get("date", ""))}</b> &middot; {esc(item.get("country", "non précisé dans la source"))} '
        f'&middot; {_axis_badge(item.get("axis", "?"))}</div>'
    )
    parts.append(f'<div style="font-weight:bold;font-size:14px;margin-bottom:2px;">{esc(item.get(title_key, ""))}</div>')
    parts.append(
        f'<div style="font-size:13px;color:#555;margin-bottom:4px;">Type : <b>{esc(item.get("doc_type", "?"))}</b>'
        f' &nbsp;|&nbsp; Maturité : <b>{esc(item.get("maturity", "?"))}</b>'
        f' &nbsp;|&nbsp; Confiance : {_confidence_badge(item.get("confidence", "?"))}{confidence_note}</div>'
    )
    if item.get("benefit_fr"):
        parts.append(f'<div style="font-size:13px;color:#1e7e34;margin-bottom:2px;">✅ <b>Bénéfice</b> : {esc(item["benefit_fr"])}</div>')
    if item.get("risk_fr"):
        parts.append(f'<div style="font-size:13px;color:#c62828;margin-bottom:2px;">⚠️ <b>Risque</b> : {esc(item["risk_fr"])}</div>')
    summary_text = item.get("summary_fr") or item.get("summary", "non précisé dans la source")
    parts.append(f'<div style="font-size:13px;margin-bottom:4px;">{esc(summary_text)}</div>')
    source_url = item.get("source_url", "")
    source_name = esc(item.get("source_name", "?"))
    if source_url:
        parts.append(
            f'<div style="font-size:12px;color:#777;">Source : {source_name} — '
            f'<a href="{esc(source_url)}" style="color:#1a5276;">{esc(source_url)}</a></div>'
        )
    else:
        parts.append(f'<div style="font-size:12px;color:#777;">Source : {source_name}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def _build_email_body_html(summary: dict) -> str:
    """Version HTML du corps de mail (gras/couleurs/soulignés) pour repérer les
    infos en un coup d'œil. Tout le texte dynamique (issu des sources externes)
    est échappé via html.escape avant insertion — jamais de HTML brut injecté."""
    esc = html.escape
    is_error = bool(summary.get("error"))
    title = f"Veille scientifique DT1 — {'échec' if is_error else 'résultat'} de l'exécution"
    title_color = "#c62828" if is_error else "#1a5276"

    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;color:#222;max-width:680px;">',
        f'<h2 style="color:{title_color};margin:0 0 4px 0;">{esc(title)}</h2>',
        f'<p style="color:#555;margin:0 0 16px 0;">Fenêtre : <b>{esc(str(summary.get("window_start")))}</b> '
        f'&rarr; <b>{esc(str(summary.get("window_end")))}</b></p>',
    ]

    if is_error:
        parts.append(
            '<div style="background:#fdecea;border-left:4px solid #c62828;padding:10px 14px;margin-bottom:16px;">'
            f'<b>ERREUR :</b> {esc(str(summary["error"]))}<br>'
            '<span style="color:#555;">Voir <code>logs\\veille.log</code> pour le détail complet.</span>'
            "</div>"
        )
        parts.append("</div>")
        return "\n".join(parts)

    fiches = summary.get("fiches_updated", [])
    entries = summary.get("new_entries", [])

    def section_header(text: str, color: str) -> str:
        return (
            f'<h3 style="background:{color};color:#fff;padding:6px 10px;margin:20px 0 10px 0;'
            f'border-radius:4px;font-size:15px;">{esc(text)}</h3>'
        )

    if fiches:
        parts.append(section_header(f"FICHES MISES À JOUR ({len(fiches)})", "#1a5276"))
        for f in fiches:
            parts.append(
                f'<div style="font-weight:bold;font-size:14px;margin:0 0 4px 0;">'
                f'🔄 {esc(f["title"])} <span style="font-weight:normal;color:#777;font-size:12px;">(fiche existante)</span></div>'
            )
            # Résumé des changements réellement appliqués à la fiche existante
            # dans le document. Avec un seul item contributeur, ce résumé fait
            # doublon avec sa carte ci-dessous (mêmes valeurs) : on ne l'affiche
            # que si plusieurs items alimentent la même fiche ce run-ci, cas où
            # ce résumé condensé apporte une info que les cartes individuelles
            # n'exposent pas directement (quelle valeur a été retenue au final).
            if len(f.get("items", [])) > 1:
                changes = []
                if f.get("new_maturity"):
                    changes.append(f'<u>Nouvelle maturité retenue</u> : <b>{esc(f["new_maturity"])}</b>')
                if f.get("new_country"):
                    changes.append(f'<u>Pays précisé</u> : <b>{esc(f["new_country"])}</b>')
                for s in f.get("source_additions", []):
                    changes.append(f'🔗 <b>Nouvelle source</b> : {esc(s)}')
                if changes:
                    parts.append(f'<div style="font-size:12px;color:#555;margin:0 0 8px 0;">{"<br>".join(changes)}</div>')
            for item in f.get("items", []):
                parts.append(_item_card_html(item, title_key="title_fr"))

    if entries:
        parts.append(section_header(f"NOUVELLES ENTRÉES ({len(entries)})", "#0e7c7b"))
        for e in entries:
            parts.append(_item_card_html(e, title_key="title"))

    if not fiches and not entries:
        parts.append('<p style="color:#555;">Aucune nouveauté cette semaine.</p>')

    if summary.get("warnings_count"):
        parts.append(
            f'<p style="color:#b25e00;"><b>{esc(str(summary["warnings_count"]))} avertissement(s)</b> — '
            "voir <code>logs\\veille.log</code> pour le détail.</p>"
        )

    parts.append(
        '<p style="color:#999;font-size:12px;margin-top:20px;">Document : <b>diabete-type1-veille.md</b><br>'
        "Journal détaillé : <code>logs\\veille.log</code></p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def send_email_notification(cfg: dict, summary: dict, force: bool = False) -> None:
    """Envoie un email de notification si configuré. N'échoue jamais bruyamment :
    une erreur d'envoi (identifiants manquants, réseau, etc.) est journalisée en
    warning mais ne doit jamais faire échouer l'exécution principale — l'email
    est un confort, pas une garantie."""
    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled", False):
        return

    has_news_or_error = bool(summary.get("fiches_updated") or summary.get("new_entries") or summary.get("error"))
    if not force and email_cfg.get("only_if_changes", True) and not has_news_or_error:
        logger.info("Email : rien de nouveau et only_if_changes actif, aucun envoi.")
        return

    creds_path = BASE_DIR / email_cfg.get("credentials_file", "email_credentials.yaml")
    if not creds_path.exists():
        logger.warning(
            "Email : fichier d'identifiants introuvable (%s) — voir email_credentials.example.yaml. "
            "Notification email ignorée.",
            creds_path,
        )
        return

    # Accepte to_addrs (liste, recommandé) ou l'ancien to_addr (chaîne unique),
    # pour un ou plusieurs destinataires.
    to_addrs = email_cfg.get("to_addrs")
    if not to_addrs:
        single = email_cfg.get("to_addr")
        to_addrs = [single] if single else []
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    if not to_addrs:
        logger.warning("Email : aucun destinataire configuré (to_addrs vide) — envoi ignoré.")
        return

    try:
        with open(creds_path, "r", encoding="utf-8") as fh:
            creds = yaml.safe_load(fh) or {}
        # Google affiche le mot de passe d'application avec des espaces tous les 4
        # caractères (lisibilité), mais le secret réel n'en contient pas — les
        # conserver fait échouer l'authentification SMTP (535 BadCredentials,
        # rencontré en test).
        app_password = str(creds["app_password"]).replace(" ", "").strip()

        subject_prefix = "Veille DT1"
        if summary.get("error"):
            subject = f"{subject_prefix} — ÉCHEC de l'exécution"
        elif summary.get("fiches_updated") or summary.get("new_entries"):
            n = len(summary.get("fiches_updated", [])) + len(summary.get("new_entries", []))
            subject = f"{subject_prefix} — {n} nouveauté(s)"
        else:
            subject = f"{subject_prefix} — aucune nouveauté"

        # multipart/alternative : la partie texte sert de repli pour les clients
        # qui n'affichent pas le HTML, la partie HTML (mise en forme gras/couleurs)
        # doit être ajoutée en dernier car les clients affichent la dernière
        # alternative compatible.
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(_build_email_body(summary), "plain", "utf-8"))
        msg.attach(MIMEText(_build_email_body_html(summary), "html", "utf-8"))
        msg["Subject"] = subject
        msg["From"] = email_cfg["from_addr"]
        msg["To"] = ", ".join(to_addrs)

        with smtplib.SMTP(email_cfg.get("smtp_host", "smtp.gmail.com"), email_cfg.get("smtp_port", 587), timeout=30) as server:
            server.starttls()
            server.login(email_cfg["from_addr"], app_password)
            server.send_message(msg, from_addr=email_cfg["from_addr"], to_addrs=to_addrs)
        logger.info("Email de notification envoyé à %s.", ", ".join(to_addrs))
    except Exception as exc:  # pragma: no cover - dépend du réseau/des identifiants
        logger.warning("Email : envoi impossible (%s). L'exécution continue normalement.", exc)


def _run(args: argparse.Namespace, cfg: dict, summary: dict) -> int:
    """Corps de l'exécution. Remplit `summary` au fur et à mesure — appelé
    depuis `main()`, qui garantit que ce résumé est écrit sur disque même en
    cas d'échec inattendu (voir `write_summary`)."""
    if args.window_end:
        window_end = dateparser.parse(args.window_end).replace(tzinfo=timezone.utc)
    else:
        window_end = datetime.now(timezone.utc)
    if args.window_start:
        window_start = dateparser.parse(args.window_start).replace(tzinfo=timezone.utc)
    else:
        window_start = window_end - timedelta(days=cfg.get("window_days", 15))

    summary["window_start"] = window_start.date().isoformat()
    summary["window_end"] = window_end.date().isoformat()
    logger.info("Fenêtre de veille : %s -> %s", window_start.date(), window_end.date())

    dedup_path = BASE_DIR / cfg.get("dedup_store", "dedup_store.json")
    dedup_store = load_dedup(dedup_path)
    seen = set(dedup_store.get("seen", []))

    raw_items: list[RawItem] = []
    for label, key, fetcher in (
        ("PubMed", "pubmed", lambda: fetch_pubmed(cfg, window_start, window_end)),
        ("ClinicalTrials.gov", "clinicaltrials", lambda: fetch_clinicaltrials(cfg, window_start, window_end)),
        ("RSS", "rss", lambda: fetch_rss(cfg, window_start, window_end)),
    ):
        try:
            results, filtered_out = fetcher()
            logger.info("%s : %d élément(s) dans la fenêtre", label, len(results))
            raw_items.extend(results)
            summary["sources"][key] = {"found": len(results), "filtered_out": filtered_out, "error": None}
        except Exception as exc:
            logger.error("Source %s en échec complet : %s", label, exc)
            summary["sources"][key] = {"found": 0, "filtered_out": 0, "error": str(exc)}

    if not args.ignore_dedup:
        new_items = [it for it in raw_items if it.dedup_key not in seen]
    else:
        new_items = raw_items
    summary["found_total"] = len(raw_items)
    summary["after_dedup"] = len(new_items)
    logger.info("%d nouvel(le)s élément(s) après déduplication", len(new_items))

    new_items.sort(key=lambda it: it.date, reverse=True)
    limit = args.limit or cfg.get("max_entries_per_run", 25)
    new_items = new_items[:limit]

    if not new_items:
        logger.info("Aucune nouveauté à insérer, fichier non modifié.")
        return 0

    date_format = cfg.get("date_format", "%d/%m/%Y")
    md_path = BASE_DIR / cfg.get("markdown_file", "diabete-type1-veille.md")
    original_text = md_path.read_text(encoding="utf-8")

    fiches = parse_fiches(original_text)
    logger.info("%d fiche(s) technologie existante(s) repérée(s) (sections 2-4)", len(fiches))

    # --- Logique à deux niveaux ---------------------------------------
    # 1) correspondance exacte avec une fiche existante -> mise à jour de
    #    fiche (jamais si confiance Faible : on ne laisse pas une rumeur
    #    non confirmée modifier une fiche curée) ;
    # 2) sinon -> nouvelle entrée en section 5. Jamais les deux à la fois.
    matched_items: list[tuple[RawItem, Fiche]] = []
    unmatched_items: list[RawItem] = []
    for item in new_items:
        fiche = match_item_to_fiche(item.title_src, f"{item.title_src} {item.summary_src}", fiches)
        logger.debug(
            "Correspondance pour %r : %s (confiance=%s)",
            item.title_src[:80],
            fiche.title_raw if fiche else "aucune -> nouvelle entrée",
            item.confidence,
        )
        if fiche is not None and item.confidence != "Faible":
            matched_items.append((item, fiche))
        else:
            unmatched_items.append(item)

    fiche_updates = build_fiche_updates(matched_items, cfg.get("risk_keywords", []), date_format)
    logger.info(
        "%d nouveauté(s) associée(s) à %d fiche(s) existante(s) mise(s) à jour ; %d nouvelle(s) entrée(s) en section 5",
        len(matched_items),
        len(fiche_updates),
        len(unmatched_items),
    )
    summary["fiches_updated"] = [
        {
            "title": upd.fiche.title_raw,
            "country": upd.display_country(),
            "new_maturity": upd.new_maturity,
            "new_country": upd.new_country,
            "benefit_additions": upd.benefit_additions,
            "risk_additions": upd.risk_additions,
            "source_additions": upd.source_additions,
            "items": upd.items,
        }
        for upd in fiche_updates.values()
    ]

    updated_text = apply_fiche_updates(original_text, fiche_updates)
    entries = [build_entry(it, date_format) for it in unmatched_items]
    new_text = insert_entries(updated_text, entries, date_format)

    # Bénéfice/risque calculés ici uniquement pour l'affichage (email, JSON de
    # résumé) — jamais écrits dans le document Markdown : le format des
    # nouvelles entrées en section 5 (format_entry_md) n'a pas de champ dédié,
    # seul un "Résumé" global y figure, par choix éditorial du document.
    risk_keywords = cfg.get("risk_keywords", [])

    def _entry_dict(e: Entry) -> dict:
        benefit_fr, risk_fr = route_benefit_risk(e.summary_fr, risk_keywords) if e.summary_fr else ("", "")
        return {
            "date": e.date.strftime(date_format),
            "country": e.country,
            "title": e.title_fr,
            "axis": e.axis,
            "doc_type": e.doc_type,
            "maturity": e.maturity,
            "summary": e.summary_fr,
            "confidence": e.confidence,
            "confidence_note": e.confidence_note,
            "source_name": e.source_name,
            "source_url": e.source_url,
            "benefit_fr": benefit_fr,
            "risk_fr": risk_fr,
        }

    summary["new_entries"] = [_entry_dict(e) for e in entries]

    if args.dry_run:
        diff = difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(md_path),
            tofile=str(md_path) + " (proposé)",
        )
        sys.stdout.writelines(diff)
        logger.info(
            "Mode dry-run : aucune écriture effectuée (%d fiche(s) modifiée(s), %d nouvelle(s) entrée(s)).",
            len(fiche_updates),
            len(entries),
        )
        return 0

    md_path.write_text(new_text, encoding="utf-8")
    dedup_store["seen"] = sorted(seen | {it.dedup_key for it in new_items})
    dedup_store["last_run"] = datetime.now(timezone.utc).isoformat()
    dedup_store["known_fiches"] = [
        {"title": f.title_raw, "axis": f.axis, "identifiers": f.identifiers} for f in fiches
    ]
    save_dedup(dedup_path, dedup_store)
    logger.info(
        "%d fiche(s) mise(s) à jour, %d nouvelle(s) entrée(s) insérée(s) dans %s",
        len(fiche_updates),
        len(entries),
        md_path,
    )
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    cfg = load_config(config_path)

    collector = setup_logging(BASE_DIR / cfg.get("log_file", "logs/veille.log"), debug=args.debug)

    summary: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "window_start": None,
        "window_end": None,
        "sources": {},
        "found_total": 0,
        "after_dedup": 0,
        "fiches_updated": [],
        "new_entries": [],
        "warnings_count": 0,
        "warnings": [],
        "error": None,
    }

    summary_path = BASE_DIR / "logs" / "last_run_summary.json"
    try:
        exit_code = _run(args, cfg, summary)
    except Exception as exc:
        logger.exception("Échec inattendu de l'exécution")
        summary["error"] = str(exc)
        exit_code = 1

    summary["warnings"] = collector.records
    summary["warnings_count"] = len(collector.records)
    write_summary(summary_path, summary)

    # L'email est un effet de bord persistant (contrairement à la notification
    # Windows, éphémère) : jamais envoyé en --dry-run, sauf --test-email qui
    # sert justement à valider la configuration SMTP sans attendre une vraie
    # nouveauté.
    if args.test_email:
        send_email_notification(cfg, summary, force=True)
    elif not args.dry_run:
        send_email_notification(cfg, summary)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
