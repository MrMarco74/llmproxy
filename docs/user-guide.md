<p align="center">
  <img src="../assets/logo.png" alt="llmproxy logo" width="96">
</p>

# LLM Monitor & llmproxy — Benutzerhandbuch

## Inhaltsverzeichnis
1. [Desktop-App installieren](#1-desktop-app-installieren)
2. [Desktop-App starten und bedienen](#2-desktop-app-starten-und-bedienen)
3. [Live-Dashboard (Browser)](#3-live-dashboard-browser)
4. [ntfy Push-Notifications einrichten](#4-ntfy-push-notifications-einrichten)
5. [Token-Budget konfigurieren](#5-token-budget-konfigurieren)
6. [Modell-Routing konfigurieren](#6-modell-routing-konfigurieren)
7. [Idle Model Eviction konfigurieren](#7-idle-model-eviction-konfigurieren)
8. [FAQ](#8-faq)

---

## 1. Desktop-App installieren

### Voraussetzungen
- Linux (getestet auf Ubuntu 22.04+, Debian 12, Arch)
- Python 3.10+
- `python3-pyqt5` und `python3-httpx` (werden automatisch geprüft)

### Installation

```bash
# Repository klonen oder Dateien auf die Maschine übertragen
git clone <repo-url>
cd llmproxy

# Installer ausführen
bash scripts/install_monitor.sh

# Optional: andere llmproxy-URL angeben (Standard: http://<proxy-host>:11435)
bash scripts/install_monitor.sh --url http://<ip-address>:11435
```

Der Installer:
- Prüft und installiert fehlende Python-Abhängigkeiten
- Kopiert die App nach `~/.local/share/llmproxy/`
- Erstellt den Starter `~/.local/bin/llm-monitor`
- Registriert die App im Startmenü (alle DE: GNOME, KDE, XFCE, ...)

### Deinstallation

```bash
bash scripts/uninstall_monitor.sh
# Konfiguration bleibt erhalten unter ~/.config/llmproxy/
# Manuell löschen: rm -rf ~/.config/llmproxy/
```

### Auf mehreren Maschinen installieren

Die App kann auf beliebig vielen Maschinen gleichzeitig laufen — der Proxy verarbeitet alle SSE-Verbindungen parallel. Jede Instanz verbindet sich unabhängig mit `http://<proxy-host>:11435/status/stream`.

---

## 2. Desktop-App starten und bedienen

### Starten

- **Startmenü**: `LLM Monitor` (Kategorie: System / Monitor)
- **Terminal**: `llm-monitor`
- **Erster Start**: Dialog zur Eingabe der llmproxy-URL (Standard: `http://<proxy-host>:11435`)

### Verbindungs-Status

- 🟢 **Grüner Punkt** oben links: verbunden, Daten fließen
- 🔴 **Roter Punkt**: Verbindung unterbrochen — App reconnectet automatisch alle 3 Sekunden

### Gaming-Mode Banner

Wenn Steam auf <gpu-host> läuft, erscheint ein roter Banner:
> 🎮 GAMING MODE — LLM-Anfragen werden geblockt

LLM-Anfragen von allen Clients werden mit HTTP 503 abgewiesen bis Steam beendet wird.

### Hardware-Sektion

- **CPU**: Auslastung in %
- **RAM**: Verbrauch in MB und Prozent
- **GPU #0 / #1**: Last, VRAM-Verbrauch, Temperatur in °C (rot ≥ 80°C, orange ≥ 70°C)
- Wenn `nvidia-smi` nicht verfügbar ist, bleiben GPU-Felder leer

### Geladene Modelle

Zeigt welche Modelle aktuell im VRAM geladen sind:
- 🟢 Grün: kleiner Kontext (≤ 8k)
- 🟠 Orange: mittlerer Kontext (8k–32k)
- 🔴 Rot: großer Kontext (≥ 32k) — viel VRAM-Druck

### Token-Statistiken

| Feld | Bedeutung |
|---|---|
| Aktives Modell | Modell des letzten Requests (grün = im VRAM) |
| Tokens In/Out /s | Durchsatz des letzten Requests |
| Max In/Out /s | Maximaler Durchsatz seit App-Start |
| Letzte tps | tokens/second des letzten Requests |
| Tokens Session | Gesamt seit App-Start |

### URL ändern

Klick auf das ⚙-Symbol oben rechts öffnet den Konfig-Dialog.

---

## 3. Live-Dashboard (Browser)

Das Dashboard läuft als Docker-Container auf <proxy-host> und ist im Browser erreichbar.

### Zugriff

```
http://<proxy-host>:18080
```

### Seiten

| Seite | URL | Inhalt |
|---|---|---|
| Live | `/` | Hardware-Gauges, geladene Modelle, letzte Requests, Tokens/s-Sparkline |
| Verlauf | `/history?days=7` | Tokens/Tag, tps per Modell, Client-Tabelle, Tool-Use, Scatter |
| Failures | `/failures` | Letzte Fehler mit User-Message, Fehler nach Grund/Modell |

### Docker-Container starten (auf <proxy-host>)

```bash
ssh root@<proxy-host>
cd /opt/llmproxy
docker compose up -d

# Logs verfolgen
docker compose logs -f

# Neustart
docker compose restart llmproxy-dashboard
```

---

## 4. Notifications konfigurieren

llmproxy verwendet ein **internes Notification-System** — kein externer Dienst nötig.
Notifications werden in der SQLite-DB gespeichert und direkt an alle verbundenen Clients verteilt.

### Wie Notifications ankommen

- **Dashboard** (`http://<proxy-host>:18080`): Glocken-Icon oben rechts mit Ungelesen-Badge.
  Klick öffnet ein Dropdown mit allen Notifications. Einzeln oder alle auf einmal als gelesen markierbar.
- **Desktop-App** (`llm-monitor`): System-Tray-Icon mit Toast-Benachrichtigung.
  Der Systembereich zeigt bei neuen Notifications eine kurze Meldung an.

### Events konfigurieren (`/opt/llmproxy/notifications.yaml` auf <proxy-host>)

```yaml
events:
  gaming_mode_start: true   # Steam gestartet
  gaming_mode_end:   true   # Steam beendet
  budget_warning:    true   # 80% des Tages-Budgets
  budget_exceeded:   true   # Budget erschöpft
  tps_anomaly:       true   # Performance-Einbruch erkannt
  thermal_warning:   true   # GPU ≥ 80°C
  model_evicted:     true   # Modell aus VRAM entladen
  load_shedding:     false  # GPU-Überlast
```

### Test

```bash
curl -X POST http://<proxy-host>:11435/debug/notify -H "Content-Type: application/json" \
     -d '{"event": "test"}'
# → Notification erscheint im Dashboard und als Toast in der Desktop-App
```

### Aktuelle Notifications abfragen

```bash
# Alle (letzte 20)
curl http://<proxy-host>:11435/notifications

# Nur ungelesene
curl "http://<proxy-host>:11435/notifications?unread_only=true"
```

Nach Änderungen an `notifications.yaml` muss llmproxy neu gestartet werden:
```bash
ssh root@<proxy-host> 'systemctl restart llmproxy'
```

---

## 5. Token-Budget konfigurieren

Das Token-Budget begrenzt wie viele Tokens (Prompt + Completion) eine IP-Adresse pro Tag verbrauchen darf.

### Konfiguration (`clients.yaml` auf <proxy-host> unter `/opt/llmproxy/`)

```yaml
budgets:
  default: 10_000_000          # Für alle nicht genannten IPs: 10M Tokens
  "<ip-address>":  50_000_000  # Haupt-PC: 50M Tokens
  "<ip-address>":  5_000_000  # Scripts/Open WebUI: 5M Tokens
```

### Verhalten bei Überschreitung

- Bei **80%**: ntfy-Warning (wenn konfiguriert)
- Bei **100%**: HTTP 429 `Too Many Requests` — Request wird abgewiesen
  - Response: `{"error": "daily token budget exceeded", "used": ..., "limit": ..., "retry_after": <Sekunden bis Mitternacht>}`
  - Reset: automatisch täglich um Mitternacht

### Aktuellen Stand abfragen

```bash
curl http://<proxy-host>:11435/budget
# → {"<ip-address>": {"used": 1234567, "limit": 50000000, "pct": 2.5}, ...}
```

### Budget manuell zurücksetzen

```bash
ssh root@<proxy-host> "sqlite3 /root/.llmproxy.db \"DELETE FROM budgets WHERE date = '$(date +%Y-%m-%d)';\""
```

---

## 6. Modell-Routing konfigurieren

Der Auto-Router leitet einfache Anfragen automatisch auf kleinere (schnellere) Modelle um.

### Konfiguration (`routing.yaml`)

```yaml
routes:
  # Sehr einfache Anfragen an qwen3:32b → auf 8B umleiten
  - if_complexity_below: 0.15
    model_pattern: "qwen3:32b"
    route_to: "qwen3:8b"

  # Extrem einfach → immer 4B
  - if_complexity_below: 0.05
    model_pattern: "*"
    route_to: "qwen3:4b"
```

### Complexity-Score

Der Score liegt zwischen `0.0` (trivial) und `1.0` (sehr komplex) und basiert auf:
- Anzahl Nachrichten im Verlauf
- Geschätzte Token-Anzahl (Zeichenanzahl / 4)
- Bonus für Tool-Use

### Routing erkennen

Im HTTP-Response ist bei geroutetem Request der Header `X-LLM-Routed-From: qwen3:32b` gesetzt.

Im Dashboard (Verlauf) ist das Feld `routed_from` in der Datenbank sichtbar.

---

## 7. Idle Model Eviction konfigurieren

Entlädt automatisch Modelle aus dem VRAM wenn sie längere Zeit nicht benutzt wurden und der VRAM-Druck hoch ist.

### Konfiguration (`eviction.yaml`)

```yaml
eviction_timeout_min: 20       # Modell nach 20min Idle entladen
vram_threshold_pct: 75         # Nur entladen wenn VRAM > 75% belegt

never_evict:
  - "nomic-embed-text"         # Embedding-Modell immer halten
  - "nomic-embed-text:latest"
```

### Verhalten

- Prüft alle 60 Sekunden
- Entlädt nur wenn **sowohl** Timeout **als auch** VRAM-Schwellwert erfüllt sind
- Sendet ntfy-Notification (wenn konfiguriert)
- Eintrag in `failures`-Tabelle mit `failure_reason="model_evicted"`

---

## 8. FAQ

**Q: Warum bekomme ich HTTP 503?**

Zwei mögliche Ursachen:
1. **Gaming-Mode**: Steam läuft auf <gpu-host>. Warte bis Steam beendet ist.
2. **Load Shedding**: GPU-Last > 95% für > 30s. Warte kurz und versuche es erneut.

Response enthält: `{"error": "<gpu-host> is in gaming mode", "gaming_mode": true, "retry_after": 60}`

---

**Q: Warum bekomme ich HTTP 429?**

Dein Tages-Token-Budget ist erschöpft. Die Response enthält `retry_after` mit den Sekunden bis zum Reset (Mitternacht).

---

**Q: Die Desktop-App zeigt "Verbindung verloren"**

- Ist <proxy-host> eingeschaltet/erreichbar? `ping <proxy-host>`
- Läuft llmproxy? `ssh root@<proxy-host> 'systemctl status llmproxy'`
- Ist Port 11435 erreichbar? `curl http://<proxy-host>:11435/health`
- Richtige URL in der App? (⚙-Button → URL prüfen)
- Falls Hardware-Gauges/Gaming-Mode fehlen: ist <gpu-host> (samt `<gpu-host>-agent`,
  Port 11436) erreichbar? `curl http://<gpu-host>:11436/status`

---

**Q: Mein Modell wurde unerwartet entladen**

Das Idle-Eviction-Feature hat das Modell nach Inaktivität aus dem VRAM entladen. Im Dashboard unter `Failures` siehst du wann und warum. Um ein Modell dauerhaft zu behalten, trage es in `eviction.yaml` unter `never_evict` ein.

---

**Q: Wie stelle ich den Auto-Router ab?**

Leere die `routes`-Liste in `routing.yaml`:
```yaml
routes: []
```
Dann llmproxy neu starten: `ssh root@<proxy-host> 'systemctl restart llmproxy'`

---

**Q: Wie sehe ich die rohen Daten in der DB?**

```bash
ssh root@<proxy-host> 'sqlite3 /root/.llmproxy.db'
# Dann z.B.:
.tables
SELECT model, COUNT(*), AVG(tokens_per_second) FROM requests GROUP BY model;
SELECT * FROM failures ORDER BY id DESC LIMIT 10;
.quit
```

---

**Q: Notifications erscheinen nicht im System-Tray**

Der System-Tray muss in deiner Desktop-Umgebung aktiviert sein. Prüfe:
- GNOME: Erweiterung `AppIndicator` oder `TopIcons Plus` aktiv?
- KDE: Systembereich-Einstellungen → Applikationsstatus aktiviert?
- Test: `curl -X POST http://<proxy-host>:11435/debug/notify -d '{"event":"test"}' -H "Content-Type: application/json"` — Notification sollte im Dashboard-Bell-Icon erscheinen.

---

**Q: Der Gaming-Mode-Block funktioniert nicht / Steam wird nicht erkannt**

Die Erkennung läuft im `<gpu-host>-agent` auf <gpu-host> (nicht mehr im Proxy selbst —
der läuft jetzt auf <proxy-host> und hat keinen lokalen Zugriff auf den Steam-Prozess).
Prüfe zunächst den Agent direkt:
```bash
curl http://<gpu-host>:11436/status   # → "gaming_mode": true/false
ssh root@<gpu-host> 'which pgrep && pgrep -x steam; echo "exit: $?"'
```
`pgrep` sucht nach dem Prozess-Namen `steam` (exakt). Bei Flatpak-Steam kann der Name abweichen — dann den Prozessnamen mit `ps aux | grep -i steam` ermitteln und das Pgrep-Kommando in `<gpu-host>-agent.py` (auf <gpu-host>, `/opt/llmproxy/<gpu-host>-agent.py`) anpassen, <gpu-host>ch `systemctl restart <gpu-host>-agent`.
