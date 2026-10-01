# MusicaTag

Applicazione macOS per consultare e modificare i tag dei brani locali nella libreria di Apple Music.

## Avvio

Apri il progetto in PyCharm e avvia `main.py` con Python 3.10 o superiore. Installa le dipendenze con `python3 -m pip install -r requirements.txt`.

1. Premi **Carica libreria** e autorizza Python/PyCharm a controllare Musica quando macOS lo richiede. Per sviluppo e test puoi attivare **Scansiona solo i primi 1000 brani** prima di caricare la libreria.
2. Attiva **Visualizza solo brani senza genere** per filtrare la tabella. Premi **Seleziona tutto** (sotto **Carica libreria**) oppure scegli i brani visibili. Attiva **Trova genere**, **Trova copertine** o entrambe e premi **Cerca informazioni**. I generi trovati vengono inseriti nei campi modificabili e le copertine vengono preparate per l'applicazione. Se per alcuni brani selezionati manca il genere, viene usato quello trovato più spesso per lo stesso artista tra i brani selezionati.
3. Facoltativamente, attiva **Cerca genere online (MusicBrainz)** per cercare il genere associato all'artista come ulteriore ripiego quando Apple e Deezer non trovano un genere. MusicBrainz non richiede una chiave API; il genere restituito è il tag più frequente associato all'artista.
4. Premi **Applica modifiche selezionate** e conferma: per le righe selezionate vengono scritti i generi trovati e incorporate le copertine direttamente nei file locali. Se Musica non fornisce il percorso di un file, l'app chiede di selezionarlo manualmente. Le copertine sono supportate per MP3, MP4/M4A, FLAC, AIFF e WAV; file protetti, non accessibili o in altri formati mostrano un errore per la copertina, mentre le modifiche ai tag possono comunque essere applicate tramite Musica.
5. Con un doppio clic su una riga il brano viene riprodotto in **Musica**. Fai un clic sul campo del genere per modificarlo manualmente; premi Invio o sposta il focus per salvare il valore. **Apri copertina selezionata** mostra nel browser la copertina del brano selezionato. Gli altri campi si modificano con `Ctrl`/`⌘` + doppio clic.

La barra in basso mostra l'avanzamento della lettura della libreria e delle ricerche. Le ricerche attendono 3 secondi ogni 40 brani per limitare le richieste; MusicBrainz viene inoltre interrogato al massimo una volta al secondo come richiesto dal servizio e i generi per artista vengono memorizzati durante la sessione. Apple è il catalogo principale; se non restituisce un risultato affidabile o limita le richieste, MusicaTag prova Deezer per genere e copertina e, se attivato, MusicBrainz per il genere dell'artista. In Deezer il genere corrisponde a quello dell'album. Le ricerche richiedono una connessione Internet e inviano titolo e artista ai cataloghi online. Sono mostrati i brani locali esposti da Apple Music come file; brani solo in streaming, protetti o non modificabili potrebbero non comparire o rifiutare le modifiche.
