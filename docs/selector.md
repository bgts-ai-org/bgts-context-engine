# Selector models

The context selector is the one optional step after ranking: it asks a decision model which
of the K ranked files the task actually edits, hands the agent those in full, lists the
likely related ones as one line each and drops the rest. What it does to an answer, and how
the policy was fitted, is in [retrieval.md](retrieval.md#context-selection-optional). This
page is about choosing a model, running it and wiring it to the engine.

The selector is off by default. With `BCE_SELECTOR` unset (or `off`),
`get_context_for_task` returns the plain ranking at K=20, deterministic and byte-exact. Name
a model and the server ranks K=50 instead and lets that model cut the answer down.

## Supported models

| `BCE_SELECTOR` | Model | Where it runs | Cost |
| --- | --- | --- | --- |
| `jev` | [Jev 1.13](https://openrouter.ai/typesafe/jev-1.13) (`typesafe/jev-1.13`), TypeSafe's System One model | hosted: [OpenRouter](https://openrouter.ai/docs/guides/community/jev) or [TypeSafe's own API](https://www.typesafeai.org/guides/jev-api-quickstart) | per input token, ~$0.0007 per query |
| `decider-2b` | [Mapika/decider-2b](https://huggingface.co/Mapika/decider-2b) (v11, Apache-2.0, 3.8 GB bf16) | your GPU, behind `decider.serve` | GPU only |
| `decider-4b` | [Mapika/decider-4b](https://huggingface.co/Mapika/decider-4b) (v2.1, Apache-2.0, 8.4 GB bf16) | your GPU, behind `decider.serve` | GPU only |

All three speak the same wire format (`state` + typed `questions` in, one probability per
question out), so the engine sends the same two requests to each. The deciders are an open
reproduction of the System One model class; their code and training recipe are at
[github.com/Mapika/decider](https://github.com/Mapika/decider).

Measured end to end on the 12-repository benchmark (600 merged changes, 6 languages, the
same `voyage-code-4` K=50 candidates, the same policy, 1 500-token budget):

| | No selector (K=50) | `jev` | `decider-2b` | `decider-4b` |
| --- | --- | --- | --- | --- |
| File recall @50 | 94.4 | **93.3** | 89.8 | 92.1 |
| File recall in the first 20 symbols | 91.0 | **93.2** | 88.3 | 91.8 |
| First file right (of 600) | 450 | **495** | 388 | 451 |
| Tokens handed to the agent | 8 310 | **1 121** | 1 355 | 1 193 |
| Files listed (full + stub) | 20.5 | 10.8 (2.3 + 8.6) | 11.5 (5.1 + 6.6) | 9.9 (3.0 + 6.9) |
| Selector latency, mean | — | **0.59 s** | 1.66 s | 2.67 s |
| Requests that failed | — | 0 (1 partial) | 0 | 0 |

How to read it:

- **`jev`** is the reference. The thresholds (`full` at p ≥ 0.5, `stub` at p ≥ 0.05) were
  fitted on its probabilities, and it is the fastest because the work runs on TypeSafe's
  hardware. The task text and 240-character excerpts of the candidate symbols leave your
  perimeter.
- **`decider-4b`** is the closest open-weight replacement: 1.2 points of recall under Jev
  for nearly the same context size, nothing leaves the perimeter and there is no per-query
  bill. It needs a GPU with room for 8.4 GB of weights plus activations, and on one 32 GB GPU
  it adds ~2.7 s per query.
- **`decider-2b`** fits on a 16 GB GPU, but under Jev's thresholds it marks more files as
  edited (5.1 full files against 2.3) and loses 3.5 points. The thresholds have not been
  re-fitted for it yet.

The latencies above were measured from a workstation to a RunPod GPU with two queries in
flight; a decider next to the engine, or on a larger GPU, is faster.

## Jev over OpenRouter

No TypeSafe account is needed; usage is billed to the OpenRouter account.

```bash
BCE_SELECTOR=jev
OPENROUTER_API_KEY=sk-or-v1-...        # also read as BCE_OPENROUTER_API_KEY
# defaults, shown for reference:
# BCE_SELECTOR_MODEL=typesafe/jev-1.13
# BCE_SELECTOR_URL=https://openrouter.ai/api/alpha/decisions
# BCE_SELECTOR_TIMEOUT=3
```

Keep the model pinned to `typesafe/jev-1.13`: the thresholds were fitted on that release, so
`~typesafe/jev-latest` or a newer release is a re-fit, not a drop-in.

## Jev on TypeSafe's own API

With a TypeSafe account, call its System One endpoint directly. `BCE_SELECTOR_API_KEY` takes
precedence over the OpenRouter key:

```bash
BCE_SELECTOR=jev
BCE_SELECTOR_URL=https://api.typesafe.ai/v1/systemone
BCE_SELECTOR_API_KEY=<TYPESAFE_API_KEY>
BCE_SELECTOR_MODEL=jev-latest          # TypeSafe's model name; pin a release when you can
```

## decider-2b / decider-4b on your own GPU

The decider server is `decider.serve` from the [`decider-ai`](https://pypi.org/project/decider-ai/)
package: a FastAPI app with `POST /v1/systemone` in TypeSafe's format. It serves one model
per process, answers with whatever weights it loaded, and has **no authentication**.

### Hardware

| Model | Weights (bf16) | GPU that worked |
| --- | --- | --- |
| decider-2b | 3.8 GB | 16 GB or more (RTX 4090, A5000, L4, A4000) |
| decider-4b | 8.4 GB | 32 GB with the smaller batch settings below; larger GPUs run the defaults |

A CPU runs it too, but at minutes per request for a K=50 answer — not usable at query time.
Linux with CUDA is the tested path. The package pulls in `flash-linear-attention`, whose Triton
kernels do not run on Windows; the model still loads there, several times slower.

### Install and start

Python 3.11 or newer, on the GPU machine (a RunPod PyTorch 2.x / CUDA 12 template works):

```bash
pip install -U "decider-ai[serve]==1.8.0"
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"   # must print True

# Download one model. The revisions are the ones the benchmark above ran.
python -c "from huggingface_hub import snapshot_download; print(snapshot_download('Mapika/decider-4b', revision='eb5fbdfc9448473ec25e399882912863afbdb70e', local_dir='/workspace/decider-4b'))"
# or: snapshot_download('Mapika/decider-2b', revision='533964dae8be954c5b5e19fa4948e48408094c1e', local_dir='/workspace/decider-2b')

export DECIDER_MODEL=/workspace/decider-4b
export DECIDER_DEVICE=cuda
export DECIDER_MAX_REQUEST_TOKENS=8388608
# decider-4b on a 32 GB GPU only:
export DECIDER_MAX_BATCH=8
export DECIDER_GRAPH_TOKEN_BUDGET=8192

nohup python -m uvicorn decider.serve:app --host 0.0.0.0 --port 8000 > /workspace/decider.log 2>&1 &
tail -f /workspace/decider.log          # ready at "Application startup complete"
```

`DECIDER_MAX_REQUEST_TOKENS` is not optional for this workload. The symbol request asks about
50 symbols on a 4-level scale, which the server expands to 200 rows and counts against a
1 048 576-token cap per request (the state counts once per row on paper, although the server
encodes it once). A long state crosses the default and the server answers `413`; the engine
then tiers files without symbol pruning (`coverage.selector.status: "partial"`).

Use `decider-ai` 1.4.0 or newer: older versions ignore the per-type temperatures stored with
the v11 / v2.1 weights. To switch models, stop the server (`pkill -f "uvicorn decider.serve"`),
point `DECIDER_MODEL` at the other folder and start it again.

### Check the server

```bash
curl http://<host>:8000/health      # {"ok": true, "model": "/workspace/decider-4b", "device": "cuda", ...}
curl http://<host>:8000/v1/models   # {"models": [{"name": "decider-4b-v2.1", ...}]}
```

### Point the engine at it

```bash
BCE_SELECTOR=decider-4b              # or decider-2b - must match the weights the server loaded
BCE_SELECTOR_URL=http://<host>:8000/v1/systemone
# BCE_SELECTOR_TIMEOUT=10            # default for the deciders; p99 was ~5 s on one 32 GB GPU
```

No API key: the engine sends none to a decider unless `BCE_SELECTOR_API_KEY` is set, and it
never forwards an OpenRouter key there. The URL defaults to `http://127.0.0.1:8000/v1/systemone`,
so a server on the same machine needs only `BCE_SELECTOR`.

The server ignores the model name in the request. If `BCE_SELECTOR=decider-2b` but the server
loaded decider-4b, the answers come from decider-4b; the engine logs one warning
(`the server answered with 'decider-4b-v2.1'`) and records the name in
`coverage.selector.served_model`.

### RunPod

- Create a pod from a PyTorch 2.x / CUDA 12 template, GPU as in the table above, ~30 GB
  container disk and ~20 GB volume (`/workspace` survives restarts).
- Expose the server port: **Edit Pod → Expose TCP Ports**, add `8000`. **Connect → TCP Port
  Mappings** then shows `<public ip>:<external port>` for it, and that pair goes into
  `BCE_SELECTOR_URL=http://<public ip>:<external port>/v1/systemone`. The HTTP proxy
  (`https://<pod-id>-8000.proxy.runpod.net`) works too, at an extra hop.
- Run the install block above in the pod's web terminal or over SSH.
- The external port changes when the pod is recreated; `curl .../health` from the engine
  machine after every restart.

### Security

`decider.serve` has no authentication and accepts any caller that can reach the port. On
RunPod the TCP mapping is public. Restrict it to the engine's address (firewall, VPN, an SSH
tunnel), or put an authenticating reverse proxy in front and give the engine its token in
`BCE_SELECTOR_API_KEY`. The requests carry the task text and excerpts of your source code.

## Verify

The CLI and the MCP server use the same configuration, so check it once from the shell:

```bash
bce --env-file /path/to/engine/.env context --task "fix the login timeout in the meeting webhook"
```

The answer carries `coverage.selector`:

```json
"selector": {
  "method": "decider-4b", "model": "decider-4b", "served_model": "decider-4b-v2.1",
  "status": "ok", "files_in": 13, "files_full": 6, "files_stub": 5, "files_dropped": 2,
  "ms": 2591.6, "input_tokens": 17908
}
```

and the items carry `tier` (`full` / `stub`). With the selector off there is no
`coverage.selector` block and no `tier`. `bce context --no-select` skips the selector for one
call.

`bce serve` prints the active selector at startup (`Selector:  decider-4b (model decider-4b at
http://…/v1/systemone, timeout 10 s)`), and `bce serve-mcp` logs the same line to stderr
(`context selector: …`), which editors show in the MCP server log. After changing
`BCE_SELECTOR`, restart the editor's MCP server.

## Troubleshooting

| What you see | Cause |
| --- | --- |
| no `coverage.selector`, log says `needs an API key` | `BCE_SELECTOR=jev` without `OPENROUTER_API_KEY` / `BCE_SELECTOR_API_KEY` |
| no `coverage.selector`, log says `unknown BCE_SELECTOR` | a value other than `off`, `jev`, `decider-2b`, `decider-4b` |
| `status: "error"`, `request failed: … refused` / `timed out` | the decider server is down, the port mapping changed, or `BCE_SELECTOR_TIMEOUT` is too short. The answer is the plain ranking |
| `status: "partial"`, `HTTP 413: too many tokens` | the server runs without `DECIDER_MAX_REQUEST_TOKENS=8388608` |
| `status: "error"`, `HTTP 503: server busy` | more queued rows than `DECIDER_MAX_QUEUE_ROWS`; raise it or add capacity |
| `served_model` differs from `BCE_SELECTOR` | the server loaded another model; check `curl …/health` |

The selector is fail-open in every case: an error never fails the tool call, it returns the
ranked candidates unselected and says so in `coverage.selector`.

## Variables

| Variable | Default | |
| --- | --- | --- |
| `BCE_SELECTOR` | `off` | `off`, `jev`, `decider-2b` or `decider-4b` |
| `OPENROUTER_API_KEY` | empty | Jev's key on OpenRouter; also read as `BCE_OPENROUTER_API_KEY` |
| `BCE_SELECTOR_API_KEY` | empty | bearer token for the selector endpoint: TypeSafe's key, or a proxy's in front of a decider. Wins over the OpenRouter key |
| `BCE_SELECTOR_MODEL` | `typesafe/jev-1.13` / `decider-2b` / `decider-4b` | sent as `model`; a decider server ignores it |
| `BCE_SELECTOR_URL` | `https://openrouter.ai/api/alpha/decisions` (jev), `http://127.0.0.1:8000/v1/systemone` (deciders) | |
| `BCE_SELECTOR_TIMEOUT` | `3` (jev), `10` (deciders) | seconds per request; also the worst-case latency added |
| `BCE_SELECTOR_FULL_THRESHOLD` | `0.5` | p(file is edited) at or above which a file keeps its symbols |
| `BCE_SELECTOR_STUB_THRESHOLD` | `0.05` | at or above which a file is listed as one stub line |
| `BCE_SELECTOR_MAX_FILES` | `12` | files listed in total (full + stub) |
| `BCE_SELECTOR_SYMBOL_MIN_SCORE` | `1.0` | symbols of a full file scored below this (0 unrelated … 3 must change) are dropped |

The four thresholds apply to every model. They were fitted on Jev; re-fitting them for a
decider is an offline replay with `local_bench/multi_repo_bench/select_bench.py` over cached
K=50 answers.
