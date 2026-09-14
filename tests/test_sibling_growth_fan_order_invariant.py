"""A sibling section's growth must not reorder or de-grid a reconverging fan.

Regression lock for #1929.  Two sections, ``orf_calling`` and ``psite_id``,
share one authored grid row.  ``psite_id``'s two P-site file sinks are reached
by lines that cross them, so the sinks go off-track and make ``psite_id``
taller.  The whole-row top-align (Stage 4.7) then grows ``orf_calling``'s bbox
above its own content, opening top slack.  Two later passes must stay robust to
that slack:

- Stage 6.11 ``_balance_one_section`` must not read the grown bbox top as room
  to lift a below-trunk fan sibling (``price`` jumping to the top of the
  five-way fan), and
- Stage 6.1 ``_fan_free_content_upward`` must not lift a fan-in branch
  (``ribotish``) into that slack, which would drag the ``orf_merge``
  reconvergence a half slot off the row grid.

The fixture is a frozen snapshot embedded below, not read from
``examples/riboseq_metro.mmd``: that map's topology has twice changed under this
lock in a way that removed the trigger, so the shipped map is not a reliable
carrier.  The snapshot is minimised to the row structure that reproduces the
bug (its cosmetic directives are dropped); attempts to shrink it further -
trimming ``te``/``reporting`` from the shared row, thinning ``preprocessing``,
or removing the title header band - each stop reproducing it, because the
trigger is the specific slack the full two-row layout opens.  Verified against
both fixes independently: reverting only the Stage 6.11 gate reorders the fan
and de-centres ``orf_merge``; reverting only the Stage 6.1 join guard lands the
reconvergence a half slot off the grid at ``y_spacing=55``.
"""

from __future__ import annotations

import warnings

import pytest
from conftest import parse_and_layout

from nf_metro.layout.constants import SAME_COORD_TOLERANCE
from nf_metro.layout.phases._common import _section_lr_port_anchor_y

# title/center_ports/diamond_style/directional are kept: each feeds the balance
# passes under test (the title's header band opens the fix-#2 slack; the others
# gate the fan geometry).  Only the per-line/file cosmetic labels were dropped.
_FROZEN_MMD = r"""%%metro title: nf-core/riboseq
%%metro center_ports: true
%%metro diamond_style: symmetric
%%metro directional: true
%%metro file: fastq_in | FASTQ
%%metro file: hybrid_gtf_out | GTF | Hybrid GTF
%%metro file: orf_catalogue | BED | ORF catalogue
%%metro file: bigwig_out | BW | Coverage
%%metro file: counts_out | TSV | Gene counts
%%metro file: psite_orf_out | TSV | ORF P-site counts
%%metro file: psite_gene_out | TSV | Gene P-site counts
%%metro file: te_out | TSV | TE results
%%metro file: report_final | HTML | MultiQC
%%metro line: riboseq | Ribo-seq | #e6007e
%%metro line: rnaseq | Matched RNA-seq | #2db572
%%metro line: tiseq | TI-seq | #2b6cb0
%%metro line: annotation | Hybrid annotation | #f2b407

%%metro grid: preprocessing, alignment, novel_transcripts | 0,0
%%metro grid: orf_calling, psite_id, te, reporting | 0,1
%%metro x_spacing: 70

graph LR
    subgraph preprocessing [Read pre-processing]
        fastq_in[ ]
        umi_extract[UMI-tools extract]
        fastp[fastp]
        trimgalore[Trim Galore!]
        bbsplit[BBSplit]
        sortmerna[SortMeRNA]
        ribodetector[RiboDetector]
        bowtie2_rrna[Bowtie2]
        fastqc[FastQC]
        infer_strand[Infer strandedness]
        equalise[Equalise\nread lengths]

        fastq_in -->|riboseq,rnaseq,tiseq| umi_extract
        umi_extract -->|riboseq,rnaseq,tiseq| fastp
        umi_extract -->|riboseq,rnaseq,tiseq| trimgalore
        fastp -->|riboseq,rnaseq,tiseq| bbsplit
        trimgalore -->|riboseq,rnaseq,tiseq| bbsplit
        bbsplit -->|riboseq,rnaseq,tiseq| sortmerna
        bbsplit -->|riboseq,rnaseq,tiseq| ribodetector
        bbsplit -->|riboseq,rnaseq,tiseq| bowtie2_rrna
        sortmerna -->|riboseq,rnaseq,tiseq| fastqc
        ribodetector -->|riboseq,rnaseq,tiseq| fastqc
        bowtie2_rrna -->|riboseq,rnaseq,tiseq| fastqc
        fastqc -->|riboseq,rnaseq,tiseq| infer_strand
        infer_strand -->|riboseq,rnaseq,tiseq| equalise
    end

    subgraph alignment [Alignment & quantification]
        star[STAR]
        umi_dedup[UMI-tools dedup]
        genomecov[BEDTools\ngenomecov]
        salmon_quant[Salmon]
        bigwig_out[ ]
        counts_out[ ]

        star -->|riboseq,rnaseq,tiseq| umi_dedup
        umi_dedup -->|riboseq,rnaseq,tiseq| genomecov
        genomecov -->|riboseq,rnaseq,tiseq| bigwig_out
        umi_dedup -->|riboseq,rnaseq,tiseq| salmon_quant
        salmon_quant -->|riboseq,rnaseq,tiseq| counts_out
    end

    subgraph novel_transcripts [Transcript discovery]
        stringtie[StringTie]
        gffcompare[gffcompare]
        hybrid_merge[Merge &\nfilter GTF]
        hybrid_gtf_out[ ]

        stringtie -->|rnaseq| gffcompare
        gffcompare -->|rnaseq| hybrid_merge
        hybrid_merge -->|rnaseq| hybrid_gtf_out
    end


    subgraph orf_calling [ORF discovery & calling]
        star_hybrid[STAR:\nhybrid 2nd pass]
        ribotish[Ribo-TISH]
        ribocode[RiboCode]
        ribotricer[Ribotricer]
        rpbp[Rp-Bp]
        price[PRICE]
        orf_merge[Merge ORF\ncatalogue]
        orf_catalogue[ ]

        star_hybrid -->|riboseq| ribocode
        ribotish -->|riboseq| orf_merge
        ribocode -->|riboseq| orf_merge
        ribotricer -->|riboseq| orf_merge
        rpbp -->|riboseq| orf_merge
        price -->|riboseq| orf_merge
        orf_merge -->|riboseq| orf_catalogue
    end

    subgraph psite_id [P-site identification]
        ribowaltz[riboWaltz]
        plastid_psite[plastid\nP-site]
        plastid_wiggle[plastid\nwiggle]
        quantify_orf_psite[Quantify ORF\nP-sites]
        psite_counts_gene[Gene in-frame\nP-sites]
        psite_orf_out[ ]
        psite_gene_out[ ]

        ribowaltz -->|riboseq| quantify_orf_psite
        plastid_psite -->|riboseq| plastid_wiggle
        plastid_wiggle -->|riboseq| quantify_orf_psite
        plastid_wiggle -->|riboseq| psite_counts_gene
        quantify_orf_psite -->|riboseq| psite_orf_out
        psite_counts_gene -->|riboseq| psite_gene_out
    end

    subgraph te [Translational efficiency]
        te_prep_gene[Gene count\nmatrix]
        te_prep_orf[ORF count\nmatrix]
        anota2seq[anota2seq]
        deltate[DESeq2 deltaTE]
        dotseq[DOTSeq]
        _te_merge[ ]
        te_out[ ]

        te_prep_gene -->|riboseq,rnaseq| anota2seq
        te_prep_gene -->|riboseq,rnaseq| deltate
        te_prep_orf -->|riboseq,rnaseq| anota2seq
        te_prep_orf -->|riboseq,rnaseq| deltate
        te_prep_orf -->|riboseq,rnaseq| dotseq
        anota2seq -->|riboseq,rnaseq| _te_merge
        deltate -->|riboseq,rnaseq| _te_merge
        dotseq -->|riboseq,rnaseq| _te_merge
        _te_merge -->|riboseq,rnaseq| te_out
    end

    subgraph reporting [Reporting]
        multiqc_final[MultiQC]
        report_final[ ]

        multiqc_final -->|riboseq,rnaseq| report_final
    end

    %% Inter-section edges
    equalise -->|riboseq,rnaseq,tiseq| star
    equalise -->|riboseq| star_hybrid
    umi_dedup -->|rnaseq| stringtie
    umi_dedup -->|riboseq| ribotish
    umi_dedup -->|riboseq| ribotricer
    umi_dedup -->|riboseq| rpbp
    umi_dedup -->|riboseq| price
    umi_dedup -->|riboseq| ribowaltz
    umi_dedup -->|riboseq| plastid_psite
    orf_merge -->|riboseq| quantify_orf_psite
    salmon_quant -->|rnaseq| te_prep_gene
    salmon_quant -->|rnaseq| te_prep_orf
    psite_counts_gene -->|riboseq| te_prep_gene
    quantify_orf_psite -->|riboseq| te_prep_orf
    _te_merge -->|riboseq,rnaseq| multiqc_final
    hybrid_merge -->|annotation| star_hybrid
    hybrid_merge -->|annotation| ribotish
    hybrid_merge -->|annotation| ribotricer
    hybrid_merge -->|annotation| ribocode
"""

# The five-way fan-out feeding ``orf_calling``'s reconvergence, and the join.
_FAN_COLUMN = ("star_hybrid", "ribotish", "ribotricer", "rpbp", "price")
_RECONVERGENCE = "orf_merge"


def _layout(**kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return parse_and_layout(_FROZEN_MMD, **kwargs)


@pytest.fixture(scope="module")
def frozen_map():
    return _layout()


def test_orf_calling_reconvergence_is_fan_centred(frozen_map) -> None:
    """``Merge ORF catalogue`` stays at the vertical centre of the five-way fan.

    Reverting the Stage 6.11 trunk-symmetry gate reads the sibling-grown bbox
    top as room and lifts ``price``, de-centring the join by more than a slot.
    """
    fan_ys = [frozen_map.stations[sid].y for sid in _FAN_COLUMN]
    fan_mid = (min(fan_ys) + max(fan_ys)) / 2
    assert abs(fan_mid - frozen_map.stations[_RECONVERGENCE].y) <= SAME_COORD_TOLERANCE


def test_orf_calling_fan_out_keeps_price_at_the_bottom(frozen_map) -> None:
    """The below-trunk branch ``price`` is not lifted to the top of the fan.

    Reverting the Stage 6.11 gate lifts ``price`` from the bottom of the fan to
    the top, reordering the whole fan-out.
    """
    fan_ys = [frozen_map.stations[sid].y for sid in _FAN_COLUMN]
    assert frozen_map.stations["price"].y == max(fan_ys)


def test_orf_calling_trunk_stays_on_row_grid() -> None:
    """``orf_calling``'s trunk sits an integer slot count from its row siblings.

    Reverting the Stage 6.1 join guard fills the sibling-opened top slack by
    lifting a fan-in branch, dragging the reconvergence a half slot off the row
    grid.  Exercised at ``y_spacing=55``, where that half slot is not an integer
    number of slots.
    """
    y_spacing = 55.0
    graph = _layout(y_spacing=y_spacing)
    orf_port = _section_lr_port_anchor_y(graph, graph.sections["orf_calling"])
    sibling_port = _section_lr_port_anchor_y(graph, graph.sections["psite_id"])
    slots = (orf_port - sibling_port) / y_spacing
    assert abs(slots - round(slots)) * y_spacing <= SAME_COORD_TOLERANCE
