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


rule bwa_mem:
    input:
        reads="reads/{sample}.fq.gz",
        ref="genome.fa",
    output:
        "aligned/{sample}.bam",
    shell:
        "bwa mem {input} > {output}"


rule samtools_index:
    input:
        "aligned/{sample}.bam",
    output:
        "aligned/{sample}.bam.bai",
    shell:
        "samtools index {input}"


rule gatk_haplotypecaller:
    input:
        bam="aligned/{sample}.bam",
        bai="aligned/{sample}.bam.bai",
        ref="genome.fa",
    output:
        "calls/gatk/{sample}.vcf",
    shell:
        "gatk {input} > {output}"


rule deepvariant:
    input:
        bam="aligned/{sample}.bam",
        bai="aligned/{sample}.bam.bai",
        ref="genome.fa",
    output:
        "calls/deepvariant/{sample}.vcf",
    shell:
        "deepvariant {input} > {output}"


rule bcftools_stats:
    input:
        "calls/gatk/{sample}.vcf",
        "calls/deepvariant/{sample}.vcf",
    output:
        "stats/{sample}.txt",
    shell:
        "bcftools stats {input} > {output}"


rule multiqc:
    input:
        expand("fastqc/{sample}.zip", sample=SAMPLES),
        expand("stats/{sample}.txt", sample=SAMPLES),
    output:
        "multiqc/report.html",
    shell:
        "multiqc {input} > {output}"
