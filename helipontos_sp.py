#!/usr/bin/env python3
"""
Helipontos SP — Cruzamento de dados de helipontos da cidade de São Paulo.

Compara três fontes oficiais para identificar status de operação e
discrepâncias de licenciamento:

Fontes:
  - ANAC: Portal de Dados Abertos (CSV de Aeródromos Privados / Helipontos)
  - GeoSampa: WFS da Prefeitura de São Paulo (camada de helipontos / EIV-RIV)
  - SMUL: Planilha de Autos de Licença de Funcionamento emitidos pela
    Secretaria Municipal de Urbanismo e Licenciamento (Decreto nº 58.094/2018)
"""

import csv
import io
import json
import logging
import re
import sys
import time
import unicodedata
import warnings
from pathlib import Path

import folium
from branca.element import IFrame
import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

warnings.filterwarnings("ignore", category=FutureWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRS_WGS84 = "EPSG:4326"
CRS_UTM23S = "EPSG:31983"
BUFFER_METROS = 100

# ANAC download URLs — tried in order until one succeeds.
ANAC_URLS = [
    # V1 helipontos-specific CSV
    (
        "https://sistemas.anac.gov.br/dadosabertos/Aerodromos/"
        "Aer%C3%B3dromos%20Privados/Lista%20de%20aer%C3%B3dromos%20privados/"
        "Helipontos/Helipontos.csv"
    ),
    # V1 all private aerodromes CSV (includes helipontos)
    (
        "https://sistemas.anac.gov.br/dadosabertos/Aerodromos/"
        "Aer%C3%B3dromos%20Privados/Lista%20de%20aer%C3%B3dromos%20privados/"
        "Aerodromos%20Privados/AerodromosPrivados.csv"
    ),
    # Gov.br hosted copy
    (
        "https://www.gov.br/anac/pt-br/acesso-a-informacao/dados-abertos/"
        "areas-de-atuacao/aerodromos/aerodromos-privados/"
        "lista-de-aerodromos-privados-1/helipontos.csv/@@download/file"
    ),
]

# DECEA WFS — official aeronautical data from Brazilian Air Force (fallback).
DECEA_WFS_URL = (
    "https://geoaisweb.decea.mil.br/geoserver/ICA/ows?"
    "service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=ICA:heliport&outputFormat=application%2Fjson"
    "&maxFeatures=5000"
)

# GeoSampa WFS — layer names to try in order.
GEOSAMPA_WFS_BASE = (
    "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/ows"
)
GEOSAMPA_LAYER_NAMES = [
    "geoportal:GEOSAMPA_heliponto",
    "geoportal:riv_heliponto",
    "geoportal:heliponto",
]

# SMUL — Planilha de Autos de Licença de Funcionamento emitidos pela Prefeitura.
SMUL_XLSX_URL = (
    "https://prefeitura.sp.gov.br/documents/d/licenciamento/"
    "auto_de_licenca_de_helipontos_emitidos_por_smul_fev-2026-xlsx"
)

# Output files
OUTPUT_CSV = "comparativo_helipontos_sp.csv"
OUTPUT_MAP = "mapa_helipontos_sp.html"
DASHBOARD_URL = "relatorio_helipontos_sp.html"  # relativo ao mapa (mesmo dir)
AISWEB_CACHE = "aisweb_helipontos_cache.json"
AISWEB_BASE_URL = "https://aisweb.decea.mil.br/?i=aerodromos&codigo="

# ROTAER superfície: código → nome legível (ROTAER AIP-Brasil)
ROTAER_SUPERFICIE = {
    "CONC": "Concreto",
    "ASPH": "Asfalto",
    "ASF": "Asfalto/Concreto asfáltico",
    "GRA": "Grama",
    "ARE": "Areia",
    "TER": "Terra",
    "SAI": "Saibro",
    "CIN": "Cinza",
    "MAC": "Macadame",
    "MET": "Metálico",
    "MTAL": "Metálico",
    "ACO": "Aço",
}

# ---------------------------------------------------------------------------
# 1. Coordinate conversion  (GMS → Decimal)
# ---------------------------------------------------------------------------


def dms_to_decimal(raw: str) -> float | None:
    """Convert a DMS (Graus Minutos Segundos) string to decimal degrees.

    Handles multiple ANAC formats:
      - ``023°35'10,0"S``   (typical V1)
      - ``023° 35' 10'' S`` (older format)
      - ``233510S``         (compact)
      - ``-23.585``         (already decimal)

    Returns a signed float (negative for S/W/O) or None on failure.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None

    text = str(raw).strip()
    if not text:
        return None

    # Already a simple float?
    try:
        val = float(text.replace(",", "."))
        return val
    except ValueError:
        pass

    # Determine hemisphere sign
    text_upper = text.upper()
    sign = -1 if any(h in text_upper for h in ("S", "W", "O")) else 1

    # Remove hemisphere letters and whitespace
    cleaned = re.sub(r"[NSWEWO]", "", text_upper).strip()

    # Try structured DMS: 023°35'10,0"  or  023° 35' 10''
    m = re.match(
        r"(\d{2,3})\s*[°º]\s*(\d{1,2})\s*['\u2019′]\s*"
        r"([\d,.]+)\s*(?:[\"''\u201D″]{1,2})?\s*$",
        cleaned,
    )
    if m:
        deg = int(m.group(1))
        minutes = int(m.group(2))
        sec = float(m.group(3).replace(",", "."))
        return sign * (deg + minutes / 60.0 + sec / 3600.0)

    # Compact format: 2335100  (DDMMSS.s packed) — 7 digits with tenths
    m = re.match(r"(\d{2,3})(\d{2})(\d{2}[,.]?\d*)", cleaned)
    if m:
        deg = int(m.group(1))
        minutes = int(m.group(2))
        sec = float(m.group(3).replace(",", "."))
        return sign * (deg + minutes / 60.0 + sec / 3600.0)

    log.warning("Could not parse coordinate: %r", raw)
    return None


# ---------------------------------------------------------------------------
# 2. Data acquisition — ANAC
# ---------------------------------------------------------------------------


def _download_with_retries(url: str, timeout: int = 30) -> requests.Response | None:
    """Try downloading *url* with simple retry logic."""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            log.warning(
                "HTTP %s from %s (attempt %d)", resp.status_code, url, attempt + 1
            )
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s (attempt %d)", url, exc, attempt + 1)
    return None


# ---------------------------------------------------------------------------
# AISWEB — Dimensões e peso máximo (ROTAER)
# ---------------------------------------------------------------------------

# Regex ROTAER: ( 18x18 CONC 3.0t L30 ) — dimensões, superfície, MTOW
# HTML pode ter tags entre elementos, ex: 3.0t<span>L30</span>
_RE_ROTAER = re.compile(
    r"\(\s*(\d+)\s*x\s*(\d+)\s+(\w+)\s+([\d,.]+)\s*t\s+",
    re.IGNORECASE | re.DOTALL,
)


def _parse_aisweb_rotaer(html: str) -> dict | None:
    """Extrai dimensões e MTOW do HTML da página AISWEB/ROTAER."""
    m = _RE_ROTAER.search(html)
    if not m:
        return None
    try:
        dim1, dim2 = int(m.group(1)), int(m.group(2))
        superficie = m.group(3).upper()
        mtow = float(m.group(4).replace(",", "."))
        dim_text = f"{dim1}×{dim2} m"  # × = multiplicação (U+00D7)
        sup_nome = ROTAER_SUPERFICIE.get(superficie[:4], ROTAER_SUPERFICIE.get(superficie[:3], superficie))
        return {
            "dimensoes": dim_text,
            "superficie": sup_nome,
            "mtow_ton": mtow,
            "mtow_display": f"{mtow:.1f} t",
        }
    except (ValueError, IndexError):
        return None


def _fetch_aisweb_oaci(codigo: str) -> dict | None:
    """Busca dimensões e MTOW no AISWEB para um código OACI."""
    if not codigo or not isinstance(codigo, str) or not codigo.strip():
        return None
    codigo = codigo.strip().upper()
    url = f"{AISWEB_BASE_URL}{codigo}"
    resp = _download_with_retries(url, timeout=25)
    if resp is None:
        return None
    return _parse_aisweb_rotaer(resp.text)


def load_aisweb_cache(cache_path: str = AISWEB_CACHE) -> dict:
    """Carrega cache de dimensões/MTOW do AISWEB."""
    p = Path(cache_path)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_aisweb_cache(cache: dict, cache_path: str = AISWEB_CACHE) -> None:
    """Salva cache de dimensões/MTOW."""
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
    except OSError as exc:
        log.warning("Could not save AISWEB cache: %s", exc)


def enrich_with_aisweb(
    gdf: gpd.GeoDataFrame,
    fetch_missing: bool = True,
    cache_path: str = AISWEB_CACHE,
) -> gpd.GeoDataFrame:
    """Enriquece GeoDataFrame com dimensões e MTOW do AISWEB."""
    cache = load_aisweb_cache(cache_path)
    gdf = gdf.copy()
    gdf["aisweb_dimensoes"] = None
    gdf["aisweb_mtow"] = None
    gdf["aisweb_superficie"] = None

    # Coletar OACIs únicos
    def _get_oaci(row):
        o = row.get("anac_oaci") or row.get("gs_oaci")
        if pd.notna(o) and str(o).strip():
            return str(o).strip().upper()
        return None

    oacis = set()
    for _, row in gdf.iterrows():
        o = _get_oaci(row)
        if o:
            oacis.add(o)

    n_fetched = 0
    for oaci in sorted(oacis):
        if oaci in cache:
            data = cache[oaci]
        elif fetch_missing:
            data = _fetch_aisweb_oaci(oaci)
            if data:
                cache[oaci] = data
                n_fetched += 1
                save_aisweb_cache(cache, cache_path)  # salva a cada novo
                time.sleep(0.15)  # rate limit para não sobrecarregar AISWEB
        else:
            data = None

        if data:
            mask = gdf.apply(lambda r: _get_oaci(r) == oaci, axis=1)
            gdf.loc[mask, "aisweb_dimensoes"] = data.get("dimensoes")
            gdf.loc[mask, "aisweb_mtow"] = data.get("mtow_display")
            gdf.loc[mask, "aisweb_superficie"] = data.get("superficie")
        log.info("AISWEB: fetched %d new records, cached %d total", n_fetched, len(cache))

    n_with_data = gdf["aisweb_dimensoes"].notna().sum()
    log.info("AISWEB: %d heliports with dimensions/MTOW data", int(n_with_data))

    return gdf


def load_anac(local_csv: str | None = None) -> gpd.GeoDataFrame:
    """Load ANAC heliport data, filter to São Paulo city, return GeoDataFrame."""

    df: pd.DataFrame | None = None

    # --- Try local file first ---
    if local_csv and Path(local_csv).exists():
        log.info("Loading ANAC data from local file: %s", local_csv)
        for sep in [";", ","]:
            try:
                df = pd.read_csv(
                    local_csv, sep=sep, encoding="utf-8-sig",
                    quoting=csv.QUOTE_NONE,
                )
                if len(df.columns) > 3:
                    break
            except Exception:
                continue

    # --- Try remote URLs ---
    if df is None or len(df.columns) <= 3:
        for url in ANAC_URLS:
            log.info("Trying ANAC URL: %s", url)
            resp = _download_with_retries(url)
            if resp is None:
                continue
            content = resp.content.decode("utf-8-sig", errors="replace")
            for sep in [";", ","]:
                try:
                    candidate = pd.read_csv(
                        io.StringIO(content), sep=sep,
                        quoting=csv.QUOTE_NONE,
                    )
                    if len(candidate.columns) > 3:
                        df = candidate
                        log.info(
                            "Loaded ANAC data from URL (%d rows, %d cols)",
                            len(df),
                            len(df.columns),
                        )
                        break
                except Exception:
                    continue
            if df is not None and len(df.columns) > 3:
                break

    # --- Fallback: DECEA WFS (official aeronautical GeoServer) ---
    if df is None or df.empty:
        log.info("Trying DECEA WFS as fallback for ANAC data...")
        df = _load_anac_from_decea()

    if df is None or df.empty:
        log.error(
            "Could not load ANAC data from any source. "
            "Place a local CSV as 'anac_helipontos.csv' and re-run."
        )
        sys.exit(1)

    # Normalise column names to uppercase for matching
    df.columns = [c.strip().upper() for c in df.columns]

    log.info("ANAC raw columns: %s", list(df.columns))
    log.info("ANAC raw rows: %d", len(df))

    # --- Identify key columns via heuristics ---
    col_map = _detect_anac_columns(df)
    log.info("ANAC column mapping: %s", col_map)

    # --- Filter to UF=SP, MUNICÍPIO=SÃO PAULO ---
    if col_map["uf"]:
        uf_vals = df[col_map["uf"]].astype(str).str.strip().str.upper()
        df = df[uf_vals.isin(["SP"]) | uf_vals.str.contains("S.O PAULO", regex=True, na=False)]
    if col_map["municipio"]:
        df = df[
            df[col_map["municipio"]]
            .astype(str)
            .str.strip()
            .str.upper()
            .str.contains("S.O PAULO|SAO PAULO|SÃO PAULO", regex=True, na=False)
        ]

    log.info("ANAC rows after SP / São Paulo filter: %d", len(df))

    if df.empty:
        log.error("No ANAC records for São Paulo found.")
        sys.exit(1)

    # --- Parse coordinates ---
    lat_col = col_map["lat"]
    lon_col = col_map["lon"]
    df["lat_dec"] = df[lat_col].apply(dms_to_decimal)
    df["lon_dec"] = df[lon_col].apply(dms_to_decimal)

    # Ensure São Paulo coordinates are negative
    df["lat_dec"] = df["lat_dec"].apply(lambda v: -abs(v) if v and v != 0 else v)
    df["lon_dec"] = df["lon_dec"].apply(lambda v: -abs(v) if v and v != 0 else v)

    df = df.dropna(subset=["lat_dec", "lon_dec"])

    geometry = [Point(xy) for xy in zip(df["lon_dec"], df["lat_dec"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=CRS_WGS84)

    # Rename useful columns for downstream use
    rename = {}
    if col_map["nome"]:
        rename[col_map["nome"]] = "anac_nome"
    if col_map["oaci"]:
        rename[col_map["oaci"]] = "anac_oaci"
    if col_map["ciad"]:
        rename[col_map["ciad"]] = "anac_ciad"

    # Detect operation / status column
    status_col = col_map.get("operacao") or col_map.get("validade")
    if status_col:
        rename[status_col] = "anac_status_raw"

    # Always expose the validade column as anac_validade
    val_col = col_map.get("validade")
    if val_col and val_col not in rename:
        rename[val_col] = "anac_validade"

    gdf = gdf.rename(columns=rename)

    # Parse anac_validade as datetime
    if "anac_validade" in gdf.columns:
        gdf["anac_validade"] = pd.to_datetime(
            gdf["anac_validade"].astype(str).str.replace(r"Z$", "", regex=True),
            errors="coerce",
        )
    elif val_col and val_col in rename.values():
        # validade was used as anac_status_raw — copy it to anac_validade too
        gdf["anac_validade"] = pd.to_datetime(
            gdf["anac_status_raw"].astype(str).str.replace(r"Z$", "", regex=True),
            errors="coerce",
        )

    # Derive active/inactive flag from Operação status only.
    # Note: "Validade do Registro" often shows the registration/renewal date
    # rather than an expiration — the ANAC Operação field is the reliable
    # source for active/inactive status.
    gdf["anac_ativo"] = True  # default
    if "anac_status_raw" in gdf.columns:
        gdf["anac_ativo"] = ~gdf["anac_status_raw"].astype(str).str.upper().str.contains(
            "INATIV|CANCEL|REVOG|VENCID", na=False
        )

    log.info("ANAC GeoDataFrame ready: %d features", len(gdf))
    return gdf


def _load_anac_from_decea() -> pd.DataFrame | None:
    """Fetch heliport data from DECEA GeoServer (Brazilian Air Force AIS)."""
    try:
        resp = _download_with_retries(DECEA_WFS_URL, timeout=60)
        if resp is None:
            return None
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return None

        rows = []
        for f in features:
            p = f.get("properties", {})
            geom = f.get("geometry", {})
            coords = geom.get("coordinates", [None, None])
            rows.append({
                "Código OACI": p.get("localidade_id", ""),
                "CIAD": p.get("ciad", ""),
                "Nome": p.get("nome", ""),
                "Município": p.get("cidade", ""),
                "UF": p.get("uf", ""),
                "Latitude": p.get("latitude", ""),
                "Longitude": p.get("longitude", ""),
                "LatGeoPoint": str(coords[1]) if coords[1] else "",
                "LonGeoPoint": str(coords[0]) if coords[0] else "",
                "Operação": p.get("opr", ""),
                "Tipo": p.get("tipo_util", ""),
                "Validade do Registro": p.get("efetivacao", ""),
            })
        df = pd.DataFrame(rows)
        log.info("Loaded %d heliports from DECEA WFS", len(df))
        return df
    except Exception as exc:
        log.warning("Failed to load from DECEA WFS: %s", exc)
        return None


def _detect_anac_columns(df: pd.DataFrame) -> dict:
    """Heuristically map ANAC DataFrame columns to semantic roles."""
    cols = {c.upper(): c for c in df.columns}

    def _find(*candidates):
        for c in candidates:
            for col_upper, col_orig in cols.items():
                if c in col_upper:
                    return col_orig
        return None

    # Prefer pre-computed decimal coords (LatGeoPoint/LonGeoPoint) when available
    lat_dec = _find("LATGEOPOINT", "LAT_DEC", "LATITUDE_DEC")
    lon_dec = _find("LONGEOPOINT", "LON_DEC", "LONGITUDE_DEC")

    return {
        "lat": lat_dec or _find("LATITUDE", "LAT"),
        "lon": lon_dec or _find("LONGITUDE", "LONG", "LON"),
        "nome": _find("NOME"),
        "oaci": _find("OACI", "ICAO", "LOCALIDADE"),
        "ciad": _find("CIAD"),
        "uf": _find("UF"),
        "municipio": _find("MUNIC", "CIDADE"),
        "operacao": _find("OPERA"),
        "validade": _find("VALIDADE", "EFETIVA"),
    }


# ---------------------------------------------------------------------------
# 3. Data acquisition — GeoSampa
# ---------------------------------------------------------------------------


def load_geosampa(local_file: str | None = None) -> gpd.GeoDataFrame:
    """Load GeoSampa heliport data from WFS or local file."""

    gdf: gpd.GeoDataFrame | None = None

    # --- Local file ---
    if local_file and Path(local_file).exists():
        log.info("Loading GeoSampa data from local file: %s", local_file)
        try:
            gdf = gpd.read_file(local_file)
        except Exception as exc:
            log.warning("Failed to read local GeoSampa file: %s", exc)

    # --- WFS ---
    if gdf is None:
        for layer in GEOSAMPA_LAYER_NAMES:
            wfs_url = (
                f"{GEOSAMPA_WFS_BASE}?service=WFS&version=1.0.0"
                f"&request=GetFeature&typeName={layer}"
                f"&outputFormat=application%2Fjson"
            )
            log.info("Trying GeoSampa WFS layer: %s", layer)
            try:
                resp = _download_with_retries(wfs_url, timeout=60)
                if resp is None:
                    continue
                data = resp.json()
                if "features" in data and len(data["features"]) > 0:
                    gdf = gpd.GeoDataFrame.from_features(
                        data["features"], crs=CRS_UTM23S
                    )
                    log.info(
                        "Loaded %d features from GeoSampa layer %s",
                        len(gdf),
                        layer,
                    )
                    break
                else:
                    log.warning("Layer %s returned 0 features or bad response", layer)
            except Exception as exc:
                log.warning("Error fetching layer %s: %s", layer, exc)

    if gdf is None or gdf.empty:
        log.error(
            "Could not load GeoSampa data. "
            "Place a local GeoJSON/Shapefile as 'helipontos.geojson' and re-run."
        )
        sys.exit(1)

    # Ensure WGS84
    if gdf.crs and gdf.crs != CRS_WGS84:
        gdf = gdf.to_crs(CRS_WGS84)

    # Normalise column names
    gdf.columns = [c.strip().lower() for c in gdf.columns]

    # Identify name column
    name_col = None
    for candidate in [
        "nm_empreendimento_heliponto",
        "nm_empreendimento",
        "nome",
        "name",
        "nm_heliponto",
    ]:
        if candidate in gdf.columns:
            name_col = candidate
            break
    if name_col and name_col != "gs_nome":
        gdf = gdf.rename(columns={name_col: "gs_nome"})

    # Identify OACI code column (tx_endereco_heliponto holds OACI in GeoSampa)
    oaci_col = None
    for candidate in ["tx_endereco_heliponto", "cd_oaci", "oaci"]:
        if candidate in gdf.columns:
            oaci_col = candidate
            break
    if oaci_col and oaci_col != "gs_oaci":
        gdf = gdf.rename(columns={oaci_col: "gs_oaci"})

    # Identify address column
    addr_col = None
    for candidate in [
        "nm_logradouro_heliponto",
        "endereco",
        "logradouro",
    ]:
        if candidate in gdf.columns:
            addr_col = candidate
            break
    if addr_col and addr_col != "gs_endereco":
        gdf = gdf.rename(columns={addr_col: "gs_endereco"})

    # Build full address if type + name available
    if "tp_logradouro_heliponto" in gdf.columns and "gs_endereco" in gdf.columns:
        gdf["gs_endereco"] = (
            gdf["tp_logradouro_heliponto"].fillna("").astype(str)
            + " "
            + gdf["gs_endereco"].fillna("").astype(str)
        ).str.strip()

    # Add house number if available
    if "nr_endereco_heliponto" in gdf.columns and "gs_endereco" in gdf.columns:
        gdf["gs_endereco"] = (
            gdf["gs_endereco"]
            + ", "
            + gdf["nr_endereco_heliponto"].fillna("").astype(str)
        ).str.rstrip(", ")

    # Identify status column from GeoSampa
    status_col = None
    for candidate in [
        "st_deferimento_processo_administrativo",
        "st_deferimento",
        "situacao",
        "status",
    ]:
        if candidate in gdf.columns:
            status_col = candidate
            break
    if status_col and status_col != "gs_situacao":
        gdf = gdf.rename(columns={status_col: "gs_situacao"})

    log.info("GeoSampa GeoDataFrame ready: %d features", len(gdf))
    log.info("GeoSampa columns: %s", list(gdf.columns))
    return gdf


# ---------------------------------------------------------------------------
# 4. Data acquisition — SMUL (Autos de Licença de Funcionamento)
# ---------------------------------------------------------------------------


def _normalize_street(s: str) -> str:
    """Normalize a street name for address matching across data sources."""
    if not s or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = str(s)
    # Remove accents
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.upper().strip()
    # Remove cross-street notation ("X R MESQUITA...")
    s = re.sub(r"\s*X\s+R\.?\s+.*$", "", s)
    # Remove comma-separated complements ("AV FOO, 782")
    s = re.sub(r",\s*\d+$", "", s)
    # Street type prefixes (start of string only)
    s = re.sub(
        r"^(AVENIDA|AV\.?|RUA|R\.?|ALAMEDA|AL\.?|TRAVESSA|TV\.?|"
        r"PRACA|PC\.?|LARGO|LG\.?|ESTRADA|EST\.?|RODOVIA|ROD\.?)\s+",
        "",
        s,
    )
    # Full honorary title words (before abbreviations to avoid partial removal)
    s = re.sub(
        r"\b(PRESIDENTE|DOUTOR|DOUTORA|DOTOR|ENGENHEIRO|ENGENHEIRA|"
        r"BRIGADEIRO|GENERAL|CORONEL|MAJOR|CAPITAO|"
        r"PROFESSOR|PROFESSORA|SENADOR|GOVERNADOR|"
        r"COMENDADOR|DESEMBARGADOR|DESEMBARGADORA|"
        r"MINISTRO|MINISTRA|DEPUTADO|MARECHAL)\b\s*",
        "",
        s,
    )
    # Abbreviated titles (require trailing space to avoid cutting longer words)
    s = re.sub(
        r"\b(DR|DRA|ENG|BRIG|GEN|GAL|CEL|MAJ|CAP|"
        r"PROF|PRES|SEN|GOV|COM|DES|MIN|DEP)\.?\s+",
        "",
        s,
    )
    # Remove suffixes like JR, JUNIOR, FILHO, NETO
    s = re.sub(r"\b(JR\.?|JUNIOR|FILHO|NETO|SOBRINHO)\b", "", s)
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_name(s: str) -> str:
    """Normalize a heliport name for fuzzy matching."""
    if not s or (isinstance(s, float) and pd.isna(s)):
        return ""
    s = str(s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.upper().strip()
    # Remove common filler words
    s = re.sub(
        r"\b(HELIPONTO|HELIPORTO|PRIVADO|CONDOMINIO|COND\.?|"
        r"EDIFICIO|EDIF\.?|ED\.?|TORRE|EMPREEND\w*|IMOB\w*|"
        r"LTDA\.?|S/?A\.?|PARTICIPACOES|FUNDO|INVESTIMENTO|"
        r"IMOBILIARIO|DE|DO|DA|DOS|DAS|E|EM)\b[.\s]*",
        "",
        s,
    )
    s = re.sub(r"[^A-Z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_overlap(a: str, b: str) -> float:
    """Return word-level Jaccard similarity between two normalised names."""
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _addr_key(street: str, number) -> str:
    """Create a normalized address key for matching: ``STREET|NUMBER``."""
    n = _normalize_street(street)
    try:
        num = str(int(float(number)))
    except (ValueError, TypeError):
        num = ""
    return f"{n}|{num}"


def load_smul(local_file: str | None = None) -> pd.DataFrame:
    """Load SMUL heliport license data (Autos de Licença de Funcionamento)."""

    df: pd.DataFrame | None = None

    # --- Try local file first ---
    if local_file and Path(local_file).exists():
        log.info("Loading SMUL data from local file: %s", local_file)
        try:
            df = pd.read_excel(local_file)
        except Exception as exc:
            log.warning("Failed to read local SMUL file: %s", exc)

    # --- Try download ---
    if df is None:
        log.info("Downloading SMUL license spreadsheet...")
        resp = _download_with_retries(SMUL_XLSX_URL, timeout=30)
        if resp and resp.status_code == 200:
            try:
                df = pd.read_excel(io.BytesIO(resp.content))
            except Exception as exc:
                log.warning("Failed to parse SMUL XLSX: %s", exc)

    if df is None or df.empty:
        log.warning(
            "Could not load SMUL data. "
            "Place 'smul_helipontos.xlsx' locally and re-run."
        )
        return pd.DataFrame()

    # --- Rename columns ---
    col_map = {
        "NOME DO HELIPONTO": "smul_nome",
        "PROPRIETÁRIO": "smul_proprietario",
        "ENDEREÇO": "smul_endereco",
        "NÚMERO": "smul_numero",
        "COMPL. / BAIRRO": "smul_bairro",
        "VALIDADE AUTO": "smul_validade",
        "Nº AUTO LICENÇA": "smul_auto",
        "Processo": "smul_processo",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # --- License validity ---
    if "smul_validade" in df.columns:
        df["smul_validade"] = pd.to_datetime(df["smul_validade"], errors="coerce")
        df["smul_vigente"] = df["smul_validade"] >= pd.Timestamp.now()
    else:
        df["smul_vigente"] = True

    # --- Build address key for matching ---
    df["_smul_addr_key"] = df.apply(
        lambda r: _addr_key(
            str(r.get("smul_endereco", "")), r.get("smul_numero")
        ),
        axis=1,
    )
    df["_smul_street"] = df["smul_endereco"].apply(_normalize_street)

    log.info("SMUL data loaded: %d licensed heliports", len(df))
    return df


def merge_smul(
    gdf: gpd.GeoDataFrame, df_smul: pd.DataFrame
) -> gpd.GeoDataFrame:
    """Match SMUL license data to the joined GeoSampa/ANAC dataset by address."""

    smul_cols = [
        "smul_nome",
        "smul_auto",
        "smul_validade",
        "smul_vigente",
        "smul_proprietario",
    ]
    if df_smul.empty:
        for col in smul_cols:
            gdf[col] = None
        gdf["smul_licenciado"] = False
        return gdf

    # --- Build address keys for the joined dataset ---
    def _gs_addr_key(row):
        endereco = str(row.get("gs_endereco", "") or "")
        # gs_endereco format: "Avenida Brigadeiro Faria Lima, 3729.0"
        if "," in endereco:
            street = endereco.rsplit(",", 1)[0].strip()
            num_str = endereco.rsplit(",", 1)[1].strip()
        else:
            street = endereco
            num_str = ""
        # Prefer raw number column if available
        raw_num = row.get("nr_endereco_heliponto")
        if pd.notna(raw_num):
            num_str = raw_num
        return _addr_key(street, num_str if num_str else None)

    gdf["_gs_addr_key"] = gdf.apply(_gs_addr_key, axis=1)
    gdf["_gs_street"] = gdf["_gs_addr_key"].apply(lambda k: k.split("|")[0])

    # --- Build SMUL lookups ---
    smul_by_key: dict[str, pd.Series] = {}
    smul_by_street: dict[str, list[pd.Series]] = {}
    for _, row in df_smul.iterrows():
        key = row["_smul_addr_key"]
        street = row["_smul_street"]
        if key and key != "|":
            smul_by_key[key] = row
        if street:
            smul_by_street.setdefault(street, []).append(row)

    # --- Initialise columns ---
    for col in smul_cols:
        gdf[col] = None
    gdf["smul_licenciado"] = False

    matched_smul_keys: set[str] = set()

    # --- Pass 1: exact address key match (street + number) ---
    for idx, row in gdf.iterrows():
        key = row["_gs_addr_key"]
        if key in smul_by_key and key not in matched_smul_keys:
            smul_row = smul_by_key[key]
            for col in smul_cols:
                if col in smul_row.index:
                    gdf.at[idx, col] = smul_row[col]
            gdf.at[idx, "smul_licenciado"] = True
            matched_smul_keys.add(key)

    # --- Pass 2: same street, closest number (within 500) ---
    for idx, row in gdf.iterrows():
        if gdf.at[idx, "smul_licenciado"]:
            continue
        gs_street = row["_gs_street"]
        gs_num_str = row["_gs_addr_key"].split("|")[1]
        if not gs_street or gs_street not in smul_by_street:
            continue
        try:
            gs_num = int(gs_num_str) if gs_num_str else 0
        except ValueError:
            gs_num = 0
        best_row = None
        best_diff = float("inf")
        for smul_row in smul_by_street[gs_street]:
            sk = smul_row["_smul_addr_key"]
            if sk in matched_smul_keys:
                continue
            smul_num_str = sk.split("|")[1]
            try:
                smul_num = int(smul_num_str) if smul_num_str else 0
            except ValueError:
                smul_num = 0
            diff = abs(gs_num - smul_num)
            if diff < best_diff:
                best_diff = diff
                best_row = smul_row
        if best_row is not None and best_diff <= 500:
            for col in smul_cols:
                if col in best_row.index:
                    gdf.at[idx, col] = best_row[col]
            gdf.at[idx, "smul_licenciado"] = True
            matched_smul_keys.add(best_row["_smul_addr_key"])

    # --- Pass 3: partial street name containment ---
    unmatched_streets = {
        row["_smul_street"]: row
        for _, row in df_smul.iterrows()
        if row["_smul_addr_key"] not in matched_smul_keys and row["_smul_street"]
    }
    for idx, row in gdf.iterrows():
        if gdf.at[idx, "smul_licenciado"]:
            continue
        gs_street = row["_gs_street"]
        if not gs_street:
            continue
        for smul_street, smul_row in list(unmatched_streets.items()):
            if smul_row["_smul_addr_key"] in matched_smul_keys:
                continue
            if smul_street in gs_street or gs_street in smul_street:
                for col in smul_cols:
                    if col in smul_row.index:
                        gdf.at[idx, col] = smul_row[col]
                gdf.at[idx, "smul_licenciado"] = True
                matched_smul_keys.add(smul_row["_smul_addr_key"])
                break

    # --- Pass 4: name-based matching (especially for ANAC-only records) ---
    # Build normalised SMUL name lookup for still-unmatched records
    smul_name_lookup: dict[str, pd.Series] = {}
    for _, row in df_smul.iterrows():
        if row["_smul_addr_key"] in matched_smul_keys:
            continue
        nname = _normalize_name(str(row.get("smul_nome", "")))
        if nname:
            smul_name_lookup[nname] = row

    if smul_name_lookup:
        for idx, row in gdf.iterrows():
            if gdf.at[idx, "smul_licenciado"]:
                continue
            # Try matching against GeoSampa name, ANAC name, or both
            for name_col in ["gs_nome", "anac_nome"]:
                raw_name = row.get(name_col)
                if not raw_name or (isinstance(raw_name, float) and pd.isna(raw_name)):
                    continue
                rec_name = _normalize_name(str(raw_name))
                if not rec_name:
                    continue
                for smul_name, smul_row in list(smul_name_lookup.items()):
                    if smul_row["_smul_addr_key"] in matched_smul_keys:
                        continue
                    # Check containment in both directions
                    if (
                        smul_name in rec_name
                        or rec_name in smul_name
                        or (len(rec_name) >= 6 and len(smul_name) >= 6
                            and _name_overlap(rec_name, smul_name) >= 0.6)
                    ):
                        for col in smul_cols:
                            if col in smul_row.index:
                                gdf.at[idx, col] = smul_row[col]
                        gdf.at[idx, "smul_licenciado"] = True
                        matched_smul_keys.add(smul_row["_smul_addr_key"])
                        break
                if gdf.at[idx, "smul_licenciado"]:
                    break

    n_matched = int(gdf["smul_licenciado"].sum())
    n_smul_unmatched = len(df_smul) - len(matched_smul_keys)
    log.info(
        "SMUL matching: %d dataset records matched, %d SMUL records unmatched",
        n_matched,
        n_smul_unmatched,
    )

    # Log unmatched SMUL records for manual review
    if n_smul_unmatched > 0:
        for _, row in df_smul.iterrows():
            if row["_smul_addr_key"] not in matched_smul_keys:
                log.debug(
                    "  SMUL unmatched: %s (%s)",
                    row.get("smul_nome", "?"),
                    row["_smul_addr_key"],
                )

    # --- Clean up temp columns ---
    gdf = gdf.drop(columns=["_gs_addr_key", "_gs_street"], errors="ignore")

    return gdf


# ---------------------------------------------------------------------------
# 5. Spatial join & status consolidation
# ---------------------------------------------------------------------------


def spatial_join(
    gdf_geosampa: gpd.GeoDataFrame,
    gdf_anac: gpd.GeoDataFrame,
    buffer_m: int = BUFFER_METROS,
) -> gpd.GeoDataFrame:
    """Cross-reference GeoSampa and ANAC by proximity (nearest within buffer)."""

    # Project to UTM for metre-based distance
    gs = gdf_geosampa.to_crs(CRS_UTM23S).copy()
    gs["_gs_idx"] = range(len(gs))
    anac = gdf_anac.to_crs(CRS_UTM23S).copy()

    # --- 1. Match GeoSampa → nearest ANAC ---
    joined = gpd.sjoin_nearest(
        gs,
        anac,
        how="left",
        max_distance=buffer_m,
        distance_col="dist_metros",
    )

    # sjoin_nearest may produce duplicates; keep closest match per GeoSampa row
    joined = joined.sort_values("dist_metros").drop_duplicates(
        subset=["_gs_idx"], keep="first"
    )

    # --- 2. Identify ANAC records with no GeoSampa match ---
    matched_anac_idx = set(
        joined.dropna(subset=["dist_metros"])["index_right"].dropna().astype(int)
    )
    unmatched_anac = anac.loc[~anac.index.isin(matched_anac_idx)].copy()

    # --- 3. Assign consolidated status ---
    def _status(row):
        has_anac = pd.notna(row.get("dist_metros"))
        anac_ativo = row.get("anac_ativo", True)
        has_smul = bool(row.get("smul_licenciado", False))
        smul_vigente = bool(row.get("smul_vigente", False))

        if not has_anac:
            return "NÃO_CADASTRADO_ANAC"
        if not anac_ativo:
            return "DIVERGENTE_ANAC_INATIVO"
        if has_smul and not smul_vigente:
            return "LICENÇA_SMUL_VENCIDA"
        if not has_smul:
            return "SEM_LICENÇA_SMUL"
        return "REGULAR"

    joined["status_consolidado"] = joined.apply(_status, axis=1)

    # Add unmatched ANAC rows as NÃO_CADASTRADO_PREFEITURA
    if not unmatched_anac.empty:
        unmatched_anac["status_consolidado"] = "NÃO_CADASTRADO_PREFEITURA"
        # Ensure compatible columns
        for col in joined.columns:
            if col not in unmatched_anac.columns:
                unmatched_anac[col] = None
        joined = pd.concat(
            [joined, unmatched_anac[joined.columns]], ignore_index=True
        )

    # Back to WGS84 for display
    joined = gpd.GeoDataFrame(joined, geometry="geometry", crs=CRS_UTM23S)
    joined = joined.to_crs(CRS_WGS84)

    log.info("Spatial join complete: %d total records", len(joined))
    for status, count in joined["status_consolidado"].value_counts().items():
        log.info("  %s: %d", status, count)

    return joined


# ---------------------------------------------------------------------------
# 5. Map visualisation (Folium)
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "REGULAR": "#2ecc71",
    "DIVERGENTE_ANAC_INATIVO": "#e67e22",
    "NÃO_CADASTRADO_ANAC": "#e74c3c",
    "NÃO_CADASTRADO_PREFEITURA": "#9b59b6",
    "SEM_LICENÇA_SMUL": "#c0392b",
    "LICENÇA_SMUL_VENCIDA": "#d35400",
}

# Statuses considered irregular
IRREGULAR_STATUSES = {
    "DIVERGENTE_ANAC_INATIVO",
    "NÃO_CADASTRADO_ANAC",
    "NÃO_CADASTRADO_PREFEITURA",
    "SEM_LICENÇA_SMUL",
    "LICENÇA_SMUL_VENCIDA",
}


def _irregular_label(status: str) -> str:
    """Return a human-readable irregularity description."""
    labels = {
        "DIVERGENTE_ANAC_INATIVO": "IRREGULAR — ANAC inativo",
        "NÃO_CADASTRADO_ANAC": "IRREGULAR — Sem cadastro na ANAC",
        "NÃO_CADASTRADO_PREFEITURA": "IRREGULAR — Sem cadastro na Prefeitura",
        "SEM_LICENÇA_SMUL": "IRREGULAR — Sem licença SMUL",
        "LICENÇA_SMUL_VENCIDA": "IRREGULAR — Licença SMUL vencida",
    }
    return labels.get(status, status)


def _fmt(val, fmt_date=False):
    """Format a value for popup display; return None if empty/NaN."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    if fmt_date and hasattr(val, "strftime"):
        return val.strftime("%d/%m/%Y")
    return s


def _build_popup_html(row, lat, lon, status, color, is_irregular):
    """Build a rich HTML popup with all available heliport data."""
    nome = row.get("gs_nome") or row.get("anac_nome") or "Sem nome"
    oaci = row.get("gs_oaci") or row.get("anac_oaci") or ""
    oaci_display = oaci if (isinstance(oaci, str) and oaci.strip()) else "\u2014"
    ciad = _fmt(row.get("anac_ciad"))
    dist = row.get("dist_metros")
    dist_str = f"{dist:.0f}m" if pd.notna(dist) else None
    endereco = _fmt(row.get("gs_endereco"))
    distrito = _fmt(row.get("nm_distrito_municipal"))

    p = []  # popup parts
    p.append(
        "<div style='font-family:Arial,sans-serif;font-size:12px;"
        "min-width:280px;max-width:400px;overflow-y:auto'>"
    )

    # --- Banner for irregulars ---
    if is_irregular:
        irreg_label = _irregular_label(status)
        p.append(
            f"<div style='background:{color};color:white;padding:6px 10px;"
            f"margin:-12px -12px 8px -12px;border-radius:4px 4px 0 0;"
            f"font-weight:bold;font-size:13px;text-align:center'>"
            f"\u26a0 {irreg_label}</div>"
        )

    p.append(f"<b style='font-size:14px'>{nome}</b><br>")

    # ── SECTION: Identificação ──
    p.append("<hr style='margin:4px 0'>")
    p.append("<b style='color:#3498db;font-size:11px'>"
             "\u2708 IDENTIFICA\u00c7\u00c3O</b><br>")
    p.append(f"<b>OACI:</b> {oaci_display}<br>")
    if ciad:
        p.append(f"<b>CIAD:</b> {ciad}<br>")
    p.append(
        f"<b>Status consolidado:</b> "
        f"<span style='color:{color};font-weight:bold'>{status}</span><br>"
    )

    # ── SECTION: ANAC (Federal) ──
    anac_nome = _fmt(row.get("anac_nome"))
    anac_ativo = row.get("anac_ativo", True)
    anac_val = row.get("anac_validade")
    anac_operacao_raw = _fmt(row.get("anac_status_raw"))
    has_anac = anac_nome or _fmt(row.get("anac_oaci"))
    if has_anac:
        p.append("<hr style='margin:4px 0'>")
        p.append("<b style='color:#2980b9;font-size:11px'>"
                 "\U0001f6e9 ANAC (Federal)</b><br>")
        if anac_nome:
            p.append(f"<b>Nome ANAC:</b> {anac_nome}<br>")
        # Destaque operação noturna (VFR diurno = sem noturno; VFR = dia e noite)
        if anac_operacao_raw:
            oper_upper = str(anac_operacao_raw).upper()
            if "VFR DIURNA" in oper_upper or "VFR DIURNO" in oper_upper:
                p.append(
                    "<b>Opera\u00e7\u00e3o noturna:</b> "
                    "<span style='color:#e67e22;font-weight:bold'>"
                    "N\u00e3o \u2014 apenas diurno</span><br>"
                )
            elif "VFR" in oper_upper or "IFR" in oper_upper:
                p.append(
                    "<b>Opera\u00e7\u00e3o noturna:</b> "
                    "<span style='color:#2ecc71;font-weight:bold'>Sim (dia e noite)</span><br>"
                )
            else:
                p.append(f"<b>Opera\u00e7\u00e3o:</b> {anac_operacao_raw}<br>")
        ativo_color = "#2ecc71" if anac_ativo else "#e74c3c"
        ativo_text = "Ativo" if anac_ativo else "Inativo"
        p.append(
            f"<b>Situa\u00e7\u00e3o ANAC:</b> "
            f"<span style='color:{ativo_color};font-weight:bold'>"
            f"{ativo_text}</span><br>"
        )
        if pd.notna(anac_val):
            anac_val_str = _fmt(anac_val, fmt_date=True) or str(anac_val)[:10]
            p.append(f"<b>Validade registro:</b> {anac_val_str}<br>")
    else:
        p.append("<hr style='margin:4px 0'>")
        p.append(
            "<b style='color:#e74c3c;font-size:11px'>"
            "\U0001f6e9 ANAC:</b> "
            "<span style='color:#e74c3c'>Sem cadastro</span><br>"
        )

    # ── SECTION: GeoSampa (Prefeitura) ──
    gs_nome = _fmt(row.get("gs_nome"))
    gs_situacao = _fmt(row.get("gs_situacao"))
    gs_processo = _fmt(row.get("cd_processo_administrativo_heliponto"))
    gs_parecer = _fmt(row.get("cd_parecer_tecnico"))
    gs_decont = _fmt(row.get("cd_parecer_decont"))
    gs_pub_doc = row.get("dt_publicacao_diario_oficial")
    gs_link_doc = _fmt(row.get("tx_link_diario_oficial"))
    gs_ciclo_diurno = _fmt(row.get("qt_ciclo_diurno"))
    gs_ciclo_vesp = _fmt(row.get("qt_ciclo_vespertino"))
    gs_ciclo_total = _fmt(row.get("qt_total_ciclo_permitido"))
    gs_obs = _fmt(row.get("tx_observacao_heliponto"))

    has_gs = gs_nome is not None
    if has_gs:
        p.append("<hr style='margin:4px 0'>")
        p.append("<b style='color:#27ae60;font-size:11px'>"
                 "\U0001f3db GeoSampa (Prefeitura)</b><br>")
        if gs_situacao:
            sit_color = "#2ecc71" if "Deferido" in gs_situacao else "#e74c3c"
            p.append(
                f"<b>Parecer:</b> "
                f"<span style='color:{sit_color}'>{gs_situacao}</span><br>"
            )
        if gs_processo:
            p.append(f"<b>Processo:</b> {gs_processo}<br>")
        if gs_parecer:
            p.append(f"<b>Parecer t\u00e9cnico:</b> {gs_parecer}<br>")
        if gs_decont:
            p.append(f"<b>Parecer DECONT:</b> {gs_decont}<br>")
        if pd.notna(gs_pub_doc):
            doc_str = _fmt(gs_pub_doc, fmt_date=True) or str(gs_pub_doc)[:10]
            if gs_link_doc:
                p.append(
                    f"<b>DOC:</b> <a href='{gs_link_doc}' target='_blank'>"
                    f"{doc_str}</a><br>"
                )
            else:
                p.append(f"<b>Publica\u00e7\u00e3o DOC:</b> {doc_str}<br>")
        if gs_ciclo_total:
            ciclos = f"Total: {gs_ciclo_total}"
            if gs_ciclo_diurno:
                ciclos += f" (Diurno: {gs_ciclo_diurno}"
                if gs_ciclo_vesp:
                    ciclos += f", Vespertino: {gs_ciclo_vesp}"
                ciclos += ")"
            p.append(f"<b>Ciclos permitidos:</b> {ciclos}<br>")
        if gs_obs:
            p.append(
                f"<b>Obs:</b> <i style='font-size:11px'>{gs_obs}</i><br>"
            )
    else:
        p.append("<hr style='margin:4px 0'>")
        p.append(
            "<b style='color:#e74c3c;font-size:11px'>"
            "\U0001f3db GeoSampa:</b> "
            "<span style='color:#e74c3c'>Sem cadastro</span><br>"
        )

    # ── SECTION: SMUL (Licença Municipal) ──
    smul_lic = row.get("smul_licenciado", False)
    p.append("<hr style='margin:4px 0'>")
    if smul_lic:
        p.append("<b style='color:#8e44ad;font-size:11px'>"
                 "\U0001f4cb SMUL (Licen\u00e7a Municipal)</b><br>")
        smul_auto = _fmt(row.get("smul_auto"))
        smul_val = row.get("smul_validade")
        smul_vig = bool(row.get("smul_vigente", False))
        smul_prop = _fmt(row.get("smul_proprietario"))
        smul_nome_val = _fmt(row.get("smul_nome"))

        if smul_nome_val:
            p.append(f"<b>Nome SMUL:</b> {smul_nome_val}<br>")
        if smul_auto:
            p.append(f"<b>Auto licen\u00e7a:</b> {smul_auto}<br>")
        if pd.notna(smul_val):
            smul_val_str = _fmt(smul_val, fmt_date=True) or str(smul_val)[:10]
            vig_color = "#2ecc71" if smul_vig else "#e74c3c"
            vig_text = "Vigente" if smul_vig else "Vencida"
            p.append(
                f"<b>Validade:</b> {smul_val_str} "
                f"(<span style='color:{vig_color};font-weight:bold'>"
                f"{vig_text}</span>)<br>"
            )
        if smul_prop:
            p.append(f"<b>Propriet\u00e1rio:</b> {smul_prop}<br>")
    else:
        p.append(
            "<b style='color:#e74c3c;font-size:11px'>"
            "\U0001f4cb SMUL:</b> "
            "<span style='color:#e74c3c'>Sem licen\u00e7a</span><br>"
        )

    # ── SECTION: Localização ──
    p.append("<hr style='margin:4px 0'>")
    p.append("<b style='color:#7f8c8d;font-size:11px'>"
             "\U0001f4cd LOCALIZA\u00c7\u00c3O</b><br>")
    if endereco:
        p.append(f"<b>Endere\u00e7o:</b> {endereco}<br>")
    if distrito:
        p.append(f"<b>Distrito:</b> {distrito}<br>")
    if dist_str:
        p.append(f"<b>Dist\u00e2ncia match:</b> {dist_str}<br>")
    p.append(f"<b>Coord:</b> {lat:.5f}, {lon:.5f}<br>")

    # ── SECTION: AISWEB/ROTAER (dimensões, superfície, peso máximo) ──
    p.append("<hr style='margin:4px 0'>")
    p.append("<b style='color:#16a085;font-size:11px'>"
             "\U0001f4ca ROTAER (AISWEB)</b><br>")
    dims = _fmt(row.get("aisweb_dimensoes"))
    mtow = _fmt(row.get("aisweb_mtow"))
    sup = _fmt(row.get("aisweb_superficie"))
    if dims or mtow or sup:
        parts = []
        if dims:
            parts.append(f"<b>{dims}</b>")
        if sup:
            parts.append(f"superf\u00edcie em <b>{sup}</b>")
        if mtow:
            mtow_num = str(row.get("aisweb_mtow", "")).replace(" t", "").replace(".", ",")
            parts.append(
                f"aguenta at\u00e9 <b style='color:#16a085'>{mtow_num} toneladas</b>"
            )
        if parts:
            p.append(
                "<span style='font-size:12px'>"
                + " &mdash; ".join(parts)
                + "</span><br>"
            )
    oaci_for_link = (row.get("anac_oaci") or row.get("gs_oaci") or "")
    if isinstance(oaci_for_link, str) and oaci_for_link.strip():
        aisweb_url = (
            f"https://aisweb.decea.mil.br/?i=aerodromos&codigo={oaci_for_link.strip().upper()}"
        )
        p.append(
            f"<a href='{aisweb_url}' target='_blank' "
            "style='color:#2980b9;text-decoration:underline;font-size:11px'>"
            "Ver detalhes no AISWEB/ROTAER</a><br>"
        )
    else:
        p.append(
            "<a href='https://aisweb.decea.mil.br/?i=aerodromos' target='_blank' "
            "style='color:#2980b9;text-decoration:underline;font-size:11px'>"
            "AISWEB — buscar pelo nome</a><br>"
        )

    p.append("</div>")

    return "".join(p)


def build_map(
    gdf: gpd.GeoDataFrame,
    output_path: str = OUTPUT_MAP,
    dashboard_url: str = DASHBOARD_URL,
) -> None:
    """Create an interactive Folium map of heliport statuses."""

    # Centre on São Paulo — Berrini/Faria Lima region
    center = [-23.585, -46.685]
    m = folium.Map(location=center, zoom_start=13, tiles=None)

    # --- Tile layers (mapas de referência) ---
    folium.TileLayer(
        tiles="OpenStreetMap",
        attr="OpenStreetMap contributors",
        name="OpenStreetMap (padrão)",
        overlay=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri World Imagery",
        name="Satélite (Esri)",
        overlay=False,
    ).add_to(m)
    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri Labels",
        name="Rótulos de ruas",
        overlay=True,
        show=True,
    ).add_to(m)
    folium.TileLayer("CartoDB positron", name="Mapa claro (CartoDB)").add_to(m)

    # --- Pulsing CSS for irregular markers ---
    pulse_css = """
    <style>
    @keyframes pulse-ring {
        0%   { transform: scale(1);   opacity: 0.8; }
        50%  { transform: scale(1.6); opacity: 0; }
        100% { transform: scale(1);   opacity: 0; }
    }
    .irregular-pulse {
        position: absolute;
        border-radius: 50%;
        animation: pulse-ring 2s ease-out infinite;
        pointer-events: none;
    }
    </style>
    """
    m.get_root().html.add_child(folium.Element(pulse_css))

    # --- Feature groups for layer control ---
    STATUS_LABELS = {
        "REGULAR": "\u2705 Regular (ANAC + SMUL)",
        "DIVERGENTE_ANAC_INATIVO": "\u26a0\ufe0f IRREGULAR — ANAC inativo",
        "NÃO_CADASTRADO_ANAC": "\u274c IRREGULAR — Sem cadastro ANAC",
        "NÃO_CADASTRADO_PREFEITURA": "\u274c IRREGULAR — Sem cadastro Prefeitura",
        "SEM_LICENÇA_SMUL": "\u274c IRREGULAR — Sem licença SMUL",
        "LICENÇA_SMUL_VENCIDA": "\u26a0\ufe0f IRREGULAR — Licença SMUL vencida",
    }
    groups = {}
    for status_key, label in STATUS_LABELS.items():
        count = len(gdf[gdf["status_consolidado"] == status_key])
        fg = folium.FeatureGroup(name=f"{label} ({count})", show=True)
        groups[status_key] = fg

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        lat, lon = geom.y, geom.x
        status = row.get("status_consolidado", "UNKNOWN")
        color = STATUS_COLORS.get(status, "gray")
        is_irregular = status in IRREGULAR_STATUSES

        nome = row.get("gs_nome") or row.get("anac_nome") or "Sem nome"
        oaci = row.get("gs_oaci") or row.get("anac_oaci") or ""
        oaci_display = oaci if (isinstance(oaci, str) and oaci.strip()) else "\u2014"

        popup_html = _build_popup_html(row, lat, lon, status, color, is_irregular)

        def _make_popup():
            iframe = IFrame(html=popup_html, width=420, height=420)
            return folium.Popup(iframe, max_width=450)

        tooltip_text = (
            f"\u26a0 IRREGULAR — {nome}"
            if is_irregular
            else nome
        )
        tooltip_text += " (clique para detalhes)"

        fg = groups.get(status, list(groups.values())[0])

        # Irregular markers: larger, with a pulsing ring and bold border
        if is_irregular:
            # Pulsing outer ring via DivIcon — com popup para garantir clique
            folium.Marker(
                location=[lat, lon],
                icon=folium.DivIcon(
                    icon_size=(36, 36),
                    icon_anchor=(18, 18),
                    html=(
                        f"<div class='irregular-pulse' style='"
                        f"width:36px;height:36px;"
                        f"border:3px solid {color};'></div>"
                    ),
                ),
                popup=_make_popup(),
                tooltip=tooltip_text,
            ).add_to(fg)

            # Main marker — larger for irregulars
            folium.CircleMarker(
                location=[lat, lon],
                radius=11,
                color=color,
                weight=3,
                fill=True,
                fill_color=color,
                fill_opacity=0.9,
                popup=_make_popup(),
                tooltip=tooltip_text,
            ).add_to(fg)
        else:
            # Regular marker — smaller, subtler
            folium.CircleMarker(
                location=[lat, lon],
                radius=7,
                color="white",
                weight=1.5,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                popup=_make_popup(),
                tooltip=tooltip_text,
            ).add_to(fg)

        # ICAO label (DivIcon) — com popup para clique no rótulo
        if isinstance(oaci, str) and oaci.strip():
            label_color = "white" if not is_irregular else color
            folium.Marker(
                location=[lat, lon],
                icon=folium.DivIcon(
                    icon_size=(0, 0),
                    icon_anchor=(0, -14),
                    html=(
                        f"<div style='"
                        f"font-size:9px;font-weight:bold;color:{label_color};"
                        f"text-shadow:1px 1px 2px black,-1px -1px 2px black,"
                        f"1px -1px 2px black,-1px 1px 2px black;"
                        f"white-space:nowrap;cursor:pointer;"
                        f"' title='Clique para ver informa\u00e7\u00f5es'>{oaci}</div>"
                    ),
                ),
                popup=_make_popup(),
                tooltip=tooltip_text,
            ).add_to(fg)

    for fg in groups.values():
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    # Summary stats for legend
    n_total = len(gdf)
    n_regular = len(gdf[gdf["status_consolidado"] == "REGULAR"])
    n_irregular = n_total - n_regular
    n_no_anac = len(gdf[gdf["status_consolidado"] == "NÃO_CADASTRADO_ANAC"])
    n_no_pref = len(gdf[gdf["status_consolidado"] == "NÃO_CADASTRADO_PREFEITURA"])
    n_inativo = len(gdf[gdf["status_consolidado"] == "DIVERGENTE_ANAC_INATIVO"])
    n_sem_smul = len(gdf[gdf["status_consolidado"] == "SEM_LICENÇA_SMUL"])
    n_smul_venc = len(gdf[gdf["status_consolidado"] == "LICENÇA_SMUL_VENCIDA"])

    # Legend with clear irregular section
    legend_html = f"""
    <div style="position: fixed; bottom: 30px; left: 30px; z-index: 1000;
         background: rgba(0,0,0,0.85); padding: 14px 18px; border-radius: 10px;
         box-shadow: 0 4px 12px rgba(0,0,0,0.6); font-size: 13px;
         font-family: Arial, sans-serif; color: white; max-width: 340px;">
      <b style="font-size:15px">Helipontos de São Paulo</b><br>
      <span style="font-size:11px;color:#aaa">
        Fontes: ANAC + GeoSampa + SMUL<br>
        Total: {n_total} &nbsp;|&nbsp;
        Regulares: {n_regular} &nbsp;|&nbsp;
        <span style="color:#ff6b6b">Irregulares: {n_irregular}</span></span>
      <hr style="border-color:#555;margin:8px 0">

      <i style="background:#2ecc71;width:12px;height:12px;display:inline-block;
         border-radius:50%;margin-right:6px;border:1px solid white;"></i>
      <b>Regular</b> — ANAC ativo + Licença SMUL ({n_regular})<br>

      <hr style="border-color:#555;margin:8px 0">
      <b style="color:#ff6b6b;font-size:13px">\u26a0 IRREGULARES</b><br>
      <div style="margin-top:4px">
        <i style="background:#c0392b;width:14px;height:14px;display:inline-block;
           border-radius:50%;margin-right:6px;border:2px solid #c0392b;"></i>
        Sem <b>licença SMUL</b> ({n_sem_smul})<br>
        <i style="background:#d35400;width:14px;height:14px;display:inline-block;
           border-radius:50%;margin-right:6px;border:2px solid #d35400;"></i>
        Licença SMUL <b>vencida</b> ({n_smul_venc})<br>
        <i style="background:#e74c3c;width:14px;height:14px;display:inline-block;
           border-radius:50%;margin-right:6px;border:2px solid #e74c3c;"></i>
        Sem cadastro na <b>ANAC</b> ({n_no_anac})<br>
        <i style="background:#9b59b6;width:14px;height:14px;display:inline-block;
           border-radius:50%;margin-right:6px;border:2px solid #9b59b6;"></i>
        Sem cadastro na <b>Prefeitura</b> ({n_no_pref})<br>
        <i style="background:#e67e22;width:14px;height:14px;display:inline-block;
           border-radius:50%;margin-right:6px;border:2px solid #e67e22;"></i>
        <b>ANAC inativo</b> ({n_inativo})<br>
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # --- Botão Dashboard + overlay ---
    dashboard_html = f"""
    <button id="btn-dashboard" onclick="document.getElementById('dashboard-overlay').style.display='flex'"
            style="position:fixed;top:20px;right:20px;z-index:1001;
                   background:#2980b9;color:white;border:none;padding:12px 20px;
                   border-radius:8px;font-size:14px;font-weight:bold;
                   cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,0.3);
                   font-family:Arial,sans-serif;">
      📊 Dashboard
    </button>
    <div id="dashboard-overlay" onclick="if(event.target===this)this.style.display='none'"
         style="display:none;position:fixed;inset:0;z-index:2000;
         background:rgba(0,0,0,0.5);align-items:center;justify-content:center;
         padding:20px;box-sizing:border-box;"
         tabindex="-1">
      <div onclick="event.stopPropagation()"
           style="background:white;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.4);
           width:95%;max-width:1100px;height:90vh;overflow:hidden;display:flex;flex-direction:column;">
        <div style="padding:12px 16px;background:#2c3e50;color:white;display:flex;
             justify-content:space-between;align-items:center;">
          <b>Dashboard — Helipontos SP</b>
          <button onclick="document.getElementById('dashboard-overlay').style.display='none'"
                  style="background:#e74c3c;color:white;border:none;padding:6px 14px;
                         border-radius:6px;cursor:pointer;font-weight:bold;">✕ Fechar</button>
        </div>
        <iframe src="{dashboard_url}" style="flex:1;width:100%;border:none;min-height:0;"></iframe>
      </div>
    </div>
    <script>
    document.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape' && document.getElementById('dashboard-overlay').style.display === 'flex')
        document.getElementById('dashboard-overlay').style.display = 'none';
    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(dashboard_html))

    m.save(output_path)
    log.info("Map saved to %s", output_path)


# ---------------------------------------------------------------------------
# 6. CSV export
# ---------------------------------------------------------------------------


def export_csv(gdf: gpd.GeoDataFrame, output_path: str = OUTPUT_CSV) -> None:
    """Export the consolidated result to CSV."""

    # Select and order key columns
    key_cols = [
        "gs_nome",
        "gs_oaci",
        "gs_endereco",
        "gs_situacao",
        "nm_distrito_municipal",
        "cd_processo_administrativo_heliponto",
        "cd_parecer_tecnico",
        "cd_parecer_decont",
        "dt_publicacao_diario_oficial",
        "qt_ciclo_diurno",
        "qt_ciclo_vespertino",
        "qt_total_ciclo_permitido",
        "tx_observacao_heliponto",
        "anac_nome",
        "anac_oaci",
        "anac_ciad",
        "anac_validade",
        "anac_status_raw",
        "anac_ativo",
        "smul_licenciado",
        "smul_nome",
        "smul_proprietario",
        "smul_auto",
        "smul_validade",
        "smul_vigente",
        "status_consolidado",
        "dist_metros",
        "aisweb_dimensoes",
        "aisweb_superficie",
        "aisweb_mtow",
    ]
    available = [c for c in key_cols if c in gdf.columns]

    # Add latitude/longitude
    export = gdf[available].copy()
    export["latitude"] = gdf.geometry.y
    export["longitude"] = gdf.geometry.x

    export.to_csv(output_path, index=False, encoding="utf-8-sig")
    log.info("CSV exported to %s (%d rows)", output_path, len(export))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Cruzamento de helipontos SP — GeoSampa × ANAC"
    )
    parser.add_argument(
        "--anac-csv",
        default=None,
        help=(
            "Path to a local ANAC CSV file. "
            "If omitted the script tries to download from ANAC servers."
        ),
    )
    parser.add_argument(
        "--geosampa-file",
        default=None,
        help=(
            "Path to a local GeoSampa GeoJSON/Shapefile. "
            "If omitted the script tries the WFS endpoint."
        ),
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=BUFFER_METROS,
        help="Buffer distance in metres for spatial join (default: 100).",
    )
    parser.add_argument(
        "--output-csv",
        default=OUTPUT_CSV,
        help="Output CSV path (default: comparativo_helipontos_sp.csv).",
    )
    parser.add_argument(
        "--smul-file",
        default=None,
        help=(
            "Path to a local SMUL XLSX file with Autos de Licença. "
            "If omitted the script tries to download from the Prefeitura portal."
        ),
    )
    parser.add_argument(
        "--output-map",
        default=OUTPUT_MAP,
        help="Output HTML map path (default: mapa_helipontos_sp.html).",
    )
    parser.add_argument(
        "--no-fetch-aisweb",
        action="store_true",
        help="Skip fetching dimensions/MTOW from AISWEB (use cache only).",
    )
    args = parser.parse_args()

    # --- Auto-detect local fallback files ---
    anac_local = args.anac_csv
    if not anac_local:
        for candidate in [
            "anac_helipontos.csv",
            "AerodromosPrivados.csv",
            "Helipontos.csv",
        ]:
            if Path(candidate).exists():
                anac_local = candidate
                break

    geosampa_local = args.geosampa_file
    if not geosampa_local:
        for candidate in [
            "helipontos.geojson",
            "helipontos.json",
            "helipontos.shp",
            "geosampa_helipontos.geojson",
        ]:
            if Path(candidate).exists():
                geosampa_local = candidate
                break

    smul_local = args.smul_file
    if not smul_local:
        for candidate in ["smul_helipontos.xlsx", "smul_helipontos.xls"]:
            if Path(candidate).exists():
                smul_local = candidate
                break

    log.info("=" * 60)
    log.info("Helipontos SP — GeoSampa × ANAC × SMUL")
    log.info("=" * 60)

    # Step 1–3: Load and clean data
    gdf_anac = load_anac(local_csv=anac_local)
    gdf_geosampa = load_geosampa(local_file=geosampa_local)
    df_smul = load_smul(local_file=smul_local)

    # Step 4: Spatial join GeoSampa × ANAC
    gdf_result = spatial_join(gdf_geosampa, gdf_anac, buffer_m=args.buffer)

    # Step 5: Match SMUL license data
    gdf_result = merge_smul(gdf_result, df_smul)

    # Step 6: Re-evaluate status with SMUL as primary municipal source
    def _final_status(row):
        # Determine data source availability
        has_geosampa = pd.notna(row.get("gs_nome"))
        has_anac_match = pd.notna(row.get("dist_metros"))
        is_anac_only = row.get("status_consolidado") == "NÃO_CADASTRADO_PREFEITURA"
        anac_ativo = row.get("anac_ativo", True)
        has_smul = bool(row.get("smul_licenciado", False))
        smul_vigente = bool(row.get("smul_vigente", False))

        # ANAC status checks (federal)
        if not has_anac_match and not is_anac_only:
            return "NÃO_CADASTRADO_ANAC"
        if not anac_ativo:
            return "DIVERGENTE_ANAC_INATIVO"

        # SMUL is the authoritative source for municipal licensing
        if has_smul and smul_vigente:
            return "REGULAR"
        if has_smul and not smul_vigente:
            return "LICENÇA_SMUL_VENCIDA"

        # No SMUL license found
        if is_anac_only and not has_geosampa:
            return "NÃO_CADASTRADO_PREFEITURA"
        return "SEM_LICENÇA_SMUL"

    gdf_result["status_consolidado"] = gdf_result.apply(_final_status, axis=1)

    log.info("Final status breakdown:")
    for status, count in gdf_result["status_consolidado"].value_counts().items():
        log.info("  %s: %d", status, count)

    # Step 6b: Enrich with AISWEB (dimensões, superfície, MTOW)
    gdf_result = enrich_with_aisweb(
        gdf_result,
        fetch_missing=not args.no_fetch_aisweb,
    )

    # Step 7: Outputs
    build_map(gdf_result, output_path=args.output_map)
    export_csv(gdf_result, output_path=args.output_csv)

    log.info("=" * 60)
    log.info("Done! Files generated:")
    log.info("  CSV: %s", args.output_csv)
    log.info("  Map: %s", args.output_map)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
