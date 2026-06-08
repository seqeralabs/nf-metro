# Live progress

!!! warning "Experimental"
    Live progress is experimental: the `%%metro process:` directive, the
    `nf-metro serve` / `nf-metro check-mapping` commands, and the event/overlay
    formats may change without notice.

nf-metro can light up a metro map in real time as a Nextflow pipeline runs.
Stock Nextflow `-with-weblog` posts task events to `nf-metro serve`, which draws
a status overlay on top of the static map - stations go pending → queued →
running → done (or failed) with a per-sample count. No Seqera Platform, no
plugin.

```
nextflow run --with-weblog ──HTTP──> nf-metro serve ──SSE──> browser overlay
   (task events)                   (map + process mapping)   (stations light up)
```

The layout is computed once and the overlay is drawn on top, so the map never
re-flows as state changes.

## 1. Map stations to processes

A metro station is a curated abstraction that usually stands for several
Nextflow processes (often a whole subworkflow), so the mapping is many-to-one.
Declare it with `%%metro process:` directives - a station id and a regex
matched against the **fully-qualified** process name:

```text
%%metro process: align | NFCORE_RNASEQ:RNASEQ:.*ALIGN.*
%%metro process: qc    | FASTQC
%%metro process: qc    | MULTIQC
```

- The whole field after `|` is one regular expression (no comma splitting, so
  quantifiers like `{1,3}` are safe). Repeat the directive to attach several
  patterns to one station.
- A bare name matches a scoped one, so `FASTQC` matches
  `NFCORE_RNASEQ:RNASEQ:FASTQC`.
- Only stations with a `process:` directive change state; everything else is
  drawn but stays neutral. Plumbing processes (versions dumps, samplesheet
  checks) are typically left unmapped on purpose.
- The mapping is **many-to-one**: a station may represent several processes,
  but a given process should light up **one** station. If a process matches the
  patterns of two stations its progress is duplicated on the map, so
  `check-mapping` reports that as a failure - keep each station's patterns
  specific enough not to overlap (lean on the scope prefix,
  `NFCORE_RNASEQ:RNASEQ:ALIGN:...`, when a tool recurs).
- The directive is pure metadata: it never affects the rendered map.

## 2. Serve the map

```bash
nf-metro serve path/to/map.mmd --port 8080
```

Then open <http://localhost:8080/> and start the pipeline with its weblog
pointed at the server:

```bash
nextflow run my/pipeline -with-weblog http://localhost:8080/events
```

Stations light up as tasks are submitted, run, and complete. A browser that
connects mid-run receives the current state immediately, so you never see a
blank map.

| Option | Meaning |
|--------|---------|
| `--port` | Port to listen on (default 8080). |
| `--host` | Interface to bind. Default `127.0.0.1` (local only); use `0.0.0.0` to accept connections from other hosts. |
| `--theme` | Theme name (`nfcore`, `light`). |
| `--token` | If set, `/events` POSTs must supply `?token=...` or an `X-Metro-Token` header. |

### Endpoints

| Path | Purpose |
|------|---------|
| `GET /` | The live page (static SVG + status overlay). |
| `GET /stream` | Server-sent events; the page subscribes to this. |
| `GET /state` | Current state as JSON (handy for scripting/debugging). |
| `POST /events` | Nextflow weblog receiver. |

## 3. Keep the mapping honest

The risk with any mapping is drift: a new process the map can't show (silently
invisible), a station pattern that matches nothing (stale), or a process whose
patterns match more than one station (duplicated progress). `check-mapping`
makes all three loud so CI can gate on them:

```bash
# Export the pipeline's process graph, then lint the map against it
nextflow run my/pipeline -with-dag dag.mmd -preview
nf-metro check-mapping path/to/map.mmd --dag dag.mmd
```

```text
Processes with no station (invisible): 1
  - BWA_MEM
Station patterns matching no process (stale): 1
  - align: NFCORE_RNASEQ:RNASEQ:OLD_ALIGNER
Processes matching more than one station (duplicates progress): 1
  - FASTQC: align, qc
```

It exits non-zero when it finds drift. Options:

| Option | Meaning |
|--------|---------|
| `--dag <file>` | A `nextflow -with-dag` Mermaid export; process names are read from its stadium nodes. |
| `--processes <file>` | A newline-delimited list of process names (e.g. captured from a run) - an authoritative alternative to `--dag`. |
| `--ignore <regex>` | Processes deliberately left unmapped (plumbing such as `.*:DUMPSOFTWAREVERSIONS`). Repeatable. |

Stations with no mapping at all are reported as a note (they never light up),
but are not treated as failures since they may be intentional.

## Deployment notes

- **Reachability.** For HPC/cloud runs the weblog POSTs come from wherever
  Nextflow executes, not your laptop. Run the server somewhere reachable from
  there (the head node), or tunnel: `ssh -L 8080:localhost:8080 headnode` and
  point the run at `http://localhost:8080/events`.
- **Security.** `/events` is unauthenticated by default and the server binds
  `127.0.0.1`. When binding a non-local interface (`--host 0.0.0.0`), set
  `--token` so only your run can post events. The token can be sent as the
  `X-Metro-Token` header or a `?token=` query param; prefer the header where you
  can, since a URL with the token lands in shell history and process listings.
- **Run lifecycle.** A `started` event resets the map, so re-running a pipeline
  re-animates a fresh map. The server tracks one run at a time. Unrecognised or
  malformed event payloads are accepted and ignored (the endpoint always returns
  200) so a Nextflow version emitting extra event types can't stall a run.
- **No denominator.** Nextflow's task count is dynamic, so the per-station
  count is "done / submitted so far", not a fixed percentage.

## Try it

The repository ships a self-contained demo under
[`examples/live/`](https://github.com/pinin4fjords/nf-metro/tree/main/examples/live):
a toy workflow whose processes only `sleep`, a mapped map, and a process list
for `check-mapping`. See its `README.md` to run it end to end.
