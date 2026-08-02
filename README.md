# DL2MBN Tune Assist (PC-App)

Python-Desktop-App (Windows/Mac/Linux) für den **Yaesu FT-710** (oder,
über Hamlib, praktisch jedes andere unterstützte Funkgerät) am PC per
CAT-Kabel. Drei Funktionen in einer App:

1. **POTA-Spot-Liste** (live von `api.pota.app/spot/activator`): Doppelklick
   auf einen Spot stellt Frequenz und Mode automatisch am Funkgerät ein.
2. **TUNE-Taste** (gedrückt halten): Funkgerät geht ca. 5 kHz ober- oder
   unterhalb der aktuellen Frequenz, schaltet auf CW, reduziert die Leistung
   und sendet einen Dauerton. Beim Loslassen wird zurück auf die
   ursprüngliche Frequenz, Betriebsart und Leistung geschaltet.
3. **Log Contact**: Klick auf "📝 Log" in einer Spot-Zeile öffnet einen
   QSO-Dialog (Rufzeichen, Datum/Zeit UTC, Band, Freq, Mode, RST, Name,
   Locator, POTA-Referenz, Kommentar - vorausgefüllt aus dem Spot). Beim
   Klick auf "Log Contact" wird der Kontakt als ADIF-Datensatz gespeichert
   und, falls ein QRZ-API-Key hinterlegt ist, automatisch ins eigene
   QRZ-Logbuch hochgeladen.

## Zwei Rig-Backends

In den **Settings** lässt sich zwischen zwei Backends wählen:

- **FT-710 (CAT direkt)** – wie bisher: die App spricht das Yaesu-"New
  CAT"-Protokoll direkt über den seriellen Port (COM-Port + Baudrate).
- **Hamlib rigctld (Netzwerk)** – die App spricht `rigctld` (Teil von
  [Hamlib](https://hamlib.github.io/)) per TCP an. Da `rigctld` die
  Protokollunterschiede zwischen den Funkgeräten selbst abstrahiert,
  funktioniert dieses Backend mit jedem der ca. 300 von Hamlib
  unterstützten Transceiver, nicht nur dem FT-710.

  Voraussetzung: Hamlib ist installiert (Linux: meist über die
  Paketverwaltung, z. B. `apt install libhamlib-utils`; Windows/Mac:
  [hamlib.github.io](https://hamlib.github.io/)) - mehr nicht, kein
  eigenes Terminal nötig. In den Settings bei Backend "Hamlib rigctld"
  wählen: Feld **Rig-Modell** listet automatisch alle von der
  installierten Hamlib-Version unterstützten Funkgeräte (über
  `rigctld -l` ausgelesen, tippen filtert die Liste), **CAT-Port**
  ist wie beim FT-710-Backend der serielle Port des Funkgeräts. Bleibt
  **Host** auf "localhost" (Standard), startet die App beim Klick auf
  **Verbinden** `rigctld` selbst im Hintergrund mit dem gewählten
  Modell + Port und beendet es beim Trennen wieder - die App merkt
  sich das zuletzt gewählte Modell für den nächsten Start. Ein anderer
  Host verbindet stattdessen zu einem bereits andernorts laufenden
  `rigctld` (Rig-Modell-Auswahl wird dann ignoriert), für den
  Netzwerk-Betrieb weiterhin per Hand gestartet:

  ```bash
  rigctld -m <Modell-Nr.> -r COM5        # Windows-Beispiel
  rigctld -m <Modell-Nr.> -r /dev/ttyUSB0  # Linux-Beispiel
  ```

  **Wichtiger Unterschied beim TUNE-Feld:** Im rigctld-Backend ist die
  Leistungsangabe kein Watt-Wert, sondern Hamlibs normalisierte
  `RFPOWER`-Stufe (0.0–1.0 = Anteil der Maximalleistung), da Hamlib
  keinen einheitlichen absoluten Watt-Setter über alle Rig-Backends
  hinweg anbietet. Das Feld beschriftet sich beim Umschalten
  entsprechend um ("Power Level (0–1)" statt "Leistung (W)").

Oberfläche im dunklen, militärisch angehauchten Olivgrün-Theme (Filter-Chips,
Statusbadges, farbcodierte Tabelle nach Betriebsart, rot durchgestrichene
Zeilen für als ungültig gemeldete Spots). Eine Einschränkung dabei: Tkinters
`Treeview` kann pro Zeile nur eine Textfarbe vergeben, keine einzeln
eingefärbten Buttons *innerhalb* einer Zelle – "QSY"/"Skip" sind daher
klickbare Textfelder in der jeweiligen Zeilenfarbe statt eigenständiger
Badges.

Anders als die Cardputer-Variante dieses Projekts gibt es hier **kein
USB-Host/VBUS-Problem**: Der PC ist ein ganz normaler USB-Host und liefert
die 5V selbst. Ein einfaches USB-Kabel (bzw. das mitgelieferte
USB-A/USB-C-zu-USB-B-Kabel) zwischen PC und FT-710 reicht aus.

## Installation

### Windows: fertige .exe

[`DL2MBN-Tune-Assist.exe`](DL2MBN-Tune-Assist.exe) liegt bereits im Ordner –
kein Python nötig, einfach starten. Sie wird automatisch per GitHub-Actions-
Workflow (`.github/workflows/build-windows-exe.yml`) auf einem echten
`windows-latest`-Runner gebaut und bei Änderungen an `pc-app/` neu committet,
nicht von Hand. Windows SmartScreen warnt bei unsignierten .exe-Dateien aus
dem Internet – das ist bei selbst gebauten PyInstaller-Programmen normal.

### Aus dem Quellcode starten (Windows/Mac/Linux)

```bash
cd pc-app
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python pota_tune_assist.py
```

Unter Windows/Mac ist `tkinter` normalerweise Teil der Standard-Python-
Installation. Unter Linux ggf. separat installieren (Debian/Ubuntu:
`sudo apt install python3-tk`).

Eigene .exe bauen (z. B. mit angepassten Konstanten):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "DL2MBN-Tune-Assist" pota_tune_assist.py
```

## FT-710 CAT-Einstellungen

- USB-Port im Menü auf **CAT** stellen (CAT-1).
- **CAT RATE** auf denselben Wert wie in der App (Standard: `38400`).

## Bedienung

1. **Settings** (oben rechts) öffnen, **Backend** wählen (FT-710 direkt
   oder Hamlib rigctld), die dazu passenden Felder ausfüllen (COM-Port +
   Baudrate bzw. Host + Port) und **Verbinden** klicken. Das CAT-Badge
   oben rechts wird grün, sobald die Verbindung steht.
2. Die Spot-Liste aktualisiert sich automatisch alle 60 Sekunden ("Auto: An"
   umschaltbar, oder manuell über "Refresh"). Band-, Mode-Dropdown sowie das
   Suchfeld filtern die Tabelle. Der **Land-Button** (zeigt "Alle Länder",
   ein Ländercode oder "N Länder") öffnet einen Mehrfachauswahl-Dialog mit
   Checkboxen für jedes Land, das gerade (oder je schon einmal) in der
   Spot-Liste vorkam ("Alle"/"Keine" zum Schnellauswählen, sowie ein
   **Kontinent-Schnellwahl** EU/NA/SA/AS/AF/OC - klickt alle bekannten
   Länder des jeweiligen Kontinents an bzw. wieder ab) - damit lässt
   sich dauerhaft festlegen, welche Länder man hunten will; nichts
   ausgewählt = alle Länder. Die Auswahl wird in `dl2mbn_config.json`
   gespeichert und bleibt über Neustarts hinweg erhalten. "Skip" blendet
   einzelne Spots aus, "Alle anzeigen" setzt das zurück. **"CAT-Log"**
   öffnet ein Debug-Fenster mit den rohen CAT-Befehlen/-Antworten (nur
   FT-710-CAT-Backend) - hilfreich, um bei Frequenz-/Mode-Problemen exakt
   zu sehen, was auf der Leitung passiert.
3. Klick auf **▶ QSY** (oder Doppelklick auf die Zeile) → Funkgerät QSYt
   automatisch auf Frequenz + Mode des Spots. Als ungültig gemeldete Spots
   werden rot durchgestrichen dargestellt, bleiben aber anklickbar. Der
   **☆-Stern** links von QSY markiert ein Rufzeichen als Favorit (⭐) -
   favorisierte Rufzeichen werden über alle Bands/Modes hinweg dauerhaft
   gemerkt (`dl2mbn_config.json`) und ihre Spots erscheinen immer ganz
   oben in der Liste, egal wie sortiert/gefiltert wird. Favoriten-Zeilen
   bekommen zusätzlich einen eigenen goldbraunen Zeilenhintergrund plus
   fette Schrift statt der normalen Mode-Farbcodierung, damit sie auch
   in einer langen Spot-Liste sofort auffallen (die Textfarbe nach Mode
   bleibt dabei erhalten).
4. **TUNE (halten)**-Knopf gedrückt halten → Versatz (siehe unten), CW, 5 W,
   Dauerton. Loslassen → zurück auf die vorherige Frequenz/Mode/Leistung.
   Ein 10-Sekunden-Sicherheits-Timeout schaltet automatisch ab.
5. Versatzrichtung (oberhalb/unterhalb) und Leistung stehen direkt in der
   Tune-Leiste am unteren Fensterrand.

## TUNE-Versatz: automatisch nach Modus-Bandbreite

Der Versatz beim Tunen ist kein fester Hz-Wert mehr, sondern richtet sich
automatisch nach der typischen Bandbreite des Modus, in dem gerade
gearbeitet wird (ausgelesen direkt vor dem Umschalten auf CW) - man landet
damit immer direkt neben der eigenen Frequenz, egal ob SSB oder CW:

| Modus | Versatz |
|---|---|
| CW / CW-R | 500 Hz |
| LSB / USB | 2400 Hz |
| RTTY / RTTY-R | 500 Hz |
| Digital (PKTLSB/PKTUSB, z. B. FT8) | 2400 Hz |
| AM | 6000 Hz |
| FM / PKTFM / C4FM | 12500 Hz |

Das Feld **"Versatz Fallback (Hz)"** in der Tune-Leiste greift nur noch,
wenn der aktuell ausgelesene Modus in keiner der obigen Zeilen steht - im
Normalbetrieb (SSB/CW/Digital) bleibt es also ungenutzt. Die Statuszeile
beim TUNE-Start zeigt immer den tatsächlich verwendeten Versatz inkl. Modus
an, z. B. `... (USB-Bandbreite 2400 Hz) ...`.

Schlägt das Auslesen der aktuellen Werte beim Tastendruck fehl (z. B. CAT
nicht verbunden), wird **nicht** gesendet – dieselbe Sicherheitslogik wie
in der Cardputer-Firmware.

Frequenz- und Mode-Befehle (bei QSY, beim Start von TUNE und beim
Wiederherstellen nach dem Loslassen) werden **immer doppelt** hintereinander
gesendet und anschließend per Rücklese-Abfrage geprüft; bei Abweichung wird
das ganze Paar bis zu zweimal wiederholt - manche Funkgeräte verarbeiten
einen einzelnen CAT-Befehl gelegentlich nicht sauber, vor allem direkt nach
einem vorherigen Befehl (z. B. `TX0;` gefolgt von `FA...;`), landen aber mit
einem wiederholten Befehl zuverlässig exakt auf dem Zielwert. Die
Statuszeile zeigt bei QSY und TUNE-Ende die exakte Ziel- neben der vom
Funkgerät bestätigten Hz-Zahl an, damit sich das im Zweifel gegen die
Anzeige am Gerät vergleichen lässt. Bleibt der Zielwert auch nach den
Wiederholungen unbestätigt, erscheint zusätzlich eine Warnung.

**Reihenfolge Mode vor Frequenz:** Bei QSY sowie TUNE-Start/-Stop wird immer
zuerst der Mode gesetzt und erst danach die Frequenz - nicht umgekehrt. Grund:
Auf manchen Yaesu-Geräten verschiebt ein Moduswechsel (v. a. rein/raus aus CW)
die tatsächliche VFO-Frequenz um den CW-Pitch/Offset. Würde man erst die
Frequenz und danach den Mode setzen, würde der nachfolgende Moduswechsel die
gerade gesetzte Frequenz wieder um diesen Betrag verschieben - per CAT
messbar am `FA`-Rücklesewert unmittelbar vor/nach dem Moduswechsel im
CAT-Log.

## Log Contact / ADIF / QRZ-Logbook

1. In den **Settings** unter "Log / QRZ Logbook" einmalig das eigene
   Rufzeichen (Pflichtfeld, sonst öffnet sich der Log-Dialog nicht), optional
   den eigenen Locator und den **QRZ-Logbook-API-Key** eintragen und
   "Speichern" klicken. Der QRZ-API-Key steht im QRZ-Logbook unter
   *Settings → API Key* (nicht der allgemeine XML-Subscription-Key eines
   Drittanbieters, sondern der Logbook-eigene). Diese Angaben werden lokal
   in `dl2mbn_config.json` neben dem Programm gespeichert (nicht in Git,
   siehe `.gitignore`). Ist kein API-Key hinterlegt, wird nur lokal
   geloggt, ohne QRZ-Upload.
2. In der Spot-Tabelle auf **📝 Log** in der gewünschten Zeile klicken. Der
   Dialog ist mit Rufzeichen, aktuellem UTC-Datum/-Zeit, Band, Frequenz,
   Mode (aus dem Spot) sowie der POTA-Referenz als `SIG_INFO` vorausgefüllt
   und komplett editierbar (z. B. für RST, Name, Locator, Kommentar).
3. **Log Contact** klicken: Der QSO wird als ADIF-Datensatz an
   `pc-app/logs/dl2mbn_log_<QSO_DATE>.adi` angehängt - **pro Tag eine eigene
   Datei**, ein neuer Tag landet nie in der Datei des Vortages. Ist ein
   API-Key hinterlegt, wird derselbe Datensatz zusätzlich automatisch per
   QRZ-Logbook-API (`https://logbook.qrz.com/api`, `ACTION=INSERT`) ins
   eigene QRZ-Logbuch hochgeladen; Erfolg/Fehler erscheinen in der Statusleiste.
4. Der geloggte Spot bleibt in der Liste sichtbar, wird aber ab sofort
   durchgestrichen dargestellt (wie ungültig gemeldete Spots, nur in
   gedämpftem Grün statt Rot) - so bleibt erkennbar, welche Stationen aus
   der aktuellen Spot-Liste schon geloggt wurden, ohne sie wie "Skip"
   komplett auszublenden.

## Heute schon gearbeitet / New Band-Mode-Park

Die App liest die heutige ADIF-Logdatei (`pc-app/logs/dl2mbn_log_<heute>.adi`)
beim Start und danach alle 60 Sekunden neu ein (außerdem sofort nach jedem
"Log Contact") und gleicht jeden Spot in der Tabelle dagegen ab - Ergebnis
steht in der neuen Spalte **HEUTE**:

- **Rufzeichen heute noch gar nicht gearbeitet:** keine Markierung.
- **Exakt derselbe Band/Mode/Park für dieses Rufzeichen bereits geloggt:**
  "♻ Heute schon gearbeitet" - der Spot wird zusätzlich wie ein bereits
  geloggter Kontakt durchgestrichen dargestellt (auch wenn er unter einer
  neuen Spot-ID erneut gespottet wurde).
- **Rufzeichen heute schon gearbeitet, aber Band, Mode oder Park sind
  neu:** "🆕 New Band/Mode/Park (Mode Freq Park)", z. B.
  `🆕 New Band (CW 14074.0 DE-1234)` - zeigt genau, welche Dimension(en)
  neu sind, mit den aktuellen Werten des Spots in Klammern.

Der Abgleich erfolgt pro Rufzeichen unabhängig vom aktuellen Spot - auch ein
Re-Spot derselben Station (neue Spot-ID) wird korrekt erkannt.

## Mode-Zuordnung für QSY

POTA-Spots melden nur "SSB" (nicht LSB/USB einzeln). Die App wählt
automatisch **LSB unterhalb 10 MHz, USB oberhalb** – der übliche
Bandplan-Standard. Digitalmodi (FT8, FT4, PSK, ...) werden als CAT-Mode
**DATA-USB** eingestellt. Die Zuordnung steht in `resolve_mode_char()` in
`pota_tune_assist.py` und lässt sich dort anpassen.

## Sicherheitshinweise

- Nur mit angeschlossener Antenne bzw. Dummy-Load und gültiger
  Amateurfunk-Lizenz verwenden.
- Der 10-Sekunden-Timeout ersetzt keine Aufsicht.
- Nutzt ausschließlich CAT-Befehle (`TX1;`/`TX0;`), keine Hardware-PTT.
- Die POTA-API wird standardmäßig alle 60 Sekunden abgefragt (siehe
  `POTA_POLL_SECONDS_DEFAULT`) – bitte nicht künstlich verkürzen, um den
  öffentlichen Dienst nicht zu überlasten.
