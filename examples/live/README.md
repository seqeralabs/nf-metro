# Live progress demo

Light up a metro map in real time as a Nextflow pipeline runs - no Seqera
Platform, no plugin. Stock Nextflow `-with-weblog` posts task events to
`nf-metro serve`, which draws a status overlay on top of the static map:
stations go pending -> queued -> running -> done (or failed) with a per-sample
count.

```
nextflow run --with-weblog ──HTTP──> nf-metro serve ──SSE──> browser overlay
   (task events)                   (map + process mapping)   (stations light up)
```

## Files

| File | Role |
|------|------|
| `pipeline.mmd` | The metro map. `%%metro process:` directives tie each station to a Nextflow process name. |
| `workflow/main.nf` | A toy RNA-seq-shaped workflow whose processes only `sleep` over 4 samples. Three branches (QC / alignment / quantification) reconverging at MultiQC. |
| `workflow/nextflow.config` | Local executor, throttled so RUNNING states are visible. |
| `processes.txt` | The process names this map should cover, for `check-mapping`. |

## Run it

From the repo root (nf-metro and Nextflow both on PATH):

```bash
nf-metro serve examples/live/pipeline.mmd --open --shutdown-after-complete -- \
    nextflow run examples/live/workflow/main.nf \
              -c examples/live/workflow/nextflow.config
```

`serve` wires `-with-weblog` automatically, opens your browser, and shuts the
server down when the pipeline finishes.

Watch the map: Trim Galore fills first, the three lines fan out and run in
parallel, then MultiQC lights up as everything converges.

### Two-shell alternative

If you want to keep the server alive across multiple re-runs:

```bash
# shell 1 - the live server
nf-metro serve examples/live/pipeline.mmd --port 8080
# open http://localhost:8080/

# shell 2 - the pipeline (no Docker; processes only sleep)
nextflow run examples/live/workflow/main.nf \
  -c examples/live/workflow/nextflow.config \
  -with-weblog http://localhost:8080/events
```

## Check the mapping stays honest

```bash
nf-metro check-mapping examples/live/pipeline.mmd --processes examples/live/processes.txt
# Mapping OK: 8/8 processes map to a station.
```

`processes.txt` is the committed list of process names this map should cover.
On a real pipeline you would instead lint against a fresh process graph:

```bash
nextflow run my/pipeline -with-dag dag.mmd -preview
nf-metro check-mapping examples/live/pipeline.mmd --dag dag.mmd
```

Either way, drift fails the check: a new process the map can't show, or a
station pattern that matches nothing.

## Notes

- Only stations with a `%%metro process:` directive change state; everything
  else is drawn but stays neutral.
- The process pattern is a regex matched against the fully-qualified name, so
  `FASTQC` matches `NFCORE_RNASEQ:RNASEQ:FASTQC`.
- For runs on HPC/cloud, the weblog POSTs come from wherever Nextflow runs, so
  the server must be reachable from there (run it on the head node, or tunnel
  with `ssh -L 8080:localhost:8080 ...`). Use `--token` to guard `/events` when
  binding a non-local interface.
