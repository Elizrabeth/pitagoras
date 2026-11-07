"""The conversation loop: brain <-> tools <-> speech.

Tool use is done with a plain-text protocol so it works with ANY model/GGUF,
even ones without native function calling:

    <tool>{"name": "play_on_spotify", "args": {"query": "lofi"}}</tool>
"""
from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import logging
import random
import re
import time
from typing import Callable

from . import memory, tools
from .tools import TOOLS, describe_tools, run_tool

log = logging.getLogger("agent")

TOOL_RULES = """
You can control the computer with tools. To use one, write ONLY this on its own line and then stop:
<tool>{{"name": "TOOL_NAME", "args": {{...}}}}</tool>
You will get the result in a <tool_result> message, then continue talking to the user.
You may say a very short line before a tool call ({ack}). Never invent tool results.
Tool calls are silent actions: never mention tool names in what you say.
Never write emoji or roleplay actions like *dances*: your character shows emotions with the emote tool.
Tracking cooldowns / timers: when the user says someone used a spell or summoner ("xayah flashneła, trackuj"),
web_search its cooldown (e.g. "League of Legends Flash summoner spell cooldown seconds"), then start_timer with
the full cooldown, a label like "Flash Xayah" and a message like "Flash jest gotowy na Xayah!". Reminders
("za 10 minut") are just start_timer too.
Controlling the PC (only when the user asks you to click, type or play something): to click anything that has a
label or text (a button, link, icon name, menu), use click_on with that text: it finds it exactly. To write into
a text box use type_text with into= the box's name/placeholder (it clicks it first). move_to only moves YOU (the
character), never the mouse: don't use it for clicking. Only for things
without text use look_at_screen with grid=true and click x,y from the grid. Then press_keys / type_text /
wait_and_look as needed. Every action shows you the new screen; keep going step by step until the task is done,
without talking between steps. If a result says STOPPED, stop immediately.
If one of your earlier replies ends with "…", the user cut you off on purpose: answer their newest message
and don't continue the old reply unless they ask you to.
When the user asks you to DO something (open, play, launch, search, look…), you MUST call the tool:
just saying you did it does nothing. Call the action tool first, emote after.
For actions (play music, open apps, click, play a game) keep it snappy: at most 2-4 words before the tool call
(or none), and after it's done at most one short sentence. Don't ask for confirmation, don't explain what you
see or what you're about to do, and don't check twice: just do it.
You may use emote once in a while to show a feeling (and to dance to music), but never as your only output:
always also answer or do what was asked.

Tools:
{tools}
"""


def _close_paren(s: str, start: int, open_c: str = "(", close_c: str = ")") -> int:
    """Index of the bracket matching s[start] (ignoring brackets inside strings), or -1."""
    depth, quote, esc = 0, None, False
    for j in range(start, len(s)):
        c = s[j]
        if quote:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
        elif c == '"' or (c == "'" and open_c == "("):
            quote = c
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j
    return -1


# Backend markers and chat-template tokens a model may copy into its reply: never shown or spoken.
JUNK = ["(interrupted)", "[interrupted]", "[old screenshot removed]", "[screenshot]", "[automatic glance",
        "[watch-along", "<end_of_turn>", "<start_of_turn>", "<eos>", "<bos>", "<|end|>", "<|im_end|>",
        "<|im_start|>", "<|eot_id|>", "<|endoftext|>", "<turn|>", "<|turn>", "</s>", "<channel|>", "<|channel>"]
JUNK_RE = re.compile("|".join(re.escape(j) for j in JUNK) + r"|\[(?:automatic glance|watch-along)[^\]]*\]"
                     r"|<tool_result[^>]*>.*?</tool_result>", re.I | re.S)  # (real <tool> calls are kept)


PASS_RE = re.compile(r"\bPASS\b[.!]?|\bPAS$")  # the 'stay quiet' word from auto turns (case-sensitive)
# emoji, pictographs, dingbats, flags, skin tones, ZWJ/variation selectors
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002190-\U000021FF"
                      "\U00002B00-\U00002BFF\U0000FE0F\U0000200D\U000020E3\U0001F3FB-\U0001F3FF]+")
# roleplay emotes like *dances around* or *giggles* (several words between asterisks), and leftover asterisks
EMOTE_RE = re.compile(r"\*[^*\n]{1,60}\*")


EMOTE_WORDS = {"giggles", "giggle", "laughs", "laugh", "smiles", "smile", "grins", "grin", "winks", "wink", "sighs",
               "dances", "dance", "nods", "shrugs", "blushes", "chuckles", "gasps", "claps", "waves", "bounces",
               "spins", "jumps", "wiggles", "hums", "sings", "headbangs", "vibes", "śmieje", "uśmiecha", "tańczy",
               "mruga", "wzdycha", "klaszcze", "skacze", "podskakuje", "macha", "chichocze", "kiwa", "nuci",
               "śpiewa", "kręci", "przewraca"}


def no_emotes(text: str) -> str:
    def emote(m: re.Match) -> str:
        inner = m.group(0).strip("*").strip()
        if " " in inner or inner.lower().strip(".!") in EMOTE_WORDS:
            return ""  # *dances around* / *giggles*: an action, not words to say
        return inner  # *perfect*: just emphasis, keep the word
    return EMOTE_RE.sub(emote, EMOJI_RE.sub("", text)).replace("*", "")


def auto_summary(prompt: str) -> str:
    """What to keep in the history for a glance/watch turn: the context, not the instructions
    (instructions like 'reply PASS' in old messages get copied into normal replies)."""
    keep = [ln.strip() for ln in prompt.splitlines()
            if ln.startswith(("Active window:", "Playing:", "What was said"))]
    return "(You looked at the user's screen by yourself" + (": " + " ".join(keep) if keep else "") + ")"


def scrub(text: str) -> str:
    """Remove backend markers from text that goes into the history (so the model doesn't learn them)."""
    text = PASS_RE.sub("", JUNK_RE.sub("", text))
    # emoji/emotes in old replies teach the model to keep using them; keep tool calls untouched
    parts = re.split(r"(<tool>.*?</tool>)", text, flags=re.S)
    text = "".join(p if p.startswith("<tool>") else no_emotes(p) for p in parts)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class StreamParser:
    """Splits streamed model output into speakable text, tool calls and
    hidden <think> blocks, even when tags arrive split across chunks.

    Understands <tool>{json}</tool> and, because small models improvise:
    emote({"emotion": "happy"}), <emote({...})>, and bare {"name": ..., "args": ...}.
    Any other {...} snippet is dropped (it's never meant to be spoken).
    """

    # opening tag -> (mode, closing tag). Gemma also has its own native tool-call tokens.
    TAGS = {"<tool>": ("tool", "</tool>"), "<think>": ("think", "</think>"),
            "<|tool_call>": ("tool", "<tool_call|>"), "<tool_result>": ("hide", "</tool_result>")}

    def __init__(self, tool_names: list[str] | None = None):
        self.buf = ""
        self.mode = "text"
        self.end = ""
        self.inner = ""
        self.tool_calls: list[str] = []
        self._eat_gt = False
        names = sorted(tool_names or [], key=len, reverse=True)
        self.inline = re.compile(r"`?\b(" + "|".join(map(re.escape, names)) + r")\(") if names else None
        self.prefixes = [n + "(" for n in names]
        alt = "|".join(map(re.escape, names)) or "(?!)"
        self.junk_tag = re.compile(r"</?(?:" + alt + r")\s*/?>")      # stray <emote > / </emote>
        self.names = names

    def _junk_start(self) -> bool:
        """Could the buffer still grow into a stray <emote > / </emote> tag?"""
        m = re.fullmatch(r"</?(\w*)(\s*/?)", self.buf)
        if not m:
            return False
        word, rest = m.groups()
        return any(n.startswith(word) for n in self.names) if not rest else word in self.names

    def _next_special(self) -> int:
        found = [i for i in (self.buf.find("<"), self.buf.find("{"), self.buf.find("("), self.buf.find("["),
                             self.buf.find("*")) if i != -1]
        if self.inline:
            m = self.inline.search(self.buf)
            if m:
                found.append(m.start())
        return min(found) if found else -1

    def _held_tail(self) -> int:
        """Length of a buffer tail that could be the start of an inline call (or of 'PASS')."""
        p = re.search(r"\bP(?:A(?:S(?:S)?)?)?$", self.buf)
        if p:
            return len(p.group(0))
        if not self.prefixes:
            return 0
        tail = re.search(r"`?\w*$", self.buf).group(0)
        word = tail.lstrip("`")
        if word and any(p.startswith(word) for p in self.prefixes):
            return len(tail)
        return 0

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out: list[str] = []
        while self.buf:
            if self.mode == "text":
                if self._eat_gt:
                    self._eat_gt = False
                    if self.buf[0] == ">":
                        self.buf = self.buf[1:]
                        continue
                i = self._next_special()
                if i == -1:
                    hold = self._held_tail()
                    out.append(self.buf[: len(self.buf) - hold])
                    self.buf = self.buf[len(self.buf) - hold :]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i:]
                low = self.buf.lower()
                junk = next((j for j in JUNK if low.startswith(j)), None)
                if junk:  # a backend marker / template token: drop it
                    if junk.startswith("[") and not junk.endswith("]"):  # "[watch-along …]": up to the "]"
                        end = self.buf.find("]")
                        if end == -1:
                            if len(self.buf) > 400:  # never closed: just drop the marker word
                                self.buf = self.buf[len(junk):]
                                continue
                            break  # wait for the closing bracket
                        self.buf = self.buf[end + 1:]
                    else:
                        self.buf = self.buf[len(junk):]
                    continue
                if any(j.startswith(low) for j in JUNK):
                    break  # might become a marker; wait for more
                if self.buf[0] == "*":  # *emote* / *emphasis*: wait for the closing star
                    close = self.buf.find("*", 1)
                    nl = self.buf.find("\n", 1)
                    if close != -1 and (nl == -1 or close < nl) and close <= 62:
                        out.append(no_emotes(self.buf[: close + 1]))
                        self.buf = self.buf[close + 1:]
                        continue
                    if close == -1 and nl == -1 and len(self.buf) < 62:
                        break
                    self.buf = self.buf[1:]  # a lone star
                    continue
                if self.buf[0] in "([":  # ordinary bracket
                    out.append(self.buf[0])
                    self.buf = self.buf[1:]
                    continue
                if self.buf[0] == "{":  # JSON snippet: a bare tool call, or junk
                    close = _close_paren(self.buf, 0, "{", "}")
                    if close == -1:
                        if len(self.buf) > 800:
                            self.buf = self.buf[1:]
                            continue
                        break
                    snippet = self.buf[: close + 1]
                    self.buf = self.buf[close + 1 :]
                    try:
                        obj = json.loads(snippet)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and (obj.get("name") or obj.get("tool")):
                        self.tool_calls.append(snippet)
                    continue
                if self.buf[0] != "<":  # inline call: name(...)
                    m = self.inline.match(self.buf)
                    open_at = m.end() - 1
                    close = _close_paren(self.buf, open_at)
                    if close == -1:
                        if len(self.buf) > 600:  # never closed; it was just text
                            out.append(self.buf[: m.end()])
                            self.buf = self.buf[m.end():]
                            continue
                        break
                    inner = self.buf[open_at + 1 : close].strip()
                    try:
                        args = json.loads(inner) if inner else {}
                    except ValueError:
                        args = {}
                    self.tool_calls.append(json.dumps({"name": m.group(1), "args": args if isinstance(args, dict) else {}}))
                    self.buf = self.buf[close + 1 :].lstrip("`")
                    self._eat_gt = True  # a following '>' belongs to <name(...)>
                    continue
                tag = next((t for t in self.TAGS if self.buf.startswith(t)), None)
                junk = self.junk_tag.match(self.buf)
                if self.buf.startswith("<tool_result "):  # a made-up result with attributes: hide it
                    gt = self.buf.find(">")
                    if gt == -1:
                        break
                    self.mode, self.end = "hide", "</tool_result>"
                    self.buf = self.buf[gt + 1:]
                elif tag:
                    self.mode, self.end = self.TAGS[tag]
                    self.buf = self.buf[len(tag):]
                elif junk:
                    self.buf = self.buf[junk.end():]
                elif self.inline and self.inline.match(self.buf, 1):  # <name(...)>
                    self.buf = self.buf[1:]
                elif any(t.startswith(self.buf) for t in self.TAGS) or self._junk_start() or (
                        len(self.buf) > 1 and any(p.startswith(self.buf[1:]) for p in self.prefixes)):
                    break  # might be the start of a tag or call; wait for more
                else:
                    out.append("<")
                    self.buf = self.buf[1:]
            else:
                end = self.end
                i = self.buf.find(end)
                if i == -1:
                    keep = len(end) - 1
                    take = self.buf[:-keep] if len(self.buf) > keep else ""
                    self.inner += take
                    self.buf = self.buf[len(take):]
                    break
                self.inner += self.buf[:i]
                if self.mode == "tool":
                    self.tool_calls.append(self.inner)
                self.inner = ""
                self.buf = self.buf[i + len(end):]
                self.mode = "text"
        # spoken character: no code formatting, no emoji
        return EMOJI_RE.sub("", PASS_RE.sub("", "".join(out))).replace("`", "")

    def finish(self) -> str:
        rest = ""
        if self.mode == "text":
            rest = self.buf
            if self.inline and self.inline.search(rest):  # unclosed inline call at the very end
                rest = self.inline.split(rest)[0]
            rest = rest.split("{")[0]  # unclosed JSON at the very end
        elif self.mode == "tool":
            self.tool_calls.append(self.inner + self.buf)  # unterminated call
        self.buf = self.inner = ""
        return no_emotes(PASS_RE.sub("", rest))


def _gemma_call(raw: str) -> tuple[str, dict] | None:
    """Gemma's native format: call:open_website{site:<|"|>netflix<|"|>}"""
    m = re.match(r"call:\s*(\w+)\s*(\{.*\})?", raw.replace('<|"|>', '"'), re.S)
    if not m:
        return None
    body = m.group(2) or "{}"
    try:
        args = json.loads(body)
    except ValueError:
        try:
            args = json.loads(re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', body))
        except ValueError:
            args = {}
    return m.group(1), args if isinstance(args, dict) else {}


def parse_tool_call(raw: str) -> tuple[str, dict] | None:
    raw = raw.strip()
    if raw.startswith("call:"):
        return _gemma_call(raw)
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        obj = json.loads(raw[a : b + 1])
    except ValueError:
        return None
    if not (obj.get("name") or obj.get("tool")):  # e.g. <tool>emote({"emotion": "happy"})</tool>
        m = re.match(r"(\w+)\s*\(", raw)
        if m:
            return m.group(1), obj
    name = obj.get("name") or obj.get("tool")
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or obj.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict) or not args:
        # small models often write the arguments flat: {"name": "click", "x": 224, "y": 263}
        args = {k: v for k, v in obj.items() if k not in ("name", "tool", "args", "arguments", "parameters", "input")}
    return (name, args) if name else None


class _StayQuiet(Exception):
    pass


GLANCE_PROMPT = (
    "[Automatic glance: this is a screenshot of the user's screen, they did not ask anything.] "
    "If you notice something fun, interesting or helpful to comment on, say ONE short line about it. "
    "Don't repeat what you said before. If there is nothing worth saying, reply with exactly: PASS"
)


ACKS_PL = ["Robi się!", "Już się robi!", "Się robi!", "Moment!"]
ACKS_EN = ["On it!", "One sec!", "Coming right up!"]
QUICK_TOOLS = {"get_time", "what_is_playing", "remember", "list_installed", "emote", "move_to", "volume",
               "media_control", "start_timer", "cancel_timer", "list_timers"}  # fast: no "on it" needed
CONTROL_TOOLS = {"click", "click_on", "move_mouse", "drag", "scroll", "type_text", "press_keys", "wait_and_look"}
ACTION_TOOLS = CONTROL_TOOLS | {"play_on_spotify", "open_app", "open_website", "open_youtube", "media_control",
                                "volume", "watch_along", "start_timer", "cancel_timer"}
# a tool result that means it did NOT work (then it may say so)
FAILED_RE = re.compile(r"\s*(error|couldn't|could not|no |not found|stopped|mouse & keyboard control is turned off|"
                       r".*\bis not installed\b|.*didn't start|.*needs a click)", re.I)
CONTROL_INTENT = re.compile(r"\b(klik\w*|click\w*|wpisz|wpisać|naciśnij|wciśnij|przyciśnij|press|type in|"
                            r"zaznacz|przewiń|scroll|zamknij okno|graj w|zagraj w|play the game)", re.I)
DONE_RE = re.compile(r"\b(gotowe|zrobione|done|finished|skończone)\b", re.I)
# ── fact check: questions get a web search first, so the answer is checked against the internet ──
QUESTION_RE = re.compile(
    r"\?\s*$|^(?:what|what's|whats|who|who's|whom|whose|when|where|why|how|which|is|are|was|were|does|do|did|"
    r"can|could|will|would|should|has|have|tell me|explain|czy|co|kto|kogo|komu|kim|kiedy|gdzie|dlaczego|czemu|"
    r"jak|jaki|jaka|jakie|jakiego|jakiej|jakim|ile|ilu|który|która|które|którego|której|skąd|dokąd|po co|"
    r"powiedz|opowiedz|wytłumacz|wyjaśnij|sprawdź|znajdź|wiesz)\b", re.I)
SMALLTALK_RE = re.compile(
    r"^(?:how are you|how's it going|hows it going|what's up|whats up|how do you feel|who are you|what are you|"
    r"what can you do|what's your name|what is your name|do you like|are you|can you hear|jak się masz|"
    r"jak sie masz|co tam|co słychać|co u ciebie|kim jesteś|czym jesteś|co umiesz|co potrafisz|jak masz na imię|"
    r"jak się nazywasz|jak leci|lubisz|słyszysz|jesteś)\b", re.I)
# about this PC, the chat or the moment: the internet doesn't know that
LOCAL_RE = re.compile(
    r"\b(screen|ekran\w*|widzisz|do you see|playing now|is playing|teraz leci|co leci|co gra|my name|moje imię|"
    r"mam na imię|remember|pamiętasz|zapamiętaj|timer\w*|minutnik\w*|time is it|która (?:jest )?godzina|"
    r"what time|mój|moja|moje|moim|mojej|my pc|my computer|komputer\w*|głośn\w*|volume)\b", re.I)
# a request to DO something, not a question (handled by tools)
DO_RE = re.compile(
    r"\b(open|launch|start|play|close|turn (?:on|off)|set|track|remind|pause|skip|otw[oó]rz\w*|odpal\w*|"
    r"uruchom\w*|włącz\w*|wyłącz\w*|puść\w*|puści\w*|zagra\w*|zamk\w*|ustaw\w*|trackuj\w*|śledź|przypomni\w*|"
    r"zatrzyma\w*|pauz\w*|następn\w*|pogłoś\w*|ścisz\w*|klik\w*|click|type|wpisz|wpisać|napisz|napisać|"
    # made up on the spot, nothing to look up
    r"joke|story|poem|sing|song for me|kawał\w*|żart\w*|dowcip\w*|historyjk\w*|bajk\w*|wiersz\w*|zaśpiewaj|"
    r"wymyśl\w*|rap\w*)\b", re.I)


def wants_fact_check(text: str) -> bool:
    """A question the internet could answer (not small talk, a request to do something, or about this PC)."""
    t = text.strip()
    if len(re.findall(r"[^\W\d_]+", t)) < 3:
        return False
    if not QUESTION_RE.search(t) or SMALLTALK_RE.search(t) or LOCAL_RE.search(t) or DO_RE.search(t):
        return False
    return not CONTROL_INTENT.search(t)


FACT_NOTE = ("(Internet results for this question, found just now:\n{results}\n"
             "Before answering, compare them with what you know. If they agree, just answer. If they disagree with "
             "you or are newer, trust these results and say what they say. If they have nothing to do with the "
             "question, ignore them and answer normally. Don't read out links, sources or numbers in brackets, "
             "and you don't need web_search for this again.)")
EMPTY_NUDGE = ("You only made a face and said nothing. Now actually do what I asked (call the right tool), "
               "or answer me in a short sentence.")
NUDGE = ("You only described the action, nothing was clicked or typed. Do it now: reply with ONLY the tool call, "
         'e.g. <tool>{"name": "click", "args": {"x": 120, "y": 340}}</tool> using x,y from the grid. '
         "If the task is really finished, reply just: Gotowe.")

LENGTHS = {  # reply length setting -> (instruction, max tokens)
    "short": ("Keep every reply very short: one sentence, about 15 words at most, unless the user asks for more.", 300),
    "medium": ("Keep replies short and conversational: usually 1-3 sentences.", 600),
    "long": ("You may give fuller answers when it helps: up to a few short paragraphs of natural speech.", 1500),
}


class Agent:
    def __init__(self, cfg, brain, hub, speaker=None):
        self.cfg = cfg
        self.brain = brain
        self.hub = hub
        self.speaker = speaker
        self.history: list[dict] = []
        self.restored_at: str | None = None
        self.max_history = int(cfg.brain.get("history_messages", 30))
        self.turn: asyncio.Task | None = None
        self.on_turn: Callable[[bool], None] = lambda active: None  # set by the app (pauses the ears)
        self.busy = False
        self.last_reply = 0.0
        self.last_activity = time.monotonic()  # last user message (for auto glances)
        self._hold: str | None = None
        self.voice_chat = False  # last turn was spoken (enables follow-ups without the wake word)
        self.unaddressed = 0     # follow-ups in a row without saying the name
        v = cfg.get("voice") or {}
        self.wake_word = bool(v.get("wake_word"))
        self.follow_up = float(v.get("follow_up_seconds", 20))
        self.set_wake_phrase(str(v.get("wake_phrase") or "hej pitagoras"))
        self.follow_ups = bool(v.get("follow_ups", False))
        self.armed_until = 0.0
        self._control_turn = False

    def set_wake_phrase(self, phrase: str) -> None:
        """e.g. "hej pitagoras" or "hej madzia". Other spellings of the default name are only kept for Pitagoras."""
        self.wake_phrase = " ".join(phrase.lower().split()) or "hej pitagoras"
        name = self.wake_phrase.split()[-1]
        known = [a.lower() for a in (self.cfg.get("name_aliases") or [])]
        self.aliases = known if name in known else [name]

    def speaks_polish(self) -> bool:
        lang = (self.cfg.get("chat") or {}).get("language", "auto")
        return lang == "pl" or (lang == "auto" and (self.cfg.get("voice") or {}).get("ears_language") == "pl")

    GREETINGS = {"hey", "hej", "hi", "hay", "hei", "ey", "ok", "okay", "oi", "yo", "ej"}

    def wake_heard(self, text: str) -> bool:
        """Was the wake phrase ("hej pitagoras") said at the START of a sentence? Forgiving about spelling
        ('Hey, Pythagoras!', 'hej pita go raz', 'heypitagoras'), strict about position, so talking ABOUT
        Pitagoras ("a patrz, Pitagoras może…") doesn't count."""
        parts = self.wake_phrase.split()
        name = parts[-1]
        greetings = self.GREETINGS | set(parts[:-1])
        names = {name, *self.aliases}
        starts = {n[:3] for n in names}  # "pit", "pyt"

        def is_name(w: str) -> bool:
            return w in names or (len(w) >= 5 and difflib.SequenceMatcher(None, w, name).ratio() >= 0.75)

        def name_at(ws: list[str]) -> bool:
            """The name right here, maybe split by the recognizer: 'pita goras', 'pita go raz'."""
            if not ws:
                return False
            if is_name(ws[0]):
                return True
            if ws[0][:3] in starts:  # only glue pieces that start like the name
                return any(is_name("".join(ws[:k])) for k in (2, 3) if len(ws) >= k)
            return False

        for sentence in re.split(r"[.!?…]+", text.lower()):
            words = re.findall(r"[^\W\d_]+", sentence)
            for i, w in enumerate(words[:2]):  # the greeting opens the sentence (one filler word allowed)
                if len(parts) > 1:
                    if w in greetings and name_at(words[i + 1:i + 4]):
                        return True
                    for g in greetings:  # merged: "heypitagoras"
                        if w.startswith(g) and len(w) > len(g) + 4 and is_name(w[len(g):]):
                            return True
                elif name_at(words[i:i + 3]):  # the wake phrase is just the name
                    return True
        return False

    # ── memory ───────────────────────────────────────────────
    @property
    def memory_on(self) -> bool:
        return bool((self.cfg.get("memory") or {}).get("enabled", True))

    def restore(self) -> None:
        """Load the saved conversation (at boot)."""
        if not self.memory_on:
            return
        msgs, saved = memory.load_conversation()
        clean = []
        for m in msgs:  # older versions saved "(interrupted)" etc.; the model learned to copy them
            if m["role"] == "user" and m["content"].startswith(("[Watch-along", "[Automatic glance")):
                m = {"role": "user", "content": auto_summary(m["content"])}
            elif m["role"] == "user":
                m = {"role": "user", "content": m["content"].replace(" [screenshot]", "").replace(" [old screenshot removed]", "")}
            if m["role"] == "assistant":
                m = {"role": "assistant", "content": scrub(m["content"])}
                if not m["content"]:
                    continue
            clean.append(m)
        self.history = clean
        self.restored_at = saved
        if msgs:
            log.info("remembered %d messages from %s", len(msgs), saved)

    def save(self) -> None:
        if not self.memory_on:
            return
        try:
            memory.save_conversation(self.history, int((self.cfg.get("memory") or {}).get("keep_messages", 200)))
        except OSError as e:
            log.warning("could not save memory: %s", e)

    LANGUAGE_RULES = {
        "pl": "Always reply in Polish (po polsku), even if the user writes or speaks in another language.",
        "en": "Always reply in English, even if the user writes or speaks in another language.",
    }

    def lang(self) -> str:
        """'pl', 'en' or 'auto' (Settings > Language)."""
        return (self.cfg.get("chat") or {}).get("language", "auto")

    def ack_example(self) -> str:
        """The 'on it' line before a tool call; models copy the example literally, so match the language."""
        if self.lang() == "pl":
            return 'in Polish, e.g. "Robi się!", "Już się robi!" or "Moment!"'
        if self.lang() == "en":
            return 'e.g. "On it!" or "One sec!"'
        return 'in the user\'s language, e.g. "Robi się!" in Polish or "On it!" in English'

    def phrase(self, pl: str, en: str) -> str:
        """Fixed lines Pitagoras says itself, in the chosen language."""
        return pl if self.lang() == "pl" or (self.lang() == "auto" and self.speaks_polish()) else en

    def language_rule(self) -> str:
        return self.LANGUAGE_RULES.get((self.cfg.get("chat") or {}).get("language", "auto"), "")

    def length(self) -> tuple[str, int]:
        return LENGTHS.get((self.cfg.get("chat") or {}).get("length", "medium"), LENGTHS["medium"])

    def system_prompt(self) -> str:
        now = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        parts = [memory.personality(self.cfg.get("persona")), self.length()[0]]
        my_name = self.wake_phrase.split()[-1].capitalize()
        if my_name.lower() not in ("pitagoras", "pythagoras"):
            parts.append(f"Your name is {my_name} (the user calls you with \"{self.wake_phrase}\").")
        user_name = ((self.cfg.get("chat") or {}).get("user_name") or "").strip()
        if user_name:
            parts.append(f"The user's name is {user_name}. When you address them, call them {user_name}; "
                         "never make up other names or nicknames for them.")
        else:
            parts.append("You don't know the user's name: don't make up names or nicknames for them.")
        if self.language_rule():
            parts.append(self.language_rule())
        if self.memory_on:
            notes = memory.notes()
            if notes:
                parts.append("What you remember about the user (your long-term memory):\n" + notes)
            if self.restored_at and self.history:
                parts.append(f"The conversation below continues from earlier sessions (last saved {self.restored_at.replace('T', ' ')}).")
        parts.append(f"Current date/time: {now}.")
        prompt = "\n".join(parts) + "\n" + TOOL_RULES.format(tools=describe_tools(), ack=self.ack_example())
        if self.language_rule():  # small models drift back to English; repeat it last
            prompt += "\n" + self.language_rule()
        return prompt

    # ── input ────────────────────────────────────────────────
    def heard(self, text: str, live_wake: str | None = None) -> None:
        """Voice input (called from the listener thread)."""
        # live_wake: the ears already heard "hej pitagoras" at the start while you were talking
        asyncio.run_coroutine_threadsafe(self._voice_input(text, live_wake), self.hub.loop)

    def announce(self, text: str) -> None:
        """Say something by itself (a timer went off): bubble + voice, and remember it."""
        self.hub.emit("assistant_start")
        self.hub.emit("assistant_text", delta=text)
        self.hub.emit("assistant_end")
        self.hub.emit("emotion", emotion="excited")
        if self.speaker:
            self.speaker.say(text)
        if not (self.turn and not self.turn.done()):  # (not in the middle of another exchange)
            self.history.append({"role": "assistant", "content": text})
            self.save()

    def wake_status(self, partial: str) -> str | None:
        """For the ears' live check: None = not for me, 'phrase' = just "hej pitagoras" so far,
        'prompt' = "hej pitagoras" + a request."""
        if not self.wake_word or time.monotonic() < self.armed_until:  # (after "Słucham…" this IS the request)
            return "prompt"
        if not self.wake_heard(partial):
            return None
        return "phrase" if len(re.findall(r"[^\W\d_]+", partial)) <= 3 else "prompt"

    def wake_feedback(self) -> None:
        """Show right away that it heard its name (called from the ears' thread)."""
        if self.wake_word and not self.in_turn():
            self.hub.emit("toast", text=self.phrase("👂 Słucham…", "👂 Listening…"))
            self.hub.emit("emotion", emotion="surprised")

    def in_turn(self) -> bool:
        """Busy with a request: thinking, using tools or still speaking."""
        return bool(self.turn and not self.turn.done()) or bool(self.speaker and not self.speaker.idle.is_set())

    async def _voice_input(self, text: str, live_wake: str | None = None) -> None:
        if self.in_turn():  # one thing at a time: speech that slipped in while busy is dropped
            log.info("(busy, ignored what was heard)")
            return
        if self.wake_word:
            now = time.monotonic()
            addressed = self.wake_heard(text) or live_wake is not None
            armed = now < self.armed_until  # "Hej Pitagoras" alone, then the request
            # optional follow-ups without the phrase right after a *spoken* exchange (off by default)
            in_follow_up = (self.follow_ups and self.voice_chat and self.unaddressed < 3
                            and now - self.last_reply < self.follow_up)
            if not (addressed or armed or in_follow_up):
                log.info("(ignored: no \"%s\")", self.wake_phrase)
                return
            self.armed_until = 0.0
            if addressed and len(re.findall(r"[^\W\d_]+", text)) <= 3:  # just the phrase: wait for the request
                self.armed_until = now + 8
                log.info("wake phrase heard, listening for the request…")
                self.wake_feedback()
                return
            self.unaddressed = 0 if (addressed or armed) else self.unaddressed + 1
        await self.submit(text)
        self.voice_chat = True

    async def submit(self, text: str, image: str | None = None, auto: bool = False) -> None:
        """Start a turn. image: base64 JPEG to show the brain.
        auto: Pitagoras started this itself (screen glance); it may stay quiet."""
        text = text.strip()
        if not text:
            return
        self.interrupt()
        if not auto:
            self.last_activity = time.monotonic()
            tools.ctx.extra["request_started"] = self.last_activity  # timers count from here
            self.voice_chat = False  # _voice_input sets it back for spoken turns
            self.hub.emit("user_text", text=text)
        self.turn = asyncio.create_task(self._run_turn(text, image, auto))
        self.on_turn(True)  # the ears stop listening until this request is completely done

    def interrupt(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()
        if self.speaker:
            self.speaker.interrupt()

    @staticmethod
    def _content(text: str, image: str | None):
        if not image:
            return text
        return [{"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}]

    def _messages(self) -> list[dict]:
        """System prompt + recent history; only the newest screenshot is kept
        (and none at all if the brain can't see)."""
        hist = self.history[-self.max_history:]
        req = getattr(self, "_turn_request", None)
        if req is not None and not any(m is req for m in hist):
            hist = [req] + hist  # long PC-control sessions: never lose sight of what was asked
        last_img = max((i for i, m in enumerate(hist) if isinstance(m["content"], list)), default=-1)
        if not self.brain.vision:
            last_img = -1
        out = [{"role": "system", "content": self.system_prompt()}]
        for i, m in enumerate(hist):
            if isinstance(m["content"], list) and i != last_img:
                text = " ".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")
                m = {"role": m["role"], "content": text}  # old screenshots: words only, no marker to copy
            out.append(m)
        # Some chat templates insist on user/assistant alternation; merge repeats.
        merged = [out[0]]
        for m in out[1:]:
            prev = merged[-1]
            if m["role"] == prev["role"] and isinstance(m["content"], str) and isinstance(prev["content"], str):
                merged[-1] = {"role": m["role"], "content": prev["content"] + "\n\n" + m["content"]}
            else:
                merged.append(m)
        return merged

    # ── a turn ───────────────────────────────────────────────
    async def _run_turn(self, user_text: str, image: str | None = None, auto: bool = False) -> None:
        if self.brain is None:
            msg = self.phrase("Nie mam teraz mózgu. Kliknij mnie prawym i wybierz jeden w menu Brain.",
                              "I don't have a brain right now. Right-click me and pick one under Brain.")
            self.hub.emit("assistant_start")
            self._say(msg)
            self.hub.emit("assistant_end")
            if self.speaker:
                self.speaker.flush()
            self.on_turn(False)
            return
        self.busy = True
        if hasattr(self.brain, "max_tokens"):
            self.brain.max_tokens = self.length()[1]
        start_len = len(self.history)
        self.history.append({"role": "user", "content": self._content(user_text, image)})
        self._hold = "" if auto else None
        self._turn_request = self.history[-1]
        control_steps = int((self.cfg.get("control") or {}).get("max_steps", 40))
        budget = 6  # tool calls per turn; grows to control.max_steps once it's controlling the PC
        nudges, nudge_msgs = 0, []
        task_comments = bool((self.cfg.get("chat") or {}).get("task_comments", False))
        quiet = False
        acted = False  # did a click/key/type happen since the last screenshot it asked for?
        from .tools import control as _control
        _control.reset()  # the user used the mouse since the last request: that's not "taking over"
        self._control_turn = bool(not auto and CONTROL_INTENT.search(user_text))
        if self._control_turn and (self.cfg.get("control") or {}).get("enabled", True):
            budget = control_steps  # asked to click/type: it's a control task from the start
        tools.ctx.extra["control_stopped"] = False
        try:
            if not auto and not self._control_turn and not image:
                self.hub.emit("state", state="thinking")
                note = await self._fact_check(user_text)  # questions: look it up first, then answer
                if note:
                    msg = {"role": "user", "content": note}
                    self.history.append(msg)
                    nudge_msgs.append(msg)  # (removed from the saved chat afterwards)
            _step = -1
            while _step + 1 < budget:
                _step += 1
                self.hub.emit("state", state="thinking")
                messages = self._messages()
                parser = StreamParser(list(TOOLS))
                raw = ""
                self._said = ""  # what was actually shown/spoken this step (kept if interrupted)
                self._mute = quiet  # task done and "task comments" off: don't show/say what comes next
                call: tuple | None = None
                if not quiet:
                    self.hub.emit("assistant_start")
                stream = self.brain.stream(messages)
                try:
                    async for chunk in stream:
                        raw += chunk
                        self._say(parser.feed(chunk))
                        call = self._take_calls(parser)
                        if call:
                            break  # stop generating after a call that needs its result
                finally:
                    await stream.aclose()  # stops generation / kills the CLI process
                self._say(parser.finish())
                self._release_hold(final=True)
                call = call or self._take_calls(parser)
                if self.speaker:
                    self.speaker.flush()

                if call and "</tool>" in raw:
                    raw = raw[: raw.find("</tool>") + len("</tool>")]
                content = scrub(raw)
                if quiet:  # nothing was said: keep only the tool calls in the history
                    content = "".join(re.findall(r"<tool>.*?</tool>", raw, re.S))
                if content:
                    self.history.append({"role": "assistant", "content": content})
                if not quiet:
                    self.hub.emit("assistant_end")

                if not call:
                    # it only made a face (an emote) and stopped: no words, no action -> once more, properly
                    if _step == 0 and not auto and not self._said.strip() and nudges == 0:
                        nudges += 1
                        log.info("(only an emote / empty reply: asking again)")
                        msg = {"role": "user", "content": EMPTY_NUDGE}
                        self.history.append(msg)
                        nudge_msgs.append(msg)
                        continue
                    # controlling the PC but only *talked* about clicking: push it to actually do it
                    if budget > 6 and nudges < 2 and not acted and not DONE_RE.search(raw):
                        nudges += 1
                        log.info("(described an action without doing it: nudging)")
                        msg = {"role": "user", "content": NUDGE}
                        self.history.append(msg)
                        nudge_msgs.append(msg)
                        continue
                    break
                nudges = 0
                call = call[0]
                if (call and _step == 0 and not auto and not self._said.strip() and call[0] in TOOLS
                        and (call[0] not in QUICK_TOOLS or (not task_comments and call[0] in ACTION_TOOLS))):
                    # it went straight to a slow action without a word: acknowledge it ourselves
                    self.hub.emit("assistant_start")
                    self._say(random.choice(self.phrase(ACKS_PL, ACKS_EN)))
                    self.hub.emit("assistant_end")
                    if self.speaker:
                        self.speaker.flush()
                if call is None:
                    result, name = "Error: could not parse the tool call JSON.", "?"
                else:
                    name, args = call
                    log.info("tool %s %s", name, args)
                    self.hub.emit("tool", name=name, args=args)
                    self.hub.emit("state", state="working")
                    result = await run_tool(name, args)
                    if name in CONTROL_TOOLS or (name == "look_at_screen" and str(args.get("grid")).lower() == "true"):
                        budget = max(budget, control_steps)
                    if name in ACTION_TOOLS and hasattr(self.brain, "max_tokens"):
                        # after doing something: a short "done" line (enough room for another tool call)
                        self.brain.max_tokens = min(self.brain.max_tokens, 60)
                    if name in CONTROL_TOOLS and name != "wait_and_look":
                        acted = not str(result).startswith(("Error", "STOPPED", "Mouse & keyboard control is turned off"))
                    elif name == "look_at_screen":
                        acted = False
                image = None
                if isinstance(result, dict):
                    result, image = result.get("text", ""), result.get("image")
                log.info("  -> %s%s", result[:200].replace("\n", " "), " [+screenshot]" if image else "")
                if call and name in ACTION_TOOLS:
                    ok = not FAILED_RE.match(str(result))
                    # task comments off: once a task succeeded, whatever comes next stays unsaid
                    # (it can still call more tools; a failure is still told to the user)
                    quiet = ok and not task_comments
                    if name not in CONTROL_TOOLS:
                        result += ("\n(Done.)" if quiet else
                                   "\n(Done. Now say at most ONE short sentence, max 8 words, no questions.)")
                text = f'<tool_result name="{name}">\n{result}\n</tool_result>'
                self.history.append({"role": "user", "content": self._content(text, image)})
                if tools.ctx.extra.get("control_stopped"):  # the user grabbed the mouse: hand it back
                    self._mute = False
                    msg = self.phrase("Okej, oddaję ci myszkę.", "Okay, the mouse is all yours.")
                    self.history.append({"role": "assistant", "content": msg})
                    self.hub.emit("assistant_start")
                    self._say(msg)
                    self.hub.emit("assistant_end")
                    if self.speaker:
                        self.speaker.flush()
                    break

            if self.speaker:
                await asyncio.to_thread(self.speaker.idle.wait)
        except _StayQuiet:
            log.info("(glanced at the screen, nothing to say)")
            del self.history[start_len:]
            self.hub.emit("assistant_end")
        except asyncio.CancelledError:
            # keep only what the user actually heard; never a marker the model could start copying
            said = scrub(getattr(self, "_said", ""))
            if said and self.history and self.history[-1]["role"] != "assistant":
                self.history.append({"role": "assistant", "content": said.rstrip(" ,;:") + "…"})
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            self.hub.emit("error", message=str(e))
            if self.speaker:
                self.speaker.say(self.phrase("Sorki, coś mi się zacięło w głowie.", "Sorry, my brain glitched."))
        finally:
            self._hold = None
            self._mute = False
            self.busy = False
            if nudge_msgs:  # internal reminders don't belong in the saved chat
                self.history = [m for m in self.history if not any(m is n for n in nudge_msgs)]
            if auto and len(self.history) > start_len and self.history[start_len]["role"] == "user":
                # keep what it saw, drop the instructions (the screenshot stays for follow-up questions)
                c = self.history[start_len]["content"]
                summary = auto_summary(user_text)
                self.history[start_len]["content"] = (
                    [{**p, "text": summary} if p.get("type") == "text" else p for p in c] if isinstance(c, list) else summary)
            self.save()
            if not auto:
                self.last_reply = time.monotonic()
            self.hub.emit("state", state="idle")
            if asyncio.current_task() is self.turn:  # (not when a newer request replaced this one)
                self.on_turn(False)

    def _question_only(self, text: str) -> str:
        """'Hej Pitagoras, kto wygrał mundial?' -> 'kto wygrał mundial?' (the search doesn't need my name)."""
        words = text.split()
        name = self.wake_phrase.split()[-1]
        for i, w in enumerate(words[:4]):
            bare = re.sub(r"[^\w]", "", w.lower())
            if bare in self.aliases or (len(bare) >= 5 and difflib.SequenceMatcher(None, bare, name).ratio() >= 0.7):
                return " ".join(words[i + 1:]).strip(" ,.!") or text
        return text

    async def _fact_check(self, user_text: str) -> str | None:
        """Search the web for a question before answering it. Returns the note for the brain, or None."""
        if not (self.cfg.get("chat") or {}).get("fact_check", True):
            return None
        q = self._question_only(user_text)
        if not wants_fact_check(q):
            return None
        from .tools import web
        region = "pl-pl" if self.phrase("pl", "en") == "pl" else "wt-wt"
        self.hub.emit("tool", name="web_search", args={"query": q})
        try:
            results = await asyncio.wait_for(asyncio.to_thread(web.search, q, 5, region), timeout=5)
        except Exception as e:  # noqa: BLE001 (offline, slow, blocked: just answer from memory)
            log.info("fact check search failed: %s", e)
            return None
        if not results:
            return None
        log.info("fact check: searched %r (%d results)", q, len(results))
        text = "\n".join(f"- {r.get('title', '')}: {r.get('body', '')}" for r in results)
        return FACT_NOTE.format(results=text[:2500])

    def _take_calls(self, parser: StreamParser) -> tuple | None:
        """Run instant tools (emote, move) right away. Returns (call,) for the
        first call whose result the model needs, ((None,) if unparseable)."""
        while parser.tool_calls:
            call = parse_tool_call(parser.tool_calls.pop(0))
            if call and call[0] in TOOLS and TOOLS[call[0]].instant:
                if call[0] == "move_to" and self._control_turn:
                    # "click the box at the bottom" is about the mouse, not about moving the character
                    log.info("(ignored move_to during a click/type task)")
                    continue
                log.info("tool %s %s", *call)
                self.hub.emit("tool", name=call[0], args=call[1])
                asyncio.create_task(run_tool(*call))
                continue
            return (call,)
        return None

    def _release_hold(self, final: bool = False) -> None:
        """Auto turns hold the first words back: a reply of just PASS means
        'nothing worth saying', and must never be spoken."""
        if self._hold is None:
            return
        head = re.sub(r"[^a-z]", "", self._hold.lower())[:4]
        if head == "pass" or (final and not self._hold.strip()):
            raise _StayQuiet
        if len(self._hold.strip()) >= 6 or final:
            held, self._hold = self._hold, None
            self._say(held)

    def _say(self, text: str) -> None:
        if not text or getattr(self, "_mute", False):
            return
        if getattr(self, "_hold", None) is not None:
            self._hold += text
            self._release_hold()
            return
        self._said = getattr(self, "_said", "") + text
        print(text, end="", flush=True)
        self.hub.emit("assistant_text", delta=text)
        if self.speaker:
            self.speaker.feed(text)
