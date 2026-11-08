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
You may say a very short line before an ACTION tool call ({ack}); never say it before simply answering a
question. Never invent tool results. If a tool fails, quietly try again or another way; don't tell the user
about errors, DNS, timeouts or retries unless you finally can't do it.
Tool calls are silent actions: never mention tool names in what you say.
Never write emoji or roleplay actions like *dances*: your character shows emotions with the emote tool.
Tracking cooldowns / timers: when the user says someone used a spell or summoner ("xayah flashneła, trackuj"),
web_search its cooldown (e.g. "League of Legends Flash summoner spell cooldown seconds"), then start_timer with
the full cooldown, a label like "Flash Xayah" and a message like "Flash jest gotowy na Xayah!". Reminders
("za 10 minut") are just start_timer too. Doing something later ("ustaw timer na 10 sekund i naciśnij spację",
"za minutę kliknij Start") = start_timer with then="naciśnij spację" (the user's own words for the action);
don't do the action now, it happens by itself when the timer ends. Only set then= when the user asked for
something to happen WHEN the timer ends; "ustaw timer i otwórz X" means open X now, with a plain timer.
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
Don't greet the user or say their name at the start of replies (no "Cześć!", "Hej Kuba!"): they called you by
name, that's not a greeting. Just answer.
When the user leaves the choice to you ("puść jakiś film", "wybierz coś", "any video", "whatever you want"),
pick one yourself right away and do it. Never ask which one, never list options back.
YouTube: "włącz/odpal kanał X" = open_youtube_channel (the channel page, not a search). "włącz kanał X i odpal
jakiś film" = ONE call: open_youtube_channel name=X play=random ("najnowszy" -> play=latest, "najpopularniejszy" ->
play=popular). A video BY a named YouTuber ("włącz film z gry fnaf od Elevena", "puść odcinek Blowka o
minecrafcie") = ONE call: open_youtube_channel name=Eleven about=fnaf play=random (name in its basic form).
Streamers on Twitch / Kick: open_stream_channel. When a request has several parts ("otwórz X i
zrób Y"), do every part, one tool after another, before you stop. "puść/odpal film(ik)
o X", "jakiś filmik" = open_youtube (it starts the top video; choose the topic yourself if none is given).
When they mean something already on the screen ("puść coś stąd", "z głównej", "kliknij jakiś film"):
look_at_screen, choose one yourself, click_on its exact title. Done in 2 steps, no questions.
Only use play_on_spotify for music the user asked for; never together with YouTube or a video.
Shopping / classifieds ("znajdź najtańsze ogłoszenie X na OLX", "szukaj butów na allegro"): open_shop_search
(cheapest=true when they want the cheapest). To open a page you found with web_search, call open_website with
the result's number or URL.
Discord ("napisz na discordzie na serwerze X na kanale Y: będę za 10 minut", "wyślij do Adama na discordzie że…")
= ONE call: send_discord server=X channel=Y message="będę za 10 minut" (or to=Adam). The message word for word
as the user said it (same language, no changes; the signature is added by itself). Never type_text for Discord.
Typing text ("wpisz/wypisz/napisz tu …", "masz zaznaczone pole, wypisz swoje hobby") = ONE call type_text with the
finished text and no into: it types where the cursor already is. Write the text yourself; don't say it out loud
and don't click. "otwórz notatnik i napisz X" = open_app notepad, then type_text (a new empty file is opened for you).

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
                     r"|<tool_result[^>]*>.*?</tool_result>|<think>.*?</think>", re.I | re.S)  # (real <tool> calls are kept)


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
        if inner[:1].isupper() or re.search(r"\d", inner):
            return inner  # *Battlefield 6*, *The Last of Us*: a title in italics, not an action
        if " " in inner or inner.lower().strip(".!") in EMOTE_WORDS:
            return ""  # *dances around* / *giggles*: an action, not words to say
        return inner  # *perfect*: just emphasis, keep the word
    return EMOTE_RE.sub(emote, EMOJI_RE.sub("", text)).replace("*", "")


async def _timed(fn, *args, timeout: float = 8, default=None):
    """Run a blocking helper in a thread, but give up after `timeout` s (UI Automation can hang on a frozen
    window, e.g. a game; that must never freeze the whole request)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
    except asyncio.TimeoutError:
        log.warning("%s took over %ss: skipped", getattr(fn, "__name__", fn), timeout)
        return default


def auto_context(prompt: str) -> dict:
    """The facts of a glance/watch prompt: window, what's playing, what was said in the video."""
    out = {}
    for ln in prompt.splitlines():
        ln = ln.strip()
        if ln.startswith("Active window:"):
            out["window"] = ln.split(":", 1)[1].strip().strip('"')
        elif ln.startswith("Playing:"):
            out["playing"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("What was said"):
            out["heard"] = ln.split(":", 1)[1].strip().strip('"')
    return out


def auto_summary(prompt: str, saw: str = "") -> str:
    """What to keep in the history for a glance/watch turn: the context, not the instructions
    (instructions like 'reply PASS' in old messages get copied into normal replies)."""
    if prompt.startswith("(You looked") or prompt.startswith("(Watching"):
        return prompt  # already a summary
    c = auto_context(prompt)
    watch = prompt.startswith("[Watch-along")
    bits = []
    if c.get("window"):
        bits.append(f'window "{c["window"][:80]}"')
    if c.get("playing"):
        bits.append(f"playing {c['playing'][:80]}")
    if c.get("heard"):
        bits.append(f'heard in it: "{c["heard"][-300:]}"')
    if saw:
        bits.append(f"you noticed: {saw[:300]}")
    head = (f"(Watching together with the user, {dt.datetime.now():%H:%M}; you commented by yourself"
            if watch else "(You looked at the user's screen by yourself")
    return head + (": " + "; ".join(bits) if bits else "") + ")"


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
        self.thoughts: list[str] = []  # <think> notes (never spoken; watch-along keeps them as memory)
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
                tagcall = re.match(r"<(\w+)\s*(?=\{)", self.buf)
                if tagcall and tagcall.group(1) in self.names:  # <emote {"emotion": "happy"}>: a call, not text
                    close = _close_paren(self.buf, tagcall.end(), "{", "}")
                    if close == -1:
                        if len(self.buf) > 800:
                            self.buf = self.buf[1:]
                            continue
                        break
                    try:
                        args = json.loads(self.buf[tagcall.end(): close + 1])
                    except ValueError:
                        args = {}
                    self.tool_calls.append(json.dumps({"name": tagcall.group(1), "args": args if isinstance(args, dict) else {}}))
                    self.buf = re.sub(r"^\s*/?>", "", self.buf[close + 1:])
                    self._eat_gt = True  # (the '>' may still be on its way in the next chunk)
                    continue
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
                        re.fullmatch(r"<(\w+)\s*", self.buf) and self.buf[1:].strip() in self.names) or (
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
                elif self.mode == "think" and self.inner.strip():
                    self.thoughts.append(self.inner.strip())
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
        elif self.mode == "think" and (self.inner + self.buf).strip():
            self.thoughts.append((self.inner + self.buf).strip())
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
                                "volume", "watch_along", "start_timer", "cancel_timer", "open_youtube_channel",
                                "open_shop_search", "open_stream_channel", "send_discord"}
# "co myślisz o tym?" with nothing else: "this" is whatever is on the screen
# the grid instructions + window title + clickable names that come with a screenshot
SCREEN_NOTE_RE = re.compile(r" This shows the active window.*?(?=\)\s*$|$)| Active window: \".*?(?=\)\s*$|$)", re.S)
def _fold_pl(s: str) -> str:
    """Lowercase without Polish accents ('naciśnij spację' -> 'nacisnij spacje') for loose word matching."""
    import unicodedata

    s = unicodedata.normalize("NFKD", s.lower().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


# the request asks for a video, not just a channel page
WANTS_VIDEO_RE = re.compile(r"\b(film\w*|filmik\w*|video|wideo|odcin\w*|coś|cos|jakiś|jakis|losow\w*|random|najnow\w*|"
                            r"ostatni\w*|nowy|nowego|newest|latest|popularn\w*|najlepsz\w*|top|any|something)\b", re.I)
# verbs that each ask for one action ("włącz kanał X i odpal jakiś film" has two)
ACTION_VERB_RE = re.compile(
    r"\b(włącz\w*|odpal\w*|puść\w*|pusc\w*|otwórz\w*|otworz\w*|uruchom\w*|zagraj\w*|wyszukaj\w*|poszukaj\w*|znajdź\w*|"
    r"pokaż\w*|ustaw\w*|nastaw\w*|zatrzymaj\w*|wyłącz\w*|zamknij\w*|kliknij\w*|naciśnij\w*|wpisz\w*|przewiń\w*|"
    r"napisz\w*|zapisz\w*|wyślij\w*|skopiuj\w*|wklej\w*|pobierz\w*|zrób\w*|write|save|send|copy|paste|"
    r"open|play|launch|start|search|find|show|set|stop|close|click|press|type)\b", re.I)


# ── typing requests ("masz zaznaczone pole, wypisz swoje hobby", "otwórz notatnik i napisz listę zakupów") ──
TYPE_VERB_RE = re.compile(r"\b(wpisz\w*|wypisz\w*|wpisa[cć]|wypisa[cć]|napisz\w*|napisa[cć]|zapisz|pisz|wklep\w*|"
                          r"wstukaj|type|write)\b", re.I)
TYPE_STRONG_RE = re.compile(r"\b(wpisz\w*|wypisz\w*|wpisa[cć]|wypisa[cć]|wklep\w*|wstukaj|type)\b", re.I)
TYPE_WHERE_RE = re.compile(r"\b(zaznaczon\w*|aktywn\w*|tutaj|tu|here|kursor\w*|pol[eua]|polu|okienk\w*|notatni\w*|"
                           r"notepad|word\w*|dokument\w*|plik\w*|pasek|pasku|wyszukiwar\w*|w nim|w niego|w nią|w niej|"
                          r"in it|text ?box|field|czat\w*|chat)\b", re.I)
TYPE_FIELD_RE = re.compile(r"\b(pol[eua]|polu|okienk\w*|pasek|pasku|wyszukiwar\w*|text ?box|field|search)\b", re.I)
TYPE_FOCUSED_RE = re.compile(r"\b(zaznaczon\w*|aktywn\w*|tutaj|tu|here|kursor\w*|w tym polu|w tym okienku)\b", re.I)
TYPE_OPEN_RE = re.compile(r"\b(otw[oó]rz\w*|odpal\w*|uruchom\w*|włącz\w*|open|launch|start)\b", re.I)
NOTEPAD_RE = re.compile(r"\b(notatni\w*|notepad)\b", re.I)
NEW_FILE_RE = re.compile(r"\b(now(?:y|ym|ego) plik\w*|now(?:ą|a|ej) kart\w*|now(?:y|ym) dokument\w*|stw[oó]rz plik|"
                         r"utw[oó]rz plik|new (?:file|tab|document))\b", re.I)
# after typing, more to do ("…i naciśnij enter", "…i zapisz")
TYPE_MORE_RE = re.compile(r"\b(enter\w*|wy[śs]lij|zatwierd[źz]|naci[śs]nij|wci[śs]nij|zapisz go|zapisz plik|"
                          r"zapisz jako|i zapisz|send|press|save)\b", re.I)
TYPE_BLOCKED = ("Not done: the user wants text TYPED {where}. Call type_text with text=<the finished text, written "
                "by you in the user's language> and NO 'into'. Don't click or look at anything.")
TYPE_NUDGE = ("You haven't typed anything: the text has to go {where}, not be said to me. Call type_text now with "
              "text=<what I asked for> and no 'into'. Reply with ONLY the tool call.")


def typing_request(text: str) -> str:
    """'' (not typing), 'focused' (type where the cursor is), 'app' (open an app, then type there),
    'field' (type into a box the user named: type_text into=…)."""
    t = text.split("\n")[0]
    if DISCORD_RE.search(t) or not TYPE_VERB_RE.search(t):
        return ""
    if not (TYPE_STRONG_RE.search(t) or TYPE_WHERE_RE.search(t)):
        return ""  # "napisz mi wiersz": just answer
    if TYPE_OPEN_RE.search(t):
        return "app"
    if TYPE_FIELD_RE.search(t) and not TYPE_FOCUSED_RE.search(t):
        return "field"
    return "focused"


def request_parts(text: str) -> int:
    """How many separate things a request asks for: 'włącz kanał X i odpal film' -> 2. A verb that only
    repeats the first one's object doesn't count ("otwórz steama i odpal deadlocka" opens both in one go anyway)."""
    verbs = ACTION_VERB_RE.findall(text)
    if len(verbs) < 2 or not re.search(r"\b(i|oraz|a potem|potem|następnie|and|then)\b|,", text, re.I):
        return 1
    return min(len(verbs), 3)


# a follow-up to something just done on the screen ("ale włącz go", "ten drugi", "nie włączyłeś", "jeszcze raz")
SCREEN_FOLLOW_RE = re.compile(
    r"\b(go|ją|je|ten|tą|tę|tamt\w*|ostatni\w*|pierwsz\w*|drug\w*|trzeci\w*|następn\w*|poprzedni\w*|inn\w*|"
    r"jeszcze raz|ponownie|nie (?:włączył\w*|kliknął\w*|działa|zadziałał\w*|odpalił\w*)|włącz|odpal|puść|wybierz|"
    r"otwórz|zamknij|wróć|cofnij|przewiń|zatrzymaj|again|that one|this one|the other|another)\b", re.I)
# keyboard keys the model sometimes tries to *click* ("click_on Spacja"): press them instead
KEY_NAMES = {k: v for v, names in {
    "space": ["spacja", "spację", "spacje", "spacji", "space", "spacebar", "klawisz spacja", "spacja key"],
    "enter": ["enter", "entera", "enterem", "return", "klawisz enter"],
    "esc": ["esc", "escape", "escapea", "eskejp"],
    "tab": ["tab", "taba", "tabulator"],
    "backspace": ["backspace", "backspacea"],
    "delete": ["delete", "del", "usuń klawisz"],
    "left": ["strzałka w lewo", "strzałkę w lewo", "left arrow", "lewa strzałka"],
    "right": ["strzałka w prawo", "strzałkę w prawo", "right arrow", "prawa strzałka"],
    "up": ["strzałka w górę", "strzałkę w górę", "up arrow"],
    "down": ["strzałka w dół", "strzałkę w dół", "down arrow"],
}.items() for k in names}
# "ustaw timer na 10 sekund i naciśnij spację": the clicking happens later, so it's not a click request now
TIMER_RE = re.compile(r"\b(timer\w*|minutnik\w*|stoper\w*|za \d+|za (?:minutę|sekundę|godzinę|chwilę|moment)|"
                      r"za (?:pół|kilka|parę|jedn\w*|dwie|dwa|trzy|pięć|"
                      r"dziesięć|piętnaście|dwadzieścia|trzydzieści)\w* (?:sekund|minut|godzin)\w*|"
                      r"after \d+|in \d+ (?:seconds?|minutes?)|odlicz\w*)", re.I)
DEICTIC_RE = re.compile(
    # just "zobacz" / "spójrz na to" / "zerknij tu": look at the screen
    r"\b((?:zobacz|spójrz|spojrz|popatrz|patrz|zerknij|obczaj|oglądnij|obejrzyj|rzuć okiem|sprawdź|look|"
    r"take a look|check(?: it| this)? out|watch)(?: się)?(?: na)?(?: to| tu| tutaj| ten ekran| ekran| tego| tę| this| here)?|"
    r"co widzisz|co jest na ekranie|co mam na ekranie|what do you see|what's on (?:my|the) screen|"
    r"co (?:myślisz|sądzisz|powiesz) o (?:tym|tej|tego|tych)|co o (?:tym|tej|tych) (?:myślisz|sądzisz)|"
    r"co to (?:jest|za)|co to\b|co tu (?:jest|widać|się dzieje)|jak ci się (?:to )?podoba|(?:zobacz|spójrz|patrz)(?: na)? to|"
    r"oceń (?:to|tego|tę)|ładne to|dobre to|"
    r"what do you think (?:of|about) (?:this|that|it)|what(?:'s| is) (?:this|that)|look at this|rate this|"
    r"how does (?:this|it) look|thoughts on this)\b", re.I)
# the user asked for YouTube / a video site, so Spotify must not start too
NOT_SPOTIFY_RE = re.compile(r"\b(yt|youtub\w*|kanał\w*|channel|film\w*|video|wideo|netflix\w*|twitch\w*|stream\w*|"
                            r"serial\w*|odcin\w*)\b", re.I)
SPOTIFY_RE = re.compile(r"\b(spotif\w*|muzy\w*|piosenk\w*|utw[oó]r\w*|album\w*|playlist\w*|song|music|track)\b", re.I)
# a tool result that means it did NOT work (then it may say so)
FAILED_RE = re.compile(r"\s*(error|couldn't|could not|no |not found|stopped|mouse & keyboard control is turned off|"
                       r".*\bis not installed\b|.*didn't start|.*needs a click)", re.I)
DISCORD_RE = re.compile(r"\b(discord\w*|dc)\b", re.I)
CONTROL_INTENT = re.compile(r"\b(klik\w*|click\w*|wpisz|wpisać|naci[sś]\w*|wci[sś]\w*|przyci[sś]\w*|press|type in|"
                            r"tapnij|stuknij|puknij|dotknij|najedź|przeciągnij|odznacz|potwierdź|zatwierdź|"
                            r"tap|hit enter|hit space|"
                            r"zaznacz|przewiń|scroll|zamknij okno|graj w|zagraj w|play the game|"
                            # picking something that's already on the screen ("puść coś stąd", "z głównej")
                            r"stąd|z tej strony|z tych|z głównej|z ekranu|from here|on this page|"
                            r"on the screen|from the home ?page)", re.I)
DONE_RE = re.compile(r"\b(gotowe|zrobione|done|finished|skończone)\b", re.I)
# ── fact check: questions get a web search first, so the answer is checked against the internet ──
QUESTION_RE = re.compile(
    r"\?\s*$|^(?:what|what's|whats|who|who's|whom|whose|when|where|why|how|which|is|are|was|were|does|do|did|"
    r"can|could|will|would|should|has|have|tell me|explain|czy|co|kto|kogo|komu|kim|kiedy|gdzie|dlaczego|czemu|"
    r"jak|jaki|jaka|jakie|jakiego|jakiej|jakim|ile|ilu|który|która|które|którego|której|skąd|dokąd|po co|"
    r"powiedz|opowiedz|wytłumacz|wyjaśnij|sprawdź|znajdź|wiesz|znasz|słyszał\w*|have you heard|do you know)\b", re.I)
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
    r"wymyśl\w*|rap\w*|"
    # shops / video sites: that's opening a page, not a question to look up
    r"olx\w*|allegr\w*|vinted\w*|amazon\w*|ebay\w*|ceneo\w*|otomoto\w*|yt|youtub\w*|kanał\w*|twitch\w*|kick\w*|"
    r"stream\w*)\b", re.I)


POINTER_WORDS = re.compile(
    r"^(?:to|tym|tego|tej|tu|tutaj|ten|ta|te|teraz|jest|się|mi|no|a|i|ej|hej|sobie|na|ekran\w*|screen|film\w*|"
    r"filmik\w*|gr\w*|game|video|wideo|zdjęci\w*|obraz\w*|obrazek|picture|image|stron\w*|page|this|that|it|here|now|"
    r"one|thing|rzecz\w*|ogóle|w|of|about)$", re.I)


def vague_this(text: str) -> bool:
    """'co myślisz o tym?', 'co to jest?', 'what do you think about this game': 'this' has no subject, so it
    means what's on the screen. ('co to jest fotosynteza' has one, so it doesn't count.)"""
    m = DEICTIC_RE.search(text)
    if not m:
        return False
    rest = re.findall(r"[^\W\d_]+", text[: m.start()] + " " + text[m.end():])
    return all(POINTER_WORDS.match(w) for w in rest)


# while watching: "co miałeś na myśli", "o co ci chodziło", "ten gol", "co się stało" -> about the video / its own
# comments, not something to look up online
WATCH_REF_RE = re.compile(
    r"\b(miał[ae]?ś na myśli|chodziło|powiedział[ae]?ś|mówił[ae]?ś|wcześniej|przed chwilą|"
    r"co się (?:stało|dzieje|wydarzyło)|co on|co ona|co oni|kto (?:to|wygrywa|prowadzi|strzelił)|"
    r"jaki (?:jest )?wynik|ile jest|dlaczego (?:on|ona|oni|tak)|"
    r"t(?:a|ę|ej|en|o|ego|ym|ych|amt\w+) (?:akcj\w*|gol\w*|bramk\w*|zagrani\w*|scen\w*|moment\w*|podani\w*|"
    r"faul\w*|strzał\w*|rzut\w*|kar\w+|sędzi\w*|piłkarz\w*|zawodnik\w*|gracz\w*|postać|postaci\w*|"
    r"film\w*|odcin\w*|mecz\w*|runda|rund\w*|walk\w*|kill\w*|zagrywk\w*)|"
    r"what did you mean|you (?:just )?said|what happened|who scored|what(?:'s| is) the score|that (?:play|goal|scene|move))",
    re.I)

# "hej pitagoras wyłącz się": close the whole app. ("zamknij się" = "shut up": that only stops it talking)
QUIT_RE = re.compile(
    r"\b(?:wyłącz|wylacz|wyłączaj|wyłączyć|wylaczyc|zakończ|zakoncz)\s+(?:się|sie|siebie|pitagoras\w*)(?!\w)|"
    r"\b(?:się|sie)\s+(?:wyłącz|wylacz)\w*|"
    r"\bzamknij\s+(?:siebie|pitagoras\w*)(?!\w)|"
    r"\b(?:shut\s*(?:yourself\s+)?down|turn\s+yourself\s+off|(?:quit|exit|close)\s+(?:yourself|pitagoras|the app))\b",
    re.I)


# "film z fnaf od Elevena", "odcinek by MrBeast": a named YouTuber (not "od razu", "od nowa", "od początku")
BY_YOUTUBER_RE = re.compile(
    r"\b(?:od|by)\s+(?!(?:razu|nowa|początku|poczatku|teraz|zaraz|dziś|dzis|jutra|wczoraj|rana|kiedy|tego|tej|"
    r"ciebie|mnie|niego|niej|nich|siebie|the|a|my|me|you)\b)([^\W\d_][\w\-]{2,})", re.I)


VIDEO_FILLER = re.compile(
    r"\b(hej|hey|pitagoras\w*|włącz\w*|wlacz\w*|puść|pusc|puścisz|odpal\w*|pokaż|pokaz|zagraj|znajdź|znajdz|"
    r"wyszukaj|daj|mi|nam|proszę|prosze|please|play|find|show|me|jaki[śs]|jakąś|jakas|jakie[śs]|losow\w*|"
    r"film\w*|filmik\w*|odcin\w*|wideo|video|materiał\w*|nagrani\w*|kanał\w*|kanal\w*|channel|"
    r"na|youtub\w*|yt|z gry|z grze|z|ze|o|about|ten|tę|te|to|ta|tego|i|and|the|a|of|"
    r"najnowsz\w*|najpopularniejsz\w*|przedostatni\w*|ostatni\w*|newest|latest|popular\w*|most|viewed|recent|"
    r"pierwsz\w*|drug[iaąe]\w*|trzec\w*|czwart\w*|piąt[yaą]\w*|szóst\w*|siódm\w*|ósm\w*|dziewiąt\w*|"
    r"dziesiąt\w*|first|second|third|fourth|fifth|\d{1,2}(?:st|nd|rd|th)|wrzucon\w*|wrzucił\w*|"
    r"(?:od|by)\s+\S+)\b", re.I)


def video_topic(text: str, name: str) -> str:
    """What the user said about the video itself: 'włącz teorię FNAF 3 od Elevena na YouTubie' -> 'teorię FNAF 3'."""
    t = re.sub(r"(?i)\b\d{1,2}\s*\.?\s*(?=naj|most|new|last|ostat|popul)", " ", text)  # "2. najnowszy"
    t = re.sub(r"(?i)\b(from|channel of|z kanału)\b", " ", t)
    t = VIDEO_FILLER.sub(" ", t)
    n = name.strip().lower()
    stem = n[:max(3, len(n) - 2)]  # Blowek -> "blow" (also catches Blowka, Blowkiem); Eleven -> "elev"
    if len(stem) >= 3:
        t = re.sub(rf"(?i)\b{re.escape(stem)}\w*", " ", t)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'\-]", " ", t)).strip()


def wants_quit(text: str) -> bool:
    """Only a short, direct 'turn yourself off' (not 'wyłącz się z tej gry i zagraj w...')."""
    words = re.findall(r"[^\W\d_]+", text)
    return bool(QUIT_RE.search(text)) and len(words) <= 6


# "poleć mi serial", "w co zagrać?", "co warto obejrzeć", "recommend me a game"
REC_RE = re.compile(
    r"\b(pole[ćc]\w*|rekomend\w*|recommend\w*|suggest\w*|zaproponuj\w*|doradź\w*|"
    r"co (?:warto|mam|byś|by|możesz) (?:\w+ )?(?:obejrzeć|zagrać|pograć|posłuchać|przeczytać|zjeść|ugotować|kupić|"
    r"zobaczyć|robić)|w co (?:zagrać|pograć|mam zagrać)|co (?:obejrzeć|posłuchać|przeczytać|zjeść|kupić)|"
    r"jak\w* (?:dobr\w* )?(?:film|serial|gr[aęy]|anime|książk|album|muzyk|słuchawk|laptop|telefon|myszk|klawiatur)\w*"
    r" (?:warto|polecasz|wybrać|kupić|obejrzeć)|what should i (?:watch|play|read|listen to|buy|eat)|"
    r"any (?:good )?(?:movies?|shows?|games?|books?|anime) (?:to|you))", re.I)


def wants_recommendation(text: str) -> bool:
    return bool(REC_RE.search(text)) and not vague_this(text)


REC_FILLER = re.compile(
    r"\b(pole[ćc]\w*|rekomend\w*|zaproponuj\w*|doradź\w*|mi|nam|jakiś|jakąś|jakieś|jakiegoś|jakichś|dobr\w*|fajn\w*|"
    r"coś|cos|proszę|możesz|może|warto|jaki\w*|jaką|jakie|co|w|mam|byś|by|czy|ty|ci|teraz|dzisiaj|"
    r"recommend\w*|suggest\w*|me|some|good|please|can|could|you|what|should|i|any|a)\b", re.I)
REC_VERBS = {"zagrać": "gry", "pograć": "gry", "obejrzeć": "filmy i seriale", "zobaczyć": "filmy i seriale",
             "posłuchać": "muzyka", "przeczytać": "książki", "zjeść": "przepisy na obiad", "ugotować": "przepisy",
             "kupić": "", "play": "games", "watch": "movies and shows", "read": "books", "listen": "music"}


def rec_query(text: str, polish: bool) -> str:
    """'poleć mi jakiś dobry serial kryminalny' -> 'serial kryminalny najlepsze 2026'
    ('w co zagrać na weekend' -> 'gry na weekend najlepsze 2026'): search engines want the topic, not the ask."""
    t = text.lower().rstrip("?!. ")
    for verb, noun in REC_VERBS.items():
        t = re.sub(rf"\b{verb}\b", noun, t)
    words = [w for w in REC_FILLER.sub(" ", t).split() if w]
    year = dt.date.today().year
    return f"{' '.join(words)} {'najlepsze' if polish else 'best'} {year}".strip()


def wants_fact_check(text: str) -> bool:
    """A question the internet could answer (not small talk, a request to do something, or about this PC)."""
    t = text.strip()
    if len(re.findall(r"[^\W\d_]+", t)) < 3 or vague_this(t):
        return False
    if wants_recommendation(t):
        return True
    if not QUESTION_RE.search(t) or SMALLTALK_RE.search(t) or LOCAL_RE.search(t) or DO_RE.search(t):
        return False
    return not CONTROL_INTENT.search(t)


REC_NOTE = ("(What people recommend online right now, found just now:\n{results}\n"
            "Recommend 2-4 concrete things: real titles/names taken from these results (not just your own "
            "favourites from your personality), each with a few words on why, matched to what the user asked. "
            "Short and natural, no "
            "lists or numbering, don't read out links or sources, and you don't need web_search for this again.)")
FACT_NOTE = ("(Internet results for this question, found just now:\n{results}\n"
             "Before answering, compare them with what you know. If they agree, just answer. If they disagree with "
             "you or are newer, trust these results and say what they say. If they have nothing to do with the "
             "question, ignore them and answer normally. Don't read out links, sources or numbers in brackets, "
             "and you don't need web_search for this again.)")
# it said it would do something ("odpalę ci to", "już otwieram") but called no tool: nothing happened
PROMISE_RE = re.compile(r"\b(odpal(?:ę|am)|otw(?:orzę|ieram)|włącz(?:ę|am)|pusz(?:czę|czam)|uruchom(?:ię|iam)|"
                        r"ustaw(?:iam|ię)|nastaw(?:iam|ię)|nacis(?:nę|kam)|klikn(?:ę)|klikam|wcis(?:nę|kam)|"
                        r"wyszuk(?:am|uję)|znajd(?:ę|uję)|sprawdz(?:ę|am)|zaraz (?:to )?(?:zrobię|ogarnę)|"
                        r"wysy[łl]am|wyślę|napiszę|piszę|lecę|ogarniam|"
                        r"i'll (?:open|play|start|search|find|launch|check|send|write)|opening|launching|playing it|"
                        r"sending)\b", re.I)
# a reply that is only "Robi się!" / "Moment!" means it meant to do something (and didn't)
ACK_ONLY_RE = re.compile(r"^\W*(?:(?:no |dobra,? |okej,? |ok,? )?(?:już )?(?:się )?robi(?: się)?|się robi|już się robi|"
                         r"moment|chwila|jasne|już|on it|okay|sure|done)\W*$", re.I)
PROMISE_NUDGE = ("You said you'd do it, but you called no tool, so nothing happened. Call the right tool now "
                 "(only the tool call, no words).")
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
        self._turn_auto = False  # the running turn is Pitagoras's own (a watch-along comment / glance)
        self.watch_log: list[dict] = []  # watch-along: what it saw, heard and said (so you can ask about it)
        self.on_quit: Callable[[], None] | None = None  # set by the app ("wyłącz się")

    # ── watch-along memory ───────────────────────────────────
    def watching(self) -> bool:
        """Watching along now, or did a few minutes ago (questions about the video still make sense)."""
        app = tools.ctx.extra.get("app")
        if app is not None and getattr(getattr(app, "watcher", None), "active", False):
            return True
        return bool(self.watch_log) and time.time() - self.watch_log[-1]["t"] < 600

    def remember_watch(self, prompt: str, saw: str, said: str) -> None:
        c = auto_context(prompt)
        if not (saw or said or c.get("heard")):
            return
        self.watch_log.append({"t": time.time(), "saw": saw[:300], "said": said[:300],
                               "heard": c.get("heard", "")[-300:], "window": c.get("window", "")[:80]})
        del self.watch_log[:-12]
        log.info("watch memory: saw=%r said=%r", saw[:80], said[:80])

    def watch_memory_note(self) -> str:
        """The recent watch-along moments, for a question asked while watching."""
        now = time.time()
        lines = []
        for e in self.watch_log[-8:]:
            ago = int(now - e["t"])
            when = f"{ago}s ago" if ago < 90 else f"{ago // 60} min ago"
            bits = []
            if e["window"]:
                bits.append(f'window "{e["window"]}"')
            if e["saw"]:
                bits.append(f"you saw: {e['saw']}")
            if e["heard"]:
                bits.append(f'heard: "{e["heard"][-200:]}"')
            bits.append(f'you said: "{e["said"]}"' if e["said"] else "you stayed quiet")
            lines.append(f"- {when}: " + "; ".join(bits))
        if not lines:
            return ""
        return ("(Watch-along memory: what you saw, heard and said while watching with me, oldest first:\n"
                + "\n".join(lines) + "\nIf I ask about the video/game/match or about something you said "
                "('co miałeś na myśli', 'o co ci chodziło'), answer from this memory and the current screen: "
                "explain in your own words what you meant and what happened. Never say you don't remember.)")

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
        user_name = " ".join(w[:1].upper() + w[1:] for w in user_name.split())  # "kuba" -> "Kuba"
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

    async def _goodbye(self) -> None:
        """'Wyłącz się': say bye, let it finish speaking, then close the whole app."""
        log.info("asked to shut down: closing Pitagoras")
        msg = random.choice(self.phrase(["Pa pa! Wyłączam się.", "Do zobaczenia! Wyłączam się.", "Okej, papa!"],
                                        ["Bye! Shutting down.", "See you later!", "Okay, bye bye!"]))
        self.hub.emit("assistant_start")
        self.hub.emit("emotion", emotion="happy")
        self._say(msg)
        self.hub.emit("assistant_end")
        if self.speaker:
            self.speaker.flush()
            await asyncio.sleep(0.3)
            await asyncio.to_thread(self.speaker.idle.wait, 8)
        else:
            await asyncio.sleep(1.5)  # (time to read the bubble)
        self.busy = False
        self.on_quit()

    async def _video_speech(self, seconds: int = 40) -> str:
        """What was said in the video lately (PC sound -> Whisper), for questions while watching."""
        app = tools.ctx.extra.get("app")
        if app is None or not ((app.cfg.get("watch") or {}).get("hear_audio", True)):
            return ""
        try:
            from .watch import TAP, transcribe

            audio = TAP.since(time.monotonic() - seconds)
            return await asyncio.to_thread(transcribe, audio, app.cfg.voice)
        except Exception as e:  # noqa: BLE001
            log.warning("could not transcribe the video audio: %s", e)
            return ""

    async def do_later(self, text: str) -> None:
        """A timer's 'then' ("naciśnij spację"): run it as a request once nothing else is going on."""
        for _ in range(120):  # (up to a minute: let a reply that's being spoken finish first)
            if not self.in_turn():
                break
            await asyncio.sleep(0.5)
        log.info("timer action: %s", text)
        await self.submit(text)

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
            # ...except while it's only making a watch-along comment by itself: you talking wins
            if not (self._turn_auto and (not self.wake_word or self.wake_heard(text) or live_wake is not None
                                         or time.monotonic() < self.armed_until)):
                log.info("(busy, ignored what was heard)")
                return
            log.info("(cut into its own comment: answering you instead)")
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
                if self._turn_auto and self.in_turn():
                    self.interrupt()  # it was commenting by itself: stop and listen
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
        self._turn_auto = auto
        if auto and self.phrase("pl", "en") == "pl":  # its own comments come from long English prompts
            text += "\nTwój komentarz powiedz po polsku (PASS zostaje PASS)."
        self.turn = asyncio.create_task(self._run_turn(text, image, auto))
        if not auto:  # (its own comments: the ears stay on while it looks and thinks, so you can cut in)
            self.on_turn(True)  # the ears stop listening until this request is completely done

    def interrupt(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()
        if self.speaker:
            self.speaker.interrupt()

    def control_banner(self) -> str:
        return self.phrase("Naciśnij Esc, aby wyjść z trybu kontroli", "Press Esc to leave control mode")

    def stop_control(self) -> None:
        """The user pressed Esc while it was clicking/typing: stop the whole task right now and say so."""
        turn = self.turn
        if not turn or turn.done():
            return
        msg = self.phrase("Okej, przerywam. Myszka jest twoja.", "Okay, stopping. The mouse is yours.")

        def told(_t):
            self.history.append({"role": "assistant", "content": msg})
            self.hub.emit("assistant_start")
            self.hub.emit("assistant_text", delta=msg)
            self.hub.emit("assistant_end")
            if self.speaker:
                self.speaker.say(msg)
            self.save()

        turn.add_done_callback(told)
        self.interrupt()

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
        # only a screenshot from THIS request counts: an older one shows a screen that has changed since
        # (it used to click things from a stale picture on the next request)
        req_at = next((i for i, m in enumerate(hist) if m is req), 0)
        last_img = max((i for i, m in enumerate(hist) if isinstance(m["content"], list) and i >= req_at), default=-1)
        if not self.brain.vision:
            last_img = -1
        out = [{"role": "system", "content": self.system_prompt()}]

        def text_of(m) -> str:
            c = m["content"]
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if p.get("type") == "text")

        # the long "what you can click" lists: only the newest one matters (the context is small)
        last_note = max((i for i, m in enumerate(hist) if SCREEN_NOTE_RE.search(text_of(m))), default=-1)
        for i, m in enumerate(hist):
            if isinstance(m["content"], list) and i != last_img:
                m = {"role": m["role"], "content": text_of(m)}  # old screenshots: words only, no marker to copy
            if i != last_note and SCREEN_NOTE_RE.search(text_of(m)):
                slim = SCREEN_NOTE_RE.sub(" (screen details from back then left out)", text_of(m))
                m = {"role": m["role"], "content": slim if isinstance(m["content"], str) else
                     [{**p, "text": slim} if p.get("type") == "text" else p for p in m["content"]]}
            out.append(m)
        # Some chat templates insist on user/assistant alternation; merge repeats.
        merged = [out[0]]
        for m in out[1:]:
            prev = merged[-1]
            if m["role"] == prev["role"] and isinstance(m["content"], str) and isinstance(prev["content"], str):
                merged[-1] = {"role": m["role"], "content": prev["content"] + "\n\n" + m["content"]}
            elif m["role"] == prev["role"] and isinstance(m["content"], str):  # (screenshot + a note)
                merged[-1] = {"role": m["role"], "content": prev["content"] + [{"type": "text", "text": m["content"]}]}
            elif m["role"] == prev["role"] and isinstance(prev["content"], str) and m["role"] != "system":
                merged[-1] = {"role": m["role"], "content": [{"type": "text", "text": prev["content"]}] + m["content"]}
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
        asked = user_text
        watch_note = ""
        watch_img = False
        if not auto:
            user_text = asked = self._question_only(user_text)  # the brain doesn't need "Hej Pitagoras" (it greets back)
            if wants_quit(asked) and self.on_quit:
                await self._goodbye()
                return
            from . import eyes

            wants_control = (CONTROL_INTENT.search(asked) and not TIMER_RE.search(asked)
                             and not DISCORD_RE.search(asked)  # (send_discord does it all, no screenshot needed)
                             and (self.cfg.get("control") or {}).get("enabled", True))
            self._typing = typing_request(asked) if (self.cfg.get("control") or {}).get("enabled", True) else ""
            if self._typing == "focused":
                # the cursor is already in the box: no screenshot (it only tempts it to click around)
                user_text += ("\n(Type it where my cursor is: ONE call type_text with text=<the finished text, written "
                              "by you, in my language> and NO 'into'. Don't click, don't look at the screen, and don't "
                              "say the text out loud: it goes into the box.)")
                log.info("(typing request: type where the cursor is)")
            elif self._typing == "app":
                user_text += ("\n(First open the app with open_app, then type with type_text text=<the finished text, "
                              "written by you, in my language> and NO 'into' (the app will be focused, and Notepad "
                              "gets a new empty file). Don't click anything and don't say the text out loud.)")
                log.info("(typing request: open the app, then type)")
            if not image and wants_control and self._typing not in ("focused", "app") and eyes.can_see()[0]:
                # "naciśnij X" / "kliknij": look first (the window with a grid), then click what's really there
                self.hub.emit("state", state="thinking")
                image = await asyncio.to_thread(eyes.capture_for_control, tools.ctx.extra.get("char_pos"))
                note = await _timed(eyes.window_note, default='')
                user_text += ("\n(Attached: the screen right now." + eyes.GRID_NOTE.format(w=eyes.LAST["w"], h=eyes.LAST["h"],
                                                                                      g=eyes.GRID)
                              + note + " If I let you choose ('któryś', 'jakiś'), pick one from the list yourself. "
                                "If I only said 'naciśnij'/'kliknij', I mean the thing I asked you to press earlier.)")
                log.info("(click request: looking at the screen first)")
            elif not image and vague_this(user_text):  # "co myślisz o tym?" / "zobacz": "this" is what's on the screen
                if eyes.can_see()[0]:
                    self.hub.emit("state", state="thinking")
                    image = await asyncio.to_thread(eyes.capture, tools.ctx.extra.get("char_pos"))
                    note = await _timed(eyes.window_note, 40, False, default='')
                    user_text += ("\n(Attached: my screen right now. \"This\" means what's on it, unless it clearly "
                                  "means what we were just talking about. Only look and answer; don't click or do "
                                  "anything unless I ask." + note + ")")
                    log.info("(vague 'this': looking at the screen)")
            elif (not image and time.monotonic() - getattr(self, "_screen_task_at", 0) < 120
                  and SCREEN_FOLLOW_RE.search(asked) and eyes.can_see()[0]):
                # right after a screen task: "ale włącz go", "ten drugi", "nie włączyłeś" -> look again, fresh
                self.hub.emit("state", state="thinking")
                image = await asyncio.to_thread(eyes.capture_for_control, tools.ctx.extra.get("char_pos"))
                note = await _timed(eyes.window_note, default='')
                user_text += ("\n(Attached: the screen right now, since we were just doing something on it."
                              + eyes.GRID_NOTE.format(w=eyes.LAST["w"], h=eyes.LAST["h"], g=eyes.GRID) + note + ")")
                log.info("(follow-up to a screen task: looking again)")
            if self.watching() and not wants_control:
                # watching together: questions are about what's on screen and what it said about it
                if not image and eyes.can_see()[0]:
                    self.hub.emit("state", state="thinking")
                    image = await asyncio.to_thread(eyes.capture, tools.ctx.extra.get("char_pos"))
                    watch_img = bool(image)
                    if image:
                        user_text += "\n(Attached: the screen right now; we're watching it together.)"
                heard = await self._video_speech(40)
                parts = [p for p in (self.watch_memory_note(),
                                     f'(Said in the video in the last ~40 s: "{heard}")' if heard else "") if p]
                watch_note = "\n".join(parts)
                log.info("(watching together: question with the screen%s and %d remembered moments)",
                         " + video audio" if heard else "", len(self.watch_log))
        self._saw_screen = bool(image)
        if image and not auto:
            self._screen_task_at = time.monotonic()
        self.history.append({"role": "user", "content": self._content(user_text, image)})
        self._hold = "" if auto else None
        self._said = ""
        self._parser = None
        self._turn_request = self.history[-1]
        control_steps = int((self.cfg.get("control") or {}).get("max_steps", 40))
        budget = 6  # tool calls per turn; grows to control.max_steps once it's controlling the PC
        nudges, nudge_msgs = 0, []
        task_comments = bool((self.cfg.get("chat") or {}).get("task_comments", False))
        quiet = False
        if watch_note:  # (internal: not saved in the chat, rebuilt fresh for every question)
            msg = {"role": "user", "content": watch_note}
            self.history.append(msg)
            nudge_msgs.append(msg)
        ran_tool = False  # any (non-instant) tool used in this turn
        actions_done = 0
        parts_asked = 1 if auto else request_parts(asked)  # "otwórz X i odpal Y" = 2 things to do
        acted = False  # did a click/key/type happen since the last screenshot it asked for?
        from .tools import control as _control
        _control.reset()  # the user used the mouse since the last request: that's not "taking over"
        if auto:
            self._typing = ""
        self._typed = self._opened = False
        self._control_turn = bool(not auto and (CONTROL_INTENT.search(asked) or self._typing)
                                  and not TIMER_RE.search(asked) and not DISCORD_RE.search(asked))
        if self._control_turn and (self.cfg.get("control") or {}).get("enabled", True):
            budget = control_steps  # asked to click/type: it's a control task from the start
            _control.begin(self.control_banner())
        tools.ctx.extra["control_stopped"] = False
        try:
            if (not auto and not self._control_turn and (not image or watch_img)
                    and not (watch_note and WATCH_REF_RE.search(asked))):
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
                self._parser = parser
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
                    # promised an action ("odpalę ci to") but never called a tool: once more, properly
                    if (not auto and not ran_tool and nudges == 0
                            and (budget <= 6 or self._control_turn or self._saw_screen)
                            and (PROMISE_RE.search(scrub(self._said)) or ACK_ONLY_RE.match(scrub(self._said)))):
                        nudges += 1
                        log.info("(promised an action without calling a tool: nudging)")
                        msg = {"role": "user", "content": PROMISE_NUDGE}
                        self.history.append(msg)
                        nudge_msgs.append(msg)
                        continue
                    # asked to TYPE something, but it said it (or stopped after opening the app): type it now
                    if not auto and self._typing in ("focused", "app") and not self._typed and nudges < 2:
                        nudges += 1
                        log.info("(typing request but nothing typed yet: nudging)")
                        if self._typing == "app" and not self._opened:
                            msg = {"role": "user", "content": "First open the app with open_app, then type the text "
                                                              "with type_text (no 'into'). Only the tool call."}
                        else:
                            msg = {"role": "user", "content": TYPE_NUDGE.format(where=self._type_where())}
                        self.history.append(msg)
                        nudge_msgs.append(msg)
                        self._mute = False
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
                    name, args, blocked = self._guard_call(name, args, user_text)
                    if (blocked is None and name in CONTROL_TOOLS and not auto
                            and (self.cfg.get("control") or {}).get("enabled", True)):
                        _control.begin(self.control_banner())  # it can click & type now: show it (and Esc)
                    if (blocked is None and name == "type_text" and not self._typed and not self._opened
                            and NEW_FILE_RE.search(asked)):
                        await asyncio.to_thread(_control.new_file_in_notepad, False)  # "stwórz nowy plik i napisz…"
                    result = blocked if blocked is not None else await run_tool(name, args)
                    ran_tool = True
                    if name in CONTROL_TOOLS or (name == "look_at_screen" and str(args.get("grid")).lower() == "true"):
                        budget = max(budget, control_steps)
                    if (name in ACTION_TOOLS and hasattr(self.brain, "max_tokens")
                            and not (self._typing in ("focused", "app") and not self._typed)):  # (the text needs room)
                        # after doing something: a short "done" line (enough room for another tool call)
                        self.brain.max_tokens = min(self.brain.max_tokens, 60)
                    if name in CONTROL_TOOLS and name != "wait_and_look":
                        acted = not str(result).startswith(("Error", "STOPPED", "Mouse & keyboard control is turned off"))
                    elif name == "look_at_screen":
                        acted = False
                image = None
                if isinstance(result, dict):
                    result, image = result.get("text", ""), result.get("image")
                    if image:
                        self._saw_screen = True
                        self._screen_task_at = time.monotonic()
                log.info("  -> %s%s", result[:200].replace("\n", " "), " [+screenshot]" if image else "")
                type_next = False  # an app was opened to type in: the typing comes next, it isn't done yet
                if call and name == "type_text" and str(result).startswith("Typed"):
                    self._typed = True
                if (call and name == "open_app" and self._typing == "app" and not self._typed
                        and not FAILED_RE.match(str(result))):
                    self._opened = type_next = True
                    notepad = NOTEPAD_RE.search(str(args.get("name", ""))) or NOTEPAD_RE.search(asked)
                    made = await asyncio.to_thread(_control.new_file_in_notepad, True) if notepad else False
                    if not notepad:
                        await asyncio.sleep(1.5)  # (let the app's window come up and take the focus)
                    result += ("\n(Notepad is open with a NEW empty file, focused." if made else
                               "\n(The app is open and focused.") + (" Now type the text: type_text text=<the "
                               "finished text> with NO 'into'. Don't click anything.)")
                if call and name in ACTION_TOOLS:
                    ok = not FAILED_RE.match(str(result))
                    # task comments off: once a task succeeded, whatever comes next stays unsaid
                    # (it can still call more tools; a failure is still told to the user)
                    quiet = ok and not task_comments and not type_next
                    actions_done += ok
                    if ok and name == "open_youtube_channel" and str(result).startswith("playing"):
                        actions_done = parts_asked  # channel + a video from it: both parts in one go
                    if ok and name == "start_timer" and args.get("then"):
                        actions_done = parts_asked  # "…i naciśnij spację" is the timer's action, for LATER
                    if name not in CONTROL_TOOLS and not type_next:
                        if ok and actions_done < parts_asked:  # "włącz kanał X i odpal jakiś film": not finished yet
                            result += (f"\n(Done. But the request has more parts ({asked!r}): do the NEXT part now "
                                       "with the right tool, without talking. If this tool already did all of it, stop.)")
                        else:
                            result += ("\n(Done.)" if quiet else
                                       "\n(Done. Now say at most ONE short sentence, max 8 words, no questions.)")
                if self.phrase("pl", "en") == "pl":  # results are in English: small models then answer in English
                    result += "\n(Mów do użytkownika po polsku.)"
                text = f'<tool_result name="{name}">\n{result}\n</tool_result>'
                self.history.append({"role": "user", "content": self._content(text, image)})
                if (self._typed and name == "type_text" and self._typing in ("focused", "app")
                        and not TYPE_MORE_RE.search(asked)):
                    break  # typed what was asked: done (no second copy, no clicking around afterwards)
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
            _control.end()  # hide its cursor + the Esc banner
            self._hold = None
            self._mute = False
            self.busy = False
            if nudge_msgs:  # internal reminders don't belong in the saved chat
                self.history = [m for m in self.history if not any(m is n for n in nudge_msgs)]
            saw = ""
            if auto:
                p = getattr(self, "_parser", None)
                saw = " ".join(p.thoughts).strip() if p else ""
                if user_text.startswith("[Watch-along"):
                    self.remember_watch(user_text, saw, scrub(getattr(self, "_said", "")))
            if auto and len(self.history) > start_len and self.history[start_len]["role"] == "user":
                # keep what it saw, drop the instructions (the screenshot stays for follow-up questions)
                c = self.history[start_len]["content"]
                summary = auto_summary(user_text, saw)
                self.history[start_len]["content"] = (
                    [{**p, "text": summary} if p.get("type") == "text" else p for p in c] if isinstance(c, list) else summary)
            self.save()
            if not auto:
                self.last_reply = time.monotonic()
            self.hub.emit("state", state="idle")
            if asyncio.current_task() is self.turn:  # (not when a newer request replaced this one)
                self.on_turn(False)

    def _type_where(self) -> str:
        return ("into the app you just opened (it's focused)" if getattr(self, "_typing", "") == "app"
                else "into the box where the user's cursor already is")

    @staticmethod
    def _names_field(user_text: str, into: str, typing: str) -> bool:
        """Did the user name the box the brain wants to click before typing? ('wpisz kot w wyszukiwarkę')"""
        said = _fold_pl(user_text.split("\n")[0])
        if typing == "field":
            return True
        return any(w[:5] in said for w in re.findall(r"\w{3,}", _fold_pl(into)))

    def _guard_call(self, name: str, args: dict, user_text: str) -> tuple[str, dict, str | None]:
        """Fix the small brain's typical slips before they happen. Returns (name, args, result-if-blocked)."""
        if DISCORD_RE.search(user_text.split("\n")[0]) and name in (
                "send_discord", "type_text", "press_keys", "click_on", "open_app", "open_website"):
            from .tools.discord_chat import parse_request

            p = parse_request(user_text.split("\n")[0])
            if name != "send_discord":
                if not p.get("message") or not (p.get("channel") or p.get("to")):
                    return name, args, None
                log.info("(Discord request: send_discord instead of %s)", name)
                name, args = "send_discord", {}
            # where and what from the user's own words: the brain likes to translate or "fix" the message
            if args.get("to"):
                p.pop("to", None)  # (the brain's "Adam" beats the parsed "Adama")
            args = {**args, **{k: v for k, v in p.items() if v}}
            if args.get("channel") and not p.get("to"):
                args.pop("to", None)  # a channel was named: never ALSO a DM (it invents "to" sometimes)
            return name, args, None
        typing = getattr(self, "_typing", "")
        typed = getattr(self, "_typed", False)
        if name == "type_text" and str(args.get("into") or "").strip():
            into = str(args["into"])
            if typing == "app" or (typing == "focused" and not self._names_field(user_text, into, typing)):
                # it makes up a box ('Notatnik', the window title, 'Wiadomość') and then clicks that text
                log.info("(dropped the made-up text box %r: typing where the cursor is)", into)
                args = {k: v for k, v in args.items() if k != "into"}
        if not typed and name in ("click_on", "click", "move_mouse", "drag", "scroll", "look_at_screen") and (
                typing == "focused" or (typing == "app" and getattr(self, "_opened", False))):
            log.info("(blocked %s: this is a typing request)", name)
            return name, args, TYPE_BLOCKED.format(where=self._type_where())
        if not typed and typing == "focused" and name in ("open_app", "open_website", "press_keys") and not (
                name == "press_keys" and NEW_FILE_RE.search(user_text.split("\n")[0])):
            log.info("(blocked %s: this is a typing request)", name)
            return name, args, TYPE_BLOCKED.format(where=self._type_where())
        if name in ("click", "drag", "move_mouse") and not getattr(self, "_saw_screen", False):
            # x,y only mean something on a screenshot it has seen in this request; otherwise it's a random click
            log.info("(blocked a blind %s: no screenshot in this request)", name)
            return name, args, ("Not done: you haven't seen the screen in this request, so those coordinates are a "
                                "guess. Use click_on with the button's text, or look_at_screen grid=true first.")
        if name == "play_on_spotify" and NOT_SPOTIFY_RE.search(user_text) and not SPOTIFY_RE.search(user_text):
            log.info("(blocked play_on_spotify: the user asked for YouTube/video, not music)")
            return name, args, "Not done: the user asked for YouTube / a video, not Spotify. Don't play music."
        if (name == "open_youtube" and str(args.get("play", "true")).lower() in ("true", "1", "yes")
                and re.search(r"\b(jaki[śs]|jakie[śs]|losow\w*|cokolwiek|any|random|some)\b", user_text, re.I)):
            args = {**args, "play": "random"}  # "puść jakiś film z minecrafta": not always the same top video
        by = BY_YOUTUBER_RE.search(user_text.split("\n")[0])
        if name == "open_youtube" and by:
            # "film z fnaf od Elevena" searched in general: it means that YouTuber's own videos
            who = by.group(1)
            about = re.sub(rf"(?i)\b{re.escape(who[:max(4, len(who) - 2)])}\w*\b", "", str(args.get("query", "")))
            about = re.sub(r"(?i)\b(od|by|film\w*|odcin\w*|wideo|video|youtube|yt|gry|gra|z|ze|o)\b", " ", about)
            about = re.sub(r"\s+", " ", about).strip(" ,.")
            about = video_topic(user_text.split("\n")[0], who) or about
            log.info("(open_youtube with a YouTuber named: using their channel: %s / %s)", who, about)
            name, args = "open_youtube_channel", {"name": who, "about": about, "play": "random"}
        if name == "open_youtube_channel" and (args.get("about") or by):
            # the small brain shortens "teorię FNAF 3" to "FNAF": take the topic from the user's own words
            said = video_topic(user_text.split("\n")[0], str(args.get("name", "")))
            mine = str(args.get("about") or "")
            if said and len(said.split()) > len(mine.split()):
                log.info("(video topic from the user's words: %r instead of %r)", said, mine)
                args = {**args, "about": said}
            if args.get("about"):
                anyone = re.search(r"\b(jaki[śs]|jakie[śs]|losow\w*|cokolwiek|any|random|some)\b", user_text, re.I)
                from .tools.web import _tokens

                if str(args.get("play", "")).lower() in ("", "random", "true") or not args.get("play"):
                    specific = len(_tokens(str(args["about"]))) >= 2  # "teoria fnaf 3": that video, not any
                    args = {**args, "play": "best" if specific and not anyone else "random"}
        if name == "open_youtube_channel":
            # "drugi najnowszy film od X": the small brain drops the "drugi" (or the "najnowszy"); read it from the user
            from .tools.web import video_rank

            ranked = video_rank(user_text.split("\n")[0])
            if ranked:
                mode, nth = ranked
                if str(args.get("play", "")).lower() not in (mode,) or int(args.get("nth") or 1) != nth:
                    log.info("(video rank from the user's words: %s #%d)", mode, nth)
                args = {**args, "play": mode, "nth": nth}
                if args.get("about") and not video_topic(user_text.split("\n")[0], str(args.get("name", ""))):
                    args.pop("about")  # (the brain put "najnowszy" in about)
        if (name == "open_youtube_channel" and args.get("play") and not args.get("about") and not args.get("nth")
                and not WANTS_VIDEO_RE.search(user_text)):
            # "odpal kanał Blowka": just the channel, they didn't ask for a video
            args = {k: v for k, v in args.items() if k != "play"}
        if (name == "open_youtube" and not WANTS_VIDEO_RE.search(user_text)
                and not re.search(r"\b(o|about|z|ze|with)\s+\w+", user_text.split("youtub")[-1], re.I)):
            # just "otwórz YouTube": the site, not a video it made up a topic for
            return "open_website", {"site": "youtube"}, None
        if name == "start_timer" and args.get("then"):
            # an action for later only if the user asked for one (it copied "naciśnij spację" from the examples)
            then_words = [w for w in re.findall(r"\w{3,}", _fold_pl(str(args["then"])))]
            asked_words = set(re.findall(r"\w{3,}", _fold_pl(user_text)))
            if then_words and sum(any(a[:5] == w[:5] for a in asked_words) for w in then_words) < len(then_words) / 2:
                log.info("(dropped a timer action the user didn't ask for: %r)", args["then"])
                args = {k: v for k, v in args.items() if k != "then"}
        if name == "click_on":
            target = str(args.get("target") or args.get("text") or "")
            key = KEY_NAMES.get(re.sub(r"[^\w ]", "", target.lower()).strip())
            if key:  # "naciśnij spację" is a key, not a button on the screen
                log.info("(click_on %r -> press_keys %s)", target, key)
                return "press_keys", {"keys": key}, None
            from .tools import web  # "clicking" a web_search result that isn't on the screen -> open its page

            url = web.result_url(target)
            if url:
                log.info("(click_on a search result -> open_website %s)", url)
                return "open_website", {"site": url}, None
        return name, args, None

    def _question_only(self, text: str) -> str:
        """'Hej Pitagoras, kto wygrał mundial?' -> 'kto wygrał mundial?' (the search doesn't need my name)."""
        words = text.split()
        parts = self.wake_phrase.split()
        name, greetings = parts[-1], self.GREETINGS | set(parts[:-1])
        for i, w in enumerate(words[:4]):
            bare = re.sub(r"[^\w]", "", w.lower())
            if bare in self.aliases or (len(bare) >= 5 and difflib.SequenceMatcher(None, bare, name).ratio() >= 0.7):
                # only when everything before the name is a greeting ("Hej, Pitagoras, …"), not "co myślisz o Pitagorasie"
                if all(re.sub(r"[^\w]", "", x.lower()) in greetings for x in words[:i]):
                    return " ".join(words[i + 1:]).strip(" ,.!") or text
                break
        return text

    async def _fact_check(self, user_text: str) -> str | None:
        """Search the web for a question before answering it. Returns the note for the brain, or None."""
        if not (self.cfg.get("chat") or {}).get("fact_check", True):
            return None
        q = self._question_only(user_text)
        if not wants_fact_check(q):
            return None
        from .tools import web
        polish = self.phrase("pl", "en") == "pl"
        region = "pl-pl" if polish else "wt-wt"
        rec = wants_recommendation(q)
        query = rec_query(q, polish) if rec else q  # what people recommend now, not "poleć mi…" word for word
        self.hub.emit("tool", name="web_search", args={"query": query})
        results = []
        for attempt in range(2):  # the search engine hiccups now and then: one quick retry
            try:
                results = await asyncio.wait_for(asyncio.to_thread(web.search, query, 6 if rec else 5, region, True),
                                                 timeout=4)
            except Exception as e:  # noqa: BLE001 (offline, slow, blocked: just answer from memory)
                log.info("fact check search failed (%s): %s", attempt + 1, e)
            if results:
                break
        if not results:
            return None
        log.info("%s: searched %r (%d results)", "recommendation" if rec else "fact check", query, len(results))
        text = "\n".join(f"- {r.get('title', '')}: {r.get('body', '')}" for r in results)
        if rec:  # the results are "TOP 10 …" headlines: read the top two lists for the actual titles
            async def read(url: str) -> str:
                try:
                    page = await asyncio.wait_for(asyncio.to_thread(web.read_webpage, url), timeout=4)
                    return "" if page.startswith("Could not") else page[:1600]
                except Exception:  # noqa: BLE001
                    return ""
            pages = await asyncio.gather(*(read(r["href"]) for r in results[:2] if r.get("href")))
            text += "".join(f"\n\nFrom {results[i].get('title', '')}:\n{p}" for i, p in enumerate(pages) if p)
            return REC_NOTE.format(results=text[:5000])
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
