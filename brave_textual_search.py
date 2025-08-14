#!/usr/bin/env python3
"""
Brave Textual Search (compact TUI, per-query memory)

Features
- Textual TUI: Input box + results list
- Click or press Enter to open a result in your default browser
- Clicks are remembered PER QUERY and hidden in future searches of that same query
- Clicks are logged to CSV (timestamp, query, title, url)
- Pagination with n / p

Setup
  pip install textual requests python-dotenv tenacity
  export BRAVE_API_KEY=your_key   # or put it in a .env file

Run
  python brave_textual_search.py
"""
import os
import json
import csv
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

version = "1.1"

# ---- Textual Imports ----
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Input, Static, ListView, ListItem, Label
from textual.reactive import reactive
from textual.binding import Binding
from textual.message import Message
from textual import events

load_dotenv()

# ---------- Config & Persistence ----------

# Use current working directory for storage (clicked.json, click_log.csv)
APP_DIR = Path.cwd()
CLICKED_PATH = APP_DIR / "clicked.json"
CLICK_LOG_CSV = APP_DIR / "click_log.csv"
APP_DIR.mkdir(parents=True, exist_ok=True)

def _load_clicked() -> dict:
    """Load clicked URLs mapping: {normalized_query: set(urls)}. Handles legacy list format."""
    if CLICKED_PATH.exists():
        try:
            data = json.loads(CLICKED_PATH.read_text("utf-8"))
            if isinstance(data, list):
                # Legacy: flat list -> keep as global (ignored for per-query filtering)
                return {"__global__": set(data)}
            if isinstance(data, dict):
                out = {}
                for k, v in data.items():
                    if isinstance(v, list):
                        out[k] = set(v)
                return out
        except Exception:
            return {}
    return {}

def _save_clicked(clicked: dict) -> None:
    try:
        serializable = {k: sorted(list(v)) for k, v in clicked.items()}
        CLICKED_PATH.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: failed to save clicked.json: {e}", file=sys.stderr)

def _log_click(ts_iso: str, query: str, title: str, url: str) -> None:
    new_file = not CLICK_LOG_CSV.exists()
    try:
        with CLICK_LOG_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["timestamp", "query", "title", "url"])
            w.writerow([ts_iso, query, title, url])
    except Exception as e:
        print(f"Warning: failed to write click_log.csv: {e}", file=sys.stderr)

def _norm_query(q: str) -> str:
    """Normalize a query string for use as a key."""
    return " ".join((q or "").strip().split()).lower()

# ---------- Browser helper ----------

def _open_in_browser(url: str) -> bool:
    """Open URL in default browser with OS fallbacks. Returns True on success."""
    try:
        import webbrowser
        if webbrowser.open(url, new=2):
            return True
    except Exception:
        pass
    try:
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
            return True
        elif sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        else:
            subprocess.Popen(["xdg-open", url])
            return True
    except Exception:
        return False
    return False

# ---------- Brave API ----------

API_URL = "https://api.search.brave.com/res/v1/web/search"

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: Optional[str] = None
    age: Optional[str] = None

class BraveSearchError(Exception):
    pass

def _headers(api_key: str, user_agent: Optional[str]) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    if user_agent:
        h["User-Agent"] = user_agent
    return h

def _extract_web_results(payload: Dict[str, Any]) -> List[SearchResult]:
    web = payload.get("web", {})
    results = web.get("results", []) or []
    out: List[SearchResult] = []
    for r in results:
        out.append(SearchResult(
            title=(r.get("title") or "").strip(),
            url=(r.get("url") or "").strip(),
            snippet=((r.get("description") or r.get("snippet") or "") or "").strip(),
            age=r.get("age"),
        ))
    return out

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.7, min=0.7, max=4),
    retry=retry_if_exception_type((requests.RequestException, BraveSearchError)),
    reraise=True,
)
def brave_search(
    query: str,
    api_key: Optional[str] = None,
    count: int = 20,
    page: int = 0,
    country: str = "US",
    search_lang: str = "en",
    safesearch: str = "moderate",
    freshness: Optional[str] = None,
    result_filter: Optional[str] = None,
    user_agent: Optional[str] = "Mozilla/5.0 BraveTextual/1.0",
) -> Dict[str, Any]:
    key = api_key or os.getenv("BRAVE_API_KEY")
    if not key:
        raise BraveSearchError("Missing BRAVE_API_KEY (export or .env)")

    params = {
        "q": query,
        "count": max(1, min(int(count), 20)),
        "offset": max(int(page), 0),  # brave uses page index (0..9)
        "country": country,
        "search_lang": search_lang,
        "safesearch": safesearch,
    }
    if freshness:
        params["freshness"] = freshness      # e.g., pd, pw, pm, py, or 2025-01-01to2025-01-31
    if result_filter:
        params["result_filter"] = result_filter  # "web", "news,web", etc.

    resp = requests.get(API_URL, headers=_headers(key, user_agent), params=params, timeout=15)
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                time.sleep(float(ra))
            except Exception:
                pass
        raise BraveSearchError("Rate limited (429)")
    if resp.status_code >= 400:
        raise BraveSearchError(f"HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()

# ---------- Textual UI ----------

class OpenResult(Message):
    def __init__(self, result: SearchResult):
        super().__init__()
        self.result = result

class ResultItem(ListItem):
    """One search hit: title, URL, and a wrapped snippet (compact)."""
    def __init__(self, result: SearchResult):
        self.result = result
        title = Label(result.title or "(untitled)", classes="title")
        url = Label(result.url, classes="url") if result.url else Label("")
        children = [title, url]
        snippet_text = (result.snippet or "").strip()
        if snippet_text:
            children.append(Label(snippet_text, classes="snippet"))
        # Mount children via super().__init__ (safe; no manual mount in __init__)
        super().__init__(*children)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(OpenResult(self.result))

class StatusBar(Static):
    def update_status(self, text: str) -> None:
        self.update(text)

class BraveTextualSearch(App):
    TITLE = f"Brave Textual Search {version}"
    CSS = """
    Screen {
        layout: vertical;
    }
    #top {
        layout: horizontal;
        height: 3;
    }
    #query_input {
        width: 1fr;
    }
    #status {
        height: 1;
        content-align: left middle;
        color: #b3b3b3;
        padding: 0 1;
    }
    .url {
        color: #ADFF2F;
    }
    .snippet {
        color: #b3b3b3;
        text-style: dim;
    }
    #results {
        padding: 0;
    }
    ListItem {
        padding: 0 1;
        margin: 0;
    }
    .title { text-style: bold; }
    """

    BINDINGS = [
        Binding("enter", "open_selected", "Open"),
        Binding("n", "next_page", "Next page"),
        Binding("p", "prev_page", "Prev page"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "clear_query", "Clear query"),
    ]

    query = reactive("", layout=True)
    page = reactive(0, layout=True)
    hidden_count = reactive(0, layout=True)
    last_altered = reactive("", layout=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.clicked: Dict[str, set] = _load_clicked()
        self.current_query: str = ""
        self.count_per_page = 20

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="top"):
            yield Input(placeholder="Type your query and press Enter…", id="query_input")
        yield StatusBar(id="status")
        yield ListView(id="results")
        yield Footer()

    # ----- Helpers -----
    def _set_status(self, msg: str) -> None:
        status = self.query_one("#status", StatusBar)
        status.update_status(msg)

    def _populate_results(self, results: List[SearchResult], hidden: int, altered: Optional[str]):
        self.hidden_count = hidden
        self.last_altered = altered or ""
        lv = self.query_one("#results", ListView)
        lv.clear()
        for r in results:
            lv.append(ResultItem(r))
        # Focus results and select first item so Enter works immediately
        try:
            if lv.children:
                if hasattr(lv, "action_cursor_home"):
                    lv.action_cursor_home()
                elif hasattr(lv, "index"):
                    lv.index = 0
                # Focus the list view
                if hasattr(self, "set_focus"):
                    self.set_focus(lv)
                else:
                    lv.focus()
        except Exception:
            pass
        if not results:
            if hidden > 0:
                self._set_status(f"All {hidden} results were previously opened. Try another page or query.")
            else:
                self._set_status("No results.")
        else:
            spell = f" | spellchecked to: {self.last_altered}" if self.last_altered else ""
            self._set_status(f"Showing {len(results)} results (hidden {hidden}){spell} — Enter: open • n/p: next/prev • q / Ctrl+Q / Ctrl+C: quit")

    def _search_thread(self, query: str, page: int):
        try:
            payload = brave_search(query=query, count=self.count_per_page, page=page)
            all_results = _extract_web_results(payload)
            # Filter out URLs clicked for THIS query only
            key = _norm_query(query)
            clicked_for_q = self.clicked.get(key, set())
            filtered = [r for r in all_results if r.url and r.url not in clicked_for_q]
            hidden = len(all_results) - len(filtered)
            altered = (payload.get("query") or {}).get("altered")
            self.call_from_thread(self._populate_results, filtered, hidden, altered)
        except Exception as e:
            self.call_from_thread(self._set_status, f"[error] {e}")

    def do_search(self, query: str, page: int = 0):
        import threading
        self.current_query = query
        self.page = page
        self._set_status("Searching…")
        threading.Thread(target=self._search_thread, args=(query, page), daemon=True).start()

    # ----- Actions -----
    def action_quit(self) -> None:
        self.exit()

    def on_key(self, event) -> None:
        # Ctrl+C quits regardless of focus
        try:
            if getattr(event, "key", None) == "c" and getattr(event, "ctrl", False):
                event.stop()
                self.exit()
                return
        except Exception:
            pass
        # Enter opens the selected item when focus is not on the query input
        try:
            if getattr(event, "key", None) in ("enter", "return"):
                focused = getattr(self.screen, "focused", None)
                if not getattr(focused, "id", "") == "query_input":
                    event.stop()
                    self.action_open_selected()
                    return
        except Exception:
            pass

    # ----- Message handlers -----
    def on_open_result(self, message: OpenResult) -> None:
        try:
            source = message.sender if isinstance(message.sender, ResultItem) else None
        except Exception:
            source = None
        self.open_result(message.result, source)

    # ----- Events -----
    def on_input_submitted(self, event: Input.Submitted) -> None:
        q = (event.value or "").strip()
        self.query = q
        if q:
            self.do_search(q, page=0)
            # Move focus to results so Enter opens the selection
            try:
                lv = self.query_one("#results", ListView)
                if hasattr(self, "set_focus"):
                    self.set_focus(lv)
                else:
                    lv.focus()
            except Exception:
                pass

    # ----- Navigation & Open -----
    def action_next_page(self) -> None:
        if not self.current_query:
            return
        self.page += 1
        self.do_search(self.current_query, page=self.page)

    def action_prev_page(self) -> None:
        if not self.current_query or self.page == 0:
            return
        self.page -= 1
        self.do_search(self.current_query, page=self.page)

    def action_clear_query(self) -> None:
        input_box = self.query_one("#query_input", Input)
        input_box.value = ""
        input_box.focus()

    def open_result(self, result: SearchResult, source_item: "ResultItem|None" = None) -> None:
        if result and result.url:
            ok = _open_in_browser(result.url)
            ts_iso = datetime.now().astimezone().isoformat(timespec="seconds")
            q = self.current_query or self.query or ""
            _log_click(ts_iso, q, result.title, result.url)
            # Remember under THIS query
            key = _norm_query(q)
            if key:
                self.clicked.setdefault(key, set()).add(result.url)
            _save_clicked(self.clicked)
            # Remove from current view
            try:
                if source_item is not None:
                    lv = self.query_one("#results", ListView)
                    lv.remove(source_item)
            except Exception:
                pass
            self._set_status(f"{'Opened' if ok else 'Tried to open'}: {result.title or result.url}")

    def action_open_selected(self) -> None:
        lv = self.query_one("#results", ListView)
        if not lv.children:
            return
        item = None
        # 1) highlighted_child if available
        try:
            hc = getattr(lv, "highlighted_child", None)
            if isinstance(hc, ResultItem):
                item = hc
        except Exception:
            pass
        # 2) index-based selection
        if item is None:
            try:
                idx = getattr(lv, "index", None)
                if idx is not None:
                    try:
                        item = lv.get_child_at_index(idx)
                    except Exception:
                        try:
                            item = list(lv.children)[idx]
                        except Exception:
                            item = None
            except Exception:
                pass
        # 3) Fallback to first child
        if item is None and lv.children:
            try:
                item = list(lv.children)[0]
            except Exception:
                item = None
        if isinstance(item, ResultItem):
            self.open_result(item.result, item)

if __name__ == "__main__":
    BraveTextualSearch().run()