# Snakemake fixtures

Real output from Snakemake 9.27.2 for the workflows in `*.smk`.
Each workflow has four exports, and all four must convert to the same map:

| File                       | Command                            |
| -------------------------- | ---------------------------------- |
| `<name>.rulegraph.dot`     | `snakemake --rulegraph dot`        |
| `<name>.dag.dot`           | `snakemake --dag dot`              |
| `<name>.rulegraph.mermaid` | `snakemake --rulegraph mermaid-js` |
| `<name>.dag.mermaid`       | `snakemake --dag mermaid-js`       |

The mermaid-js exports use `.mermaid`, not `.mmd`, so that the test sweeps that glob `tests/fixtures/**/*.mmd` for metro maps skip them.

To regenerate, run each command from a directory that holds the inputs the workflows expect:

```bash
mkdir -p reads && touch reads/s1.fq.gz reads/s2.fq.gz genome.fa
snakemake -s <path>/<name>.smk --rulegraph dot --forceall --quiet all > <name>.rulegraph.dot
```
