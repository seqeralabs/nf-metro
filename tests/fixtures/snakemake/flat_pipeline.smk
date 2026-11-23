SAMPLES = ["s1", "s2"]


rule all:
    input:
        "multiqc/report.html",


rule fastqc:
    input:
        "reads/{sample}.fq.gz",
    output:
        "fastqc/{sample}.zip",
    shell:
        "fastqc {input} > {output}"


rule trim_reads:
    input:
        "reads/{sample}.fq.gz",
    output:
        "trimmed/{sample}.fq.gz",
    shell:
        "trim {input} > {output}"


rule align:
    input:
        reads="trimmed/{sample}.fq.gz",
        ref="genome.fa",
    output:
        "aligned/{sample}.bam",
    shell:
        "align {input} > {output}"


rule sort_bam:
    input:
        "aligned/{sample}.bam",
    output:
        "sorted/{sample}.bam",
    shell:
        "sort {input} > {output}"


rule multiqc:
    input:
        expand("fastqc/{sample}.zip", sample=SAMPLES),
        expand("sorted/{sample}.bam", sample=SAMPLES),
    output:
        "multiqc/report.html",
    shell:
        "multiqc {input} > {output}"
