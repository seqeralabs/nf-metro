# nf-core Pipelines

Real-world pipelines rendered with nf-metro. These are maintained as `.mmd` files alongside the pipeline source code and rendered automatically.

See the [Gallery](../gallery/index.md) for layout pattern examples and the [Guide](../guide.md) for how to write your own.

## [nf-core/rnaseq](https://github.com/nf-core/rnaseq)

RNA-seq analysis with multiple aligner and quantification routes (STAR/RSEM, STAR/Salmon, HISAT2, Salmon pseudo-alignment, Kallisto).

![nf-core/rnaseq](../assets/renders/pipeline_rnaseq_auto.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/rnaseq
    %%metro logo: examples/nf-core-rnaseq_logo_dark.png
    %%metro style: dark
    %%metro line: star_rsem | Aligner: STAR, Quantification: RSEM | #0570b0
    %%metro line: star_salmon | Aligner: STAR, Quantification: Salmon (default) | #2db572
    %%metro line: hisat2 | Aligner: HISAT2, Quantification: None | #f5c542
    %%metro line: pseudo_salmon | Pseudo-aligner: Salmon, Quantification: Salmon | #e63946
    %%metro line: pseudo_kallisto | Pseudo-aligner: Kallisto, Quantification: Kallisto | #7b2d3b
    %%metro legend: bl
    
    graph LR
        subgraph preprocessing [Pre-processing]
            cat_fastq[cat fastq]
            fastqc_raw[FastQC]
            infer_strandedness[infer strandedness]
            umi_tools_extract[UMI-tools extract]
            fastp[FastP]
            trimgalore[Trim Galore!]
            fastqc_trimmed[FastQC]
            bbsplit[BBSplit]
            sortmerna[SortMeRNA]
    
            cat_fastq -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| fastqc_raw
            fastqc_raw -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| infer_strandedness
            infer_strandedness -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| umi_tools_extract
    
            umi_tools_extract -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| fastp
            umi_tools_extract -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| trimgalore
            fastp -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| fastqc_trimmed
            trimgalore -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| fastqc_trimmed
    
            fastqc_trimmed -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| bbsplit
            bbsplit -->|star_salmon,star_rsem,hisat2,pseudo_salmon,pseudo_kallisto| sortmerna
        end
    
        subgraph genome_align [Genome alignment & quantification]
            star[STAR]
            hisat2_align[HISAT2]
            rsem[RSEM]
            salmon_quant[Salmon]
            umi_tools_dedup[UMI-tools dedup]
    
            star -->|star_rsem| rsem
            star -->|star_salmon| umi_tools_dedup
            umi_tools_dedup -->|star_salmon| salmon_quant
            hisat2_align -->|hisat2| umi_tools_dedup
        end
    
        subgraph postprocessing [Post-processing]
            samtools[SAMtools]
            picard[Picard]
            bedtools[BEDTools]
            bedgraph[bedGraphToBigWig]
            stringtie[StringTie]
    
            samtools -->|star_salmon,star_rsem,hisat2| picard
            picard -->|star_salmon,star_rsem,hisat2| bedtools
            bedtools -->|star_salmon,star_rsem,hisat2| bedgraph
            bedgraph -->|star_salmon,star_rsem,hisat2| stringtie
        end
    
        subgraph pseudo_align [Pseudo-alignment & quantification]
            salmon_pseudo[Salmon]
            kallisto[Kallisto]
            multiqc_pseudo[MultiQC]
    
            salmon_pseudo -->|pseudo_salmon| multiqc_pseudo
            kallisto -->|pseudo_kallisto| multiqc_pseudo
        end
    
        subgraph qc_report [Quality control & reporting]
            rseqc[RSeQC]
            preseq[Preseq]
            qualimap[Qualimap]
            dupradar[dupRadar]
            deseq2_pca[DESeq2 PCA]
            kraken2[Kraken2/Bracken]
            multiqc_final[MultiQC]
    
            rseqc -->|star_salmon,star_rsem,hisat2| preseq
            preseq -->|star_salmon,star_rsem,hisat2| qualimap
            qualimap -->|star_salmon,star_rsem,hisat2| dupradar
            dupradar -->|star_salmon,star_rsem,hisat2| deseq2_pca
            deseq2_pca -->|star_salmon,star_rsem,hisat2| kraken2
            kraken2 -->|star_salmon,star_rsem,hisat2| multiqc_final
        end
    
        %% Inter-section edges
        sortmerna -->|star_salmon,star_rsem| star
        sortmerna -->|hisat2| hisat2_align
        sortmerna -->|pseudo_salmon| salmon_pseudo
        sortmerna -->|pseudo_kallisto| kallisto
        salmon_quant -->|star_salmon| samtools
        rsem -->|star_rsem| samtools
        umi_tools_dedup -->|hisat2| samtools
        stringtie -->|star_salmon,star_rsem,hisat2| rseqc
    ```

## [nf-core/epitopeprediction](https://github.com/nf-core/epitopeprediction)

MHC binding prediction from VCF, protein FASTA, or peptide TSV inputs through five prediction tools.

![nf-core/epitopeprediction](../assets/renders/pipeline_epitopeprediction.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/epitopeprediction
    %%metro style: dark
    %%metro line_order: span
    %%metro logo: docs/images/nf-core-epitopeprediction_logo_dark.png
    %%metro legend: bl
    %%metro line: vcf | Variant Input | #2db572
    %%metro line: protein | Protein Input | #e6550d
    %%metro line: peptide | Peptide Input | #756bb1
    %%metro file: VCF_IN | VCF
    %%metro file: FASTA_IN | FASTA
    %%metro file: TSV_IN | TSV
    %%metro file: TSV_OUT | TSV
    %%metro file: HTML_OUT | HTML
    
    graph LR
        subgraph input_processing [Input Processing]
            VCF_IN[ ]
            gunzip_vcf([Gunzip VCF])
            snpsift_split([SnpSift Split])
            variant_pred([Variant Pred])
            FASTA_IN[ ]
            fasta2peptides([Fasta2Peptides])
            TSV_IN[ ]
    
            VCF_IN -->|vcf| gunzip_vcf
            gunzip_vcf -->|vcf| snpsift_split
            snpsift_split -->|vcf| variant_pred
            FASTA_IN -->|protein| fasta2peptides
        end
    
        subgraph binding_prediction [Binding Prediction]
            split_peptides([Split Peptides])
            prepare_input([Prepare Input])
            mhcflurry([MHCflurry])
            mhcnuggets([MHCnuggets])
            mhcnuggetsii([MHCnuggetsII])
            netmhcpan([NetMHCpan])
            netmhciipan([NetMHCIIpan])
            merge_pred([Merge Pred])
    
            split_peptides -->|vcf,protein,peptide| prepare_input
            prepare_input -->|vcf,protein,peptide| mhcflurry
            prepare_input -->|vcf,protein,peptide| mhcnuggets
            prepare_input -->|vcf,protein,peptide| mhcnuggetsii
            prepare_input -->|vcf,protein,peptide| netmhcpan
            prepare_input -->|vcf,protein,peptide| netmhciipan
            mhcflurry -->|vcf,protein,peptide| merge_pred
            mhcnuggets -->|vcf,protein,peptide| merge_pred
            mhcnuggetsii -->|vcf,protein,peptide| merge_pred
            netmhcpan -->|vcf,protein,peptide| merge_pred
            netmhciipan -->|vcf,protein,peptide| merge_pred
        end
    
        subgraph reporting [Reporting]
            summarize([Summarize Results])
            _tsv_pad[ ]
            multiqc([MultiQC])
            TSV_OUT[ ]
            HTML_OUT[ ]
    
            summarize -->|vcf,protein,peptide| _tsv_pad
            summarize -->|vcf,protein,peptide| multiqc
            _tsv_pad -->|vcf,protein,peptide| TSV_OUT
            multiqc -->|vcf,protein,peptide| HTML_OUT
        end
    
        %% Inter-section edges
        variant_pred -->|vcf| split_peptides
        fasta2peptides -->|protein| split_peptides
        TSV_IN -->|peptide| split_peptides
        merge_pred -->|vcf,protein,peptide| summarize
    ```

## [nf-core/hlatyping](https://github.com/nf-core/hlatyping)

HLA typing from FASTQ or BAM inputs via OptiType and HLA-HD.

![nf-core/hlatyping](../assets/renders/pipeline_hlatyping.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/hlatyping
    %%metro style: dark
    %%metro logo: examples/nf-core-hlatyping_logo_dark.png
    %%metro file: fastq_in | FASTQ
    %%metro file: bam_in | BAM
    %%metro file: report_tsv | TSV
    %%metro file: report_html | HTML
    %%metro line: fastq | FASTQ | #2db572
    %%metro line: bam | BAM | #e6842a
    %%metro legend: bl
    %%metro legend_min_height: 72
    
    graph LR
        subgraph preprocessing [Pre-processing]
            %%metro exit: right | fastq, bam
            fastq_in[ ]
            bam_in[ ]
            cat_fastq[cat FASTQ]
            check_paired[Check Paired]
            collatefastq[BAM to FASTQ]
            fastqc[FastQC]
    
            fastq_in -->|fastq| cat_fastq
            cat_fastq -->|fastq| fastqc
            bam_in -->|bam| check_paired
            check_paired -->|bam| collatefastq
            collatefastq -->|bam| fastqc
        end
    
        subgraph hla_typing [HLA Typing]
            %%metro entry: left | fastq, bam
            %%metro exit: right | fastq, bam
            yara_index[Yara Index]
            yara_mapper[Yara Mapper]
            optitype_run[OptiType]
            _hlahd_delay[ ]
            hlahd_run[HLA-HD]
    
            yara_index -->|fastq,bam| yara_mapper
            yara_mapper -->|fastq,bam| optitype_run
            _hlahd_delay -->|fastq,bam| hlahd_run
        end
    
        subgraph reporting [Reporting]
            %%metro entry: left | fastq, bam
            _branch2[ ]
            _tsv_delay[ ]
            report_tsv[ ]
            multiqc[MultiQC]
            report_html[ ]
    
            _branch2 -->|fastq,bam| _tsv_delay
            _tsv_delay -->|fastq,bam| report_tsv
            _branch2 -->|fastq,bam| multiqc
            multiqc -->|fastq,bam| report_html
        end
    
        %% Inter-section edges
        fastqc -->|fastq,bam| yara_index
        fastqc -->|fastq,bam| _hlahd_delay
        optitype_run -->|fastq,bam| _branch2
        hlahd_run -->|fastq,bam| _branch2
    ```

## [nf-core/variantprioritization](https://github.com/nf-core/variantprioritization)

Somatic and germline variant prioritization using PCGR and CPSR.

![nf-core/variantprioritization](../assets/renders/pipeline_variantprioritization.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/variantprioritization
    %%metro file: cna_in | CNA
    %%metro file: vcf_in | VCF
    %%metro file: report_pcgr | HTML
    %%metro file: report_cpsr | HTML
    %%metro line: somatic | Somatic | #4CAF50
    %%metro line: germline | Germline | #9923A0
    %%metro line: reference | Reference | #2196F3
    
    graph LR
    
        subgraph preprocessing [Pre-processing of vcf files]
            vcf_in[ ]
            tabix[tabix]
            bcftools_norm[bcftools/norm]
            bcftools_filter[bcftools/filter]
    
            vcf_in -->|somatic,germline| tabix
            tabix -->|somatic,germline| bcftools_norm
            bcftools_norm -->|somatic,germline| bcftools_filter
        end
    
        subgraph format_files [Prepare files for PCGR]
            reformat_vcf[Reformat VCF]
            intersect[Intersect VCF]
            prepare_pcgr[Prepare VCF]
            cna_in[ ]
            reformat_cna[Reformat CNA]
    
            reformat_vcf -->|somatic| intersect
            reformat_vcf -->|somatic| prepare_pcgr
            intersect -->|somatic| prepare_pcgr
            cna_in -->|somatic| reformat_cna
        end
    
        subgraph get_reference [Reference]
            get_pcgr[PCGR DB]
            get_vep[VEP Cache]
        end
    
        subgraph run_pcgr [PCGR]
            pcgr[PCGR]
            report_pcgr[ ]
    
            pcgr -->|somatic| report_pcgr
        end
    
        subgraph run_cpsr [CPSR]
            cpsr[CPSR]
            report_cpsr[ ]
    
            cpsr -->|germline| report_cpsr
        end
    
        %% Inter-section edges
        get_pcgr -->|reference| cpsr
        get_vep -->|reference| cpsr
        bcftools_filter -->|germline| cpsr
        bcftools_filter -->|somatic| reformat_vcf
        reformat_cna -->|somatic| pcgr
        prepare_pcgr -->|somatic| pcgr
        get_pcgr -->|reference| pcgr
        get_vep -->|reference| pcgr
    ```

## [nf-core/variantbenchmarking](https://github.com/nf-core/variantbenchmarking)

Benchmarking of variant callers against truth sets with Truvari, hap.py, RTGtools, and more.

![nf-core/variantbenchmarking](../assets/renders/pipeline_variantbenchmarking.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/variantbenchmarking
    %%metro style: dark
    %%metro line_order: span
    %%metro legend: bl
    %%metro compact_offsets: true
    %%metro grid: inputs | 0,0
    %%metro grid: preprocess | 1,0
    %%metro grid: normalization | 2,0
    %%metro grid: filtering | 3,0
    %%metro grid: stats | 4,0
    %%metro grid: benchmarking | 3,1
    %%metro grid: ensembl_truth | 4,1
    %%metro grid: output_processing | 1,1,1,2
    %%metro file: ref_genome_file | FASTA
    %%metro file: truth_vcf_file | VCF
    %%metro file: regions_bed_file | BED
    %%metro file: targets_bed_file | BED
    %%metro file: samplesheet_file | TSV
    %%metro file: snv_stats_out | TSV
    %%metro file: sv_stats_out | TSV
    %%metro file: merged_csvs_out | CSV
    %%metro file: html_report_out | HTML
    %%metro file: multiqc_out | HTML
    %%metro line: truth | Truth Preprocessing | #4CAF50
    %%metro line: test | Test Preprocessing | #ff9800
    %%metro line: sv_cnv | SV/CNV Benchmarking | #E53935
    %%metro line: snv_indel | SNV/INDEL Benchmarking | #AB47BC
    %%metro line: concordance | Concordance | #FFB300
    %%metro line: intersection | Intersection | #26A69A
    %%metro line: output | Output Processing | #03A9F4
    
    graph LR
        subgraph inputs [Inputs]
            %%metro exit: right | truth, test
            ref_genome_file[ ]
            ref_genome[Reference Genome]
            truth_vcf_file[ ]
            truth_vcf[Truth VCF]
            regions_bed_file[ ]
            regions_bed[Regions BED]
            targets_bed_file[ ]
            targets_bed[Targets BED]
            samplesheet_file[ ]
            samplesheet[Samplesheet]
            _inputs_hub[hidden]
    
            ref_genome_file -->|truth| ref_genome
            truth_vcf_file -->|truth| truth_vcf
            regions_bed_file -->|truth| regions_bed
            targets_bed_file -->|truth| targets_bed
            samplesheet_file -->|test| samplesheet
            ref_genome -->|truth| _inputs_hub
            truth_vcf -->|truth| _inputs_hub
            regions_bed -->|truth| _inputs_hub
            targets_bed -->|truth| _inputs_hub
            samplesheet -->|test| _inputs_hub
        end
    
        subgraph preprocess [Preprocessing (Optional)]
            %%metro entry: left | truth, test
            %%metro exit: right | truth, test
            subsample[Subsample]
            liftover[Liftover\n(Picard, UCSC)]
            subsample -->|test| liftover
        end
    
        subgraph normalization [Variant Normalization (Optional)]
            %%metro entry: left | truth, test
            %%metro exit: right | truth, test
            sv_processing[SV\nProcessing]
            var_norm[Variant\nNormalization]
            sv_processing -->|test| var_norm
        end
    
        subgraph filtering [Variant Filtering (Optional)]
            %%metro entry: left | test
            %%metro exit: right | test
            filter_contigs[Filter\nContigs]
            bcftools_filter[bcftools\nfilter]
            survivor_filter[SURVIVOR\nfilter]
            filter_contigs -->|test| bcftools_filter
            filter_contigs -->|test| survivor_filter
        end
    
        subgraph ensembl_truth [Ensembl Truth]
            %%metro direction: TB
            %%metro entry: top | test
            %%metro exit: left | truth
            _ensembl_hub[hidden]
            survivor_merge[SURVIVOR\nmerge]
            bcftools_merge[bcftools\nmerge]
            consensus_filter[Consensus\nFilter]
            _ensembl_hub -->|test| survivor_merge
            _ensembl_hub -->|test| bcftools_merge
            survivor_merge -->|test| consensus_filter
            bcftools_merge -->|test| consensus_filter
        end
    
        subgraph benchmarking [Benchmarking]
            %%metro direction: RL
            %%metro entry: top | test, truth
            %%metro entry: right | test, truth
            bench_hub[ ]
            truvari[Truvari]
            rtg_vcfeval[RTGtools vcfeval]
            svanalyzer[SVanalyzer]
            rtg_bndeval[RTGtools bndeval]
            happy[hap.py]
            wittyer[wittyer]
            sompy[som.py]
            intersection_tool[Intersection]
            gatk4_conc[GATK4 Concordance]
            bench_hub -->|sv_cnv| truvari
            bench_hub -->|snv_indel| rtg_vcfeval
            bench_hub -->|sv_cnv| svanalyzer
            bench_hub -->|sv_cnv| rtg_bndeval
            bench_hub -->|snv_indel| happy
            bench_hub -->|sv_cnv| wittyer
            bench_hub -->|snv_indel| sompy
            bench_hub -->|intersection| intersection_tool
            bench_hub -->|concordance| gatk4_conc
            %%metro exit: left | sv_cnv, snv_indel, concordance, intersection
        end
    
        subgraph output_processing [Output Processing]
            %%metro direction: RL
            %%metro entry: right | sv_cnv, snv_indel, concordance, intersection
            results_hub[ ]
            merge_res[Merge\nTP/FP/FN]
            vcf2csv[VCF to\nCSV]
            sum_stats[Summary\nStats]
            plots[Plots]
            datavzrd_tool[datavzrd]
            merged_csvs[Merged\nCSVs]
            merged_csvs_out[ ]
            bench_summaries[Benchmarking\nSummaries]
            html_report[HTML\nReport]
            html_report_out[ ]
            multiqc[MultiQC\nReport]
            multiqc_out[ ]
    
            results_hub -->|output| merge_res
            merge_res -->|output| vcf2csv
            results_hub -->|output| sum_stats
            sum_stats -->|output| plots
            sum_stats -->|output| datavzrd_tool
            vcf2csv -->|output| plots
            vcf2csv -->|output| merged_csvs
            merged_csvs -->|output| merged_csvs_out
            results_hub -->|output| bench_summaries
            datavzrd_tool -->|output| html_report
            html_report -->|output| html_report_out
            sum_stats -->|output| multiqc
            bench_summaries -->|output| multiqc
            plots -->|output| multiqc
            multiqc -->|output| multiqc_out
        end
    
        subgraph stats [Variant Statistics]
            %%metro entry: left | test
            %%metro exit: right | test, output
            bcftools_stats[bcftools\nstats]
            survivor_stats[SURVIVOR\nstats]
            snv_stats[SNV stats]
            sv_stats[SV stats]
            snv_stats_out[ ]
            sv_stats_out[ ]
            bcftools_stats -->|output| snv_stats
            survivor_stats -->|output| sv_stats
            snv_stats -->|output| snv_stats_out
            sv_stats -->|output| sv_stats_out
        end
    
        %% Section 1 -> 2: test through Subsample, truth to Liftover
        _inputs_hub -->|test| subsample
        _inputs_hub -->|truth| liftover
        %% Section 2 -> 3: test to SV Processing, truth to Var Norm
        liftover -->|test| sv_processing
        liftover -->|truth| var_norm
        %% Section 1 -> 3 (bypass section 2)
        _inputs_hub -->|test| sv_processing
        _inputs_hub -->|truth| var_norm
        %% Section 3 -> 4
        var_norm -->|test| filter_contigs
        %% Section 4 -> 5: each filter to its corresponding stats
        bcftools_filter -->|test| bcftools_stats
        survivor_filter -->|test| survivor_stats
        %% Section 4 -> Ensembl Truth
        bcftools_filter -->|test| _ensembl_hub
        survivor_filter -->|test| _ensembl_hub
        %% Multiple sections -> Benchmarking
        filter_contigs -->|test| bench_hub
        var_norm -->|truth| bench_hub
        consensus_filter -->|truth| bench_hub
        %% Benchmarking -> Output Processing
        truvari -->|sv_cnv| results_hub
        svanalyzer -->|sv_cnv| results_hub
        rtg_bndeval -->|sv_cnv| results_hub
        wittyer -->|sv_cnv| results_hub
        rtg_vcfeval -->|snv_indel| results_hub
        happy -->|snv_indel| results_hub
        sompy -->|snv_indel| results_hub
        intersection_tool -->|intersection| results_hub
        gatk4_conc -->|concordance| results_hub
    ```

## [sanger-tol/genomeassembly](https://github.com/sanger-tol/genomeassembly)

Genome assembly from long reads and Hi-C data through purging, polishing, scaffolding, and QC.

![sanger-tol/genomeassembly](../assets/renders/pipeline_genomeassembly.svg)

??? note "Mermaid source"

    ```text
    %%metro title: sanger-tol/genomeassembly
    %%metro style: dark
    %%metro line: long_reads | Long reads | #2db572
    %%metro line: hic_reads | Hi-C reads | #e6842a
    %%metro line: i10x_reads | 10X reads | #756bb1
    %%metro line: assemblies | Assembly | #0570b0
    %%metro file: input_long_reads | FASTX
    %%metro file: input_hic_reads | CRAM
    %%metro file: input_10x_reads | FASTQ
    %%metro grid: raw_asm | 0, 0, 2
    %%metro grid: purging | 1, 0, 2
    %%metro grid: polishing | 2, 0, 2
    %%metro grid: scaffolding | 3, 0, 4
    %%metro grid: genome_statistics | 4, 0, 4
    %%metro line_order: span
    %%metro compact_offsets: true
    %%metro legend: bl
    graph LR
        subgraph raw_asm [Raw assembly]
            %%metro exit: bottom | hic_reads
            %%metro exit: right | assemblies,long_reads
            input_long_reads[ ]
            input_hic_reads[ ]
            hifiasm[Hifiasm]
            input_long_reads -->|long_reads| hifiasm
            input_hic_reads -->|hic_reads| hifiasm
        end
        subgraph purging [Purging]
            %%metro entry: left | assemblies,long_reads
            %%metro exit: right | assemblies
            purging_minimap2[minimap2]
            purge_dups[purge_dups]
            purging_minimap2 -->|assemblies,long_reads| purge_dups
        end
        subgraph polishing [Polishing]
            %%metro entry: left | assemblies
            %%metro exit: right | assemblies
            input_10x_reads[ ]
            longranger[Longranger]
            freebayes[FreeBayes]
            input_10x_reads -->|i10x_reads| longranger
            longranger -->|i10x_reads,assemblies| freebayes
        end
        subgraph scaffolding [Scaffolding]
            %%metro entry: left | assemblies
            %%metro entry: bottom | hic_reads
            %%metro exit: right | assemblies
            scaffolding_bwamem2[bwa-mem2]
            scaffolding_minimap2[minimap2]
            yahs[YaHS]
            pretextmap[PretextMap]
            juicer[Juicer]
            cooler[Cooler]
            scaffolding_bwamem2 -->|assemblies,hic_reads| yahs
            scaffolding_minimap2 -->|assemblies,hic_reads| yahs
            yahs -->|assemblies,hic_reads| pretextmap
            yahs -->|assemblies,hic_reads| juicer
            yahs -->|assemblies,hic_reads| cooler
        end
        subgraph genome_statistics [Genome QC]
            %%metro entry: left | assemblies
            asmstats[asmstats]
            gfastats[GFAStats]
            busco[BUSCO]
            merquryfk[MerquryFK]
            asmstats -->|assemblies| gfastats
            asmstats -->|assemblies| busco
            asmstats -->|assemblies| merquryfk
        end
    
        %% Inter-section edges
        hifiasm -->|assemblies,long_reads| purging_minimap2
        hifiasm -->|hic_reads| scaffolding_bwamem2
        hifiasm -->|hic_reads| scaffolding_minimap2
        hifiasm -->|assemblies| longranger
        hifiasm -->|assemblies| scaffolding_bwamem2
        hifiasm -->|assemblies| scaffolding_minimap2
        purge_dups -->|assemblies| longranger
        purge_dups -->|assemblies| scaffolding_bwamem2
        purge_dups -->|assemblies| scaffolding_minimap2
        freebayes -->|assemblies| scaffolding_bwamem2
        freebayes -->|assemblies| scaffolding_minimap2
        hifiasm -->|assemblies| asmstats
        purge_dups -->|assemblies| asmstats
        freebayes -->|assemblies| asmstats
        yahs -->|assemblies| asmstats
    ```

## [nf-core/funcprofiler](https://github.com/nf-core/funcprofiler)

Functional profiling of metagenomic samples with HUMAnN, DIAMOND, RGI, mifaser, eggNOG-mapper, and more.

![nf-core/funcprofiler](../assets/renders/pipeline_funcprofiler.svg)

??? note "Mermaid source"

    ```text
    %%metro title: nf-core/funcprofiler
    %%metro style: dark
    %%metro line: qc | Preprocessing & QC | #4CAF50
    %%metro line: concat | Merge & Concat | #2196F3
    %%metro line: humann3 | HUMAnN v3 | #FF9800
    %%metro line: humann4 | HUMAnN v4 | #FF5722
    %%metro line: fmhfunprofiler | FMH FunProfiler | #E91E63
    %%metro line: rgi | RGI | #CDDC39
    %%metro line: mifaser | mifaser | #00BCD4
    %%metro line: diamond | DIAMOND | #9C27B0
    %%metro line: eggnog | eggNOG-mapper | #3F51B5
    %%metro line: multiqc | Reporting | #795548
    %%metro line: db | Database Prep | #607D8B
    
    graph LR
        subgraph input[Input]
            input_short([Short Read\nFASTQ])
            input_dbs([Input Databases])
            sr_qc(Preprocess)
            merge(MERGE_RUNS)
        end
    
        subgraph profiling[Functional Profiling]
            humann3(HUMAnN v3)
            humann4(HUMAnN v4)
            fmhfunprofiler(FMH FunProfiler)
            RGI(RGI)
            mifaser(mifaser)
            diamond(DIAMOND)
            eggnog_mapper(eggNOG-mapper)
        end
    
        subgraph QC[Quality Check]
            multiqc(MultiQC)
        end
    
        subgraph Output[Output]
            output([Results Directory])
        end
    
        %% DB Prep
        input_dbs -->|db| humann3
        input_dbs -->|db| humann4
        input_dbs -->|db| fmhfunprofiler
        input_dbs -->|db| RGI
        input_dbs -->|db| mifaser
        input_dbs -->|db| diamond
        input_dbs -->|db| eggnog_mapper
    
        %% Preprocessing
        input_short -->|qc| sr_qc
        sr_qc -->|concat| merge
    
        %% Profiling split
        merge -->|humann3| humann3
        merge -->|humann4| humann4
        merge -->|fmhfunprofiler| fmhfunprofiler
        merge -->|rgi| RGI
        merge -->|mifaser| mifaser
        merge -->|diamond| diamond
        merge -->|eggnog| eggnog_mapper
    
        %% Reporting
        sr_qc -->|multiqc| multiqc
        humann3 -->|multiqc| multiqc
        humann4 -->|multiqc| multiqc
        fmhfunprofiler -->|multiqc| multiqc
        RGI -->|multiqc| multiqc
        mifaser -->|multiqc| multiqc
        diamond -->|multiqc| multiqc
        eggnog_mapper -->|multiqc| multiqc
    
        %% Output
        multiqc -->|multiqc| output
        humann3 -->|humann3| output
        humann4 -->|humann4| output
        fmhfunprofiler -->|fmhfunprofiler| output
        RGI -->|rgi| output
        mifaser -->|mifaser| output
        diamond -->|diamond| output
        eggnog_mapper -->|eggnog| output
    ```
