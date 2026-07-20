"""
X.com Grok chat client via reverse-engineered API.

Sends questions to Grok about wildfires and gets structured answers.
Uses same cookies as xmonitor.py (X_AUTH_TOKEN + X_CT0).

Free — uses the user's Grok quota on X.com.
May break if X changes their internal API.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_root = Path(__file__).resolve().parents[2]
from dotenv import load_dotenv
load_dotenv(_root / '.env')

# Bearer token for X.com API (same for all users)
X_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

GROK_CREATE_CONV_URL = "https://x.com/i/api/graphql/6cmfJY3d7EPWuCSXWrkOFg/CreateGrokConversation"
GROK_CHAT_URL = "https://api.x.com/2/grok/add_response.json"


@dataclass
class GrokFireInfo:
    """Fire info returned by Grok."""
    municipality: str
    province: str
    region: str
    lat: float | None = None
    lon: float | None = None
    hectares: float | None = None
    status: str = "active"
    detection_date: str = ""
    description: str = ""
    source_tweet: str = ""


@dataclass
class GrokValidation:
    """Validation result for a #IF hashtag from Grok."""
    confirmed: bool
    corrected_municipality: str | None = None
    location_detail: str = ""
    chronology_text: str = ""
    chronology_url: str = ""
    raw_response: str = ""


@dataclass
class GrokResponse:
    """Parsed Grok response about fires."""
    text: str
    fires: list[GrokFireInfo] = field(default_factory=list)
    raw: Any = None


import uuid


def _get_session() -> requests.Session | None:
    """Build requests session with X.com cookies (matches GrokAiChat)."""
    auth_token = os.environ.get("X_AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0")

    if not auth_token or not ct0:
        logger.warning("X_AUTH_TOKEN or X_CT0 not set")
        return None

    cookies_str = f"auth_token={auth_token}; ct0={ct0}"
    client_uuid = uuid.uuid4().hex

    session = requests.Session()
    session.headers.update({
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "es-ES,es;q=0.9,en;q=0.8",
        "authorization": f"Bearer {X_BEARER_TOKEN}",
        "content-type": "application/json",
        "cookie": cookies_str,
        "origin": "https://x.com",
        "priority": "u=1, i",
        "referer": "https://x.com/i/grok",
        "sec-ch-ua": '"Chromium";v="137", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "x-client-uuid": client_uuid,
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "es",
    })
    return session


def create_conversation(session: requests.Session) -> str | None:
    """Create a new Grok conversation. Returns conversation_id."""
    try:
        resp = session.post(
            GROK_CREATE_CONV_URL,
            json={"variables": {}, "queryId": "6cmfJY3d7EPWuCSXWrkOFg"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Create conversation failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        conv_id = data["data"]["create_grok_conversation"]["conversation_id"]
        logger.info("Created Grok conversation: %s", conv_id)
        return conv_id
    except Exception as e:
        logger.error("Failed to create Grok conversation: %s", e)
        return None


def ask_grok(session: requests.Session, conversation_id: str, message: str) -> str | None:
    """Send a message to Grok and get the response text."""
    try:
        payload = {
            "responses": [
                {
                    "message": message,
                    "sender": 1,
                    "promptSource": "",
                    "fileAttachments": [],
                }
            ],
            "systemPromptName": "",
            "grokModelOptionId": "grok-2a",
            "conversationId": conversation_id,
            "returnSearchResults": True,
            "returnCitations": True,
            "promptMetadata": {
                "promptSource": "NATURAL",
                "action": "INPUT",
            },
            "imageGenerationCount": 4,
            "requestFeatures": {
                "eagerTweets": True,
                "serverHistory": True,
            },
        }
        resp = session.post(GROK_CHAT_URL, json=payload, timeout=90)
        if resp.status_code != 200:
            logger.error("Grok chat failed: %s %s", resp.status_code, resp.text[:200])
            return None

        # Response is JSONL — concatenate all message chunks
        # Format: each line is {"result": {"message": "...", ...}}
        response_data = resp.text
        try:
            response_data = json.loads(resp.text)
        except (json.JSONDecodeError, ValueError):
            pass  # Keep as text for NDJSON parsing

        full_text = ""
        if isinstance(response_data, dict):
            # Single JSON response
            if response_data.get("result", {}).get("message"):
                full_text = response_data["result"]["message"]
            elif response_data.get("text"):
                # NDJSON as string
                response_data = response_data["text"]

        if isinstance(response_data, str):
            # Parse NDJSON
            for line in response_data.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    result = chunk.get("result", {})
                    if result.get("message"):
                        full_text += result["message"]
                    elif result.get("responseType") == "limiter":
                        logger.warning("Grok rate limited")
                        return None
                except json.JSONDecodeError:
                    continue

        return full_text if full_text else None

    except Exception as e:
        logger.error("Failed to get Grok response: %s", e)
        return None


def ask_grok_about_fires(
    region_name: str,
    region_query: str,
    existing_fires: list[dict],
    session: requests.Session | None = None,
) -> str | None:
    """Ask Grok about fires in a specific region.

    Args:
        region_name: Human-readable region name (e.g., "Andalucía")
        region_query: Grok search query for this region
        existing_fires: List of already-known fires for dedup
        session: Pre-built requests session (optional)

    Returns:
        Grok's response text about fires in the region.
    """
    if session is None:
        session = _get_session()
    if session is None:
        return None

    # Build existing fires summary for Grok
    existing_summary = ""
    if existing_fires:
        lines = []
        for f in existing_fires[:50]:  # Limit to avoid token overflow
            mun = f.get("municipality", "?")
            prov = f.get("province", "")
            prov_str = f" ({prov})" if prov else ""
            lines.append(f"- {mun}{prov_str}")
        existing_summary = f"\n\nINCENDIOS QUE YA TENEMOS REGISTRADOS en {region_name}:\n" + "\n".join(lines) + "\n"

    prompt = f"""Busca y lista todos los incendios forestales activos o recientes (últimas 24h) en {region_name}.

Fuentes oficiales a consultar: INFOCA, 112, bomberos forestales, Protección Civil de {region_name}.

Para cada incendio encontrado, proporciona:
- Municipio exacto donde está el incendio (NO el pueblo de evacuación)
- Provincia
- Coordenadas lat/lon aproximadas
- Superficie afectada (hectáreas) si disponible
- Estado (activo, controlado, extinguido)
- Fecha de detección si disponible
{existing_summary}
IMPORTANTE: 
- La ubicación debe ser del PUNTO DEL INCENDIO, no de donde se evacua la gente ni de donde se saca la foto.
- Si solo hay humo visible desde otra localidad, indica la localidad de origen del incendio.
- Si no hay incendios activos, responde "No hay incendios activos en {region_name}".
- Lista solo incendios REALES confirmados por fuentes oficiales."""

    # Create conversation and ask
    conv_id = create_conversation(session)
    if not conv_id:
        return None

    return ask_grok(session, conv_id, prompt)


def parse_grok_fires_response(response_text: str) -> list[GrokFireInfo]:
    """Parse Grok's response to extract fire information.

    This is a best-effort parser — Grok responses are natural language.
    Returns list of GrokFireInfo with whatever structured data we can extract.
    """
    if not response_text:
        return []

    fires = []
    # Simple heuristic: look for numbered items or bullet points with location names
    lines = response_text.split("\n")
    current_fire = None

    for line in lines:
        line = line.strip()
        if not line:
            if current_fire:
                fires.append(current_fire)
                current_fire = None
            continue

        # Detect fire entries (numbered, bulleted, or "Incendio de/del/en")
        is_fire_line = (
            line.startswith(("-", "*", "•"))
            or (len(line) > 2 and line[0].isdigit() and line[1] in ".-) ")
            or "incendio" in line.lower()
            or "fuego" in line.lower()
        )

        if is_fire_line:
            if current_fire:
                fires.append(current_fire)
            current_fire = GrokFireInfo(
                municipality=line.lstrip("-*•0123456789. ()"),
                province="",
                region="",
                description=line,
            )

            # Try to extract coordinates from the line
            import re
            coord_match = re.search(r'(\d+\.?\d*)\s*[,/]\s*(-?\d+\.?\d*)', line)
            if coord_match:
                try:
                    current_fire.lat = float(coord_match.group(1))
                    current_fire.lon = float(coord_match.group(2))
                except ValueError:
                    pass

            # Try to extract hectares
            ha_match = re.search(r'(\d+\.?\d*)\s*(?:ha|hectáreas?)', line, re.IGNORECASE)
            if ha_match:
                try:
                    current_fire.hectares = float(ha_match.group(1))
                except ValueError:
                    pass

    if current_fire:
        fires.append(current_fire)

    return fires


def get_fire_report_by_region(
    region_name: str,
    region_query: str,
    existing_fires: list[dict],
) -> tuple[str | None, list[GrokFireInfo]]:
    """Get fire report for a region from Grok.

    Returns (raw_response_text, parsed_fires).
    """
    session = _get_session()
    if not session:
        return None, []

    response_text = ask_grok_about_fires(region_name, region_query, existing_fires, session)
    if not response_text:
        return None, []

    fires = parse_grok_fires_response(response_text)
    return response_text, fires


def validate_if_hashtag(
    hashtag: str,
    municipality: str,
    session: requests.Session | None = None,
) -> GrokValidation | None:
    """Validate a #IF hashtag using Grok to confirm it's a real fire.

    Args:
        hashtag: The #IF hashtag (e.g., "#IFJaen")
        municipality: Municipality extracted from hashtag
        session: Pre-built requests session (optional)

    Returns:
        GrokValidation with confirmation, corrected location, and chronology.
    """
    if session is None:
        session = _get_session()
    if session is None:
        return None

    prompt = f"""Busca en X.com el hashtag {hashtag} y verifica si corresponde a un incendio forestal REAL activo.

Responde EXCLUSIVAMENTE con este JSON (sin texto adicional):
{{
  "confirmed": true/false,
  "real_municipality": "nombre real del municipio donde está el incendio",
  "location_detail": "descripción de la ubicación exacta del punto de incendio",
  "chronology": "resumen cronológico de los tweets más relevantes con este hashtag (fechas, autores, contenido)"
}}

REGLAS CRÍTICAS:
- Si el hashtag NO es de un incendio real, responde: {{"confirmed": false}}
- La ubicación debe ser del PUNTO DEL INCENDIO, NO de donde se ve el humo ni de donde se evacua
- Si el municipio del hashtag no coincide con el real, pon el nombre real en "real_municipality"
- Si mencionan un municipio cercano como referencia, indica cuál es el origen real del fuego
- Incluye en "chronology" los tweets más relevantes con sus timestamps"""

    conv_id = create_conversation(session)
    if not conv_id:
        return None

    response_text = ask_grok(session, conv_id, prompt)
    if not response_text:
        return None

    # Parse JSON response
    try:
        # Try to extract JSON from response (Grok may wrap it in markdown)
        json_match = re.search(r'\{[^{}]*"confirmed"[^{}]*\}', response_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            data = json.loads(response_text)

        confirmed = data.get("confirmed", False)
        corrected = data.get("real_municipality")
        location_detail = data.get("location_detail", "")
        chronology = data.get("chronology", "")

        # Build chronology URL for X.com search
        import urllib.parse
        search_query = urllib.parse.quote(hashtag)
        chronology_url = f"https://x.com/search?q={search_query}&src=typed_query&f=live"

        return GrokValidation(
            confirmed=confirmed,
            corrected_municipality=corrected if corrected != municipality else None,
            location_detail=location_detail,
            chronology_text=chronology,
            chronology_url=chronology_url,
            raw_response=response_text,
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to parse Grok validation response: %s", e)
        # Try to infer from text
        confirmed = any(word in response_text.lower() for word in ["incendio", "fuego", "activo", "forestal"])
        return GrokValidation(
            confirmed=confirmed,
            chronology_text=response_text,
            chronology_url=f"https://x.com/search?q={urllib.parse.quote(hashtag)}&src=typed_query&f=live",
            raw_response=response_text,
        )
