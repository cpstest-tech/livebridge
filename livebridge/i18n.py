"""Tiny translation layer: English source strings, other languages as dicts.

To add a language, add a dict to CATALOGS keyed by its two-letter code.
"""
import os

IT = {
    # login
    "Go LIVE on TikTok from Linux": "Vai in LIVE su TikTok da Linux",
    "Log in with the TikTok account you want to stream with. Your browser will open TikTok's authorization page.":
        "Accedi con l'account TikTok con cui vuoi andare in live. Il browser aprirà la pagina di autorizzazione di TikTok.",
    "Log in with TikTok": "Accedi con TikTok",
    "Waiting for your browser…": "In attesa del browser…",
    "Approve the request on TikTok. When the browser asks to open LiveBridge, allow it.":
        "Approva la richiesta su TikTok. Quando il browser chiede di aprire LiveBridge, consenti.",
    "Browser didn't come back?": "Il browser non è tornato qui?",
    "Paste the live-studio-app:// link here": "Incolla qui il link live-studio-app://",
    "Open the page again": "Riapri la pagina",
    "Cancel": "Annulla",
    "Logging in…": "Accesso in corso…",
    "Log out": "Esci",
    "Log out of TikTok?": "Uscire da TikTok?",
    "You'll need to log in again to go LIVE.": "Dovrai accedere di nuovo per andare in live.",
    # setup
    "Ready to go LIVE": "Pronto per la LIVE",
    "LIVE details": "Dettagli LIVE",
    "Title": "Titolo",
    "Allow comments": "Consenti commenti",
    "Allow gifts": "Consenti regali",
    "Mature content (18+)": "Contenuto per adulti (18+)",
    "Topic ID": "ID topic",
    "Optional, leave 0 if unsure": "Facoltativo, lascia 0 se non sei sicuro",
    "Advanced": "Avanzate",
    "OBS Studio": "OBS Studio",
    "Control OBS automatically": "Controlla OBS automaticamente",
    "LiveBridge sets the stream key and starts/stops streaming for you":
        "LiveBridge imposta la chiave e avvia/ferma lo streaming al posto tuo",
    "Ready": "Pronto",
    "Connected, ready to stream": "Connesso, pronto per lo streaming",
    "Closed — LiveBridge will open it": "Chiuso — LiveBridge lo aprirà",
    "Not installed": "Non installato",
    "Install OBS Studio, or turn off automatic control and paste the key yourself":
        "Installa OBS Studio, oppure disattiva il controllo automatico e incolla la chiave a mano",
    "WebSocket server is off": "Il server WebSocket è disattivato",
    "Close OBS and press Set up, or enable it in Tools › WebSocket Server Settings":
        "Chiudi OBS e premi Configura, oppure attivalo in Strumenti › Impostazioni server WebSocket",
    "Set up": "Configura",
    "Can't connect — restart OBS": "Impossibile connettersi — riavvia OBS",
    "Go LIVE": "Vai in LIVE",
    "Starting your LIVE…": "Avvio della LIVE…",
    "Opening OBS…": "Apertura di OBS…",
    "OBS configured": "OBS configurato",
    "A previous LIVE is still open": "Una LIVE precedente è ancora aperta",
    "Resume": "Riprendi",
    "End it": "Chiudila",
    # live
    "LIVE": "LIVE",
    "viewers": "spettatori",
    "Heartbeat": "Heartbeat",
    "Connected to TikTok": "Connesso a TikTok",
    "Connection problems, retrying… ({n})": "Problemi di connessione, riprovo… ({n})",
    "OBS": "OBS",
    "Streaming · {kbps} kbps": "In streaming · {kbps} kbps",
    "Reconnecting…": "Riconnessione…",
    "Not streaming — press Start Streaming in OBS": "Non in streaming — premi Avvia streaming in OBS",
    "Not reachable": "Non raggiungibile",
    "Manual mode": "Modalità manuale",
    "Stream settings": "Impostazioni stream",
    "Paste these in OBS › Settings › Stream › Custom": "Incollali in OBS › Impostazioni › Stream › Personalizzato",
    "Server": "Server",
    "Stream key": "Chiave stream",
    "Copy": "Copia",
    "Show": "Mostra",
    "Copied": "Copiato",
    "End LIVE": "Termina LIVE",
    "End your LIVE?": "Terminare la LIVE?",
    "Your viewers will see that the LIVE has ended.": "I tuoi spettatori vedranno che la LIVE è terminata.",
    "Keep streaming": "Continua",
    "Ending…": "Chiusura…",
    "LIVE ended": "LIVE terminata",
    "TikTok ended the LIVE": "TikTok ha terminato la LIVE",
    "Quit and end your LIVE?": "Uscire e terminare la LIVE?",
    "Closing LiveBridge ends the LIVE, because TikTok needs a heartbeat while you stream.":
        "Chiudendo LiveBridge la LIVE termina, perché TikTok ha bisogno di un heartbeat mentre trasmetti.",
    "End and quit": "Termina ed esci",
    # errors
    "Something went wrong": "Qualcosa è andato storto",
    "Your TikTok session has expired, please log in again": "La sessione TikTok è scaduta, accedi di nuovo",
    "About LiveBridge": "Informazioni su LiveBridge",
    "Unofficial. Not affiliated with TikTok or ByteDance.": "Non ufficiale. Non affiliato a TikTok o ByteDance.",
    # api / obs errors
    "Login expired. Start the login again.": "Login scaduto. Ricomincia l'accesso.",
    "This link doesn't contain a login token. Start the login again.":
        "Questo link non contiene un token di accesso. Ricomincia l'accesso.",
    "Could not determine your TikTok region": "Impossibile determinare la tua regione TikTok",
    "Network error: the connection to TikTok dropped, try again":
        "Errore di rete: la connessione a TikTok si è interrotta, riprova",
    "TikTok didn't return a stream key": "TikTok non ha restituito una chiave stream",
    "Request rejected by TikTok (HTTP 403)": "Richiesta rifiutata da TikTok (HTTP 403)",
    "OBS Studio is not installed": "OBS Studio non è installato",
    "Close OBS first, then try again": "Chiudi prima OBS, poi riprova",
    "OBS is running but its WebSocket server can't be reached. Restart OBS.":
        "OBS è aperto ma il suo server WebSocket non risponde. Riavvia OBS.",
    "OBS rejected the websocket password": "OBS ha rifiutato la password WebSocket",
}

CATALOGS = {"it": IT}


def _lang():
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v:
            return v.split(":")[0].split(".")[0].split("_")[0].lower()
    return "en"


_CATALOG = CATALOGS.get(_lang(), {})


def fmt_int(n):
    s = f"{n:,}"
    return s.replace(",", ".") if _lang() in ("it", "de", "es", "pt", "nl", "tr", "id") else s


def _(s, **kw):
    s = _CATALOG.get(s, s)
    return s.format(**kw) if kw else s
