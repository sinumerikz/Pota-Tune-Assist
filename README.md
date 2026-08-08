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
  ADIF-Datensatz gespeichert und, falls konfiguriert, automatisch ins
  eigene QRZ-Logbuch **und/oder** eine eigene Wavelog-Instanz hochgeladen.
- **"Heute schon gearbeitet"-Erkennung** – jeder Spot wird gegen das
  heutige Log abgeglichen und mit "♻ Heute schon gearbeitet" oder
  "🆕 New Band/Mode/Park (...)" markiert, je nachdem ob Band, Mode oder Park
  für dieses Rufzeichen bereits neu sind.
- **Favoriten** – ☆-Stern neben einem Spot markiert ein Rufzeichen dauerhaft
  als Favorit; favorisierte Spots erscheinen immer oben in der Liste und
  sind farblich hervorgehoben, egal wie sortiert oder gefiltert wird.
- **Draußenfunker-Liste** – lädt beim Start automatisch eine Rufzeichen-
  Watchlist herunter und hebt Spots davon (🏕) ähnlich wie Favoriten hervor
  und sortiert sie nach oben.
- **Spot-Alarm** – Sound + kurzes Popup, sobald ein neuer Spot eines
  Favoriten oder Draußenfunkers erscheint, abschaltbar per Button.
- **Filter** – Band-, Mode- (Alle/CW/SSB/**CW & SSB**/Digital) und
  Alters-Dropdown (Alle/5/10/15 min blendet ältere Spots aus), Freitext-
  Suchfeld sowie ein Mehrfachauswahl-Dialog für Länder (mit Kontinent-
  Schnellwahl EU/NA/SA/AS/AF/OC). Band, Mode, Alter und Länderauswahl
  bleiben über Neustarts hinweg erhalten (nur das Suchfeld nicht, das
  ist bei jedem Start wieder leer). "Skip" blendet einzelne Spots aus,
  "Alle anzeigen" setzt das zurück.
- **Sortierbare Spalten** – Klick auf einen Spaltenkopf (CALLSIGN, OP, HEUTE,
  FREQ, MODE, REF, NAME, LOC, KM, HÖRCHANCE, AGE) sortiert danach, erneuter
  Klick dreht die Richtung um (▲/▼ im Spaltenkopf). ⭐-Favoriten und
  🏕-Draußenfunker bleiben dabei immer oben, unabhängig von der Sortierung.
- **Entfernung zum Aktivator (KM)** – aus den Koordinaten der
  **Park-Referenz**, die POTA zu jedem Spot mitliefert. Das ist der Ort, an
  dem der Aktivator tatsächlich sitzt (die QRZ-Adresse wäre sein Zuhause),
  kostet nichts und braucht kein QRZ-Abo – nur den eigenen Locator in den
  Settings. Für die seltenen Parks ohne hinterlegte Position wird die
  Referenz einmalig bei POTA nachgeschlagen und dauerhaft zwischen-
  gespeichert; erst wenn auch das nichts liefert, springt (falls
  konfiguriert) der QRZ-Lookup ein.
- **Name des Aktivators (OP)** – optionale Spalte per QRZ.com-XML-Lookup
  (eigenes, kostenpflichtiges QRZ-Abo nötig); ohne QRZ-Zugangsdaten in den
  Settings bleibt die Spalte ausgeblendet und es werden keine Abfragen
  ausgeführt.
- **Hörchance (RBN)** – schätzt pro Spot ab, wie gut die Chance steht, den
  Aktivator am eigenen Standort wirklich zu hören, z. B. `78 % gut` oder
  `22 % schwach`. Grundlage ist das
  [Reverse Beacon Network](https://www.reversebeacon.net/): weltweit
  verteilte Skimmer-Empfänger, die laufend melden, welches Rufzeichen sie
  mit welchem Signal-Rausch-Abstand hören. Die App verbindet sich mit dem
  RBN-Telnet-Feed und wertet aus, was die Skimmer **in der eigenen Umgebung**
  (bis 1200 km) über genau diesen Aktivator auf genau diesem Band melden –
  also eine gemessene Momentaufnahme der Ausbreitung auf der eigenen
  Funkstrecke statt einer allgemeinen Vorhersage. Je näher ein meldender
  Skimmer an der eigenen Position liegt, desto stärker zählt seine Meldung
  – sowohl untereinander (mehrere Skimmer werden gewichtet gemittelt) als
  auch absolut: ein einzelner Skimmer, der zwar noch innerhalb der 1200 km
  liegt, aber z. B. 950 km entfernt ist, zieht die Prozentzahl spürbar nach
  unten, statt fast so stark zu zählen wie einer direkt nebenan.
  Kostenlos und ohne Anmeldung; gebraucht werden nur das eigene Rufzeichen
  (dient als Login) und der eigene Locator. Da die Skimmer nur CW und RTTY
  dekodieren, steht bei SSB/FM/Digital-Spots `–`. Ein `~` vor dem Wert
  bedeutet "Schätzung", weil gerade kein Skimmer in der eigenen Region aktiv
  ist und deshalb niemand die lokale Ausbreitung beurteilen kann.
  Standardmäßig **aus** (die Funktion öffnet eine dauerhafte Verbindung nach
  außen) – einschalten per Klick auf das `RBN`-Badge oben rechts oder in den
  Settings.
- **Weltkarte** – echte, zoom-/verschiebbare OpenStreetMap-Ansicht unterhalb
  der Spot-Liste (Größe per Ziehen an der Trennlinie anpassbar). Eigener
  Standort als grüner Marker (aus dem Locator in den Settings), sichtbare
  Spots als farbige Marker je Band – gesetzt auf die Koordinaten der
  Park-Referenz, also ohne QRZ-Abo für alle sichtbar. Mehrere Aktivatoren im
  selben Park teilen sich einen Marker. Per QSY erscheint zusätzlich eine
  gestrichelte Linie zum eigenen Standort mit Entfernung in km, und – bei
  aktivierter Hörchance (siehe oben) und vorhandenen RBN-Meldungen für
  diesen Aktivator – ein oranger Ring um den Park, der zeigt, wie weit er
  laut RBN gerade tatsächlich gehört wird (Radius = Entfernung des
  am weitesten entfernten meldenden Skimmers). Auch das ist eine reine
  Momentaufnahme des aktuellen RBN-Datenstands, kein Ausbreitungsmodell.
- **Sonnendaten (SFI/K/A/MUF)** – Badge oben rechts, alle 15 Minuten
  aktualisiert, kein Setup nötig.
- **CAT-Log** – Debug-Fenster mit den rohen CAT-Befehlen/-Antworten (nur
  beim FT-710-CAT-Backend), hilfreich bei Frequenz-/Mode-Problemen.
- **Programm-Log** – Debug-Fenster mit dem Verlauf aller Programmereignisse
  und Fehler (Verbindungsauf-/-abbau, Spot-Abruf, QRZ-/Wavelog-Uploads,
  interne Fehler), im Gegensatz zur einzeiligen Statuszeile bleibt hier der
  Verlauf der letzten 500 Meldungen sichtbar.
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

  **"rigctld nicht gefunden" unter Windows:** Die Windows-Builds von
  Hamlib sind meist ein reines .zip zum Selbst-Entpacken, kein Installer –
  die App findet `rigctld.exe` automatisch nur, wenn es im PATH liegt oder
  unter `...\Hamlib\bin\rigctld.exe` (Program Files, ProgramW6432 oder
  LocalAppData). Liegt es woanders (z. B. `C:\hamlib-w64-4.x\bin\rigctld.exe`),
  entweder diesen `bin`-Ordner zum PATH hinzufügen, oder – einfacher – in
  den Settings unter "Hamlib rigctld" das neue Feld **"rigctld-Pfad"**
  ausfüllen (über "Durchsuchen…" direkt auf `rigctld.exe` zeigen). Wird
  lokal in `pota_tune_assist.db` gespeichert und ab dann für
  Modell-Liste und Auto-Start verwendet.

  ```bash
  rigctld -m <Modell-Nr.> -r COM5        # Windows-Beispiel
  rigctld -m <Modell-Nr.> -r /dev/ttyUSB0  # Linux-Beispiel
  ```

  **Unterschied beim TUNE-Feld:** Im rigctld-Backend ist die
  Leistungsangabe kein Watt-Wert, sondern Hamlibs normalisierte
  `RFPOWER`-Stufe (0.0–1.0 = Anteil der Maximalleistung). Das Feld
  beschriftet sich beim Umschalten entsprechend um ("Power Level (0–1)"
  statt "Leistung (W)").

  **Reagiert TUNE über rigctld nicht:** Nicht jeder Hamlib-Rig-Backend
  unterstützt PTT (`T`-Kommando) oder das RFPOWER-Level – wird das vom
  gewählten Rig-Modell nicht unterstützt, meldet rigctld einen Fehler.
  Das erscheint jetzt immer als Klartext in der Statuszeile unten
  ("Tune abgebrochen: ..."), auch bei unerwarteten Fehlern – vorher konnte
  das in der `--windowed`-.exe ohne jede Rückmeldung fehlschlagen.

  **"Kein Level in rigctld-Antwort":** Manche Hamlib-Rig-Backends können
  den aktuellen RFPOWER-Wert nicht auslesen (Setzen geht oft trotzdem).
  Das blockiert TUNE nicht mehr komplett – die App tunt weiterhin mit der
  konfigurierten Tune-Leistung, kann die ursprüngliche Leistung danach nur
  nicht automatisch wiederherstellen (Hinweis dazu in der Statuszeile);
  Frequenz und Mode werden trotzdem wie gewohnt zurückgesetzt.

Oberfläche im dunklen, militärisch angehauchten Olivgrün-Theme (Filter-Chips,
Statusbadges, farbcodierte Tabelle nach Betriebsart, rot durchgestrichene
Zeilen für als ungültig gemeldete Spots, gedämpft grün durchgestrichene
Zeilen für bereits geloggte Kontakte). Unter Windows 10 (2004+)/11 wird
zusätzlich auch die native Fenster-Titelleiste (Haupt- und alle
Dialogfenster) per DWM-API dunkel eingefärbt statt im Standard-Weiß von
Windows zu bleiben (benötigt Windows 10 Version 2004/20H1 oder neuer bzw.
Windows 11 - auf älteren Windows-Versionen bleibt sie weiß, dazu erscheint
ein Hinweis im Programm-Log).

**Settings** ist kein eigenes Fenster, sondern ein ein-/ausklappbares Panel
rechts neben der Spot-Tabelle (Button "Settings" oben, oder das ✕ im Panel
selbst zum Schließen) und scrollt (Mausrad oder Scrollbar rechts), falls der
Inhalt nicht auf einmal in den sichtbaren Bereich passt.

## Installation

### Windows: fertige .exe

[`POTA-Tune-Assist.exe`](POTA-Tune-Assist.exe) liegt bereits im Ordner –
kein Python nötig, einfach starten. Sie wird automatisch per GitHub-Actions-
Workflow (`.github/workflows/build-windows-exe.yml`) auf einem echten
`windows-latest`-Runner gebaut und bei Änderungen an `pota_tune_assist.py`
oder `requirements.txt` auf `main` neu committet, nicht von Hand. Der
Workflow baut vor der eigentlichen .exe einen zweiten, identischen
Konsolen-Build nur zum Testen und startet ihn kurz – bricht die App dabei
sofort ab (z. B. fehlende Tcl/Tk-DLLs im Build), wird nichts committet,
damit kein kaputter Build im Repo landet. Windows SmartScreen warnt bei
unsignierten .exe-Dateien aus dem Internet – das ist bei selbst gebauten
PyInstaller-Programmen normal.

Es gibt keine automatische Update-Prüfung/Selbstaktualisierung – neue
Versionen holst du dir per `git pull` bzw. durch erneutes Herunterladen
von `POTA-Tune-Assist.exe` aus dem Repository.

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
   oben rechts wird grün, sobald die Verbindung steht. Backend, Port/Baud
   bzw. Host/Netzwerk-Port werden bei jeder erfolgreichen Verbindung
   automatisch in `pota_tune_assist.db` gespeichert – beim
   nächsten Programmstart verbindet die App **automatisch** damit, ganz
   ohne Klick auf "Verbinden". Bricht die Verbindung während des Betriebs
   ab (Kabel wackelt, USB-Aussetzer, rigctld stürzt ab), erkennt die App
   das binnen 15 Sekunden und versucht selbstständig einen Reconnect
   (Wartezeit zwischen Versuchen verdoppelt sich bei wiederholtem
   Fehlschlag bis maximal 60 Sekunden). Ein manueller Klick auf
   **"Trennen"** unterbricht diese automatischen Versuche bewusst, bis
   wieder auf **"Verbinden"** geklickt wird.
2. Die Spot-Liste aktualisiert sich automatisch alle 60 Sekunden ("Auto: An"
   umschaltbar, oder manuell über "Refresh"). Band-, Mode-Dropdown sowie das
   Suchfeld filtern die Tabelle. Der **Land-Button** (zeigt "Alle Länder",
   ein Ländercode oder "N Länder") öffnet einen Mehrfachauswahl-Dialog mit
   Checkboxen für jedes Land, das gerade (oder je schon einmal) in der
   Spot-Liste vorkam ("Alle"/"Keine" zum Schnellauswählen, sowie ein
   **Kontinent-Schnellwahl** EU/NA/SA/AS/AF/OC – klickt alle bekannten
   Länder des jeweiligen Kontinents an bzw. wieder ab). Die Auswahl wird in
   `pota_tune_assist.db` gespeichert und bleibt über Neustarts hinweg
   erhalten. "Skip" blendet einzelne Spots aus, "Alle anzeigen" setzt das
   zurück. **"CAT-Log"** öffnet ein Debug-Fenster mit den rohen
   CAT-Befehlen/-Antworten (nur FT-710-CAT-Backend). **"Programm-Log"**
   öffnet ein Debug-Fenster mit dem Verlauf aller Programmereignisse und
   Fehler (Verbindung, Spots, Uploads, interne Fehler) – nützlich, wenn z. B.
   Spots oder Sonnendaten plötzlich stehen bleiben.
3. Klick auf **▶ QSY** (oder Doppelklick auf die Zeile) → Funkgerät QSYt
   automatisch auf Frequenz + Mode des Spots. Als ungültig gemeldete Spots
   werden rot durchgestrichen dargestellt, bleiben aber anklickbar. Der
   **☆-Stern** links von QSY markiert ein Rufzeichen als Favorit (⭐) –
   favorisierte Rufzeichen werden über alle Bands/Modes hinweg dauerhaft
   gemerkt (`pota_tune_assist.db`) und ihre Spots erscheinen immer ganz
   oben in der Liste, egal wie sortiert/gefiltert wird. Favoriten-Zeilen
   bekommen zusätzlich einen eigenen goldbraunen Zeilenhintergrund plus
   fette Schrift statt der normalen Mode-Farbcodierung. Die **🏕-Spalte**
   links daneben markiert Rufzeichen aus der Draußenfunker-Liste (siehe
   unten) mit eigenem blaugrünem Zeilenhintergrund – auch diese Spots
   erscheinen ganz oben (nach den Favoriten). **CALLSIGN** und **NAME**
   (Parkname) sind klickbare Links (Mauszeiger wechselt beim Drüberfahren
   zur Hand): Klick auf das Rufzeichen öffnet das POTA-Profil des
   Aktivierenden im Browser, Klick auf den Parknamen die POTA-Referenzseite
   des Parks – beides öffnet sich zusätzlich zum normalen QSY/Favorit-Klick
   in den jeweiligen Spalten.
4. **TUNE (halten)**-Knopf gedrückt halten → Versatz (siehe unten), CW, 5 W,
   Dauerton. Loslassen → zurück auf die vorherige Frequenz/Mode/Leistung.
   Ein 10-Sekunden-Sicherheits-Timeout schaltet automatisch ab.
5. Versatzrichtung (oberhalb/unterhalb) und Leistung stehen direkt in der
   Tune-Leiste am unteren Fensterrand.

## Draußenfunker-Liste

Bei jedem **Programmstart** lädt die App im Hintergrund automatisch die
aktuelle Rufzeichen-Watchlist von
[calls.draussenfunker.de](https://calls.draussenfunker.de/df-polo-notes.txt)
herunter (Ham2K-PoLo-"Callsign notes"-Format) – keine manuelle Pflege
nötig. Der Download läuft asynchron, damit die Oberfläche sofort startet;
sobald die Liste da ist, werden passende Spots automatisch markiert.

Die zuletzt heruntergeladene Liste wird lokal in `draussenfunker.txt`
zwischengespeichert. Schlägt der Download beim Start fehl (z. B. kein
Internet, etwa mitten im Park), liest die App stattdessen diesen letzten
bekannten Stand erneut ein – nur beim allerersten Start ganz ohne
Internetverbindung bleibt die Liste leer.

Taucht ein Spot mit einem Rufzeichen aus dieser Liste auf, bekommt die
Zeile in der Tabelle die **🏕-Markierung** in der Spalte links neben
"▶ QSY" sowie einen eigenen blaugrünen Zeilenhintergrund (zur
Unterscheidung von den goldenen ⭐-Favoriten) und wird – wie Favoriten –
automatisch nach oben in der Liste einsortiert (Favoriten zuerst, danach
Draußenfunker-Treffer, danach der Rest).

### Spot-Alarm (Sound + Popup)

Erscheint beim automatischen Refresh (alle 60 s) ein **neuer** Spot eines
⭐-Favoriten oder eines 🏕-Draußenfunkers, spielt die App einen Signalton
und zeigt oben rechts ein kurzes, sich nach 7 Sekunden selbst
schließendes Popup mit Rufzeichen, Frequenz, Mode und POTA-Referenz –
so verpasst man den Spot nicht, obwohl man nicht ständig auf die Tabelle
schauen muss. Als "neu" zählt dabei nur, was seit dem letzten Refresh
neu dazugekommen ist; beim allerersten Laden nach dem Programmstart wird
nie gewarnt, auch wenn Favoriten/Draußenfunker da schon aktiv sind.
Bereits ungültig gemeldete Spots lösen keinen Alarm aus. Der Button
**"Alarm: An/Aus"** neben "Auto: An" schaltet die Funktion bei Bedarf
komplett stumm. **"Alarm testen"** löst Sound + Popup sofort einmal aus
(auch bei "Alarm: Aus") – praktisch, um zu prüfen, ob der Ton auf dem
eigenen System überhaupt zu hören ist, ohne auf einen echten Treffer zu
warten. Der Ton wird über zwei Wege versucht (`winsound.Beep` plus der
System-Klingelton), damit er möglichst unabhängig vom Windows-Soundschema
funktioniert; schlägt `winsound` fehl, erscheint der Grund in der
Statuszeile unten.

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

## Log Contact / ADIF / QRZ-Logbook / Wavelog

1. In den **Settings** unter "Log / QRZ Logbook" einmalig das eigene
   Rufzeichen (Pflichtfeld, sonst öffnet sich der Log-Dialog nicht), optional
   den eigenen Locator und den **QRZ-Logbook-API-Key** eintragen und
   "Speichern" klicken. Der QRZ-API-Key steht im QRZ-Logbook unter
   *Settings → API Key* (nicht der allgemeine XML-Subscription-Key, sondern
   der Logbook-eigene). Diese Angaben werden lokal in `pota_tune_assist.db`
   neben dem Programm gespeichert. Ist kein API-Key hinterlegt, wird nur
   lokal geloggt, ohne QRZ-Upload.
2. Optional zusätzlich (oder alternativ) im Abschnitt **"Wavelog"**: eigene
   **Server-URL** (z. B. `https://log.example.com`, ohne abschließenden
   Slash – funktioniert mit selbst gehosteten wie mit gehosteten
   Wavelog-Instanzen), **API-Key** (Wavelog → Settings → API Keys) und
   **Station-Profil-ID** (Wavelog → Station Setup, meist `1` bei nur einem
   Profil) eintragen. Erst wenn alle drei Felder ausgefüllt sind, wird
   zusätzlich zu Wavelog hochgeladen. Der API-Key muss in Wavelog als
   **"read/write"** angelegt sein – ein "read-only"-Key liefert beim Upload
   `401 Unauthorized`.
3. In der Spot-Tabelle auf **📝 Log** in der gewünschten Zeile klicken. Der
   Dialog ist mit Rufzeichen, aktuellem UTC-Datum/-Zeit, Band, Frequenz,
   Mode (aus dem Spot) sowie der POTA-Referenz als `SIG_INFO` vorausgefüllt
   und komplett editierbar (z. B. für RST, Name, Locator, Kommentar).
4. **Log Contact** klicken: Der QSO wird als ADIF-Datensatz an
   `logs/pota_tune_assist_log_<QSO_DATE>.adi` angehängt – **pro Tag eine eigene
   Datei**, ein neuer Tag landet nie in der Datei des Vortages. Derselbe
   Datensatz wird danach parallel per QRZ-Logbook-API
   (`https://logbook.qrz.com/api`, `ACTION=INSERT`) und/oder per
   Wavelog-API (`<Server-URL>/index.php/api/qso`) hochgeladen, je nachdem
   welche der beiden oben konfiguriert sind – unabhängig voneinander,
   beide gleichzeitig sind möglich; Erfolg/Fehler erscheinen jeweils in der
   Statusleiste.
5. Der geloggte Spot bleibt in der Liste sichtbar, wird aber ab sofort
   durchgestrichen dargestellt (in gedämpftem Grün) – so bleibt erkennbar,
   welche Stationen aus der aktuellen Spot-Liste schon geloggt wurden, ohne
   sie wie "Skip" komplett auszublenden.

## Name des Aktivators (OP-Spalte) und Entfernung (KM-Spalte)

Optional, erfordert eine **kostenpflichtige QRZ.com-XML-Lookup-Subscription**
(ein eigener QRZ.com-Login, nicht der Logbook-API-Key von oben – separates
QRZ-Feature/Abo). In den **Settings** unter "QRZ XML-Lookup" **QRZ-Benutzer**
und **QRZ-Passwort** eintragen und "Speichern" klicken. Für die Entfernung
zusätzlich oben unter "Log / QRZ Logbook" den **eigenen Locator** (Maidenhead,
z. B. `JO40`) ausfüllen.

- Sind QRZ-Benutzer und -Passwort ausgefüllt, erscheint automatisch eine neue
  Spalte **OP** in der Spot-Tabelle mit dem Vor-/Nachnamen des Aktivators aus
  dessen QRZ-Profil (leer, falls dort nicht hinterlegt). Ist zusätzlich der
  eigene Locator gesetzt, erscheint auch die Spalte **KM** mit der
  Luftlinien-Entfernung. Beide Spalten stammen aus derselben QRZ-XML-Abfrage
  pro Rufzeichen (Ergebnis für die laufende Sitzung zwischengespeichert, damit
  derselbe Aktivator nicht mehrfach abgefragt wird) und sind wie alle anderen
  sortierbar. Neue Rufzeichen werden über einen Pool von 6 parallelen
  Abfragen nachgeladen (nicht nacheinander), damit z. B. nach dem Start
  viele gleichzeitig aktive Spots zügig ihre Daten bekommen.
- Fehlen QRZ-Benutzer oder -Passwort, bleiben OP- und KM-Spalte komplett
  ausgeblendet und es werden **keinerlei** QRZ-XML-Abfragen ausgeführt. Fehlt
  nur der eigene Locator, bleibt nur die KM-Spalte ausgeblendet, die
  OP-Spalte erscheint trotzdem.
- Schlägt der QRZ-Login fehl (falsche Zugangsdaten), erscheint das einmalig
  in der Statuszeile und die App versucht es nicht automatisch erneut –
  Zugangsdaten korrigieren und neu speichern, um es erneut zu versuchen.
- Findet QRZ zu einem Rufzeichen keine Position, bleibt die KM-Zelle für
  diesen Spot leer.

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

## Sonnendaten (SFI/K/A/MUF)

Oben rechts neben der Spot-Anzahl zeigt ein Badge die aktuellen
Ausbreitungs-Indizes: **SFI** (Solar Flux Index), **K**-Index, **A**-Index
sowie **MUF** – abgerufen von N0NBHs kostenlosem, in der Amateurfunk-Szene
etablierten Solar-Daten-Feed (`hamqsl.com`), alle 15 Minuten neu geladen,
kein API-Key nötig. Der MUF-Wert ist der allgemeine MUF(3000km)-Wert
dieses Feeds (bezogen auf dessen Referenzstation) – **keine speziell für
Deutschland berechnete MUF**, dafür bräuchte es Ionosonden-Daten (z. B.
von der Station Juliusruh), die nicht als einfache kostenlose API
verfügbar sind. Schlägt der Abruf fehl (kein Internet, Feed nicht
erreichbar), bleibt das Badge einfach bei "--" stehen, ohne Fehlermeldung.

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
