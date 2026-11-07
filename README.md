# Pitagoras

A desktop AI companion: a little yellow character that floats on top of your windows. It listens and talks back, sees your screen, plays music in Spotify, searches and opens the web, and dances to whatever you're playing. You choose its brain: any GGUF, LM Studio, Ollama, Claude Code, or your own server.

```
mic ─► ears (Whisper / Windows speech) ─► Agent ◄──► brain (local GGUF · LM Studio · Ollama · Claude Code · custom)
                                            │  ▲
screen ─► eyes (screenshots) ───────────────┤  │ tool calls: spotify · web · media · apps · look · emote · move
                                            ▼  │
speaker ◄─ voice (Kokoro/Piper/Windows) ─ mouth levels ─┐
system audio ─► beat/bass/bpm ──────────────────────────┼──► ws://127.0.0.1:8765 ◄──► character (overlay.py or your own)
Windows media session ─► now playing ───────────────────┘
```

## Install

**Download `Pitagoras-Setup-x.y.z.exe` from [Releases](../../releases/latest) and run it.**

- Pick any folder (default `C:\Pitagoras`; Browse… → Make New Folder). **Everything goes into that folder**: its
  own portable Python, the packages, models, caches, memory and settings. Nothing is installed on the system or
  put in AppData. No admin is needed.
- Pick a brain to download right in the installer (with a progress bar): **Gemma 4 E4B by Google** (official) or
  **Gemma 4 E4B Uncensored by HauhauCS**, or none (use LM Studio / Ollama / Claude later). Also llama.cpp to run it,
  and a Polish offline voice.
- After copying, a setup window sets up Python and the packages. This is about 8 GB with CUDA PyTorch, so give it
  a while.
- Start it from the desktop / Start menu shortcut (both optional).
- Uninstalling removes the program; it asks before deleting your brains, voices, memory and settings.

**Needs:** Windows 10/11 x64, ~15 GB free, and an NVIDIA GPU (RTX, 8 GB+ VRAM recommended) for local brains, Whisper ears and voice
models. Without one, use a brain from LM Studio / Ollama / Claude and expect slower ears.

Windows SmartScreen may warn about the installer because it isn't code-signed: **More info → Run anyway**.

### From source

```powershell
git clone <this repo> ; cd pitagoras
.\setup.ps1 -WithLlamaCpp -Brain google   # portable Python 3.12 + venv, packages, Kokoro voice, llama.cpp (CUDA),
                                          # Gemma 4 E4B (or -Brain uncensored)
.\launcher\build.ps1                   # builds Pitagoras.exe
.\Pitagoras.exe                        # or: .\.venv\Scripts\python.exe run.py
```

`.\installer\build.ps1 -Version 1.0.0` builds the installer into `dist\` (needs Inno Setup 6.4+). Add
`-Personality my_personality.txt` to make that personality the default (builds `...-personal.exe`, which
`publish.ps1` never uploads).

## Using it

The character appears in the bottom-right corner.

**Pitagoras.exe** starts it with no console window. It writes its output to
`logs\pitagoras.log` (the run before is in `pitagoras.previous.log`), shows the error if Pitagoras fails to
start, and won't start a second copy. Quit from the character's right-click menu. To rebuild the exe (e.g.
after changing your colors, for the icon): `.\launcher\build.ps1`.

- **Left-click** it to open a text box and type to it.
- **Drag** it to move it. It remembers where you put it.
- **Right-click** it for the menu:

| Menu | What it does |
|---|---|
| 💬 Chat / 👀 Look at my screen / ✋ Stop talking | quick actions |
| 📺 Watch along with me | It watches the video, stream or game with you and reacts now and then. It uses a screenshot, the window title and what was *said* (PC audio → Whisper). You can also just say "watch this with me". |
| 🧠 Brain | Pick a brain: **Local** (the `brains\` folder), **LM Studio**, **Ollama**, **Claude Code** (Haiku/Sonnet/Opus) or **Custom server…**. 👁 marks brains that can see the screen. |
| 👂 Ears | Whisper (local GPU, tiny → large-v3-turbo) · Windows speech recognition · Off. Also choose the microphone. |
| 👁 Eyes | Off · Only when I ask · Comment every ~2/5/15 min (it glances at your screen by itself and only speaks if something is worth saying) |
| 🔊 Voice | Mute (speech bubble only), Kokoro voices, Windows voices |
| Wake word | Only answer when it hears "Pitagoras". After a spoken reply, up to 3 follow-ups work without the name. |
| ⚙ Settings › 🌍 Language | Auto · Polski · English. Replies are always in that language, and the voice switches to one that speaks it (keeping its gender). |
| ⚙ Settings › 👂 I speak | Which language the ears listen for: Polish + English (auto), Polski or English |
| ⚙ Settings › 📏 Reply length | Short (one sentence) · Medium (1–3 sentences) · Long (fuller answers) |
| ⚙ Settings › 💭 Chatter | How often it comments while watching along: Quiet (~2 min, notable moments only) · Normal (~45 s) · Chatty (~20 s). It stays silent when there's nothing worth saying. |
| ⚙ Settings › 🎨 Colors | Color pickers for the body, mouth, outline and listening ring, or edit `appearance.json` (`"body": [255, 210, 63]`, `"#ff4d8d"` or `"rgb(80, 220, 120)"`). Changes show up immediately. |
| 🎭 Personality | **Edit personality…** opens `personality.txt` in Notepad. Save it and the next reply uses it. **Reset to default** restores the original. |
| 📒 Memory | The chat is saved to `memory\conversation.json` and read again when it starts, so it remembers past sessions. Facts you ask it to remember go to `memory\notes.txt` (**Edit memory notes…**). **Forget this conversation** keeps the notes; **Clear all memory…** deletes both. |

**🎙 Voice model** (top-level menu): your RVC voice models from `voices\` (a folder with `.pth` + `.index`, or a loose `.pth`); **Rescan** after adding one. **Voice › Speed / Pitch / Volume** change the base voice *before* the voice model (volume is applied last).

**Controlling the PC:** ask it to click, type or play something ("Hej Pitagoras, zagraj w FNAF"). It screenshots the active window with a coordinate grid, then clicks / presses keys / types, seeing the screen after every action (up to `control.max_steps` actions). **Move the mouse yourself to take over: it stops immediately.** Turn it off in Settings › "Let Pitagoras use mouse & keyboard". Aiming needs a good vision brain: Claude (Sonnet) is far more accurate than small local models.

**Timers & cooldowns:** "Hej Pitagoras, Xayah flashneła, trackuj" → it looks up the cooldown online, starts the timer from the moment you asked (search time is subtracted), shows it under the character and says "Flash jest gotowy na Xayah!" when it's up. Reminders work the same ("przypomnij mi za 10 minut…"); "anuluj timer" cancels.

**⚙ Settings › 🙋 Preferences:** change the wake phrase (e.g. "hej madzia": it then also answers to that name) and set your name so it calls you by it.

**Opening things:** "open steam and turn on deadlock", "launch skyrim" or "open photoshop" work because it scans the Start menu and your Steam libraries (Steam games start Steam on their own). If something isn't installed ("open netflix"), it opens the website instead.

Menu choices are saved to `settings.yaml`, which overrides `config.yaml`. Delete `settings.yaml` to reset them.

Other ways to run it:

```powershell
.\.venv\Scripts\python.exe run.py --no-overlay   # backend only (e.g. while you build your own character)
.\.venv\Scripts\python.exe run.py --no-voice     # no mic, no speech
.\.venv\Scripts\python.exe -m pitagoras.overlay  # character only, connects to a running backend
```

## Brains folder

```
brains\
  Gemma-4-E4B\
    Gemma-4-E4B-...-Q4_K_M.gguf   ← the model (the largest .gguf without "mmproj" in its name)
    mmproj-...-f16.gguf           ← optional vision projector: lets it see the screen
    brain.yaml                    ← optional: context: 16384, gpu_layers: 999, extra_args: ["--flash-attn", "on"]
```

Each folder appears under Brain › Local. Choose **Rescan** after adding one.

To reuse a model from LM Studio without copying it, make a hard link. This only works on the same drive:

```powershell
New-Item -ItemType HardLink -Path brains\MyModel\model.gguf -Target D:\lmstudiomodels\...\model.gguf
```

**Claude Code** uses your Claude login (`claude` must be on PATH). It runs with its own tools turned off and uses Pitagoras' tools instead. Every reply starts a new CLI process, so expect 2–4 s of latency.

**Windows speech ears** need Settings › Privacy & security › Speech › **Online speech recognition** turned on.

## Character protocol (WebSocket `ws://127.0.0.1:8765`, JSON)

`pitagoras/overlay.py` is just one client of this protocol. Your own character app can replace it by speaking the same protocol.

### Pitagoras → character

| type | fields | meaning |
|---|---|---|
| `hello` | `name`, `tools[]` | sent on connect |
| `state` | `state`: `loading` / `idle` / `thinking` / `working` / `speaking` | what it's doing |
| `mouth` | `level` 0..1 | lip-sync, ~33×/s while speaking |
| `mic` | `level` 0..1 | your mic loudness |
| `user_speaking` | `active` | you started or stopped talking |
| `user_text` | `text` | what it heard or what you typed |
| `assistant_start` / `assistant_text` / `assistant_end` | `delta` | the reply, streamed (for speech bubbles) |
| `tool` | `name`, `args` | it's using a tool |
| `emotion` | `emotion` | happy, sad, excited, angry, surprised, thinking, love, sleepy, dance, neutral |
| `move` | `position` | left, right, center, top, bottom, top-left, …, follow_mouse |
| `music` | `level`, `bass`, `treble`, `beat`, `bpm` | system audio analysis, ~20×/s |
| `now_playing` | `title`, `artist`, `album`, `status`, `app` | when the song or play state changes |
| `toast` | `text` | short status message ("Brain ready: …") |
| `settings` | `current{}`, `options{}` | current choices and everything selectable (to build a menu) |
| `error` | `message` | something broke |

### Character → Pitagoras

| type | fields | meaning |
|---|---|---|
| `say` | `text` | message from the user |
| `interrupt` | | stop talking |
| `speak` | `text` | say this exact text (no AI) |
| `look` | `prompt?` | look at the screen and comment |
| `get_settings` | | ask for a `settings` message |
| `set` | `key`, `value` (`data` for a custom brain) | keys: `brain`, `ears`, `mic`, `eyes`, `voice`, `muted`, `wake_word`, `length`, `chatter`, `watch`, `memory` |
| `where` | `x`, `y` | the character's screen position (eyes capture that monitor) |
| `clear_memory` | `all?` | forget the conversation (`all: true` also deletes saved notes) |
| `edit_personality` / `reset_personality` / `edit_notes` / `open_memory_folder` | | open the files in Notepad / Explorer |
| `open_brains_folder` / `quit` | | |

## Adding a tool

```python
# pitagoras/tools/lights.py  (and import it in tools/__init__.py load_all)
from . import tool

@tool("Turn the room lights on or off.", on="true or false")
def lights(on: bool) -> str:
    ...
    return "lights on"
```

`instant=True` makes a tool fire-and-forget: it runs while the model keeps talking, like emote and move. A tool can also return `{"text": ..., "image": <base64 jpeg>}` to show the brain a picture.

Tool calls are plain text, so they work with any model. The parser also accepts the shortcuts small models tend to write, such as `emote({...})` or bare JSON.
