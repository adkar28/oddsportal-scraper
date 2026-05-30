"""
OddsPortal → Google Sheets
Tüm ülkeler + lig isimleri + takım isimleri
"""

import requests
import json
import time
import re
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup

# ─── AYARLAR ────────────────────────────────────────────────────────────────
GOOGLE_SHEET_ID   = "1mMURJFuBWSLV9ePZcBBlfTW_olgfuP9xXEqxGvWAERE"
WORKSHEET_LEAGUES = "Ligler"
WORKSHEET_TEAMS   = "Takımlar"
DELAY_BETWEEN_LEAGUES = 1.5   # Saniye — rate limit koruması
MAX_LEAGUES_FOR_TEAMS = 999   # Kaç lig için takım çekilsin (999 = hepsi)
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.oddsportal.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.oddsportal.com/football/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ─── 1. TÜM LİG LİSTESİNİ ÇEK ───────────────────────────────────────────────

def fetch_all_leagues():
    """
    OddsPortal /football/ sayfasından tüm ülke + lig listesini çeker.
    Önce __NEXT_DATA__ JSON'unu dener, sonra sidebar HTML parse'ına geçer.
    """
    print("[1/3] Lig listesi çekiliyor...")
    url = f"{BASE_URL}/football/"

    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"  [HATA] Sayfa alınamadı: {e}")
        return []

    # Yöntem A — __NEXT_DATA__ JSON bloğu
    leagues = _parse_next_data(r.text)
    if leagues:
        print(f"  ✓ __NEXT_DATA__ ile {len(leagues)} lig bulundu.")
        return leagues

    # Yöntem B — HTML sidebar
    leagues = _parse_sidebar_html(r.text)
    if leagues:
        print(f"  ✓ HTML sidebar ile {len(leagues)} lig bulundu.")
        return leagues

    # Yöntem C — OddsPortal dahili AJAX endpoint
    leagues = _fetch_ajax_leagues()
    if leagues:
        print(f"  ✓ AJAX endpoint ile {len(leagues)} lig bulundu.")
        return leagues

    print("  [UYARI] Hiç lig bulunamadı.")
    return []


def _parse_next_data(html):
    """Next.js __NEXT_DATA__ bloğundan lig verisini çıkar."""
    leagues = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not tag:
            return []

        data = json.loads(tag.string)
        # Olası yolları dene
        page_props = data.get("props", {}).get("pageProps", {})

        # tournaments / sportTree / menu gibi anahtarlar
        for key in ["tournaments", "sportTree", "menuData", "data", "initialData"]:
            obj = page_props.get(key)
            if obj:
                extracted = _extract_leagues_from_obj(obj)
                if extracted:
                    leagues.extend(extracted)
                    break

        # Tüm JSON'u gez (derinlikli arama)
        if not leagues:
            leagues = _deep_find_leagues(data)

    except Exception as e:
        print(f"    [DEBUG] __NEXT_DATA__ parse hatası: {e}")

    return leagues


def _deep_find_leagues(obj, depth=0, max_depth=8):
    """JSON içinde 'tournament', 'league', 'country' anahtar kombinasyonlarını ara."""
    results = []
    if depth > max_depth:
        return results

    if isinstance(obj, dict):
        # Bu düğüm bir lig kaydı gibi görünüyor mu?
        name = obj.get("name") or obj.get("tournamentName") or obj.get("leagueName")
        country = obj.get("country") or obj.get("countryName") or obj.get("sport-country-name")
        url_slug = obj.get("url") or obj.get("slug") or obj.get("href") or ""

        if name and country and "football" in str(url_slug).lower():
            results.append({
                "country": country,
                "league": name,
                "url": url_slug,
            })

        for v in obj.values():
            results.extend(_deep_find_leagues(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(_deep_find_leagues(item, depth + 1, max_depth))

    return results


def _extract_leagues_from_obj(obj):
    """Belirli bir objeden lig listesi çıkar."""
    leagues = []
    if isinstance(obj, list):
        for item in obj:
            leagues.extend(_extract_leagues_from_obj(item))
    elif isinstance(obj, dict):
        # Ülke → lig ağacı yapısı
        country = obj.get("name") or obj.get("country") or ""
        children = obj.get("children") or obj.get("leagues") or obj.get("tournaments") or []
        if children and isinstance(children, list):
            for child in children:
                league_name = child.get("name") or child.get("tournamentName") or ""
                league_url = child.get("url") or child.get("slug") or child.get("href") or ""
                if league_name:
                    leagues.append({
                        "country": country,
                        "league": league_name,
                        "url": league_url,
                    })
        else:
            leagues.extend(_extract_leagues_from_obj(children))
    return leagues


def _parse_sidebar_html(html):
    """OddsPortal sidebar/nav HTML'inden lig linklerini çıkar."""
    leagues = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        # OddsPortal sol menüdeki lig linkleri /football/ULKE/LIG/ formatında
        pattern = re.compile(r"^/football/([^/]+)/([^/]+)/?$")
        seen = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = pattern.match(href)
            if m and href not in seen:
                seen.add(href)
                country_slug = m.group(1)
                league_slug = m.group(2)
                leagues.append({
                    "country": slug_to_name(country_slug),
                    "country_slug": country_slug,
                    "league": slug_to_name(league_slug),
                    "league_slug": league_slug,
                    "url": href,
                })
    except Exception as e:
        print(f"    [DEBUG] HTML sidebar parse hatası: {e}")
    return leagues


def _fetch_ajax_leagues():
    """OddsPortal'ın eski AJAX endpoint'ini dene."""
    leagues = []
    # OddsPortal'ın çeşitli AJAX endpoint'leri
    endpoints = [
        "/ajax-sport-country-tournament_/1/",
        "/api/v2/sport/1/tournaments/",
        "/api/v1/football/tournaments/",
    ]
    for ep in endpoints:
        try:
            r = SESSION.get(BASE_URL + ep, timeout=10)
            if r.status_code == 200:
                data = r.json()
                extracted = _deep_find_leagues(data)
                if extracted:
                    leagues = extracted
                    break
        except Exception:
            continue
    return leagues


def slug_to_name(slug):
    """URL slug'ını okunabilir isme çevir: 'premier-league' → 'Premier League'"""
    return slug.replace("-", " ").title()


# ─── 2. LİG SAYFASINDAN TAKIM İSİMLERİNİ ÇEK ─────────────────────────────

def fetch_teams_for_league(league):
    """
    Bir lig URL'sinden takım isimlerini çeker.
    Standings (puan tablosu) sayfası en temiz kaynaktır.
    """
    url_path = league.get("url", "")
    if not url_path:
        return []

    # Standings sayfasını dene
    standings_url = f"{BASE_URL}{url_path.rstrip('/')}/standings/"
    teams = _scrape_teams_from_page(standings_url)

    # Standings boşsa maç listesi sayfasından çek
    if not teams:
        teams = _scrape_teams_from_matches(f"{BASE_URL}{url_path}")

    return list(dict.fromkeys(teams))  # Tekrarsız, sıralı


def _scrape_teams_from_page(url):
    """Standings sayfasından takım isimlerini çıkar."""
    teams = []
    try:
        time.sleep(DELAY_BETWEEN_LEAGUES)
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")

        # __NEXT_DATA__ içinde ara
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag:
            data = json.loads(tag.string)
            teams = _deep_find_teams(data)
            if teams:
                return teams

        # HTML'den takım linklerini çek: /team/ içeren href'ler
        for a in soup.find_all("a", href=re.compile(r"/team/")):
            name = a.get_text(strip=True)
            if name and len(name) > 1:
                teams.append(name)

    except Exception as e:
        pass

    return teams


def _scrape_teams_from_matches(url):
    """Maç listesi sayfasından takım isimlerini çıkar."""
    teams = []
    try:
        time.sleep(DELAY_BETWEEN_LEAGUES)
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag:
            data = json.loads(tag.string)
            teams = _deep_find_teams(data)

    except Exception:
        pass

    return teams


def _deep_find_teams(obj, depth=0, max_depth=10):
    """JSON içinde takım isimlerini ara."""
    teams = []
    if depth > max_depth:
        return teams

    if isinstance(obj, dict):
        # Maç olayı: home/away team
        for key in ["home-name", "homeName", "home", "away-name", "awayName", "away"]:
            val = obj.get(key)
            if val and isinstance(val, str) and len(val) > 1:
                teams.append(val)

        # Puan tablosu satırı
        for key in ["name", "teamName", "team"]:
            val = obj.get(key)
            if val and isinstance(val, str) and len(val) > 1 and "league" not in val.lower():
                teams.append(val)

        for v in obj.values():
            teams.extend(_deep_find_teams(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            teams.extend(_deep_find_teams(item, depth + 1, max_depth))

    return teams


# ─── 3. GOOGLE SHEETS'E YAZ ───────────────────────────────────────────────

def connect_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def ensure_worksheet(spreadsheet, name, headers):
    try:
        ws = spreadsheet.worksheet(name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=5000, cols=10)
    ws.update("A1", [headers])
    return ws


def write_leagues(spreadsheet, leagues):
    print(f"\n[3a] {len(leagues)} lig → '{WORKSHEET_LEAGUES}' sekmesine yazılıyor...")
    headers = ["#", "Ülke", "Lig", "URL"]
    ws = ensure_worksheet(spreadsheet, WORKSHEET_LEAGUES, headers)

    rows = []
    for i, lg in enumerate(leagues, 1):
        rows.append([
            i,
            lg.get("country", ""),
            lg.get("league", ""),
            lg.get("url", ""),
        ])

    if rows:
        ws.resize(rows=len(rows) + 1)
        ws.update("A2", rows)
    print(f"  ✓ {len(rows)} satır yazıldı.")


def write_teams(spreadsheet, team_rows):
    print(f"\n[3b] {len(team_rows)} takım kaydı → '{WORKSHEET_TEAMS}' sekmesine yazılıyor...")
    headers = ["#", "Ülke", "Lig", "Takım"]
    ws = ensure_worksheet(spreadsheet, WORKSHEET_TEAMS, headers)

    if team_rows:
        ws.resize(rows=len(team_rows) + 1)
        ws.update("A2", [[i + 1, r["country"], r["league"], r["team"]] for i, r in enumerate(team_rows)])
    print(f"  ✓ {len(team_rows)} satır yazıldı.")


# ─── MAIN ─────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  OddsPortal Lig & Takım Scraper — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # 1. Tüm ligleri çek
    leagues = fetch_all_leagues()

    if not leagues:
        print("[HATA] Lig listesi boş geldi. OddsPortal yapısı değişmiş olabilir.")
        print("       Lütfen sorun bildirin.")
        return

    # Sırala: ülkeye göre
    leagues.sort(key=lambda x: (x.get("country", ""), x.get("league", "")))
    print(f"\n  Toplam {len(leagues)} lig bulundu.\n")

    # 2. Her lig için takımları çek
    print(f"[2/3] Takımlar çekiliyor (en fazla {MAX_LEAGUES_FOR_TEAMS} lig)...")
    team_rows = []
    limit = min(len(leagues), MAX_LEAGUES_FOR_TEAMS)

    for i, league in enumerate(leagues[:limit], 1):
        label = f"{league.get('country', '?')} / {league.get('league', '?')}"
        print(f"  [{i}/{limit}] {label} ...", end=" ", flush=True)

        teams = fetch_teams_for_league(league)
        if teams:
            print(f"{len(teams)} takım")
            for t in teams:
                team_rows.append({
                    "country": league.get("country", ""),
                    "league": league.get("league", ""),
                    "team": t,
                })
        else:
            print("takım bulunamadı")

    # 3. Sheets'e yaz
    print("\n[3/3] Google Sheets'e bağlanılıyor...")
    try:
        spreadsheet = connect_sheets()
        write_leagues(spreadsheet, leagues)
        write_teams(spreadsheet, team_rows)
    except Exception as e:
        print(f"\n[HATA] Sheets yazma başarısız: {e}")
        print("       credentials.json ve GOOGLE_SHEET_ID doğru mu?")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  ✓ Tamamlandı! — {now}")
    print(f"  Ligler: {len(leagues)} | Takımlar: {len(team_rows)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
