import urllib.request
import json
import zipfile
import csv
import io
import ssl
import sys
import os

# Wymuszenie ignorowania błędów certyfikatów SSL (niezbędne do testów lokalnych na Windows)
ssl._create_default_https_context = ssl._create_unverified_context

# ========================================================================
# KONFIGURACJA: Lista monitorowanych przystanków
# Skrypt sam znajdzie wszystkie numery ID i stworzy osobną strukturę
TARGET_STOPS = ["Galeria Dominikańska", "Rondo"]
# ========================================================================

# Funkcja pomocnicza: Konwersja czasu HH:MM na minuty od północy (dla ESP32)
def time_to_min(t_str):
    try:
        h, m = map(int, t_str.split(":"))
        return (h % 24) * 60 + m
    except:
        return 0

# Funkcja pomocnicza: Tworzenie bezpiecznych nazw folderów dla ESP32
def get_safe_name(name):
    pl_chars = str.maketrans("ąęćłńóśźżĄĘĆŁŃÓŚŹŻ ", "acelnoszzACELNOSZZ_")
    safe = name.translate(pl_chars).lower()
    return "".join(c for c in safe if c.isalnum() or c == "_")


print("1. Pobieranie listy plików z OpenData Wrocław...")
catalog_url = "https://api.open-data.cui.wroclaw.pl/od2/6/"
req = urllib.request.Request(catalog_url, headers={'User-Agent': 'GitHub-Actions-Bot'})

with urllib.request.urlopen(req) as r:
    catalog = json.loads(r.read().decode())

# Pierwszy plik na liście 'pliki' jest najnowszy
latest_id = catalog["pliki"][0]
download_url = f"https://api.open-data.cui.wroclaw.pl/od2-files/{latest_id}/download/"

print(f"2. Pobieranie paczki ZIP rozkładu (ID pliku: {latest_id})...")
urllib.request.urlretrieve(download_url, "gtfs.zip")

print("3. Przetwarzanie bazy danych GTFS...")
routes = {}              # route_id -> numer linii (np. "145")
trips = {}               # trip_id -> (numer linii, kierunek, brygada)
stops_info = {}          # stop_id -> {"name": ..., "lat": int, "lon": int}
target_stop_ids = {}     # stop_id -> przyjazna_nazwa_przystanku
trips_of_interest = {}   # trip_id -> lista słupków docelowych na tym tripie
preceding_stops = {}     # trip_id -> lista wszystkich przystanków na trasie tego tripa

with zipfile.ZipFile("gtfs.zip") as z:
    # Krok A0: Mapowanie wszystkich przystanków w mieście i szukanie naszych celów
    with z.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            s_id = row["stop_id"]
            stops_info[s_id] = {
                "name": row["stop_name"],
                "lat": int(float(row["stop_lat"]) * 1000000),
                "lon": int(float(row["stop_lon"]) * 1000000)
            }
            
            stop_name_raw = row["stop_name"].lower().strip()
            for target_stop in TARGET_STOPS:
                if stop_name_raw == target_stop.lower().strip():
                    target_stop_ids[s_id] = target_stop
                
    if not target_stop_ids:
        print("BŁĄD: Nie znaleziono żadnego z podanych przystanków w bazie danych!")
        sys.exit(1)
        
    print(f"   -> Znalezione kody ID dla Twoich przystanków: {list(target_stop_ids.keys())}")

    # Krok A: Mapowanie identyfikatorów na numery linii (np. "Klucz_X" -> "145")
    with z.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            routes[row["route_id"]] = row["route_short_name"]
            
    # Krok B: Powiązanie kursów z liniami i kierunkami docelowymi oraz brygadą
    with z.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            r_id = row["route_id"]
            if r_id in routes:
                trips[row["trip_id"]] = {
                    "linia": routes[r_id],
                    "kierunek": row["trip_headsign"],
                    "brygada": int(row["brigade_id"])
                }
                
    # Krok C: Pierwszy pas w stop_times - szukamy kursów, które odwiedzają nasze przystanki
    print("   -> Analiza powiązań sekwencji przystankowych...")
    with z.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            s_id = row["stop_id"]
            t_id = row["trip_id"]
            if s_id in target_stop_ids:
                if t_id not in trips_of_interest:
                    trips_of_interest[t_id] = []
                trips_of_interest[t_id].append({
                    "stop_id": s_id,
                    "seq": int(row["stop_sequence"]),
                    "time": row["departure_time"][:5]
                })

    # Krok D: Drugi pas w stop_times - zbieramy pełne trasy TYLKO dla kursów nas interesujących
    with z.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            t_id = row["trip_id"]
            if t_id in trips_of_interest:
                if t_id not in preceding_stops:
                    preceding_stops[t_id] = []
                preceding_stops[t_id].append({
                    "seq": int(row["stop_sequence"]),
                    "stop_id": row["stop_id"],
                    "time": row["departure_time"][:5]
                })

# Krok E: Budowanie finalnej struktury z wektorami tras (Trace) oraz GPS dla konkretnego kierunku
stop_times = []
for t_id, targets in trips_of_interest.items():
    if t_id not in trips:
        continue
    
    all_stops = sorted(preceding_stops[t_id], key=lambda x: x["seq"])
    
    for target in targets:
        t_seq = target["seq"]
        
        prev_stops = [s for s in all_stops if s["seq"] < t_seq]
        trace_stops = prev_stops[-3:] # 3 punkty pomiarowe wstecz
        
        trace_data = []
        for ps in trace_stops:
            s_info = stops_info.get(ps["stop_id"])
            if s_info:
                trace_data.append({
                    "p": time_to_min(ps["time"]),
                    "g": [s_info["lat"], s_info["lon"]]
                })
        
        # Wyciągamy dokładne koordynaty geograficzne dedykowanego słupka (kierunku) dla tego kursu
        s_info_target = stops_info.get(target["stop_id"])
        exact_gps = [s_info_target["lat"], s_info_target["lon"]] if s_info_target else [0, 0]
        
        stop_times.append({
            "l": trips[t_id]["linia"],
            "k": trips[t_id]["kierunek"],
            "o": target["time"],
            "t": time_to_min(target["time"]),
            "s": target_stop_ids[target["stop_id"]],
            "g": exact_gps,
            "b": trips[t_id]["brygada"],
            "trace": trace_data
        })

# Sortujemy wszystkie odjazdy chronologicznie
stop_times.sort(key=lambda x: x["o"])

# ========================================================================
# KROK 4: Podział na PRZYSTANKI -> LINIE -> GODZINY (Struktura modułowa)
# ========================================================================
print("4. Generowanie ultra-zoptymalizowanych struktur folderów...")

for target_stop in TARGET_STOPS:
    safe_stop_name = get_safe_name(target_stop)
    
    # Filtrujemy bazę pod jeden konkretny przystanek
    stop_specific_times = [dep for dep in stop_times if dep["s"] == target_stop]
    specific_stop_ids = [s_id for s_id, name in target_stop_ids.items() if name == target_stop]
    
    # Wyciągamy unikalne linie występujące na TYM przystanku
    unique_lines = set(dep["l"] for dep in stop_specific_times)
    
    for line_name in unique_lines:
        safe_line_name = get_safe_name(line_name)
        line_folder = f"hours/{safe_stop_name}/{safe_line_name}"
        os.makedirs(line_folder, exist_ok=True)
        
        # Zawężamy odjazdy tylko do tej jednej linii na tym przystanku
        line_specific_times = [dep for dep in stop_specific_times if dep["l"] == line_name]
        
        for hour in range(24):
            hourly_departures = []
            
            allowed_hours = [
                str(hour).zfill(2),
                str((hour + 1) % 24).zfill(2),
                str((hour + 2) % 24).zfill(2)
            ]
            
            for dep in line_specific_times:
                dep_hour = dep["o"][:2]
                if dep_hour == "24": dep_hour = "00"
                elif dep_hour == "25": dep_hour = "01"
                elif dep_hour == "26": dep_hour = "02"
                
                if dep_hour in allowed_hours:
                    hourly_departures.append({
                        "k": dep["k"],
                        "o": dep["o"],
                        "t": dep["t"],
                        "b": dep["b"],
                        "g": dep["g"],  
                        "trace": dep["trace"]
                    })
                    
            output = {
                "stop": target_stop,
                "line": line_name,
                "stop_ids": specific_stop_ids,
                "hour": hour,
                "data": hourly_departures
            }
            
            with open(f"{line_folder}/{hour}.json", "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

print("Sukces! Rozkład pocięty atomowo na relacje: Przystanek -> Linia -> Godzina.")

# ========================================================================
# KROK 5: Dynamiczne generowanie pliku menu (Z WYCZYSZCZONYMI ZNAKAMI PL)
# ========================================================================
print("5. Generowanie pliku konfiguracyjnego menu (menu_config.json)...")

menu_stops = []
menu_lines = []
seen_lines = set()

active_stops = set(dep["s"] for dep in stop_times)

# Mapa do czyszczenia polskich znaków z zachowaniem spacji dla ekranu LCD
pl_to_en_map = str.maketrans("ąęćłńóśźżĄĘĆŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")

for stop in TARGET_STOPS:
    if stop in active_stops:
        safe_stop = get_safe_name(stop)
        
        # --- MODYFIKACJA: Usuwanie polskich znaków i kapitalizacja ---
        clean_friendly_name = stop.translate(pl_to_en_map).upper()
        
        menu_stops.append({
            "friendly": clean_friendly_name,
            "safe": safe_stop
        })

for dep in stop_times:
    stop_safe = get_safe_name(dep["s"])
    line_str = dep["l"]
    
    # Usuwanie polskich znaków również z kierunków (np. SĘPOLNO -> SEPOLNO)
    direction_str = dep["k"].translate(pl_to_en_map).upper().strip() 
    
    line_key = (stop_safe, line_str, direction_str)
    
    if line_key not in seen_lines:
        seen_lines.add(line_key)
        menu_lines.append({
            "stop": stop_safe,
            "line": line_str,
            "dir": direction_str
        })

menu_lines.sort(key=lambda x: (x["stop"], x["line"], x["dir"]))

menu_config_output = {
    "stops": menu_stops,
    "lines": menu_lines
}

with open("hours/menu_config.json", "w", encoding="utf-8") as f:
    json.dump(menu_config_output, f, ensure_ascii=False, indent=2)

print("Sukces! Plik hours/menu_config.json został pomyślnie utworzony (Brak znaków PL).")