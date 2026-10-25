# Local LLM benchmarks — Radeon RX 6700 XT (12 GB)

Measured October 2026 on this machine. Every number below comes from a run on this
hardware, not from a spec sheet. Prompt for throughput tests: a ~6,400-token text,
prefix caching disabled, 192–256 tokens generated with `ignore_eos`, 3 repetitions.
Run-to-run deviation was below 1%.

## Test system

| Component | Value |
|---|---|
| GPU | AMD Radeon RX 6700 XT, 12 GB (11.98 GB usable), RDNA2 Navi 22, ~384 GB/s |
| Driver | RADV (Mesa), Vulkan 1.4 |
| CPU | AMD Ryzen 7 5700X, 8 cores / 16 threads |
| RAM | 32 GB + 8 GB swap |
| OS | Nobara / Fedora 44, kernel 7.2.9 |
| Runtime | LM Studio's llama.cpp Vulkan build 2.53.0 |

`VRAM total` below is the whole card's usage, including whatever the desktop held at
the time. That baseline ranged from 0.3 GB (bare session) to 1.5 GB (browser plus
compositor), so subtract roughly 1–1.5 GB to get your usable headroom. On this card
**anything above ~11.5 GB total puts you in the cliff described below.**

## Installed models

| LM Studio key | Quant | Weights | + vision projector | Total |
|---|---|---|---|---|
| `qwen/qwen3.5-9b` | Q6_K | 6.85 GB | 0.86 GB | 8.28 GB |
| `google/gemma-4-12b-qat` | QAT Q4_0 | 6.50 GB | 0.16 GB | 7.15 GB |
| `google/gemma-4-26b-a4b-qat` | QAT Q4_0 | 13.45 GB | 1.11 GB | 15.63 GB |

Total on disk: ~31 GB. Install with `lms get <key>`, e.g.
`lms get google/gemma-4-26b-a4b-qat`.

## Benchmark: tuned llama.cpp server

Direct invocation of LM Studio's Vulkan runtime, full layer offload, Flash Attention
on, `--load-mode none`. The 26B-A4B rows vary `--n-cpu-moe` (how many layers keep
their MoE experts in RAM).

| Model | Context / KV cache | VRAM total | Prefill | Generation |
|---|---|---|---|---|
| Qwen3.5-9B Q6_K | 32k / f16 | 8.75 GB | 652 tok/s | **47.5 tok/s** |
| Qwen3.5-9B Q6_K | 32k / f16, FA off | 9.80 GB | 650 tok/s | 46.9 tok/s |
| Qwen3.5-9B Q6_K | **128k** / q8_0 | 9.99 GB | 655 tok/s | 47.2 tok/s |
| Gemma 4 12B QAT Q4_0 | 32k / f16 | 9.92 GB | 485 tok/s | 39.9 tok/s |
| Gemma 4 12B QAT Q4_0 | 64k / q8_0 | 9.31 GB | 494 tok/s | 39.4 tok/s |
| Gemma 4 26B-A4B QAT, `-ncmoe 15` | 32k / q8_0 | 9.06 GB | 803 tok/s | 34.5 tok/s |
| Gemma 4 26B-A4B QAT, `-ncmoe 13` | 32k / q8_0 | 9.86 GB | 840 tok/s | **36.6 tok/s** |
| Gemma 4 26B-A4B QAT, `-ncmoe 11` | 32k / q8_0 | 10.66 GB | 873 tok/s | 39.1 tok/s |
| Gemma 4 26B-A4B QAT, `-ncmoe 9` | 32k / q8_0 | 11.46 GB | 906 tok/s | 42.2 tok/s |

The 26B rows were measured in an otherwise idle session (0.3 GB desktop). With a
normal desktop of ~1.4 GB add about 1.0 GB to those figures — verified: the same
`-ncmoe` values measured 10.09 GB (`15`) and 10.89 GB (`13`) with a heavier desktop.

## The VRAM cliff at ~11.5 GB

Once total card usage exceeds roughly 11.5 GB, the driver moves compute buffers into
shared memory. Generation speed holds up, but **prompt processing collapses**:

| Config | VRAM total | Prefill | Time to prefill 6.4k tokens |
|---|---|---|---|
| `-ncmoe 9` | 11.46 GB | 906 tok/s | 7.1 s |
| `-ncmoe 7` | 11.76 GB | 180 tok/s | 35.5 s ⚠️ |
| `-ncmoe 5` | 11.76 GB | 182 tok/s | 35.5 s ⚠️ |

That is a **5× slowdown on prompt processing** for a 0.3 GB saving. This is also why
LM Studio's offload-ratio slider is a trap for this model: it pushes usage to
11.8–11.9 GB and lands in precisely this regime.

## Benchmark: LM Studio / Bionic in-app path

Same model (Gemma 4 26B-A4B QAT), loaded through the app, measured over LM Studio's
own API. LM Studio passes `--fit on --fit-ctx 16384` and no explicit expert offload.

| How it was loaded | Context | Generation | Prefill | VRAM total |
|---|---|---|---|---|
| `lms load --auto` | 16k (auto-fit) | **33.5 tok/s** | ~700 tok/s | 10.7–11.1 GB |
| `lms load -c 16384` (context pinned) | 16k | 21.9 tok/s | ~740 tok/s | 11.41 GB |
| `lms load -c 32768` (context pinned) | 32k | 21.6 tok/s | ~743 tok/s | 11.70 GB |
| `lms load --gpu 0.8 -c 32768` | 32k | 22.7 tok/s | ~155 tok/s ⚠️ | 11.87 GB |
| `lms load --gpu 0.75 -c 32768` | 32k | 28.8 tok/s | ~160 tok/s ⚠️ | 11.80 GB |
| Flatpak app, JIT load on request | 8k (app default) | 31.9 tok/s | ~700 tok/s | 11.46 GB |
| tuned server, `-ncmoe 13` | 32k | 36.6 tok/s | 840 tok/s | 9.86 GB |

The VRAM column mixes baselines: the app rows were measured with a ~1.0–1.4 GB
desktop, the tuned-server row in an idle session (~0.3 GB). Add ~1 GB to the last
row for a fair comparison.

The Flatpak row is the 26B pulled in on demand by a request to the app's server on
8080 (`"model": "gemma-4-26b-a4b-it-qat"`): 256 tokens generated, three runs
(31.9 tok/s median), prefill measured with a 5,630-token prompt. It lands at
11.46 GB — just under the cliff — and prompt processing stays healthy there (~8.0 s
for 5,630 tokens).

### Head-to-head for the 26B

| Path | Generation | Prefill | 6.4k-token prefill |
|---|---|---|---|
| Bionic, automatic placement | 33.5 tok/s | ~700 tok/s | 9.3 s |
| Tuned server (`start-gemma-26b.sh`) | 36.6 tok/s | 840 tok/s | 7.7 s |

The tuned server is ~9% faster at generation and ~20% faster at prefill, and it is
the only way to get a full 32k context at that speed. The in-app automatic path
needs no script and reaches 92% of it.

## Key findings

1. **MoE expert placement is the biggest lever on this card.** For the 26B, moving
   experts between VRAM and RAM takes the same model from 21.7 to 36.6 tok/s — a 69%
   difference with no quality change. It is worth tuning, not a detail.
2. **Bionic's automatic placement is good; pinning the context is the trap.**
   Automatic: 33.5 tok/s. Pinned 16k *or* 32k: 21.7 tok/s. If you want maximum
   in-app speed, do not set a fixed context length.
3. **The GPU-offload slider makes this model worse, not better** (28.8 tok/s with a
   40-second prefill). Prefer `Auto`, or the tuned server.
4. **12 GB is enough for three different shapes:** a 9B dense model at 128k context,
   a 12B dense model at 64k, or a 26B MoE with partial expert offload at 32k.
5. **Flash Attention buys no speed on RDNA2 via Vulkan** here (46.9 tok/s off vs
   47.5 tok/s on — within noise), but it did cost ~1 GB more VRAM when disabled
   (9.80 vs 8.75 GB at 32k), so leaving it on is the better default.
6. **Prompt processing, not generation, is the weak spot.** ~650–900 tok/s prefill
   means a 16k-token agent context costs 20+ seconds before the first output token.
7. **Gemma 4 thinking eats `max_tokens`.** Even "What is 17×23?" consumes ~250
   reasoning tokens, so `max_tokens` below ~512 returns an empty `content` field and
   the whole output ends up in `reasoning_content`. Use `--think off` for short answers.

## Which model for what

| Use case | Model | Why |
|---|---|---|
| Long documents, agents, many languages | Qwen3.5-9B Q6_K | 47.5 tok/s at a full 128k context, fastest to prefill per token |
| Coding | Gemma 4 12B QAT | best published coding scores of the models compared here, 64k context |
| Best answer quality | Gemma 4 26B-A4B QAT | ~31B-class quality via MoE, best prefill (840 tok/s) |
| Fast, cheap, no tuning | any, loaded with automatic placement | 33–47 tok/s without touching a single setting |

## Running the tuned server

The tuned server defaults to port **8081**. LM Studio's own local server owns
**8080** (see [LM Studio's own server](#lm-studios-own-server-8080) below), so the two
can run side by side.

```bash
./start-gemma-26b.sh                  # -ncmoe 13, 32k context, port 8081
./start-gemma-26b.sh --ncmoe 15       # safer, ~1 GB more headroom
./start-gemma-26b.sh --think off      # short, fast answers
./start-gemma-26b.sh --dry-run        # print the command only
```

The script auto-detects the newest LM Studio Vulkan runtime, checks model and VRAM,
and warns when `--ncmoe` enters the cliff zone. Server is OpenAI-compatible at
`http://127.0.0.1:8081/v1`. Stop with Ctrl+C. See `--help` for all options.

Raw equivalent:

```bash
llama-server -m ~/.lmstudio/models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/gemma-4-26B-A4B-it-QAT-Q4_0.gguf \
  -ngl 99 -ncmoe 13 -c 32768 -fa on -ctk q8_0 -ctv q8_0 \
  -b 2048 -ub 512 -t 8 --load-mode none --port 8081
```

## Talking to the server

LM Studio's runtime ships `llama-server` **without** the usual bundled web UI — the
root path returns `404`, so there is no browser interface. Three ways in:

**1. The terminal client** ([chat-gemma.py](chat-gemma.py)) — streaming, shows
reasoning dimmed, reports tokens per second:

```bash
./chat-gemma.py                      # interactive
./chat-gemma.py -p "Explain MoE"     # one answer, then exit
./chat-gemma.py --think off          # start with reasoning disabled
./chat-gemma.py --url http://127.0.0.1:8080 --model gemma-4-26b-a4b-it-qat
```

In the session: `/think on|off`, `/system TEXT`, `/model NAME`, `/reset`,
`/save [FILE]`, `/help`, `/exit`. Thinking can be toggled per message via
`chat_template_kwargs`, so no server restart is needed.

Input behaves like a normal terminal program: GNU readline provides arrows,
Home/End, Ctrl+A/E/K/U/W and Tab completion, and the history is kept in
`~/.local/state/chat-gemma/history`. A line ending in `\` continues the message on
the next line (terminals that support bracketed paste also take a pasted block in
one go), Ctrl+C clears the line or stops a running answer, Ctrl+D quits, and a
server that is down reports the error instead of ending the session. Colours are
dropped when stdout is not a terminal or `NO_COLOR` is set.

The client gives the model **file access with permissions**, and it always asks first.
The Gemma 4 template supports native tool calls (`supports_tools: true` in
llama.cpp's `/props`), so the model can call `list_folder`, `read_file`, `search_files`
(read-only tools) and `write_file` (explicit write access); the client refuses every
path you have not granted and shows the exact path when it asks:

```
the model wants to read ~/notes
  tool: list_folder | not granted yet | nothing is read before you answer
  allow this folder? [o]nce / [f]folder for this session / [n]o >
```

**Read-only tools:** `list_folder` (with optional glob filter), `read_file` (with line range), and
`search_files` (recursive file search with snippets).

**Write access:** The model can create files via `write_file`, but only in folders where you
explicitly granted write access: `/allow-write PATH` or `--allow-write PATH`. A read grant
ever implies write permission — the two are separate and both require your consent.

**Commands:** `/allow PATH` (read), `/allow-write PATH` (write-only, e.g. ~/logs),
`/grants` (show read + write roots), `/revoke PATH`, `/tools on|off`.

**Persisted grants:** Grants are saved to `~/.config/chat-gemma/grants.json` across sessions.
Delete the file to clear them, or use `/allow-write` for temporary session-only access.

Paths are resolved with `realpath`, so symlinks pointing out of granted folders are not
covered. Without a terminal to ask on (piped stdin, `-p`) nothing is granted implicitly:
ungranted paths are denied and the model is told so.

**2. curl / any OpenAI-compatible client** pointed at `http://127.0.0.1:8081/v1`:

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"Hello"}],
       "chat_template_kwargs":{"enable_thinking":false}}'
```

**3. Python**, roughly what the client does:

```python
import json, urllib.request
body = {"model": "local", "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 512}
r = urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8081/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}))
print(json.load(r)["choices"][0]["message"]["content"])
```

Set `"max_tokens": -1` for unlimited output. With thinking enabled, remember the
reasoning consumes budget (see finding 7).

Note that `lms chat` cannot target this server — it has no `--host`/`--base-url`
option and always talks to LM Studio's own local server (see below).

### LM Studio's own server: 8080

The desktop app's OpenAI-compatible server is kept on `http://127.0.0.1:8080`, one port
away from the tuned server, so both can be up at the same time. Two LM Studio builds
are installed here, each with its own settings and its own server: the Flatpak build is
the one serving 8080, while the version bundled with Bionic — the one whose runtimes
and models the scripts use — stays on its default 1234 and is normally stopped.

Move the port in **Settings → Developer → Server Port** (leave the network interface on
localhost), or from the shell for the Bionic build:

```bash
~/.lmstudio/bin/lms server start --port 8080
```

Both persist the port into that installation's application data
(`.internal/http-server-config.json`), which is protected: change it through the app or
its own CLI, not by editing files.

The Flatpak build is sandboxed and cannot see `~/.lmstudio`, so its model library is
pointed at the shared GGUF store with a one-off override:

```bash
flatpak override --user --filesystem=$HOME/.lmstudio/models ai.lmstudio.lm-studio
```

The three GGUFs then appear in the app (and under both `/api/v0/models` and
`/v1/models`) as `gemma-4-26b-a4b-it-qat`, `gemma-4-12b-it-qat` and `qwen3.5-9b` — use
those keys as the `model` field, not `local`. Both servers share the single 12 GB card,
so only one of them can hold a large model at a time — the 26B in the app already
reaches 11.46 GB with an idle desktop, right at the edge.

The app's engine accepts but ignores `chat_template_kwargs`, so `/think off` in
`chat-gemma.py` only takes effect against the tuned server.

### What does not work

- `lms load --engine-config-file` → *"Engine configuration files and CWD are
  currently supported only by vLLM."* Not available for the llama.cpp engine.
- `POST /api/v1/models/load` → rejects extra keys (`Unrecognized key(s) in object:
  'gpu'`). The `gpu.numCpuExpertLayersRatio` field exists in LM Studio's load-config
  schema, but only the GUI exposes it.

So for the llama.cpp engine there are exactly two ways to tune expert placement:
the **"Number of CPU Expert Layers" slider** in the load dialog (MoE models only,
set it to ~13), or a **manually launched server** as above.

## Caveats

- Single machine, single GPU, one afternoon. Numbers are reproducible here, not
  universal.
- The 12 GB card was shared with the desktop; a heavier desktop shifts every VRAM
  figure up and can push you into the cliff.
- Vision (the `mmproj` files) costs a further ~0.2–1.1 GB when images are actually
  passed in.
- Quality differences between the three models were taken from published benchmarks,
  not measured here — this document is about speed and memory only.
