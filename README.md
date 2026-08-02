# POTA Tune Assist

Python-Desktop-App (Windows/Mac/Linux) für den Amateurfunk: verbindet sich
per CAT mit dem Funkgerät, zeigt eine live aktualisierte POTA-Spot-Liste,
tunt die Antenne per Knopfdruck und loggt QSOs inklusive automatischem
QRZ-Upload – alles in einer Oberfläche.

## Funktionen im Überblick

- **POTA-Spot-Liste** – live von `api.pota.app/spot/activator`, automatische
  Aktualisierung alle 60 Sekunden (abschaltbar) oder manuell per "Refresh".
- **QSY per Klick** – Doppelklick auf einen Spot (oder Klick auf "▶ QSY")
  stellt Frequenz und Mode automatisch am Funkgerät ein.
- **TUNE-Taste (gedrückt halten)** – Funkgerät weicht automatisch von der
  aktuellen Frequenz ab, schaltet auf CW, reduziert die Leistung und sendet
  einen Dauerton; beim Loslassen geht alles zurück auf die ursprüngliche
  Frequenz, Betriebsart und Leistung. Ein 10-Sekunden-Sicherheits-Timeout
  schaltet automatisch ab, falls losgelassen vergessen wird.
- **Automatischer TUNE-Versatz nach Modus-Bandbreite** – kein fester Hz-Wert,
  sondern passend zum aktuell aktiven Modus (siehe Tabelle unten), damit man
  immer direkt neben der eigenen Frequenz landet.
- **Log Contact** – Klick auf "📝 Log" in einer Spot-Zeile öffnet einen
  vorausgefüllten QSO-Dialog (Rufzeichen, Datum/Zeit UTC, Band, Frequenz,
  Mode, RST, Name, Locator, POTA-Referenz, Kommentar); der Kontakt wird als
  ADIF-Datensatz gespeichert und, falls ein QRZ-API-Key hinterlegt ist,
  automatisch ins eigene QRZ-Logbuch hochgeladen.
- **"Heute schon gearbeitet"-Erkennung** – jeder Spot wird gegen das
  heutige Log abgeglichen und mit "♻ Heute schon gearbeitet" oder
  "🆕 New Band/Mode/Park (...)" markiert, je nachdem ob Band, Mode oder Park
  für dieses Rufzeichen bereits neu sind.
- **Favoriten** – ☆-Stern neben einem Spot markiert ein Rufzeichen dauerhaft
  als Favorit; favorisierte Spots erscheinen immer oben in der Liste und
  sind farblich hervorgehoben, egal wie sortiert oder gefiltert wird.
- **Filter** – Band- und Mode-Dropdown, Freitext-Suchfeld sowie ein
  Mehrfachauswahl-Dialog für Länder (mit Kontinent-Schnellwahl EU/NA/SA/
  AS/AF/OC); die Länderauswahl bleibt über Neustarts hinweg erhalten.
  "Skip" blendet einzelne Spots aus, "Alle anzeigen" setzt das zurück.
- **CAT-Log** – Debug-Fenster mit den rohen CAT-Befehlen/-Antworten (nur
  beim FT-710-CAT-Backend), hilfreich bei Frequenz-/Mode-Problemen.
- **Zuverlässige CAT-Kommunikation** – Frequenz- und Mode-Befehle werden bei
  QSY, TUNE-Start und TUNE-Ende doppelt gesendet und per Rücklese-Abfrage
  geprüft, bei Abweichung bis zu zweimal wiederholt; der Mode wird dabei
  immer vor der Frequenz gesetzt, um modusbedingte VFO-Verschiebungen (z. B.
  CW-Pitch) zu vermeiden.

## Zwei Rig-Backends

In den **Settings** lässt sich zwischen zwei Backends wählen:

- **FT-710 (CAT direkt)** – die App spricht das Yaesu-"New CAT"-Protokoll
  direkt über den seriellen Port (COM-Port + Baudrate, Standard `38400`).
- **Hamlib rigctld (Netzwerk)** – die App spricht `rigctld` (Teil von
  [Hamlib](https://hamlib.github.io/)) per TCP an. Da `rigctld` die
  Protokollunterschiede zwischen den Funkgeräten selbst abstrahiert,
  funktioniert dieses Backend mit jedem der von Hamlib unterstützten
  Transceiver, nicht nur dem FT-710.

  Voraussetzung: Hamlib ist installiert (Linux: z. B.
  `apt install libhamlib-utils`; Windows/Mac: [hamlib.github.io](https://hamlib.github.io/)).
  In den Settings bei Backend "Hamlib rigctld" wählen: Das Feld
  **Rig-Modell** listet automatisch alle von der installierten
  Hamlib-Version unterstützten Funkgeräte (tippen filtert die Liste),
  **CAT-Port** ist der serielle Port des Funkgeräts. Bleibt **Host** auf
  "localhost" (Standard), startet die App beim Klick auf **Verbinden**
  `rigctld` selbst im Hintergrund mit dem gewählten Modell + Port und
  beendet es beim Trennen wieder – das zuletzt gewählte Modell wird für
  den nächsten Start gemerkt. Ein anderer Host verbindet stattdessen zu
  einem bereits andernorts laufenden `rigctld` (Rig-Modell-Auswahl wird
  dann ignoriert):

  ```bash
  rigctld -m <Modell-Nr.> -r COM5        # Windows-Beispiel
  rigctld -m <Modell-Nr.> -r /dev/ttyUSB0  # Linux-Beispiel
  ```

  **Unterschied beim TUNE-Feld:** Im rigctld-Backend ist die
  Leistungsangabe kein Watt-Wert, sondern Hamlibs normalisierte
  `RFPOWER`-Stufe (0.0–1.0 = Anteil der Maximalleistung). Das Feld
  beschriftet sich beim Umschalten entsprechend um ("Power Level (0–1)"
  statt "Leistung (W)").

Oberfläche im dunklen, militärisch angehauchten Olivgrün-Theme (Filter-Chips,
Statusbadges, farbcodierte Tabelle nach Betriebsart, rot durchgestrichene
Zeilen für als ungültig gemeldete Spots, gedämpft grün durchgestrichene
Zeilen für bereits geloggte Kontakte).

## Installation

### Windows: fertige .exe

[`POTA-Tune-Assist.exe`](POTA-Tune-Assist.exe) liegt bereits im Ordner –
kein Python nötig, einfach starten. Windows SmartScreen warnt bei
unsignierten .exe-Dateien aus dem Internet – das ist bei selbst gebauten
PyInstaller-Programmen normal.

### Aus dem Quellcode starten (Windows/Mac/Linux)

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python pota_tune_assist.py
```

Unter Windows/Mac ist `tkinter` normalerweise Teil der Standard-Python-
Installation. Unter Linux ggf. separat installieren (Debian/Ubuntu:
`sudo apt install python3-tk`).

Eigene .exe bauen:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "POTA-Tune-Assist" pota_tune_assist.py
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
   **Kontinent-Schnellwahl** EU/NA/SA/AS/AF/OC – klickt alle bekannten
   Länder des jeweiligen Kontinents an bzw. wieder ab). Die Auswahl wird in
   `pota_tune_assist_config.json` gespeichert und bleibt über Neustarts hinweg
   erhalten. "Skip" blendet einzelne Spots aus, "Alle anzeigen" setzt das
   zurück. **"CAT-Log"** öffnet ein Debug-Fenster mit den rohen
   CAT-Befehlen/-Antworten (nur FT-710-CAT-Backend).
3. Klick auf **▶ QSY** (oder Doppelklick auf die Zeile) → Funkgerät QSYt
   automatisch auf Frequenz + Mode des Spots. Als ungültig gemeldete Spots
   werden rot durchgestrichen dargestellt, bleiben aber anklickbar. Der
   **☆-Stern** links von QSY markiert ein Rufzeichen als Favorit (⭐) –
   favorisierte Rufzeichen werden über alle Bands/Modes hinweg dauerhaft
   gemerkt (`pota_tune_assist_config.json`) und ihre Spots erscheinen immer ganz
   oben in der Liste, egal wie sortiert/gefiltert wird. Favoriten-Zeilen
   bekommen zusätzlich einen eigenen goldbraunen Zeilenhintergrund plus
   fette Schrift statt der normalen Mode-Farbcodierung. Die **🏕-Spalte**
   links daneben markiert Rufzeichen aus der Draußenfunker-Liste (siehe
   unten) mit eigenem blaugrünem Zeilenhintergrund – auch diese Spots
   erscheinen ganz oben (nach den Favoriten).
4. **TUNE (halten)**-Knopf gedrückt halten → Versatz (siehe unten), CW, 5 W,
   Dauerton. Loslassen → zurück auf die vorherige Frequenz/Mode/Leistung.
   Ein 10-Sekunden-Sicherheits-Timeout schaltet automatisch ab.
5. Versatzrichtung (oberhalb/unterhalb) und Leistung stehen direkt in der
   Tune-Leiste am unteren Fensterrand.

## Draußenfunker-Liste

Eigene Rufzeichen-Liste (z. B. befreundete Aktivierende), die beim
**Programmstart** einmal aus der Textdatei `draussenfunker.txt` (liegt
neben dem Programm bzw. der .exe) eingelesen wird – ein Rufzeichen pro
Zeile, Groß-/Kleinschreibung egal:

```
# Zeilen mit # sind Kommentare, Leerzeilen werden ignoriert
DL2MBN
OE1ABC   Hans, aktiviert oft im Schwarzwald
DL3XYZ
```

Text nach dem ersten Leerzeichen in einer Zeile wird ignoriert – dort kann
also z. B. der Name oder eine Notiz stehen. Existiert die Datei nicht, ist
die Liste einfach leer, es gibt keine Fehlermeldung.

Taucht ein Spot mit einem Rufzeichen aus dieser Liste auf, bekommt die
Zeile in der Tabelle die **🏕-Markierung** in der Spalte links neben
"▶ QSY" sowie einen eigenen blaugrünen Zeilenhintergrund (zur
Unterscheidung von den goldenen ⭐-Favoriten) und wird – wie Favoriten –
automatisch nach oben in der Liste einsortiert (Favoriten zuerst, danach
Draußenfunker-Treffer, danach der Rest). Anders als die Favoriten ist die
Liste nicht in der App editierbar, sondern wird ausschließlich über die
Textdatei gepflegt; Änderungen daran wirken erst nach einem Neustart der
App.

## TUNE-Versatz: automatisch nach Modus-Bandbreite

Der Versatz beim Tunen richtet sich automatisch nach der typischen
Bandbreite des Modus, in dem gerade gearbeitet wird (ausgelesen direkt vor
dem Umschalten auf CW) – man landet damit immer direkt neben der eigenen
Frequenz, egal ob SSB oder CW:

| Modus | Versatz |
|---|---|
| CW / CW-R | 500 Hz |
| LSB / USB | 2400 Hz |
| RTTY / RTTY-R | 500 Hz |
| Digital (PKTLSB/PKTUSB, z. B. FT8) | 2400 Hz |
| AM | 6000 Hz |
| FM / PKTFM / C4FM | 12500 Hz |

Das Feld **"Versatz Fallback (Hz)"** in der Tune-Leiste greift nur, wenn der
aktuell ausgelesene Modus in keiner der obigen Zeilen steht. Die Statuszeile
beim TUNE-Start zeigt immer den tatsächlich verwendeten Versatz inkl. Modus
an, z. B. `... (USB-Bandbreite 2400 Hz) ...`.

Schlägt das Auslesen der aktuellen Werte beim Tastendruck fehl (z. B. CAT
nicht verbunden), wird **nicht** gesendet.

Frequenz- und Mode-Befehle (bei QSY, beim Start von TUNE und beim
Wiederherstellen nach dem Loslassen) werden immer doppelt hintereinander
gesendet und anschließend per Rücklese-Abfrage geprüft; bei Abweichung wird
das ganze Paar bis zu zweimal wiederholt. Die Statuszeile zeigt bei QSY und
TUNE-Ende die exakte Ziel- neben der vom Funkgerät bestätigten Hz-Zahl an.
Bleibt der Zielwert auch nach den Wiederholungen unbestätigt, erscheint
zusätzlich eine Warnung.

**Reihenfolge Mode vor Frequenz:** Bei QSY sowie TUNE-Start/-Stop wird immer
zuerst der Mode gesetzt und erst danach die Frequenz. Grund: Auf manchen
Yaesu-Geräten verschiebt ein Moduswechsel (v. a. rein/raus aus CW) die
tatsächliche VFO-Frequenz um den CW-Pitch/Offset. Würde man erst die
Frequenz und danach den Mode setzen, würde der nachfolgende Moduswechsel die
gerade gesetzte Frequenz wieder um diesen Betrag verschieben.

## Log Contact / ADIF / QRZ-Logbook

1. In den **Settings** unter "Log / QRZ Logbook" einmalig das eigene
   Rufzeichen (Pflichtfeld, sonst öffnet sich der Log-Dialog nicht), optional
   den eigenen Locator und den **QRZ-Logbook-API-Key** eintragen und
   "Speichern" klicken. Der QRZ-API-Key steht im QRZ-Logbook unter
   *Settings → API Key* (nicht der allgemeine XML-Subscription-Key, sondern
   der Logbook-eigene). Diese Angaben werden lokal in `pota_tune_assist_config.json`
   neben dem Programm gespeichert. Ist kein API-Key hinterlegt, wird nur
   lokal geloggt, ohne QRZ-Upload.
2. In der Spot-Tabelle auf **📝 Log** in der gewünschten Zeile klicken. Der
   Dialog ist mit Rufzeichen, aktuellem UTC-Datum/-Zeit, Band, Frequenz,
   Mode (aus dem Spot) sowie der POTA-Referenz als `SIG_INFO` vorausgefüllt
   und komplett editierbar (z. B. für RST, Name, Locator, Kommentar).
3. **Log Contact** klicken: Der QSO wird als ADIF-Datensatz an
   `logs/pota_tune_assist_log_<QSO_DATE>.adi` angehängt – **pro Tag eine eigene
   Datei**, ein neuer Tag landet nie in der Datei des Vortages. Ist ein
   API-Key hinterlegt, wird derselbe Datensatz zusätzlich automatisch per
   QRZ-Logbook-API (`https://logbook.qrz.com/api`, `ACTION=INSERT`) ins
   eigene QRZ-Logbuch hochgeladen; Erfolg/Fehler erscheinen in der
   Statusleiste.
4. Der geloggte Spot bleibt in der Liste sichtbar, wird aber ab sofort
   durchgestrichen dargestellt (in gedämpftem Grün) – so bleibt erkennbar,
   welche Stationen aus der aktuellen Spot-Liste schon geloggt wurden, ohne
   sie wie "Skip" komplett auszublenden.

## Heute schon gearbeitet / New Band-Mode-Park

Die App liest die heutige ADIF-Logdatei (`logs/pota_tune_assist_log_<heute>.adi`) beim
Start und danach alle 60 Sekunden neu ein (außerdem sofort nach jedem
"Log Contact") und gleicht jeden Spot in der Tabelle dagegen ab – Ergebnis
steht in der Spalte **HEUTE**:

- **Rufzeichen heute noch gar nicht gearbeitet:** keine Markierung.
- **Exakt derselbe Band/Mode/Park für dieses Rufzeichen bereits geloggt:**
  "♻ Heute schon gearbeitet" – der Spot wird zusätzlich wie ein bereits
  geloggter Kontakt durchgestrichen dargestellt (auch wenn er unter einer
  neuen Spot-ID erneut gespottet wurde).
- **Rufzeichen heute schon gearbeitet, aber Band, Mode oder Park sind
  neu:** "🆕 New Band/Mode/Park (Mode Freq Park)", z. B.
  `🆕 New Band (CW 14074.0 DE-1234)` – zeigt genau, welche Dimension(en)
  neu sind, mit den aktuellen Werten des Spots in Klammern.

Der Abgleich erfolgt pro Rufzeichen unabhängig vom aktuellen Spot – auch ein
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
