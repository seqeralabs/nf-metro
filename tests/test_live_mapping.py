"""Tests for live-progress mapping and the check-mapping linter."""

from pathlib import Path

from nf_metro.live.mapping import (
    check_mapping,
    process_names_from_dag,
    stations_for_process,
)

NEXTFLOW_DAG = Path(__file__).parent / "fixtures" / "nextflow" / "with_subworkflows.mmd"

MAPPING = {
    "trim": ["TRIMGALORE"],
    "align": ["STAR_ALIGN", "SAMTOOLS_SORT"],
    "qc": ["FASTQC"],
}


def test_stations_for_process_matches_qualified_name():
    assert stations_for_process("NFCORE_RNASEQ:RNASEQ:FASTQC", MAPPING) == ["qc"]


def test_stations_for_process_case_insensitive():
    assert stations_for_process("trimgalore", MAPPING) == ["trim"]


def test_stations_for_process_no_match():
    assert stations_for_process("BCFTOOLS", MAPPING) == []


def test_process_names_from_dag_reads_stadium_nodes():
    names = process_names_from_dag(NEXTFLOW_DAG.read_text())
    assert "FASTQC" in names and "STAR_ALIGN" in names
    # Channel/operator nodes (Channel.of, collect hubs) are dropped.
    assert not any("Channel" in n for n in names)


def test_check_mapping_clean():
    report = check_mapping(
        MAPPING,
        station_ids=["trim", "align", "qc"],
        process_names=["TRIMGALORE", "STAR_ALIGN", "SAMTOOLS_SORT", "FASTQC"],
    )
    assert report.ok
    assert report.unmapped_processes == []
    assert report.dead_patterns == []


def test_check_mapping_flags_unmapped_process():
    report = check_mapping(
        MAPPING,
        station_ids=["trim", "align", "qc"],
        process_names=[
            "TRIMGALORE",
            "STAR_ALIGN",
            "SAMTOOLS_SORT",
            "FASTQC",
            "BWA_MEM",
        ],
    )
    assert not report.ok
    assert report.unmapped_processes == ["BWA_MEM"]


def test_check_mapping_ignore_suppresses_unmapped():
    report = check_mapping(
        MAPPING,
        station_ids=["trim", "align", "qc"],
        process_names=["TRIMGALORE", "DUMPSOFTWAREVERSIONS"],
        ignore=[".*DUMPSOFTWAREVERSIONS"],
    )
    assert report.unmapped_processes == []


def test_check_mapping_flags_dead_pattern():
    report = check_mapping(
        {"trim": ["TRIMGALORE"], "ancient": ["OLD_TOOL"]},
        station_ids=["trim", "ancient"],
        process_names=["TRIMGALORE"],
    )
    assert not report.ok
    assert report.dead_patterns == [("ancient", "OLD_TOOL")]


def test_check_mapping_reports_unmapped_stations():
    report = check_mapping(
        {"trim": ["TRIMGALORE"]},
        station_ids=["trim", "decorative"],
        process_names=["TRIMGALORE"],
    )
    assert report.ok  # an unmapped station is reported but is not a failure
    assert report.unmapped_stations == ["decorative"]
