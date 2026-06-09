#!/usr/bin/env nextflow
nextflow.enable.dsl = 2

params.samples = ['sampleA', 'sampleB', 'sampleC', 'sampleD']

process SAMPLESHEET {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 2) + 1 ))
    """
}

process TRIMGALORE {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 3) + 2 ))
    """
}

process FASTQC {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 3) + 2 ))
    """
}

process GENOME_INDEX {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 2) + 2 ))
    """
}

process STAR_ALIGN {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 5) + 5 ))
    """
}

process SAMTOOLS_SORT {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 3) + 2 ))
    """
}

process SALMON_QUANT {
    tag "$sample"
    input:  val sample
    output: val sample
    script:
    """
    sleep \$(( (RANDOM % 4) + 3 ))
    """
}

process MULTIQC {
    tag "report"
    input:  val samples
    output: val 'done'
    script:
    """
    sleep 3
    """
}

workflow {
    samples = Channel.fromList(params.samples)

    SAMPLESHEET(samples)
    TRIMGALORE(SAMPLESHEET.out)

    FASTQC(TRIMGALORE.out)
    GENOME_INDEX(TRIMGALORE.out)
    STAR_ALIGN(GENOME_INDEX.out)
    SAMTOOLS_SORT(STAR_ALIGN.out)
    SALMON_QUANT(GENOME_INDEX.out)

    MULTIQC(FASTQC.out.mix(SAMTOOLS_SORT.out, SALMON_QUANT.out).collect())
}
