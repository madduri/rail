SCR_DIR=../../src/rnawesome
TORNADO=../..
TOOLS=$TORNADO/tools
MANIFEST_FN=eg1.manifest

mkdir -p intermediate
INTERMEDIATE_DIR=intermediate/

# Step 1: Readletize input reads and use Bowtie to align the readlets 
ALIGN_AGGR="cat"
ALIGN="python $SCR_DIR/align.py"

# Step 2: Collapse identical intervals from same sample
# In Hadoop, we want to partition by first field then sort by second
# -partitioner org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner
# -D stream.num.map.output.key.fields=2
# -D mapred.text.key.partitioner.options=-k1,1
MERGE_AGGR1="grep '^exon'"
MERGE_AGGR2="cut -f 2-"
MERGE_AGGR3="sort -n -k2,2"
MERGE_AGGR4="sort -s -k1,1"
MERGE="python $SCR_DIR/merge.py"

# Step 3: Walk over genome windows and emit per-sample, per-position
#         coverage tuples
# In Hadoop, we want to partition by first field then sort by second
# -partitioner org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner
# -D stream.num.map.output.key.fields=2
# -D mapred.text.key.partitioner.options=-k1,1
WALK_PRENORM_AGGR1="sort -n -k2,2"
WALK_PRENORM_AGGR2="sort -s -k1,1"
WALK_PRENORM="python $SCR_DIR/walk_prenorm.py"

# Step 4: For all samples, take all coverage tuples for the sample and
#         from them calculate a normalization factor
# In Hadoop, we want to partition by first field then sort by second
# -partitioner org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner
# -D stream.num.map.output.key.fields=1
# -D mapred.text.key.partitioner.options=-k1,1
NORMALIZE_AGGR1="sort -n -k2,2"
NORMALIZE_AGGR2="sort -s -k1,1"
NORMALIZE="python $SCR_DIR/normalize.py"
SAMPLE_OUT=intermediate/samples
mkdir -p  $SAMPLE_OUT
UCSC_TOOLS=$TOOLS/ucsc_tools
BIGBED_EXE=$UCSC_TOOLS/bedToBigBed
CHROM_SIZES=$PWD/chrom.sizes
# Step 5: Collect all the norm factors together and write to file
# In Hadoop no partitioning or sorting (mapper only)
NORMALIZE_POST_AGGR="cat"
NORMALIZE_POST="python $SCR_DIR/normalize_post.py"

# Step 6: Walk over genome windows again (taking output from Step 2)
#         but this time, calculate per-position coverage vectors and
#         fit a linear model to each
# In Hadoop, we want to partition by first field then sort by second
# -partitioner org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner
# -D stream.num.map.output.key.fields=2
# -D mapred.text.key.partitioner.options=-k1,1
WALK_FIT="python $SCR_DIR/walk_fit.py"

# Step 7: Given all the t-statistics, moderate them and emit moderated
#         t-stats
# In Hadoop no partitioning or sorting (mapper only)
EBAYES_AGGR="cat"
EBAYES="python $SCR_DIR/ebayes.py"

# Step 8: Given all the moderated t-statistics, calculate the HMM
#         parameters to use in the next step
# In Hadoop no partitioning or sorting (mapper only)
HMM_PARAMS_AGGR="cat"
HMM_PARAMS="python $SCR_DIR/hmm_params.py"

# Step 9: Given sorted bins of moderated t-statistics, and HMM
#         parameters, run the HMM
# In Hadoop, we want to partition by first field then sort by second
# -partitioner org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner
# -D stream.num.map.output.key.fields=2
# -D mapred.text.key.partitioner.options=-k1,1
HMM_AGGR1="sort -n -k2,2"
HMM_AGGR2="sort -s -k1,1"
HMM="python $SCR_DIR/hmm.py"

# Step 10: Given the permutation outputs from the HMM step, this 
#          stores the coverage vectors for each permutation into separate files
PATH_AGGR1="sort -n -k3,3"
PATH_AGGR2="sort -s -k2,2"
PATH_AGGR3="sort -n -k1,1"
AGGR_PATH="python $SCR_DIR/aggr_path.py"
PERM_OUT=intermediate/permutations
mkdir -p $PERM_OUT

# Temporary files so we can form a DAG
WALK_IN_TMP=${TMPDIR}walk_in.tsv
HMM_IN_TMP=${TMPDIR}hmm_in.tsv

# Parameters
GENOME_LEN=48502
NTASKS=20
HMM_OVERLAP=100
PERMUTATIONS=5

BOWTIE_EXE=$BOWTIE_HOME/bowtie

echo "Temporary file for walk_fit.py input is '$WALK_IN_TMP'" 1>&2
echo "Temporary file for hmm.py input is '$HMM_IN_TMP'" 1>&2

cat *.tab5 \
	| $ALIGN_AGGR | $ALIGN \
		--v2 \
		--ntasks=$NTASKS \
		--genomeLen=$GENOME_LEN \
		--bowtieArgs '-v 2 -m 1 -p 6' \
		--bowtieExe $BOWTIE_EXE \
		--bowtieIdx=../fasta/lambda_virus \
		--readletLen 20 \
		--readletIval 2 \
		--manifest $MANIFEST_FN \
		| tee ${INTERMEDIATE_DIR}align_out.tsv \
	| grep '^exon' | $MERGE_AGGR2 | $MERGE_AGGR3 | $MERGE_AGGR4 | $MERGE \
	| tee $WALK_IN_TMP | $WALK_PRENORM \
		--manifest $MANIFEST_FN \
		--ntasks=$NTASKS \
		--genomeLen=$GENOME_LEN \
	| $NORMALIZE_AGGR1 | $NORMALIZE_AGGR2 | $NORMALIZE \
		--percentile 0.75 \
		--out_dir $SAMPLE_OUT \
                --bigbed_exe $BIGBED_EXE \
                --chrom_sizes $CHROM_SIZES \
	| $NORMALIZE_POST_AGGR | $NORMALIZE_POST \
		--manifest $MANIFEST_FN > ${INTERMEDIATE_DIR}norm.tsv

cp $WALK_IN_TMP ${INTERMEDIATE_DIR}walk_in_input.tsv

cat $WALK_IN_TMP \
	| tee ${INTERMEDIATE_DIR}walk_fit_in.tsv | $WALK_FIT \
		--ntasks=$NTASKS \
		--genomeLen=$GENOME_LEN \
		--seed=777 \
		--permutations=$PERMUTATIONS \
		--permutations-out=${INTERMEDIATE_DIR}permutations.tsv \
		--normals=${INTERMEDIATE_DIR}norm.tsv \
	| $EBAYES_AGGR | tee ${INTERMEDIATE_DIR}ebayes_in.tsv | $EBAYES \
		--ntasks=$NTASKS \
		--genomeLen=$GENOME_LEN \
		--hmm-overlap=$HMM_OVERLAP \
	| tee ${INTERMEDIATE_DIR}hmm_in.tsv | $HMM_PARAMS_AGGR | $HMM_PARAMS \
		--null \
		--out ${INTERMEDIATE_DIR}hmm_params.tsv 

cat ${INTERMEDIATE_DIR}hmm_in.tsv \
	| $HMM_AGGR1 | $HMM_AGGR2 | $HMM \
		--ntasks=$NTASKS \
		--genomeLen=$GENOME_LEN \
		--params ${INTERMEDIATE_DIR}hmm_params.tsv \
		--hmm-overlap=$HMM_OVERLAP \
	| tee ${INTERMEDIATE_DIR}hmm_out.tsv > hmm.out

cat hmm.out | $PATH_AGGR1 | $PATH_AGGR2 | $PATH_AGGR3 | $AGGR_PATH \
                --out_dir $PERM_OUT \
                --bigbed_exe $BIGBED_EXE \
                --chrom_sizes $CHROM_SIZES \


echo DONE 1>&2

echo "Normalization file:" 1>&2
cat ${INTERMEDIATE_DIR}norm.tsv 1>&2

echo "HMM parameter file:" 1>&2
cat ${INTERMEDIATE_DIR}hmm_params.tsv 1>&2

#sh clean.sh #This is just for testing to get rid of intermediate sample and permutation files
