"""
rail-rna_pipeline.py

"""

import pipeline
import os
import site
import re

base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
site.addsitedir(os.path.join(base_path, "util"))

#
# Note: All the %%(something)%% placeholders below are substituted in a
# mode-dependent manner.  That is, a different string could be substituted
# depending on whether we're in local, hadoop or emr mode.  This module tries
# to stay as mode-agnostic as possible.
#
# %%BASE%%: Base Rail directory
# %%MANIFEST%%: Manifest file
# %%BOWTIE%%: Path to Bowtie executable on workers/local machine
# %%BEDTOBIGBED%%: Path to bedToBigBed executable on workers/local machine
#


class PreprocessingStep(pipeline.Step):
    def __init__(self, inps, output, _, gconf):
        compress_arg = ""
        if gconf.preprocCompress is not None and gconf.preprocCompress == "gzip":
            compress_arg = "--gzip-output "
        push_output = output.plus('push')
        mapper_str = """
            python %%BASE%%/src/rail-rna/preprocess.py 
                --nucs-per-file=8000000
                %s
                --push=%s
                --ignore-first-token""" % (compress_arg, push_output.toUpperUrl())
        mapper_str = re.sub('\s+', ' ', mapper_str.strip())
        super(PreprocessingStep, self).__init__(
            inps,
            output,
            name="Preprocess",  # name
            lineByLine=True,
            mapper=mapper_str)


class AlignStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        mapper_str = """
            python %%BASE%%/src/rail-rna/align.py
                --bowtie-idx=%%REF_BOWTIE_INDEX%% 
                --bowtie-exe=%%BOWTIE%%
                --max-readlet-size %d 
                --readlet-interval %d 
                --partition-length %d
                --max-intron-size %d
                --min-intron-size %d
                --capping-fraction %f
                --exon-differentials 
                --verbose %s %s
                --min-cap-query-size %d
                --cap-search-window-size %d
                -- %s """ % (tconf.readletLen, tconf.readletIval, tconf.partitionLen,
                             tconf.max_intron_size,
                             tconf.min_intron_size, tconf.capping_fraction,
                             '--stranded' if tconf.stranded else '',
                             '--do-not-search-for-caps' if tconf.do_not_search_for_caps else '',
                             tconf.min_cap_query_size,
                             tconf.cap_search_window_size,
                             tconf.bowtieArgs())
        mapper_str = re.sub('\s+', ' ', mapper_str.strip())
        super(AlignStep, self).__init__(
            inps,
            output,
            name="Align",
            mapper=mapper_str,
            multipleOutput=True)


class BamStep(pipeline.Step):
    def __init__(self, inps, output, tconf, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/bam.py
                --out=%s/alignments
                --bowtie-idx=%%REF_BOWTIE_INDEX%% 
                --samtools-exe=%%SAMTOOLS%%
                --bam-basename=%s
                %s
                %s
            """ % (gconf.out, tconf.bam_basename,
                   '--output-by-chromosome' if tconf.output_bam_by_chromosome else '',
                   '--output-sam' if tconf.output_sam else '')
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(BamStep, self).__init__(
            inps,
            output,
            name="Bam",
            aggr=(pipeline.Aggregation(1, None, (2 if tconf.output_bam_by_chromosome else 1), 3)
                  if gconf.out is not None else pipeline.Aggregation(1, None, 1, 2)),
            reducer=reducer_str)


class IntronStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/intron.py
                --bowtie-idx=%%REF_BOWTIE_INDEX%% 
                --cluster-radius=%d
                --intron-partition-overlap=%d
                --verbose
                --partition-length %d
                --motif-radius=%d
                %s
        """ % (tconf.clusterRadius, tconf.intronPartitionOlap,
               tconf.partitionLen, tconf.motifRadius,
               '--stranded' if tconf.stranded else '')
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(IntronStep, self).__init__(
            inps,
            output,  # output URL
            name="Intron",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 2),  # 8 tasks per reducer
            reducer=reducer_str,
            multipleOutput=True)


class BedPreStep(pipeline.Step):
    def __init__(self, inps, output, _, _2):
        reducer_str = """
            python %%BASE%%/src/rail-rna/bed_pre.py
                --bowtie-idx=%%REF_BOWTIE_INDEX%% %s
        """ % ''
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(BedPreStep, self).__init__(
            inps,
            output,  # output URL
            name="BedPre",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 1),  # 8 tasks per reducer
            reducer=reducer_str,
            multipleOutput=True)


class BedStep(pipeline.Step):
    def __init__(self, inps, output, tconf, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/bed.py 
                --bowtie-idx=%%REF_BOWTIE_INDEX%%
                --out=%s/junctions 
                --bed-basename=%s""" % (gconf.out, tconf.bed_basename)
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(BedStep, self).__init__(
            inps,
            output,  # output URL
            name="Bed",  # namef
            aggr=pipeline.Aggregation(1, None, 1, 4),
            reducer=reducer_str)


class IntronPostStep(pipeline.Step):
    def __init__(self, inps, output, _, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/intron_post.py
                --bowtie-build-exe=%%BOWTIE-BUILD%%
                --bowtie-idx=%%REF_BOWTIE_INDEX%%
                --out=%s/index
            """ % gconf.out
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(IntronPostStep, self).__init__(
            inps,
            output,
            name="IntronPost",
            aggr=pipeline.Aggregation(1, None, 1, 5),
            reducer=reducer_str)


class MergeStep(pipeline.Step):
    def __init__(self, inps, output, _, _2):
        reducer_str = "python %%BASE%%/src/rail-rna/merge.py --partition-stats"
        super(MergeStep, self).__init__(
            inps,
            output,  # output URL
            name="Merge",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 2),
            reducer=reducer_str,
            multipleOutput=True)


class RealignStep(pipeline.Step):
    def __init__(self, inps, output, tconf, gconf, cache=None):
        mapper_str = """
            python %%BASE%%/src/rail-rna/realign.py
                --original-idx=%%REF_BOWTIE_INDEX%% 
                --bowtie-idx=%%REF_INTRON_INDEX%%
                --bowtie-exe=%%BOWTIE%%
                --partition-length %d
                --exon-differentials 
                --verbose %s
                -- %s""" % (tconf.partitionLen,
                            '--stranded' if tconf.stranded else '',
                            tconf.bowtieArgs())
        mapper_str = re.sub('\s+', ' ', mapper_str.strip())
        super(RealignStep, self).__init__(
            inps,
            output,
            name="Realign",
            mapper=mapper_str,
            multipleOutput=True,
            cache=cache
            )


class WalkPrenormStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/walk_prenorm.py 
                --partition-stats 
                --partition-len=%d""" % tconf.partitionLen
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(WalkPrenormStep, self).__init__(
            inps,
            output,  # output URL
            name="WalkPreNormalize",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 2),
            reducer=reducer_str,
            multipleOutput=True)


class NormalizePreStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/normalize_pre.py 
                --partition-stats 
                --partition-len=%d""" % tconf.partitionLen
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(NormalizePreStep, self).__init__(
            inps,
            output,  # output URL
            name="NormalizePre",  # name
            aggr=pipeline.Aggregation(None, 8, 2, 3),
            reducer=reducer_str,
            multipleOutput=True)


class CollapseStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/collapse.py 
                %s
            """ % ('',)
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(CollapseStep, self).__init__(
            inps,
            output,  # output URL
            name="Collapse",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 1),
            reducer=reducer_str,
            multipleOutput=True)


class CoveragePreStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/coverage_pre.py
                --bowtie-idx=%%REF_BOWTIE_INDEX%%
                --partition-stats %s""" % ('',)
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(CoveragePreStep, self).__init__(
            inps,
            output,  # output URL
            name="CoveragePre",  # name
            aggr=pipeline.Aggregation(None, 8, 2, 3),
            reducer=reducer_str,
            multipleOutput=True)


class CoverageStep(pipeline.Step):
    def __init__(self, inps, output, tconf, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/coverage.py
                --bowtie-idx=%%REF_BOWTIE_INDEX%%
                --percentile %f
                --out=%s/coverage
                --bigbed-exe=%%BEDTOBIGBED%%
                --verbose""" % (tconf.normPercentile, gconf.out)
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(CoverageStep, self).__init__(
            inps,
            output,  # output URL
            name="Coverage",  # name
            aggr=pipeline.Aggregation(None, 1, 1, 3),
            reducer=reducer_str)


class CoveragePostStep(pipeline.Step):
    def __init__(self, inps, output, _, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/coverage_post.py 
                --out=%s/normalize 
                --manifest=%%MANIFEST%%""" % gconf.out
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(CoveragePostStep, self).__init__(
            inps,
            output,  # output URL
            name="CoveragePost",  # name
            aggr=pipeline.Aggregation(1, None, 0, 0),
            reducer=reducer_str)


class NormalizeStep(pipeline.Step):
    def __init__(self, inps, output, tconf, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/normalize2.py 
                --percentile %f
                --out_dir=%s/coverage
                --bigbed_exe=%%BEDTOBIGBED%%
                --faidx=%%REF_FASTA_INDEX%%
                --verbose""" % (tconf.normPercentile, gconf.out)
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(NormalizeStep, self).__init__(
            inps,
            output,  # output URL
            name="Normalize",  # name
            aggr=pipeline.Aggregation(None, 1, 1, 3),
            reducer=reducer_str)


class NormalizePostStep(pipeline.Step):
    def __init__(self, inps, output, _, gconf):
        reducer_str = """
            python %%BASE%%/src/rail-rna/normalize_post.py 
                --out=%s/normalize 
                --manifest=%%MANIFEST%%""" % gconf.out
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(NormalizePostStep, self).__init__(
            inps,
            output,  # output URL
            name="NormalizePost",  # name
            aggr=pipeline.Aggregation(1, None, 0, 0),
            reducer=reducer_str)


class WalkFitStep(pipeline.Step):
    def __init__(self, inps, output, _, _2):
        reducer_str = """
            python %%BASE%%/src/rail-rna/walk_fit.py"""
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(WalkFitStep, self).__init__(
            inps,
            output,  # output URL
            name="WalkFit",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 2),
            reducer=reducer_str)


class EbayesStep(pipeline.Step):
    """ Just 1 reduce task """
    def __init__(self, inps, output, _, _2):
        reducer_str = """
            python %%BASE%%/src/rail-rna/walk_fit.py"""
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(EbayesStep, self).__init__(
            inps,
            output,
            name="EmpiricalBayes",  # name
            aggr=pipeline.Aggregation(1, None, 0, 0),
            reducer=reducer_str)


class HmmParamsStep(pipeline.Step):
    def __init__(self, inps, output, _, _2):
        reducer_str = """
            python %%BASE%%/src/rail-rna/hmm_params.py"""
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(HmmParamsStep, self).__init__(
            inps,
            output,
            name="HMMparams",  # name
            aggr=pipeline.Aggregation(1, None, 0, 0),
            reducer=reducer_str)


class HmmStep(pipeline.Step):
    def __init__(self, inps, output, _, _2):
        reducer_str = """
            python %%BASE%%/src/rail-rna/hmm.py"""
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(HmmStep, self).__init__(
            inps,
            output,
            name="HMM",  # name
            aggr=pipeline.Aggregation(None, 8, 1, 2),
            reducer=reducer_str)


class AggrPathStep(pipeline.Step):
    def __init__(self, inps, output, tconf, _):
        reducer_str = """
            python %%BASE%%/src/rail-rna/aggr_path.py"""
        reducer_str = re.sub('\s+', ' ', reducer_str.strip())
        super(AggrPathStep, self).__init__(
            inps,
            output,
            name="HMMPaths",  # name
            aggr=pipeline.Aggregation(tconf.numPermutations * 2, None, 1, 2),
            reducer=reducer_str)
